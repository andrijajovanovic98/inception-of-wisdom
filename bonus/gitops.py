"""
Inception-of-Wisdom (IoW) - Bonus: GitOps redeploy helpers (Argo CD + k3d).

When IOW_REDEPLOY_MODE=gitops, a heal commit (+ push) should be enough for the
cluster to pick up the change; the agent waits for sync/rollout instead of
docker restart.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import logging
from typing import Optional, Tuple

logger = logging.getLogger("bonus.gitops")

DEFAULT_APP_NAME = os.environ.get("IOW_ARGOCD_APP", "iow-demo")
DEFAULT_NAMESPACE = os.environ.get("IOW_GITOPS_NAMESPACE", "iow-demo")
DEFAULT_DEPLOYMENT = os.environ.get("IOW_GITOPS_DEPLOYMENT", "iow-demo")


def redeploy_mode() -> str:
    """Return 'gitops' or 'docker' (default)."""
    return (os.environ.get("IOW_REDEPLOY_MODE") or "docker").strip().lower()


def _run(cmd: list[str], timeout: float = 60.0) -> Tuple[int, str, str]:
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return res.returncode, (res.stdout or "").strip(), (res.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


def tools_available() -> Tuple[bool, str]:
    missing = [t for t in ("kubectl",) if shutil.which(t) is None]
    if missing:
        return False, f"missing tools: {', '.join(missing)}"
    return True, "ok"


def wait_for_gitops_redeploy(
    grace_period: float = 60.0,
    app_name: Optional[str] = None,
) -> Tuple[bool, str]:
    """Best-effort Argo sync + kubectl rollout wait."""
    app = app_name or DEFAULT_APP_NAME
    ok, msg = tools_available()
    if not ok:
        return False, msg

    timeout_s = int(max(30.0, grace_period))

    if shutil.which("argocd"):
        logger.info("GitOps: argocd app sync '%s'...", app)
        code, out, err = _run(
            ["argocd", "app", "sync", app, "--force", "--prune"],
            timeout=float(timeout_s),
        )
        if code != 0:
            logger.warning("argocd sync warning: %s", err or out)
    else:
        logger.warning("argocd CLI not found; relying on kubectl rollout only")

    logger.info(
        "GitOps: waiting for deployment/%s in ns %s (timeout=%ss)...",
        DEFAULT_DEPLOYMENT,
        DEFAULT_NAMESPACE,
        timeout_s,
    )
    code, out, err = _run(
        [
            "kubectl",
            "-n",
            DEFAULT_NAMESPACE,
            "rollout",
            "status",
            f"deployment/{DEFAULT_DEPLOYMENT}",
            f"--timeout={timeout_s}s",
        ],
        timeout=float(timeout_s + 15),
    )
    if code == 0:
        return True, out or f"deployment/{DEFAULT_DEPLOYMENT} rolled out"
    return False, err or out or "kubectl rollout status failed"
