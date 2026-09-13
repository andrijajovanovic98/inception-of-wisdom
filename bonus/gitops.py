"""
Inception-of-Wisdom (IoW) - Bonus: GitOps redeploy (Argo CD + iow-k3s).

When IOW_REDEPLOY_MODE=gitops the heal commit is what drives the deployment:

1. the heal branch is pushed to the Gitea repository Argo CD watches,
2. Argo CD is synced to that revision (logging in with the cluster's admin secret),
3. the workload is rolled so its init container re-clones the new revision,
4. the rollout is only accepted once a *new* pod is actually serving.

Step 4 matters: `kubectl rollout status` on an untouched Deployment returns success
immediately, which would report a heal as verified while the cluster still ran the
old code.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import logging
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("bonus.gitops")

DEFAULT_APP_NAME = os.environ.get("IOW_ARGOCD_APP", "iow-demo")
DEFAULT_NAMESPACE = os.environ.get("IOW_GITOPS_NAMESPACE", "iow-demo")
DEFAULT_DEPLOYMENT = os.environ.get("IOW_GITOPS_DEPLOYMENT", "iow-demo")
DEFAULT_ARGOCD_SERVER = os.environ.get("IOW_ARGOCD_SERVER", "127.0.0.1:8080")
DEFAULT_BRANCH = os.environ.get("IOW_GITOPS_BRANCH", "iow/auto-heal")


def redeploy_mode() -> str:
    """Return 'gitops' or 'docker' (default)."""
    return (os.environ.get("IOW_REDEPLOY_MODE") or "docker").strip().lower()


def _run(cmd: List[str], timeout: float = 60.0) -> Tuple[int, str, str]:
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


def _bin_dirs() -> List[str]:
    iow = os.environ.get("IOW_DIR", "/tmp/iow")
    custom = os.environ.get("IOW_GITOPS_BIN", "")
    dirs = []
    if custom:
        dirs.append(custom)
    dirs.append(os.path.join(iow, "bin"))
    return dirs


def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for d in _bin_dirs():
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def tools_available() -> Tuple[bool, str]:
    """kubectl present and the cluster actually answering."""
    if _which("kubectl") is None:
        return False, (
            "missing tool: kubectl "
            "(run make argocd-bonus - installs into /tmp/iow/bin, no sudo)"
        )

    os.environ["PATH"] = os.pathsep.join(_bin_dirs()) + os.pathsep + os.environ.get("PATH", "")
    kube = os.environ.get("KUBECONFIG") or os.path.join(
        os.environ.get("IOW_DIR", "/tmp/iow"), "kube", "config"
    )
    os.environ.setdefault("KUBECONFIG", kube)

    kubectl = _which("kubectl") or "kubectl"
    code, _, err = _run([kubectl, "get", "nodes", "-o", "name"], timeout=20.0)
    if code != 0:
        return False, f"cluster unreachable via KUBECONFIG={kube}: {err}"
    return True, "ok"


# ----------------------------------------------------------------------------------
# Pod inspection
# ----------------------------------------------------------------------------------
def _kubectl() -> str:
    return _which("kubectl") or "kubectl"


def _pod_identities(namespace: str, selector: str) -> List[Tuple[str, str]]:
    """(name, uid) of every pod matching the selector."""
    code, out, _ = _run(
        [_kubectl(), "-n", namespace, "get", "pods", "-l", selector,
         "-o", "jsonpath={range .items[*]}{.metadata.name}|{.metadata.uid}{'\\n'}{end}"],
        timeout=20.0,
    )
    if code != 0 or not out:
        return []
    identities = []
    for line in out.splitlines():
        if "|" in line:
            name, _, uid = line.partition("|")
            identities.append((name.strip(), uid.strip()))
    return identities


def _ready_pod_identities(namespace: str, selector: str) -> List[Tuple[str, str]]:
    """Only pods that are Ready."""
    code, out, _ = _run(
        [_kubectl(), "-n", namespace, "get", "pods", "-l", selector, "-o", "json"],
        timeout=20.0,
    )
    if code != 0 or not out:
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return []
    ready = []
    for item in payload.get("items", []):
        meta = item.get("metadata", {})
        conditions = item.get("status", {}).get("conditions", [])
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            ready.append((meta.get("name", ""), meta.get("uid", "")))
    return ready


# ----------------------------------------------------------------------------------
# Argo CD
# ----------------------------------------------------------------------------------
def _argocd_admin_password() -> Optional[str]:
    code, out, _ = _run(
        [_kubectl(), "-n", "argocd", "get", "secret", "argocd-initial-admin-secret",
         "-o", "jsonpath={.data.password}"],
        timeout=20.0,
    )
    if code != 0 or not out:
        return None
    try:
        return base64.b64decode(out).decode("utf-8").strip()
    except Exception:
        return None


def _argocd_sync(app: str, timeout_s: int) -> Tuple[bool, str]:
    """Log in and sync. A failure here is reported, never swallowed."""
    argocd = _which("argocd")
    if not argocd:
        return False, "argocd CLI not found in PATH or /tmp/iow/bin"

    server = os.environ.get("IOW_ARGOCD_SERVER", DEFAULT_ARGOCD_SERVER)
    password = os.environ.get("IOW_ARGOCD_PASSWORD") or _argocd_admin_password()
    if not password:
        return False, "could not read the Argo CD admin password from the cluster secret"

    code, _, err = _run(
        [argocd, "login", server, "--username", "admin", "--password", password,
         "--insecure", "--plaintext"],
        timeout=10.0,
    )
    if code != 0:
        return False, f"argocd login failed: {err}"

    code, out, err = _run(
        [argocd, "app", "sync", app, "--server", server, "--insecure", "--plaintext",
         "--prune", "--timeout", str(timeout_s)],
        timeout=float(timeout_s + 15),
    )
    if code != 0:
        return False, f"argocd app sync failed: {err or out}"
    return True, "argocd sync ok"


# ----------------------------------------------------------------------------------
# Publishing the heal commit
# ----------------------------------------------------------------------------------
def push_heal_commit(git_manager: Any, branch: Optional[str] = None) -> Tuple[bool, str]:
    """Push the heal branch to the Gitea repository Argo CD watches.

    Without this the commit stays local and the cluster can never see the fix.
    """
    branch = branch or getattr(git_manager, "heal_branch", DEFAULT_BRANCH)
    creds_file = os.environ.get("GITEA_CREDS_FILE", "/tmp/iow/gitea/gitea.env")
    creds: Dict[str, str] = {}
    if os.path.isfile(creds_file):
        try:
            with open(creds_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        creds[key.strip()] = val.strip()
        except OSError as e:
            return False, f"could not read {creds_file}: {e}"

    for key in ("GITEA_URL", "GITEA_OWNER", "GITEA_REPO", "GITEA_TOKEN"):
        env_val = os.environ.get(key)
        if env_val:
            creds[key] = env_val

    token = creds.get("GITEA_TOKEN", "")
    owner = creds.get("GITEA_OWNER", "iow")
    repo = creds.get("GITEA_REPO", "inception-of-wisdom")
    base = (creds.get("GITEA_URL") or "http://127.0.0.1:3000").rstrip("/")
    if not token:
        return False, "no Gitea token available to push the heal commit"

    url = f"{base}/{owner}/{repo}.git"
    rc, _, err = git_manager._run_git(
        ["-c", f"http.extraHeader=Authorization: token {token}",
         "push", "--force", url, f"refs/heads/{branch}:refs/heads/{branch}"],
        check=False,
    )
    if rc != 0:
        return False, f"push of '{branch}' to {owner}/{repo} failed: {err}"
    return True, f"pushed '{branch}' to {owner}/{repo}"


# ----------------------------------------------------------------------------------
# Redeploy + verification
# ----------------------------------------------------------------------------------
def wait_for_gitops_redeploy(
    grace_period: float = 60.0,
    app_name: Optional[str] = None,
    git_manager: Any = None,
) -> Tuple[bool, str]:
    """Publish the commit, sync Argo CD, roll the workload, confirm a new pod serves."""
    app = app_name or DEFAULT_APP_NAME
    namespace = DEFAULT_NAMESPACE
    deployment = DEFAULT_DEPLOYMENT
    selector = os.environ.get("IOW_GITOPS_SELECTOR", f"app={deployment}")

    ok, msg = tools_available()
    if not ok:
        return False, msg

    timeout_s = int(max(60.0, grace_period * 2))

    if git_manager is not None:
        pushed, push_detail = push_heal_commit(git_manager)
        if not pushed:
            return False, f"GitOps publish failed: {push_detail}"
        logger.info("GitOps: %s", push_detail)

    before = {uid for _, uid in _pod_identities(namespace, selector)}

    synced, sync_detail = _argocd_sync(app, timeout_s)
    if synced:
        logger.info("GitOps: %s", sync_detail)
    else:
        # Not fatal on its own: the rollout below is what actually proves the
        # new revision is serving, and it is verified rather than assumed.
        logger.warning("GitOps: %s - falling back to a direct rollout.", sync_detail)

    kubectl = _kubectl()
    code, _, err = _run(
        [kubectl, "-n", namespace, "rollout", "restart", f"deployment/{deployment}"],
        timeout=30.0,
    )
    if code != 0:
        return False, f"kubectl rollout restart failed: {err}"

    code, out, err = _run(
        [kubectl, "-n", namespace, "rollout", "status", f"deployment/{deployment}",
         f"--timeout={timeout_s}s"],
        timeout=float(timeout_s + 15),
    )
    if code != 0:
        return False, f"kubectl rollout status failed: {err or out}"

    # Prove a *different* pod is now Ready. Without this check an unchanged
    # deployment reports success instantly and a heal looks verified for free.
    deadline = time.time() + max(30.0, grace_period)
    while time.time() < deadline:
        ready_now = {uid for _, uid in _ready_pod_identities(namespace, selector)}
        fresh = ready_now - before
        if fresh:
            return True, (
                f"{deployment} rolled out: {len(fresh)} new pod(s) Ready "
                f"({'argocd sync + ' if synced else ''}rollout verified)"
            )
        time.sleep(1.0)

    return False, (
        f"{deployment} reported a rollout but no new pod became Ready - "
        "the cluster is still running the previous revision"
    )


# ----------------------------------------------------------------------------------
# Kubernetes target adapter (Observer in gitops mode)
# ----------------------------------------------------------------------------------
class KubeTargetMonitor:
    """DockerMonitor-compatible view of a Kubernetes workload.

    In gitops mode the target is a pod, not a compose container. Without this the
    Observer probed the cluster over HTTP while reading container state and logs from
    an unrelated compose container.
    """

    def __init__(
        self,
        namespace: str = DEFAULT_NAMESPACE,
        deployment: str = DEFAULT_DEPLOYMENT,
        selector: Optional[str] = None,
    ):
        self.namespace = namespace
        self.deployment = deployment
        self.selector = selector or os.environ.get(
            "IOW_GITOPS_SELECTOR", f"app={deployment}"
        )
        self.container_name = f"{namespace}/{deployment}"
        self._last_state: Optional[Any] = None
        self._restart_baseline: Optional[int] = None

    # -- state ---------------------------------------------------------------
    def inspect(self) -> Any:
        from p1.docker_monitor import ContainerState

        now = time.time()
        ok, msg = tools_available()
        if not ok:
            state = ContainerState(
                name=self.container_name,
                status="daemon_unavailable",
                error_reason=msg,
                timestamp=now,
            )
            self._last_state = state
            return state

        code, out, err = _run(
            [_kubectl(), "-n", self.namespace, "get", "pods", "-l", self.selector, "-o", "json"],
            timeout=20.0,
        )
        if code != 0:
            state = ContainerState(
                name=self.container_name, status="error",
                error_reason=err or "kubectl get pods failed", timestamp=now,
            )
            self._last_state = state
            return state

        try:
            items = json.loads(out).get("items", [])
        except json.JSONDecodeError:
            items = []

        if not items:
            state = ContainerState(
                name=self.container_name, status="not_found",
                error_reason=f"No pod matches '{self.selector}' in {self.namespace}",
                timestamp=now,
            )
            self._last_state = state
            return state

        pod = items[0]
        phase = str(pod.get("status", {}).get("phase", "unknown")).lower()
        statuses = pod.get("status", {}).get("containerStatuses", []) or []
        restarts = sum(int(cs.get("restartCount", 0) or 0) for cs in statuses)

        status = {"running": "running", "pending": "restarting", "failed": "exited",
                  "succeeded": "exited"}.get(phase, phase)
        exit_code = None
        is_crash = False
        reason = None

        for cs in statuses:
            terminated = (cs.get("lastState", {}) or {}).get("terminated") \
                or (cs.get("state", {}) or {}).get("terminated")
            waiting = (cs.get("state", {}) or {}).get("waiting") or {}
            if terminated and terminated.get("exitCode", 0) not in (0, None):
                exit_code = terminated.get("exitCode")
                is_crash = True
                reason = (
                    f"Pod container terminated with exit code {exit_code} "
                    f"({terminated.get('reason', 'Error')})"
                )
            if waiting.get("reason") == "CrashLoopBackOff":
                is_crash = True
                status = "restarting"
                reason = f"Pod entered CrashLoopBackOff (restarts: {restarts})"

        if self._restart_baseline is None:
            self._restart_baseline = restarts

        state = ContainerState(
            name=self.container_name,
            status=status,
            exit_code=exit_code,
            restart_count=restarts,
            is_crash=is_crash,
            error_reason=reason,
            timestamp=now,
        )
        self._last_state = state
        return state

    def get_last_state(self) -> Optional[Any]:
        return self._last_state

    # -- logs ----------------------------------------------------------------
    def get_log_stream(
        self, tail: Optional[int] = None, since: Optional[int] = None
    ) -> Optional[Iterator[bytes]]:
        """Follow pod logs with timestamps, matching DockerMonitor's contract."""
        ok, _ = tools_available()
        if not ok:
            return None

        # No Ready pod means there is nothing to follow yet; returning None lets the
        # streamer back off instead of spinning on an instantly-closing stream.
        if not _ready_pod_identities(self.namespace, self.selector):
            return None

        cmd = [_kubectl(), "-n", self.namespace, "logs", "-l", self.selector,
               "--timestamps=true", "--follow", "--max-log-requests=1"]
        if since is not None:
            cmd.append(f"--since-time={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(since))}")
        elif tail is not None:
            cmd.append(f"--tail={tail}")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            logger.debug(f"kubectl logs failed: {e}")
            return None

        if proc.stdout is None:
            return None

        def _iter() -> Iterator[bytes]:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    yield line
            finally:
                try:
                    proc.terminate()
                except Exception:
                    pass

        return _iter()

    # -- lifecycle -----------------------------------------------------------
    def restart_target(self, timeout: int = 10) -> bool:
        """Roll the deployment - the cluster equivalent of a container restart."""
        ok, msg = tools_available()
        if not ok:
            logger.error(f"Cannot restart k8s target: {msg}")
            return False
        code, _, err = _run(
            [_kubectl(), "-n", self.namespace, "rollout", "restart",
             f"deployment/{self.deployment}"],
            timeout=30.0,
        )
        if code != 0:
            logger.error(f"kubectl rollout restart failed: {err}")
            return False
        self._restart_baseline = None
        return True
