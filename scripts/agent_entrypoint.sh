#!/bin/sh
# IoW agent entrypoint - run the agent as the owner of the mounted repository.
#
# Subject (file ownership on shared volumes): if the agent writes the target's files
# or git objects as root while the host user owns the checkout, git refuses to work
# on the repository afterwards and the peer cannot edit their own tree.
#
#   rootful Docker : /workspace shows the host user's uid (e.g. 1000) -> drop to it,
#                    keeping the Docker socket's group so the agent can still reach
#                    the daemon.
#   rootless Docker: the host user already *is* uid 0 inside the container, so
#                    /workspace shows up as root-owned and nothing needs to change.
set -eu

WORKSPACE="${IOW_REPO_PATH:-/workspace}"
RUNTIME_DIR="${IOW_CACHE_DIR:-/tmp/iow}"

if [ "$(id -u)" = "0" ] && [ -d "$WORKSPACE" ]; then
  uid="$(stat -c %u "$WORKSPACE")"
  gid="$(stat -c %g "$WORKSPACE")"
  if [ "$uid" != "0" ]; then
    groups="$gid"
    if [ -S /var/run/docker.sock ]; then
      groups="$groups,$(stat -c %g /var/run/docker.sock)"
    fi
    # Everything the agent writes at runtime must be the host user's too.
    mkdir -p "$RUNTIME_DIR/hf-cache" "$RUNTIME_DIR/chroma_db" "$RUNTIME_DIR/gitea" "$RUNTIME_DIR/home"
    chown "$uid:$gid" "$RUNTIME_DIR" "$RUNTIME_DIR/gitea" "$RUNTIME_DIR/home" 2>/dev/null || true
    chown -R "$uid:$gid" "$RUNTIME_DIR/hf-cache" "$RUNTIME_DIR/chroma_db" 2>/dev/null || true
    find "$RUNTIME_DIR" -maxdepth 1 -name '*.json' -exec chown "$uid:$gid" {} + 2>/dev/null || true
    export HOME="$RUNTIME_DIR/home"
    echo "[iow-agent] running as uid=$uid gid=$gid (owner of $WORKSPACE), groups=$groups"
    exec setpriv --reuid="$uid" --regid="$gid" --groups="$groups" --inh-caps=-all -- "$@"
  fi
fi

echo "[iow-agent] running as uid=$(id -u) ($WORKSPACE is owned by uid 0 here - rootless Docker maps the host user to it)"
exec "$@"
