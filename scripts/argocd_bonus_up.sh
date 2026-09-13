#!/usr/bin/env bash
# IoW argocd-bonus bootstrap: k3d cluster + Argo CD + demo Application.
# Safe to re-run. Requires: docker, k3d, kubectl, (optional) argocd CLI.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${IOW_K3D_CLUSTER:-iow}"
IOW_DIR="${IOW_DIR:-/tmp/iow}"
NS_DEMO="iow-demo"
NS_ARGO="argocd"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[!] Missing required tool: $1"
    echo "    Install k3d + kubectl (+ argocd CLI) like Inception-of-Things, then retry."
    exit 1
  fi
}

need docker
need k3d
need kubectl

echo "[*] Ensuring k3d cluster '${CLUSTER}'..."
if ! k3d cluster list 2>/dev/null | grep -q "^${CLUSTER} "; then
  k3d cluster create "${CLUSTER}" \
    --agents 0 \
    --port "30051:30051@loadbalancer" \
    --wait
else
  echo "    cluster already exists"
fi

kubectl config use-context "k3d-${CLUSTER}" >/dev/null

echo "[*] Installing Argo CD into namespace ${NS_ARGO}..."
kubectl create namespace "${NS_ARGO}" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n "${NS_ARGO}" -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
echo "[*] Waiting for argocd-server..."
kubectl -n "${NS_ARGO}" rollout status deployment/argocd-server --timeout=180s || true

kubectl create namespace "${NS_DEMO}" --dry-run=client -o yaml | kubectl apply -f -

# Import local demo image into k3d so the Deployment can run without a registry.
if docker image inspect inception-of-wisdom-demo_app:latest >/dev/null 2>&1; then
  echo "[*] Importing demo image into k3d..."
  k3d image import inception-of-wisdom-demo_app:latest -c "${CLUSTER}" || true
else
  echo "[!] Image inception-of-wisdom-demo_app:latest not found locally."
  echo "    Run: docker compose build demo_app   (or make up) then re-run this script."
fi

REPO_URL="${IOW_GITOPS_REPO_URL:-http://host.k3d.internal:3000/iow/inception-of-wisdom.git}"
echo "[*] Applying Argo CD Application (repo=${REPO_URL})..."
# Patch repoURL for campus host Gitea reachable from k3d
sed "s|repoURL:.*|repoURL: ${REPO_URL}|" "${ROOT}/k8s/argocd/application.yaml" \
  | kubectl apply -f -

# Apply manifests once so the Service/NodePort exists even before first git sync.
kubectl apply -f "${ROOT}/k8s/demo-app/deployment.yaml" || true

mkdir -p "${IOW_DIR}/gitops"
cat > "${IOW_DIR}/gitops/env" <<EOF
IOW_REDEPLOY_MODE=gitops
IOW_ARGOCD_APP=iow-demo
IOW_GITOPS_NAMESPACE=${NS_DEMO}
IOW_GITOPS_DEPLOYMENT=iow-demo
TARGET_URL=http://127.0.0.1:30051
IOW_K3D_CLUSTER=${CLUSTER}
EOF

echo "[+] argocd-bonus ready"
echo "    Target (NodePort): http://127.0.0.1:30051"
echo "    Argo CD ns: ${NS_ARGO}  App: iow-demo"
echo "    Env file: ${IOW_DIR}/gitops/env"
echo "    Check: kubectl -n ${NS_DEMO} get pods,svc"
echo "           argocd app get iow-demo   # after argocd login"
