#!/usr/bin/env bash
# IoW argocd-bonus: single-node k3s in Docker + Argo CD (no sudo).
# Campus rootless Docker cannot run nested k3d (missing cpu cgroup). Workaround:
#   docker run --privileged --cgroupns=host + KubeletInUserNamespace + cgroups-per-qos=false
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${IOW_K3D_CLUSTER:-iow}"
IOW_DIR="${IOW_DIR:-/tmp/iow}"
BIN_DIR="${IOW_GITOPS_BIN:-${IOW_DIR}/bin}"
NS_DEMO="iow-demo"
NS_ARGO="argocd"
API_PORT="${IOW_K3D_API_PORT:-6550}"
NODE_PORT="${IOW_K3D_NODE_PORT:-30051}"
K3S_NAME="${IOW_K3S_CONTAINER:-iow-k3s}"
K3S_IMAGE="${IOW_K3S_IMAGE:-rancher/k3s:v1.27.12-k3s1}"
GIT_IMAGE="${IOW_GIT_IMAGE:-alpine/git:2.45.2}"
HEAL_BRANCH="${IOW_GITOPS_BRANCH:-iow/auto-heal}"

mkdir -p "${IOW_DIR}/gitops" "${BIN_DIR}" "$(dirname "${IOW_DIR}/kube/config")"
chmod +x "${ROOT}/scripts/ensure_gitops_tools.sh" 2>/dev/null || true

# Prefer campus rootless socket.
if [ -z "${DOCKER_HOST:-}" ] && [ -S "/run/user/$(id -u)/docker.sock" ]; then
  export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
fi

echo "[*] Ensuring GitOps tools under ${BIN_DIR} (no sudo)..."
IOW_DIR="${IOW_DIR}" bash "${ROOT}/scripts/ensure_gitops_tools.sh"
# shellcheck disable=SC1091
source "${IOW_DIR}/gitops/path.env" || true
export PATH="${BIN_DIR}:${PATH}"
export KUBECONFIG="${IOW_DIR}/kube/config"
mkdir -p "$(dirname "${KUBECONFIG}")"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[!] Missing required tool after bootstrap: $1"
    exit 1
  fi
}

need docker
need kubectl

if ! docker info >/dev/null 2>&1; then
  echo "[!] Docker daemon not reachable (needed by k3s)."
  exit 1
fi

purge_k3d_leftovers() {
  if command -v k3d >/dev/null 2>&1; then
    k3d cluster delete "${CLUSTER}" >/dev/null 2>&1 || true
  fi
  local ids
  ids="$(docker ps -aq --filter "name=k3d-${CLUSTER}" 2>/dev/null || true)"
  if [ -n "${ids}" ]; then
    # shellcheck disable=SC2086
    docker rm -f ${ids} >/dev/null 2>&1 || true
  fi
  docker network rm "k3d-${CLUSTER}" >/dev/null 2>&1 || true
  docker volume rm "k3d-${CLUSTER}-images" >/dev/null 2>&1 || true
}

purge_cluster() {
  echo "[*] Purging IoW k3s/k3d leftovers..."
  purge_k3d_leftovers
  docker rm -f "${K3S_NAME}" >/dev/null 2>&1 || true
  rm -f "${KUBECONFIG}" "${KUBECONFIG}.raw" 2>/dev/null || true
}

write_kubeconfig() {
  docker cp "${K3S_NAME}:/etc/rancher/k3s/k3s.yaml" "${KUBECONFIG}"
  sed -i.bak \
    -e "s#server: https://127.0.0.1:6443#server: https://127.0.0.1:${API_PORT}#g" \
    -e "s#server: https://localhost:6443#server: https://127.0.0.1:${API_PORT}#g" \
    -e "s#0\\.0\\.0\\.0#127.0.0.1#g" \
    "${KUBECONFIG}" 2>/dev/null || true
  rm -f "${KUBECONFIG}.bak"
  if ! grep -q "server: https://127.0.0.1:${API_PORT}" "${KUBECONFIG}"; then
    python3 - "${KUBECONFIG}" "${API_PORT}" <<'PY'
import re, sys
path, port = sys.argv[1], sys.argv[2]
text = open(path).read()
text = re.sub(r"server:\s*https://\S+", f"server: https://127.0.0.1:{port}", text)
open(path, "w").write(text)
PY
  fi
  export KUBECONFIG
}

cluster_healthy() {
  write_kubeconfig 2>/dev/null || return 1
  kubectl get nodes >/dev/null 2>&1 || return 1
  kubectl get nodes 2>/dev/null | grep -q Ready || return 1
  return 0
}

start_k3s() {
  echo "[*] Starting k3s container '${K3S_NAME}' (privileged + cgroupns=host - campus rootless fix)"
  echo "    image=${K3S_IMAGE}  api=127.0.0.1:${API_PORT}  nodePort=${NODE_PORT}"
  local net_args=()
  if docker network inspect iow-network >/dev/null 2>&1; then
    net_args=(--network iow-network)
  fi
  docker run -d --name "${K3S_NAME}" \
    --privileged \
    --cgroupns=host \
    "${net_args[@]}" \
    --add-host=host.docker.internal:host-gateway \
    -p "127.0.0.1:${API_PORT}:6443" \
    -p "127.0.0.1:${NODE_PORT}:${NODE_PORT}" \
    "${K3S_IMAGE}" \
    server \
    --disable=traefik \
    --snapshotter=native \
    --tls-san=127.0.0.1 \
    --kubelet-arg=feature-gates=KubeletInUserNamespace=true \
    --kubelet-arg=cgroups-per-qos=false \
    --kubelet-arg=enforce-node-allocatable=
  if docker network inspect iow-network >/dev/null 2>&1; then
    docker network connect iow-network "${K3S_NAME}" >/dev/null 2>&1 || true
  fi
}

gitea_ip() {
  docker inspect iow_gitea --format '{{with index .NetworkSettings.Networks "iow-network"}}{{.IPAddress}}{{end}}' 2>/dev/null
}

# Push demo_app/ (same as workspace) + k8s manifests to Gitea.
# stdout = IP only (for capture); logs on stderr.
publish_manifests_to_gitea() {
  local creds="${IOW_DIR}/gitea/gitea.env"
  if [ ! -f "${creds}" ]; then
    echo "[!] Missing ${creds} - skip git publish" >&2
    return 1
  fi
  # shellcheck disable=SC1090
  set -a; source "${creds}"; set +a
  local ip
  ip="$(gitea_ip)"
  if [ -z "${ip}" ]; then
    echo "[!] Could not resolve iow_gitea on iow-network" >&2
    return 1
  fi
  local work
  work="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '${work}'" RETURN
  echo "[*] Publishing demo_app/ + k8s/demo-app → Gitea (same sources as workspace)..." >&2
  git clone "http://${GITEA_USER}:${GITEA_TOKEN}@127.0.0.1:3000/iow/inception-of-wisdom.git" "${work}/repo" >/dev/null 2>&1
  mkdir -p "${work}/repo/k8s/demo-app" "${work}/repo/demo_app"
  cp -a "${ROOT}/k8s/demo-app/." "${work}/repo/k8s/demo-app/"
  cp -a "${ROOT}/demo_app/." "${work}/repo/demo_app/"
  (
    cd "${work}/repo"
    git config user.email "iow@iow.local"
    git config user.name "iow"
    git add k8s/demo-app demo_app
    git diff --cached --quiet || git commit -m "IoW: sync demo_app + k8s manifests for Argo CD" >/dev/null
    git push origin HEAD:main >/dev/null
    # Force: the heal branch is the revision Argo CD tracks and the agent rewrites
    # it every cycle, so a stale non-fast-forward must not block the bootstrap.
    git push --force origin "HEAD:${HEAL_BRANCH}" >/dev/null
  )
  echo "[+] Gitea demo_app/ matches ${ROOT}/demo_app" >&2
  printf '%s\n' "${ip}"
}

# stdout = URL only (kubectl noise suppressed).
register_argocd_repo() {
  local ip="$1"
  local creds="${IOW_DIR}/gitea/gitea.env"
  # shellcheck disable=SC1090
  set -a; source "${creds}"; set +a
  local url="http://${ip}:3000/iow/inception-of-wisdom.git"
  kubectl -n "${NS_ARGO}" create secret generic iow-gitea-repo \
    --from-literal=type=git \
    --from-literal=url="${url}" \
    --from-literal=username="${GITEA_USER}" \
    --from-literal=password="${GITEA_TOKEN}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "${NS_ARGO}" label secret iow-gitea-repo \
    argocd.argoproj.io/secret-type=repository --overwrite >/dev/null
  printf '%s\n' "${url}"
}

wait_ready() {
  local max_s="${1:-60}"
  local i
  echo "[*] Waiting up to ${max_s}s for kubectl get nodes Ready..."
  for i in $(seq 1 "${max_s}"); do
    if [ "$(docker inspect -f '{{.State.Status}}' "${K3S_NAME}" 2>/dev/null || echo missing)" != "running" ]; then
      echo "[!] ${K3S_NAME} is not running"
      docker logs --tail 40 "${K3S_NAME}" 2>&1 || true
      return 1
    fi
    if cluster_healthy; then
      echo "[+] k3s Ready (${i}s)"
      kubectl get nodes
      return 0
    fi
    sleep 1
  done
  echo "[!] Timed out waiting for Ready node"
  docker logs --tail 50 "${K3S_NAME}" 2>&1 || true
  return 1
}

# Always rebuild from ROOT/demo_app so the k8s image matches the workspace app.
import_demo_image() {
  local img="inception-of-wisdom-demo_app:latest"
  echo "[*] Building demo image from ${ROOT}/demo_app (same as compose iow_demo_target)..."
  (cd "${ROOT}" && docker compose build demo_app)
  if ! docker image inspect "${img}" >/dev/null 2>&1; then
    echo "[!] ${img} missing after build"
    return 1
  fi
  echo "[*] Importing ${img} into k3s containerd..."
  docker save "${img}" | docker exec -i "${K3S_NAME}" ctr -n k8s.io images import - || {
    echo "[!] ctr import failed - trying k3s ctr"
    docker save "${img}" | docker exec -i "${K3S_NAME}" k3s ctr images import -
  }

  # The init container clones the heal branch, so its image must be present too.
  echo "[*] Importing ${GIT_IMAGE} (init container) into k3s containerd..."
  docker pull "${GIT_IMAGE}" >/dev/null 2>&1 || true
  docker save "${GIT_IMAGE}" | docker exec -i "${K3S_NAME}" ctr -n k8s.io images import - \
    || docker save "${GIT_IMAGE}" | docker exec -i "${K3S_NAME}" k3s ctr images import - \
    || echo "[!] Could not import ${GIT_IMAGE} - the cluster will try to pull it"
}

apply_argocd_application() {
  local repo_url="$1"
  # Only the repo URL is rewritten. targetRevision stays on the heal branch so a
  # Wisdom Loop commit is what Argo CD picks up; pointing it at main would leave
  # the agent pushing to a branch nobody watches.
  python3 - "${ROOT}/k8s/argocd/application.yaml" "${repo_url}" "${HEAL_BRANCH}" <<'PY' | kubectl apply -f -
import sys
from pathlib import Path
src, repo, revision = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = src.read_text(encoding="utf-8")
out = []
for line in text.splitlines():
    stripped = line.strip()
    indent = line[: len(line) - len(line.lstrip())]
    if stripped.startswith("repoURL:"):
        out.append(f"{indent}repoURL: {repo}")
    elif stripped.startswith("targetRevision:"):
        out.append(f"{indent}targetRevision: {revision}")
    else:
        out.append(line)
print("\n".join(out) + "\n")
PY
}

# Point the demo pods at the same repo/branch Argo CD tracks.
apply_source_configmap() {
  local repo_url="$1"
  kubectl -n "${NS_DEMO}" create configmap iow-demo-source \
    --from-literal=repo_url="${repo_url}" \
    --from-literal=revision="${HEAL_BRANCH}" \
    --dry-run=client -o yaml | kubectl apply -f -
}

ensure_port_forward() {
  local kind="$1" ns="$2" svc="$3" local_port="$4" remote_port="$5"
  local pf_pid_file="${IOW_DIR}/gitops/${kind}-port-forward.pid"
  if [ -f "${pf_pid_file}" ]; then
    local old
    old="$(cat "${pf_pid_file}" 2>/dev/null || true)"
    if [ -n "${old}" ] && kill -0 "${old}" 2>/dev/null; then
      kill "${old}" >/dev/null 2>&1 || true
      sleep 1
    fi
    rm -f "${pf_pid_file}"
  fi
  local i
  for i in $(seq 1 30); do
    if kubectl -n "${ns}" get svc "${svc}" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  echo "[*] port-forward ${kind}: 127.0.0.1:${local_port} → ${ns}/svc/${svc}:${remote_port}"
  nohup kubectl -n "${ns}" port-forward --address 127.0.0.1 \
    "svc/${svc}" "${local_port}:${remote_port}" \
    >"${IOW_DIR}/gitops/${kind}-port-forward.log" 2>&1 &
  echo $! > "${pf_pid_file}"
  sleep 2
  if kill -0 "$(cat "${pf_pid_file}")" 2>/dev/null; then
    echo "[+] ${kind} → http://127.0.0.1:${local_port}"
  else
    echo "[!] ${kind} port-forward failed; see ${IOW_DIR}/gitops/${kind}-port-forward.log"
  fi
}

ensure_cluster() {
  if docker ps --format '{{.Names}}' | grep -qx "${K3S_NAME}"; then
    if cluster_healthy; then
      echo "[*] Cluster already healthy (${K3S_NAME})"
      kubectl get nodes
      return 0
    fi
    echo "[!] Existing ${K3S_NAME} unhealthy - recreating"
  fi
  purge_cluster
  start_k3s
  wait_ready 60
}

# --- main ---
echo "[*] Ensuring IoW k3s cluster (Docker k3s; replaces nested k3d on campus rootless)..."
ensure_cluster
IOW_DIR="${IOW_DIR}" bash "${ROOT}/scripts/ensure_gitops_tools.sh" >/dev/null || true
write_kubeconfig

echo "[*] Installing Argo CD into namespace ${NS_ARGO}..."
kubectl create namespace "${NS_ARGO}" --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl apply -n "${NS_ARGO}" -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.3/manifests/install.yaml; then
  echo "[!] Could not fetch Argo CD install.yaml from GitHub - retry once..."
  sleep 2
  kubectl apply -n "${NS_ARGO}" -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.3/manifests/install.yaml
fi
echo "[*] Waiting for argocd-server (up to 3 min)..."
kubectl -n "${NS_ARGO}" rollout status deployment/argocd-server --timeout=180s || true

kubectl create namespace "${NS_DEMO}" --dry-run=client -o yaml | kubectl apply -f -

if docker network inspect iow-network >/dev/null 2>&1; then
  docker network connect iow-network "${K3S_NAME}" >/dev/null 2>&1 || true
fi

# Build+import AFTER namespace exists so we can restart deployment.
import_demo_image

GITEA_IP="$(publish_manifests_to_gitea | tail -n1)"
REPO_URL="${IOW_GITOPS_REPO_URL:-}"
if [ -z "${REPO_URL}" ]; then
  if [ -n "${GITEA_IP}" ]; then
    REPO_URL="$(register_argocd_repo "${GITEA_IP}" | tail -n1)"
  else
    REPO_URL="http://$(gitea_ip):3000/iow/inception-of-wisdom.git"
  fi
fi
# Strip any accidental whitespace/newlines from capture.
REPO_URL="$(printf '%s' "${REPO_URL}" | tr -d '\r\n' | head -c 500)"
echo "[*] Applying Argo CD Application (repo=${REPO_URL})..."
apply_argocd_application "${REPO_URL}"

apply_source_configmap "${REPO_URL}"
kubectl apply -f "${ROOT}/k8s/demo-app/deployment.yaml" || true
# Re-apply after the manifest, so the loop's branch wins over the bundled default.
apply_source_configmap "${REPO_URL}"
# Ensure pods use the image we just imported (tag:latest may have been cached).
kubectl -n "${NS_DEMO}" rollout restart deployment/iow-demo >/dev/null 2>&1 || true
kubectl -n "${NS_DEMO}" rollout status deployment/iow-demo --timeout=90s || true

echo "[*] Verifying demo namespace..."
kubectl -n "${NS_DEMO}" get pods,svc || true

# No demo port-forward: the k3s container already publishes 127.0.0.1:${NODE_PORT},
# so a port-forward there can only ever fail with "address already in use".
ensure_port_forward argocd "${NS_ARGO}" argocd-server 8080 80 || true

ARGO_PASS="$(kubectl -n "${NS_ARGO}" get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)"

cat > "${IOW_DIR}/gitops/env" <<EOF
IOW_REDEPLOY_MODE=gitops
IOW_ARGOCD_APP=iow-demo
IOW_GITOPS_NAMESPACE=${NS_DEMO}
IOW_GITOPS_DEPLOYMENT=iow-demo
TARGET_URL=http://127.0.0.1:${NODE_PORT}
IOW_K3D_CLUSTER=${CLUSTER}
IOW_K3S_CONTAINER=${K3S_NAME}
IOW_GITOPS_BIN=${BIN_DIR}
KUBECONFIG=${KUBECONFIG}
PATH=${BIN_DIR}:\$PATH
EOF

echo ""
echo "[+] argocd-bonus READY"
echo "    kubectl:  source ${IOW_DIR}/gitops/path.env && kubectl get nodes"
echo "    demo:     http://127.0.0.1:${NODE_PORT}  (same image as demo_app/)"
echo "    compose:  http://127.0.0.1:5001          (iow_demo_target)"
echo "    Argo CD:  http://127.0.0.1:8080  (user=admin pass=${ARGO_PASS:-<see secret>})"
kubectl get nodes
kubectl -n "${NS_DEMO}" get pods,svc || true
kubectl -n "${NS_ARGO}" get pods 2>/dev/null | head -20 || true
