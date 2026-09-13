#!/bin/sh
# Bootstrap local Gitea: admin user, API token, review mirror repo, credentials file.
# Must be POSIX ash-compatible (Gitea image uses busybox sh).
set -eu

GITEA_HTTP="${GITEA_HTTP:-http://gitea:3000}"
GITEA_PUBLIC_URL="${GITEA_PUBLIC_URL:-http://localhost:3000}"
USER_NAME="${GITEA_ADMIN_USER:-iow}"
USER_PASS="${GITEA_ADMIN_PASSWORD:-iowiow123}"
USER_EMAIL="${GITEA_ADMIN_EMAIL:-iow@iow.local}"
REPO_NAME="${GITEA_REPO:-inception-of-wisdom}"
CREDS_DIR="${CREDS_DIR:-/creds}"

if [ -n "${GITEA_USER:-}" ]; then USER_NAME="$GITEA_USER"; fi
if [ -n "${GITEA_PASS:-}" ]; then USER_PASS="$GITEA_PASS"; fi

ensure_curl() {
  if command -v curl >/dev/null 2>&1; then
    return 0
  fi
  if command -v apk >/dev/null 2>&1; then
    apk add --no-cache curl >/dev/null
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null
    apt-get install -y -qq curl >/dev/null
  else
    echo "[gitea-init] curl required" >&2
    exit 1
  fi
}

json_get() {
  _json="$1"
  _key="$2"
  printf '%s' "$_json" | sed -n "s/.*\"${_key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -n1
}

echo "[gitea-init] Waiting for Gitea at ${GITEA_HTTP}..."
ensure_curl

i=0
while [ "$i" -lt 90 ]; do
  if curl -sf "${GITEA_HTTP}/api/v1/version" >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

if ! curl -sf "${GITEA_HTTP}/api/v1/version" >/dev/null 2>&1; then
  echo "[gitea-init] Gitea did not become ready in time" >&2
  exit 1
fi
echo "[gitea-init] Gitea is up."

run_as_git() {
  if [ "$(id -u)" = "0" ]; then
    if command -v su-exec >/dev/null 2>&1; then
      su-exec git "$@"
    elif command -v gosu >/dev/null 2>&1; then
      gosu git "$@"
    else
      su -s /bin/sh git -c "$*"
    fi
  else
    "$@"
  fi
}

if command -v gitea >/dev/null 2>&1; then
  if run_as_git gitea admin user create \
    --username "$USER_NAME" \
    --password "$USER_PASS" \
    --email "$USER_EMAIL" \
    --admin \
    --must-change-password=false; then
    echo "[gitea-init] Created admin user ${USER_NAME}"
  else
    echo "[gitea-init] Admin user ${USER_NAME} already present or create skipped"
  fi
else
  echo "[gitea-init] gitea binary missing; expecting user ${USER_NAME}"
fi

TOKEN_NAME="iow-agent"
TOKEN=""

if [ -f "${CREDS_DIR}/gitea.env" ]; then
  # shellcheck disable=SC1090
  . "${CREDS_DIR}/gitea.env"
  if [ -n "${GITEA_TOKEN:-}" ]; then
    if curl -sf -H "Authorization: token ${GITEA_TOKEN}" \
      "${GITEA_HTTP}/api/v1/user" >/dev/null 2>&1; then
      TOKEN="$GITEA_TOKEN"
      echo "[gitea-init] Reusing existing API token"
    fi
  fi
fi

if [ -z "$TOKEN" ]; then
  EXISTING=$(curl -sf -u "${USER_NAME}:${USER_PASS}" \
    "${GITEA_HTTP}/api/v1/users/${USER_NAME}/tokens" || true)
  OLD_ID=$(printf '%s' "$EXISTING" | sed -n "s/.*\"name\":\"${TOKEN_NAME}\"[^}]*\"id\":\\([0-9]*\\).*/\\1/p" | head -n1)
  if [ -z "$OLD_ID" ]; then
    OLD_ID=$(printf '%s' "$EXISTING" | sed -n "s/.*\"id\":\\([0-9]*\\)[^}]*\"name\":\"${TOKEN_NAME}\".*/\\1/p" | head -n1)
  fi
  if [ -n "$OLD_ID" ]; then
    curl -sf -X DELETE -u "${USER_NAME}:${USER_PASS}" \
      "${GITEA_HTTP}/api/v1/users/${USER_NAME}/tokens/${OLD_ID}" >/dev/null 2>&1 || true
  fi

  TOKEN_RESP=$(curl -sf -X POST "${GITEA_HTTP}/api/v1/users/${USER_NAME}/tokens" \
    -u "${USER_NAME}:${USER_PASS}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${TOKEN_NAME}\",\"scopes\":[\"all\"]}" || true)

  if [ -z "$TOKEN_RESP" ]; then
    TOKEN_RESP=$(curl -sf -X POST "${GITEA_HTTP}/api/v1/users/${USER_NAME}/tokens" \
      -u "${USER_NAME}:${USER_PASS}" \
      -H "Content-Type: application/json" \
      -d "{\"name\":\"${TOKEN_NAME}\"}" || true)
  fi

  TOKEN=$(json_get "$TOKEN_RESP" "sha1")
  if [ -z "$TOKEN" ]; then
    echo "[gitea-init] Failed to create API token: ${TOKEN_RESP}" >&2
    exit 1
  fi
  echo "[gitea-init] Created API token ${TOKEN_NAME}"
fi

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: token ${TOKEN}" \
  "${GITEA_HTTP}/api/v1/repos/${USER_NAME}/${REPO_NAME}" || true)

if [ "$HTTP_CODE" != "200" ]; then
  curl -sf -X POST "${GITEA_HTTP}/api/v1/user/repos" \
    -H "Authorization: token ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${REPO_NAME}\",\"private\":false,\"auto_init\":true,\"default_branch\":\"main\",\"description\":\"IoW HITL review mirror\"}" \
    >/dev/null
  echo "[gitea-init] Created repository ${USER_NAME}/${REPO_NAME}"
else
  echo "[gitea-init] Repository ${USER_NAME}/${REPO_NAME} already exists"
fi

mkdir -p "$CREDS_DIR"
cat > "${CREDS_DIR}/gitea.env" <<EOF
GITEA_URL=${GITEA_HTTP}
GITEA_PUBLIC_URL=${GITEA_PUBLIC_URL}
GITEA_OWNER=${USER_NAME}
GITEA_REPO=${REPO_NAME}
GITEA_USER=${USER_NAME}
GITEA_PASSWORD=${USER_PASS}
GITEA_TOKEN=${TOKEN}
GITEA_REMOTE_NAME=gitea
EOF
chmod 644 "${CREDS_DIR}/gitea.env"
echo "[gitea-init] Credentials written to ${CREDS_DIR}/gitea.env"
echo "[gitea-init] UI: ${GITEA_PUBLIC_URL}  user=${USER_NAME} pass=${USER_PASS}"
