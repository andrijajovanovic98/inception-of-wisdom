#!/usr/bin/env bash
# Tear down IoW k3s / Argo CD bonus cluster (does not touch Docker demo_app / Gitea).
set -euo pipefail

IOW_DIR="${IOW_DIR:-/tmp/iow}"
BIN_DIR="${IOW_GITOPS_BIN:-${IOW_DIR}/bin}"
CLUSTER="${IOW_K3D_CLUSTER:-iow}"
K3S_NAME="${IOW_K3S_CONTAINER:-iow-k3s}"

export PATH="${BIN_DIR}:${PATH}"
export KUBECONFIG="${KUBECONFIG:-${IOW_DIR}/kube/config}"

if [ -z "${DOCKER_HOST:-}" ] && [ -S "/run/user/$(id -u)/docker.sock" ]; then
  export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
fi

if [ -f "${IOW_DIR}/gitops/path.env" ]; then
  # shellcheck disable=SC1091
  source "${IOW_DIR}/gitops/path.env"
fi

# Stop port-forwards.
for pf in demo argocd; do
  if [ -f "${IOW_DIR}/gitops/${pf}-port-forward.pid" ]; then
    _pid="$(cat "${IOW_DIR}/gitops/${pf}-port-forward.pid" 2>/dev/null || true)"
    if [ -n "${_pid}" ] && kill -0 "${_pid}" 2>/dev/null; then
      kill "${_pid}" >/dev/null 2>&1 || true
      echo "[*] Stopped ${pf} port-forward (pid ${_pid})"
    fi
    rm -f "${IOW_DIR}/gitops/${pf}-port-forward.pid"
  fi
done
if [ -f "${IOW_DIR}/gitops/demo-port-forward.pid" ]; then
  _pf="$(cat "${IOW_DIR}/gitops/demo-port-forward.pid" 2>/dev/null || true)"
  if [ -n "${_pf}" ] && kill -0 "${_pf}" 2>/dev/null; then
    kill "${_pf}" >/dev/null 2>&1 || true
  fi
  rm -f "${IOW_DIR}/gitops/demo-port-forward.pid"
fi

if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${K3S_NAME}"; then
  echo "[*] Removing k3s container '${K3S_NAME}'..."
  docker rm -f "${K3S_NAME}" >/dev/null 2>&1 || true
  echo "[+] ${K3S_NAME} removed"
else
  echo "[*] Container '${K3S_NAME}' not found"
fi

# Also wipe legacy nested-k3d cluster if present.
if command -v k3d >/dev/null 2>&1; then
  if k3d cluster list 2>/dev/null | awk '{print $1}' | grep -qx "${CLUSTER}"; then
    echo "[*] Deleting legacy k3d cluster '${CLUSTER}'..."
    k3d cluster delete "${CLUSTER}" || true
  fi
fi
ids="$(docker ps -aq --filter "name=k3d-${CLUSTER}" 2>/dev/null || true)"
if [ -n "${ids}" ]; then
  # shellcheck disable=SC2086
  docker rm -f ${ids} >/dev/null 2>&1 || true
fi

rm -f "${IOW_DIR}/gitops/env" "${IOW_DIR}/kube/config" 2>/dev/null || true
echo "[+] argocd-bonus-down done (demo/gitea untouched)"
