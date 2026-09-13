#!/usr/bin/env bash
# Tear down IoW k3d / Argo CD bonus cluster (does not touch Docker demo_app / Gitea).
set -euo pipefail

CLUSTER="${IOW_K3D_CLUSTER:-iow}"

if ! command -v k3d >/dev/null 2>&1; then
  echo "[!] k3d not installed; nothing to tear down"
  exit 0
fi

if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER} "; then
  echo "[*] Deleting k3d cluster '${CLUSTER}'..."
  k3d cluster delete "${CLUSTER}"
  echo "[+] Cluster deleted"
else
  echo "[*] Cluster '${CLUSTER}' not found"
fi

rm -f /tmp/iow/gitops/env 2>/dev/null || true
