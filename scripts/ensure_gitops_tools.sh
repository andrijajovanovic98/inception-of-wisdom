#!/usr/bin/env bash
# Download k3d / kubectl / argocd into /tmp/iow/bin - no sudo, no system packages.
# Fallback: extract kubectl from a public Docker image when dl.k8s.io is blocked.
set -euo pipefail

IOW_DIR="${IOW_DIR:-/tmp/iow}"
BIN_DIR="${IOW_GITOPS_BIN:-${IOW_DIR}/bin}"
ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|amd64) GOARCH=amd64 ;;
  aarch64|arm64) GOARCH=arm64 ;;
  *) echo "[!] Unsupported arch: ${ARCH}"; exit 1 ;;
esac

K3D_VERSION="${K3D_VERSION:-v5.7.5}"
KUBECTL_VERSION="${KUBECTL_VERSION:-v1.31.4}"
ARGOCD_VERSION="${ARGOCD_VERSION:-v2.13.3}"
# Tag without leading 'v' for bitnami/rancher images
KUBECTL_IMAGE_TAG="${KUBECTL_VERSION#v}"

mkdir -p "${BIN_DIR}" "${IOW_DIR}/gitops"
chmod 777 "${IOW_DIR}" 2>/dev/null || true
export PATH="${BIN_DIR}:${PATH}"
export KUBECONFIG="${KUBECONFIG:-${IOW_DIR}/kube/config}"
mkdir -p "$(dirname "${KUBECONFIG}")"

have() { command -v "$1" >/dev/null 2>&1; }

download_first() {
  local dest="$1"; shift
  local url
  for url in "$@"; do
    echo "    ← ${url}"
    if command -v curl >/dev/null 2>&1; then
      if curl -fsSL --retry 2 --retry-delay 1 \
          -A "Mozilla/5.0 (compatible; IoW-gitops/1.0)" \
          -o "${dest}.partial" "${url}"; then
        mv "${dest}.partial" "${dest}"
        chmod +x "${dest}"
        return 0
      fi
      rm -f "${dest}.partial"
    else
      if wget -q -O "${dest}.partial" "${url}"; then
        mv "${dest}.partial" "${dest}"
        chmod +x "${dest}"
        return 0
      fi
      rm -f "${dest}.partial"
    fi
  done
  return 1
}

extract_kubectl_via_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "    docker present but daemon not reachable"
    return 1
  fi
  echo "[*] Extracting kubectl via Docker image (no sudo, bypass CDN blocks)..."
  local img="bitnami/kubectl:${KUBECTL_IMAGE_TAG}"
  local cname="iow-kubectl-extract-$$"
  docker pull "${img}"
  docker create --name "${cname}" "${img}" >/dev/null
  # bitnami layout; fall back to common paths
  if docker cp "${cname}:/opt/bitnami/kubectl/bin/kubectl" "${BIN_DIR}/kubectl" 2>/dev/null \
    || docker cp "${cname}:/usr/local/bin/kubectl" "${BIN_DIR}/kubectl" 2>/dev/null \
    || docker cp "${cname}:/bin/kubectl" "${BIN_DIR}/kubectl" 2>/dev/null; then
    chmod +x "${BIN_DIR}/kubectl"
    docker rm -f "${cname}" >/dev/null 2>&1 || true
    return 0
  fi
  docker rm -f "${cname}" >/dev/null 2>&1 || true
  return 1
}

ensure_kubectl() {
  if have kubectl && kubectl version --client >/dev/null 2>&1; then
    echo "[*] kubectl already available: $(command -v kubectl)"
    return 0
  fi
  if [ -x "${BIN_DIR}/kubectl" ]; then
    echo "[*] kubectl already in ${BIN_DIR}"
    return 0
  fi
  echo "[*] Installing kubectl ${KUBECTL_VERSION} → ${BIN_DIR} (no sudo)..."
  if download_first "${BIN_DIR}/kubectl" \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${GOARCH}/kubectl" \
      "https://cdn.dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${GOARCH}/kubectl"; then
    return 0
  fi
  if extract_kubectl_via_docker; then
    return 0
  fi
  echo "[!] Could not install kubectl (CDN blocked and Docker extract failed)."
  exit 1
}

ensure_k3d() {
  if have k3d; then
    echo "[*] k3d already available: $(command -v k3d)"
    return 0
  fi
  if [ -x "${BIN_DIR}/k3d" ]; then
    echo "[*] k3d already in ${BIN_DIR}"
    return 0
  fi
  echo "[*] Installing k3d ${K3D_VERSION} → ${BIN_DIR} (no sudo)..."
  if download_first "${BIN_DIR}/k3d" \
      "https://github.com/k3d-io/k3d/releases/download/${K3D_VERSION}/k3d-linux-${GOARCH}"; then
    return 0
  fi
  echo "[!] Could not download k3d from GitHub."
  exit 1
}

ensure_argocd() {
  if have argocd; then
    echo "[*] argocd already available: $(command -v argocd)"
    return 0
  fi
  if [ -x "${BIN_DIR}/argocd" ]; then
    echo "[*] argocd already in ${BIN_DIR}"
    return 0
  fi
  echo "[*] Installing argocd ${ARGOCD_VERSION} → ${BIN_DIR} (no sudo)..."
  if download_first "${BIN_DIR}/argocd" \
      "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-linux-${GOARCH}"; then
    return 0
  fi
  echo "[!] Could not download argocd from GitHub."
  exit 1
}

echo "[*] GitOps tools dir: ${BIN_DIR}"
ensure_kubectl
ensure_k3d
ensure_argocd

cat > "${IOW_DIR}/gitops/path.env" <<EOF
export PATH="${BIN_DIR}:\$PATH"
export IOW_GITOPS_BIN="${BIN_DIR}"
export IOW_K3D_CLUSTER="\${IOW_K3D_CLUSTER:-iow}"
export KUBECONFIG="\${KUBECONFIG:-${IOW_DIR}/kube/config}"
mkdir -p "\$(dirname "\$KUBECONFIG")" 2>/dev/null || true
# Only refresh kubeconfig when the cluster exists AND answers kubectl.
if command -v k3d >/dev/null 2>&1 && k3d cluster list 2>/dev/null | awk '{print \$1}' | grep -qx "\$IOW_K3D_CLUSTER"; then
  _tmp="\$(mktemp)"
  if k3d kubeconfig get "\$IOW_K3D_CLUSTER" 2>/dev/null \
      | sed 's#https://0\\.0\\.0\\.0:#https://127.0.0.1:#g' > "\$_tmp" \
    && kubectl --kubeconfig "\$_tmp" get nodes >/dev/null 2>&1; then
    mv "\$_tmp" "\$KUBECONFIG"
  else
    rm -f "\$_tmp"
  fi
fi
export KUBECONFIG
EOF

echo "[+] GitOps CLIs ready (no sudo):"
echo "    $(command -v kubectl)  → $(kubectl version --client 2>/dev/null | head -1 || true)"
echo "    $(command -v k3d)      → $(k3d version 2>/dev/null | head -1 || true)"
echo "    $(command -v argocd)   → $(argocd version --client --short 2>/dev/null | head -1 || true)"
echo "    Shell:  source ${IOW_DIR}/gitops/path.env"
echo "    Check:  kubectl version --client && k3d version"
