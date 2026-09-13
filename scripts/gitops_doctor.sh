#!/usr/bin/env bash
# IoW GitOps doctor: show compose vs k3d container groups, dump bad logs, optional purge.
set -euo pipefail

IOW_DIR="${IOW_DIR:-/tmp/iow}"
BIN_DIR="${IOW_GITOPS_BIN:-${IOW_DIR}/bin}"
CLUSTER="${IOW_K3D_CLUSTER:-iow}"
PURGE="${1:-}"

export PATH="${BIN_DIR}:${PATH}"
# Prefer user docker socket when present (campus rootless).
if [ -z "${DOCKER_HOST:-}" ] && [ -S "/run/user/$(id -u)/docker.sock" ]; then
  export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "[!] Docker not reachable (DOCKER_HOST=${DOCKER_HOST:-default})"
  exit 1
fi

echo "================================================================================"
echo " IoW GitOps doctor  DOCKER_HOST=${DOCKER_HOST:-unix:///var/run/docker.sock}"
echo "================================================================================"

echo ""
echo "=== GROUP A - IoW compose (should be UP for make bonus/pr-bonus/argocd-bonus) ==="
echo "    expected: iow_demo_target, iow_gitea   (optional: iow_agent)"
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' \
  | awk 'NR==1 || /iow_demo|iow_gitea|iow_agent|gitea/' || true

echo ""
echo "=== GROUP B - k3s (make argocd-bonus) ==="
echo "    expected: iow-k3s   (legacy: k3d-${CLUSTER}-server-0 / serverlb / tools)"
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' \
  | awk 'NR==1 || /iow-k3s|k3d-'"${CLUSTER}"'/' || true

echo ""
echo "=== Images (ghcr / rancher / k3d) - red container ≠ always bad image ==="
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' \
  | awk 'NR==1 || /ghcr.io\/k3d|rancher\/k3s|k3d|argoproj|gitea|inception-of-wisdom/' || true

echo ""
echo "=== cluster status ==="
if docker ps --format '{{.Names}}' | grep -qx iow-k3s; then
  echo "    iow-k3s container is running (campus k3s-in-Docker path)"
fi
if command -v k3d >/dev/null 2>&1; then
  k3d cluster list 2>/dev/null || true
fi

dump_logs() {
  local name="$1"
  if docker ps -a --format '{{.Names}}' | grep -qx "${name}"; then
    echo ""
    echo "---------- docker logs --tail 80 ${name} ----------"
    docker logs --tail 80 "${name}" 2>&1 || true
    echo "---------- inspect ${name} ----------"
    docker inspect -f 'status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} err={{.State.Error}} finished={{.State.FinishedAt}}' "${name}" 2>&1 || true
  fi
}

echo ""
echo "=== Logs for k3s / k3d nodes ==="
dump_logs "iow-k3s"
dump_logs "k3d-${CLUSTER}-server-0"
dump_logs "k3d-${CLUSTER}-serverlb"
dump_logs "k3d-${CLUSTER}-tools"

echo ""
echo "=== Verdict ==="
compose_ok=1
docker ps --format '{{.Names}}' | grep -qx 'iow_demo_target' || compose_ok=0
docker ps --format '{{.Names}}' | grep -qx 'iow_gitea' || compose_ok=0
if [ "${compose_ok}" = "1" ]; then
  echo "[+] GROUP A OK (demo + gitea running)"
else
  echo "[!] GROUP A incomplete - run: make up   (or continue with make argocd-bonus which starts them)"
fi

export PATH="${BIN_DIR}:${PATH}"
export KUBECONFIG="${IOW_DIR}/kube/config"
k3s_status="$(docker inspect -f '{{.State.Status}}' iow-k3s 2>/dev/null || echo missing)"
server_status="$(docker inspect -f '{{.State.Status}}' "k3d-${CLUSTER}-server-0" 2>/dev/null || echo missing)"
lb_status="$(docker inspect -f '{{.State.Status}}' "k3d-${CLUSTER}-serverlb" 2>/dev/null || echo missing)"

case "${k3s_status}" in
  running)
    if command -v kubectl >/dev/null 2>&1 && [ -f "${KUBECONFIG}" ] && kubectl get nodes 2>/dev/null | grep -q Ready; then
      echo "[+] GROUP B OK (iow-k3s running + kubectl Ready)"
      kubectl get nodes
    else
      echo "[!] GROUP B: iow-k3s Up but kubectl not Ready yet (see logs)"
      echo "    Next: make gitops-doctor-purge && make argocd-bonus"
    fi
    ;;
  missing)
    case "${lb_status}" in
      created|restarting)
        echo "[!] Legacy k3d serverlb status=${lb_status} - nested k3d broken on campus rootless"
        echo "    Fix: make gitops-doctor-purge && make argocd-bonus  (uses iow-k3s path)"
        ;;
    esac
    case "${server_status}" in
      running)
        echo "[!] Legacy k3d-${CLUSTER}-server-0 still present (usually cgroup-fatal)"
        echo "    Fix: make gitops-doctor-purge && make argocd-bonus"
        ;;
      missing)
        echo "[*] GROUP B not present yet - run: make argocd-bonus"
        ;;
      *)
        echo "[!] Legacy k3d server status=${server_status}"
        ;;
    esac
    ;;
  *)
    echo "[!] GROUP B BAD - iow-k3s status=${k3s_status}"
    echo "    Next: make gitops-doctor-purge && make argocd-bonus"
    ;;
esac

if [ "${PURGE}" = "purge" ] || [ "${PURGE}" = "--purge" ]; then
  echo ""
  echo "[*] PURGE - removing iow-k3s + legacy k3d (keeps iow_demo_target / iow_gitea)"
  docker rm -f iow-k3s >/dev/null 2>&1 || true
  k3d cluster delete "${CLUSTER}" >/dev/null 2>&1 || true
  ids="$(docker ps -aq --filter "name=k3d-${CLUSTER}" 2>/dev/null || true)"
  if [ -n "${ids}" ]; then
    # shellcheck disable=SC2086
    docker rm -f ${ids} >/dev/null 2>&1 || true
  fi
  docker network rm "k3d-${CLUSTER}" >/dev/null 2>&1 || true
  docker volume rm "k3d-${CLUSTER}-images" >/dev/null 2>&1 || true
  for pf in demo argocd; do
    if [ -f "${IOW_DIR}/gitops/${pf}-port-forward.pid" ]; then
      kill "$(cat "${IOW_DIR}/gitops/${pf}-port-forward.pid")" >/dev/null 2>&1 || true
      rm -f "${IOW_DIR}/gitops/${pf}-port-forward.pid"
    fi
  done
  rm -f "${IOW_DIR}/kube/config" 2>/dev/null || true
  echo "[+] purged. Re-run: make argocd-bonus"
fi

echo ""
echo "Usage:  make gitops-doctor           # inspect"
echo "        make gitops-doctor-purge     # wipe broken cluster only, keep Gitea/demo"
echo "        make argocd-bonus            # recreate k3s + install Argo CD"
echo "        Argo UI after bonus:         http://127.0.0.1:8080  (admin / initial secret)"
