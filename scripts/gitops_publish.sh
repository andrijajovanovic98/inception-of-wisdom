#!/usr/bin/env bash
# Publish the working tree's demo_app/ to the branch Argo CD tracks; Argo CD then rolls the
# demo deployment, whose new pod clones exactly that code.
#
# In GitOps mode the target runs what is on that branch, not the local files: a
# `make break-import` alone changes nothing the cluster serves. This is the peer's way to
# plant a bug in the cluster (`make break-import gitops-publish`) or to put the code back
# (`make demo-reset gitops-publish`). The Wisdom Loop then heals from the same tree it
# sees on disk, and pushes its fix to the same branch.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IOW_DIR="${IOW_DIR:-/tmp/iow}"
BRANCH="${IOW_GITOPS_BRANCH:-iow/auto-heal}"
NS="${IOW_GITOPS_NAMESPACE:-iow-demo}"
DEPLOY="${IOW_GITOPS_DEPLOYMENT:-iow-demo}"
CREDS="${IOW_DIR}/gitea/gitea.env"

export PATH="${IOW_DIR}/bin:${PATH}"
export KUBECONFIG="${KUBECONFIG:-${IOW_DIR}/kube/config}"

if [ ! -f "${CREDS}" ] || ! kubectl -n "${NS}" get deployment "${DEPLOY}" >/dev/null 2>&1; then
  echo "[!] No GitOps cluster to publish to - start it with: make argocd-bonus"
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "${CREDS}"
set +a
URL="${IOW_GITOPS_REMOTE:-http://127.0.0.1:3000/${GITEA_OWNER:-iow}/${GITEA_REPO:-inception-of-wisdom}.git}"
# The token travels as a header, never inside a URL or a .git/config.
AUTH=(-c "http.extraHeader=Authorization: token ${GITEA_TOKEN}")

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
if ! git "${AUTH[@]}" clone -q --depth 1 --branch "${BRANCH}" "${URL}" "${WORK}/repo" 2>/dev/null; then
  echo "[!] Could not clone '${BRANCH}' from ${URL} - run make argocd-bonus first"
  exit 1
fi

# demo_app/ exactly as the loop would snapshot it: tracked and untracked files, ignored ones left out.
rm -rf "${WORK}/repo/demo_app"
(cd "${ROOT}" && git ls-files -z -co --exclude-standard -- demo_app) \
  | while IFS= read -r -d '' path; do
      [ -f "${ROOT}/${path}" ] || continue
      mkdir -p "${WORK}/repo/$(dirname "${path}")"
      cp -p "${ROOT}/${path}" "${WORK}/repo/${path}"
    done

cd "${WORK}/repo"
git add -A demo_app
# The pod template carries the code's git tree hash: a new hash is a new template, and
# Argo CD's own sync rolls the pod - the commit alone is the redeploy.
MANIFEST=k8s/demo-app/deployment.yaml
stamped=0
if [ -f "${MANIFEST}" ] && grep -q 'iow\.42/source:' "${MANIFEST}"; then
  source_hash="$(git write-tree --prefix=demo_app/)"
  sed -i -E "s|^([[:space:]]*iow\.42/source:[[:space:]]*)\"[^\"]*\"|\1\"${source_hash}\"|" "${MANIFEST}"
  git add "${MANIFEST}"
  stamped=1
fi
if git diff --cached --quiet; then
  echo "[*] '${BRANCH}' already holds this demo_app/ - restarting the pod instead"
  kubectl -n "${NS}" rollout restart "deployment/${DEPLOY}" >/dev/null
else
  git -c user.name="IoW operator" -c user.email="operator@iow.local" \
    commit -qm "operator: publish demo_app/ from the working tree"
  git "${AUTH[@]}" push -q origin "HEAD:refs/heads/${BRANCH}"
  echo "[+] Published demo_app/ to '${BRANCH}' ($(git rev-parse --short HEAD))"
  git show --stat --format= HEAD | sed 's/^/    /'
  if [ "${stamped}" = 1 ]; then
    echo "[*] Argo CD syncs the commit within ~15 s and rolls the pod itself"
  else
    kubectl -n "${NS}" rollout restart "deployment/${DEPLOY}" >/dev/null
    echo "[*] ${MANIFEST} has no source stamp - rolled deployment/${DEPLOY} by hand"
  fi
fi
echo "    Watch it: kubectl -n ${NS} get pods -w   (a planted bug crash-loops, then the loop heals it)"
