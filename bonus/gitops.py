"""
Inception-of-Wisdom (IoW) - Bonus: GitOps redeploy (Argo CD + iow-k3s).

When IOW_REDEPLOY_MODE=gitops the heal commit alone drives the deployment:

1. every commit the agent makes stamps the pod template with the git tree hash of the
   code (GitManager.manifest_stamp), so a code change is a manifest change;
2. the heal branch is pushed to the Gitea repository Argo CD watches;
3. Argo CD's own polling (15 s, set by the bootstrap) syncs the commit, and Kubernetes
   rolls the pod, whose init container clones that revision;
4. the redeploy is only accepted once a pod carrying the pushed stamp is Ready.

The agent restarts nothing itself unless the pushed code is what already runs (nothing
for Argo CD to roll), and it asks Argo CD to sync only when it has not moved in time.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import logging
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger("bonus.gitops")

DEFAULT_APP_NAME = os.environ.get("IOW_ARGOCD_APP", "iow-demo")
DEFAULT_NAMESPACE = os.environ.get("IOW_GITOPS_NAMESPACE", "iow-demo")
DEFAULT_DEPLOYMENT = os.environ.get("IOW_GITOPS_DEPLOYMENT", "iow-demo")
DEFAULT_ARGOCD_SERVER = os.environ.get("IOW_ARGOCD_SERVER", "127.0.0.1:8080")
DEFAULT_BRANCH = os.environ.get("IOW_GITOPS_BRANCH", "iow/auto-heal")
# The manifest whose pod template carries the code's tree hash, and that annotation.
DEFAULT_MANIFEST = os.environ.get("IOW_GITOPS_MANIFEST", "k8s/demo-app/deployment.yaml")
STAMP_KEY = "iow.42/source"
# How long Argo CD gets to pick a pushed commit up by itself and roll a pod for it,
# before the agent asks it to sync (it polls the repository every 15 s).
SELF_SYNC_S = float(os.environ.get("IOW_GITOPS_SELF_SYNC_S", "75"))


def redeploy_mode() -> str:
    """Return 'gitops' or 'docker' (default)."""
    return (os.environ.get("IOW_REDEPLOY_MODE") or "docker").strip().lower()


# Kubernetes backs a crash-looping pod off up to 300 s between restarts (Docker: 60 s).
CRASH_WINDOW_FLOOR_S = 330.0


def crash_dedup_window(configured: float) -> float:
    """The crash dedup window that keeps one crash loop one event on Kubernetes.

    iow.config.yml's 90 s outlasts Docker's restart backoff, not the cluster's: once the
    pod's backoff passed 90 s every restart raised the same crash again, and the loop was
    retriggered by the same symptom whenever the cooldown had run out (seen live).
    """
    return max(float(configured), CRASH_WINDOW_FLOOR_S)


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
    except subprocess.TimeoutExpired:
        # Not str(e): it quotes the whole command line, and `argocd login` carries the
        # admin password on it - straight into the dashboard log.
        what = " ".join([os.path.basename(cmd[0])] + cmd[1:2])
        return -1, "", f"'{what}' timed out after {timeout:.0f}s"
    except Exception as e:
        return -1, "", f"{os.path.basename(cmd[0])}: {e}"


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


def _ready_pods(namespace: str, selector: str) -> List[Tuple[str, str, Optional[str]]]:
    """(name, uid, source stamp) of every Ready pod that is not being deleted."""
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
        if meta.get("deletionTimestamp"):
            continue
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            stamp = (meta.get("annotations") or {}).get(STAMP_KEY)
            ready.append((meta.get("name", ""), meta.get("uid", ""), stamp))
    return ready


def _deployed_stamp(namespace: str, deployment: str) -> Optional[str]:
    """The source stamp of the pod template the cluster runs now."""
    code, out, _ = _run(
        [_kubectl(), "-n", namespace, "get", "deployment", deployment, "-o", "json"],
        timeout=20.0,
    )
    if code != 0 or not out:
        return None
    try:
        template = json.loads(out)["spec"]["template"]["metadata"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return (template.get("annotations") or {}).get(STAMP_KEY)


def _wait_for_ready_pod(
    namespace: str, selector: str, timeout: float,
    stamp: Optional[str] = None, not_uids: Iterable[str] = (),
) -> int:
    """How many pods became Ready - of `stamp`, or none of `not_uids` - within timeout."""
    excluded = set(not_uids)
    deadline = time.time() + timeout
    while True:
        fresh = [
            uid for _, uid, pod_stamp in _ready_pods(namespace, selector)
            if uid not in excluded and (stamp is None or pod_stamp == stamp)
        ]
        if fresh or time.time() >= deadline:
            return len(fresh)
        time.sleep(1.0)


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


def _is_argocd(server: str, plaintext: bool) -> bool:
    """Whether `server` answers like Argo CD (its unauthenticated /api/version)."""
    try:
        import httpx
        scheme = "http" if plaintext else "https"
        answer = httpx.get(f"{scheme}://{server}/api/version", verify=False, timeout=5.0)
        return answer.status_code == 200 and "Version" in answer.json()
    except Exception:
        return False


def _argocd_sync(app: str, timeout_s: int) -> Tuple[bool, str]:
    """Log in and sync. A failure here is reported, never swallowed."""
    argocd = _which("argocd")
    if not argocd:
        return False, "argocd CLI not found in PATH or /tmp/iow/bin"

    server = os.environ.get("IOW_ARGOCD_SERVER", DEFAULT_ARGOCD_SERVER).strip()
    if not server:
        return False, "no Argo CD API port-forward (IOW_ARGOCD_SERVER is empty)"
    # The stock install (make argocd-bonus) serves TLS with a self-signed certificate:
    # talk TLS and skip verification. `--plaintext` against it hangs until the timeout,
    # so every sync used to fail and fall back to a bare rollout.
    plaintext = os.environ.get("IOW_ARGOCD_PLAINTEXT", "").lower() in ("1", "true", "yes")
    # The login carries the admin password. The port-forward's port can belong to some
    # other local service (8080 is a popular one): never send it there.
    if not _is_argocd(server, plaintext):
        return False, f"{server} does not answer like Argo CD - not logging in there"

    password = os.environ.get("IOW_ARGOCD_PASSWORD") or _argocd_admin_password()
    if not password:
        return False, "could not read the Argo CD admin password from the cluster secret"

    transport = ["--insecure"]
    if plaintext:
        transport.append("--plaintext")  # a server started with server.insecure=true

    code, _, err = _run(
        [argocd, "login", server, "--username", "admin", "--password", password]
        + transport,
        timeout=15.0,
    )
    if code != 0:
        return False, f"argocd login failed: {err}"

    code, out, err = _run(
        [argocd, "app", "sync", app, "--server", server] + transport
        + ["--prune", "--timeout", str(timeout_s)],
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
    """Publish the commit and confirm that a pod serving it is Ready.

    The commit alone is the redeploy: it changed the pod template's source stamp, so
    Argo CD's own sync rolls the pod and the agent only watches. Argo CD is asked to sync
    when it has not moved in time; the pod is restarted by hand only when the pushed code
    is what already runs (a rollback onto deployed code) - then there is nothing to roll.
    """
    app = app_name or DEFAULT_APP_NAME
    namespace = DEFAULT_NAMESPACE
    deployment = DEFAULT_DEPLOYMENT
    selector = os.environ.get("IOW_GITOPS_SELECTOR", f"app={deployment}")

    ok, msg = tools_available()
    if not ok:
        return False, msg

    timeout_s = int(max(60.0, grace_period * 2))
    before = {uid for _, uid in _pod_identities(namespace, selector)}
    wanted: Optional[str] = None

    if git_manager is not None:
        pushed, push_detail = push_heal_commit(git_manager)
        if not pushed:
            return False, f"GitOps publish failed: {push_detail}"
        logger.info("GitOps: %s", push_detail)
        stamp_of = getattr(git_manager, "source_stamp", None)
        if callable(stamp_of):
            wanted = stamp_of(getattr(git_manager, "heal_branch", DEFAULT_BRANCH))

    if wanted and wanted != _deployed_stamp(namespace, deployment):
        started = time.time()
        if _wait_for_ready_pod(namespace, selector, SELF_SYNC_S, stamp=wanted):
            return True, (
                f"Argo CD synced the commit by itself: a pod of source {wanted[:8]} is Ready "
                f"after {time.time() - started:.0f}s (nothing restarted by the agent)"
            )
        logger.warning(
            "GitOps: no pod of source %s after %.0fs - asking Argo CD to sync.", wanted[:8], SELF_SYNC_S
        )
        synced, sync_detail = _argocd_sync(app, timeout_s)
        if _wait_for_ready_pod(namespace, selector, max(30.0, grace_period) + timeout_s, stamp=wanted):
            return True, f"a pod of source {wanted[:8]} is Ready after an explicit Argo CD sync"
        return False, (
            f"no pod of source {wanted[:8]} became Ready ({sync_detail}) - "
            "the cluster is still running the previous revision"
        )

    # Nothing in the manifests changed (the pushed code already runs, or the manifest has
    # no stamp): only a restart re-clones the code.
    synced, sync_detail = (False, "same source as deployed") if wanted else _argocd_sync(app, timeout_s)
    if synced:
        logger.info("GitOps: %s", sync_detail)
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
    fresh = _wait_for_ready_pod(namespace, selector, max(30.0, grace_period), not_uids=before)
    if fresh:
        return True, (
            f"{deployment} rolled out: {fresh} new pod(s) Ready "
            f"({'argocd sync + ' if synced else ''}rollout restart verified)"
        )
    return False, (
        f"{deployment} reported a rollout but no new pod became Ready - "
        "the cluster is still running the previous revision"
    )


# Waiting reasons that mean a container never got as far as running its command.
_START_BLOCKERS = {
    "ErrImagePull", "ImagePullBackOff", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError",
}


def _start_blocker(pod: Dict[str, Any]) -> Optional[str]:
    """Why the pod cannot start the app at all, or None.

    A failing init container (here: the one that fetches the source), an image that
    cannot be pulled or a missing config keep the app from ever running. That is not
    a crash a code patch can fix - seen live: the source fetch could not resolve
    Gitea, the probes found nothing on the port, and the loop "healed" correct code.
    """
    status = pod.get("status", {}) or {}
    for cs in status.get("initContainerStatuses", []) or []:
        state = cs.get("state", {}) or {}
        done = state.get("terminated") or {}
        if done and done.get("exitCode") == 0:
            continue
        waiting = state.get("waiting") or {}
        last = (cs.get("lastState", {}) or {}).get("terminated") or {}
        failed = done.get("exitCode") not in (None, 0) or last.get("exitCode") not in (None, 0)
        if waiting.get("reason") in _START_BLOCKERS or failed:
            reason = waiting.get("reason") or done.get("reason") or last.get("reason") or "Error"
            return (
                f"Init container '{cs.get('name', '?')}' is failing ({reason}): "
                "the pod never started the app"
            )
    for cs in status.get("containerStatuses", []) or []:
        waiting = (cs.get("state", {}) or {}).get("waiting") or {}
        if waiting.get("reason") in _START_BLOCKERS:
            return (
                f"Container '{cs.get('name', '?')}' cannot start ({waiting['reason']}): "
                "the pod never started the app"
            )
    return None


def _newest_pod(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The pod the workload is converging to: the newest one not being deleted.

    During a rollout the old pod keeps serving until the new one is Ready, and when
    the new revision crash-loops Kubernetes leaves it that way: the service is up,
    the deployed commit is broken. Watching the first pod of the list hid exactly
    that crash (seen live).
    """
    live = [p for p in items if not (p.get("metadata") or {}).get("deletionTimestamp")]
    pool = live or items
    if not pool:
        return None
    return max(pool, key=lambda p: str((p.get("metadata") or {}).get("creationTimestamp", "")))


def _app_has_run(pod: Dict[str, Any]) -> bool:
    """Whether an app container of the pod has started at least once (so has logs)."""
    for cs in (pod.get("status") or {}).get("containerStatuses", []) or []:
        state = cs.get("state") or {}
        last = cs.get("lastState") or {}
        if state.get("running") or state.get("terminated") or last.get("terminated"):
            return True
    return False


class _ProcessLines:
    """A followed `kubectl logs` as a stream: iterate its lines, close() from any thread.

    A generator cannot be closed while another thread is blocked inside it, so the
    Observer's "Restart Stream" could never unblock a worker reading a pod's log.
    """

    def __init__(self, proc: "subprocess.Popen[bytes]") -> None:
        self._proc = proc

    def __iter__(self) -> Iterator[bytes]:
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                yield line
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass


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

        pod = _newest_pod(items) or items[0]
        blocker = _start_blocker(pod)
        if blocker:
            state = ContainerState(
                name=self.container_name, status="not_started",
                error_reason=blocker, timestamp=now,
            )
            self._last_state = state
            return state

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

    def target_facts(self) -> Dict[str, Any]:
        """Container ports and (non-secret) literal env of the newest pod's app container."""
        from p1.docker_monitor import target_facts_from

        code, out, _ = _run(
            [_kubectl(), "-n", self.namespace, "get", "pods", "-l", self.selector, "-o", "json"],
            timeout=20.0,
        )
        try:
            pod = _newest_pod(json.loads(out).get("items", [])) if code == 0 else None
        except json.JSONDecodeError:
            pod = None
        containers = ((pod or {}).get("spec") or {}).get("containers") or []
        if not containers:
            return {}
        app = containers[0]
        ports = [p.get("containerPort") for p in app.get("ports") or []]
        env = [
            (str(e.get("name", "")), str(e.get("value", "")))
            for e in app.get("env") or [] if "value" in e
        ]
        return target_facts_from(ports, env)

    # -- logs ----------------------------------------------------------------
    def _newest_pod_with_logs(self) -> Optional[str]:
        """Name of the newest pod, if its app has run at least once."""
        code, out, _ = _run(
            [_kubectl(), "-n", self.namespace, "get", "pods", "-l", self.selector, "-o", "json"],
            timeout=20.0,
        )
        try:
            items = json.loads(out).get("items", []) if code == 0 else []
        except json.JSONDecodeError:
            items = []
        pod = _newest_pod(items)
        if pod is None or not _app_has_run(pod):
            return None
        return str((pod.get("metadata") or {}).get("name", "")) or None

    def fetch_logs(self, since: Optional[float] = None, tail: int = 60) -> str:
        """What the newest pod printed (not followed), as DockerMonitor.fetch_logs.

        The verifier hands this back to the Analyst when a redeploy fails. Without it
        a gitops attempt that crashed the new pod reported only "kubectl rollout status
        timed out", and the next attempt was aimed at an unrelated chunk (seen live).
        """
        ok, _ = tools_available()
        if not ok:
            return ""
        name = self._newest_pod_with_logs()
        if not name:
            return ""
        cmd = [_kubectl(), "-n", self.namespace, "logs", name, f"--tail={tail}"]
        if since is not None:
            cmd.append(f"--since-time={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(since))}")
        code, out, _ = _run(cmd, timeout=20.0)
        return out if code == 0 else ""

    def get_log_stream(
        self, tail: Optional[int] = None, since: Optional[int] = None
    ) -> Optional[Iterable[bytes]]:
        """Follow the newest pod's logs with timestamps, matching DockerMonitor's contract.

        By name, not by selector: during a rollout two pods match, and a followed
        selector with --max-log-requests=1 fails outright - no logs for as long as a
        rollout lasts, or for good when the new revision crash-loops.
        """
        ok, _ = tools_available()
        if not ok:
            return None

        # An app that has not run yet has nothing to follow; returning None lets the
        # streamer back off instead of spinning on an instantly-closing stream.
        name = self._newest_pod_with_logs()
        if not name:
            return None

        cmd = [_kubectl(), "-n", self.namespace, "logs", name,
               "--timestamps=true", "--follow"]
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
        return _ProcessLines(proc)

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
