"""
Inception-of-Wisdom (IoW) - Dashboard & Central Application
Integrates Part 1 (Observer), Part 2 (Analyst), and Part 3 (Wisdom Loop) into a unified FastAPI service.
"""

from __future__ import annotations

import os
import time
import logging
import asyncio
import threading
from typing import Optional, Dict, Any, List, Sequence, Tuple
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("iow.dashboard")

# Enforce local cache directories in /tmp/ to protect user quota. The embedding model
# resolves through HF_HOME only - the cache `make setup` prefetches into.
os.environ.setdefault("HF_HOME", "/tmp/iow/hf-cache")
os.environ.setdefault("CHROMA_CACHE_DIR", "/tmp/iow/chroma_db")
os.environ.setdefault("TORCH_HOME", "/tmp/iow/hf-cache/torch")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)


class _QuietCancelledFilter(logging.Filter):
    """Drop Ctrl+C / graceful-shutdown CancelledError noise from ASGI/SSE."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "CancelledError" in msg or "timeout graceful shutdown" in msg:
            return False
        if record.exc_info and record.exc_info[0] is not None:
            try:
                if issubclass(record.exc_info[0], asyncio.CancelledError):
                    return False
            except Exception:
                pass
        return True


for _name in ("uvicorn.error", "uvicorn.access", "sse_starlette.sse", "asyncio"):
    logging.getLogger(_name).addFilter(_QuietCancelledFilter())

# FastAPI imports
try:
    from fastapi import FastAPI, Request, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
    from fastapi.staticfiles import StaticFiles
except ImportError:
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]
    HTTPException = Exception  # type: ignore[assignment,misc]
    HTMLResponse = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]
    FileResponse = None  # type: ignore[assignment,misc]
    Response = None  # type: ignore[assignment,misc]
    StaticFiles = None  # type: ignore[assignment,misc]

# Part 1: Observer imports
from p1.docker_monitor import DockerMonitor  # noqa: E402
from p1.log_streamer import LogStreamer  # noqa: E402
from p1.http_probe import HttpProbeManager  # noqa: E402
from p1.event_manager import EventManager, ObserverEvent  # noqa: E402
from p1.observer_api import create_observer_router  # noqa: E402

# Part 2: Analyst imports
from p2.chunker import AstChunker  # noqa: E402
from p2.db import ChromaVectorDB  # noqa: E402
from p2.retriever import CodeRetriever  # noqa: E402
from p2.diagnostician import CrashDiagnostician, DiagnosticReport  # noqa: E402
from p2.watcher import IndexWatcher  # noqa: E402
from p2.analyst_api import create_analyst_router  # noqa: E402

# Part 3: Wisdom Loop imports
from p3.patcher import CodePatcher, StructuredPatch  # noqa: E402
from p3.sanity import SanityChecker  # noqa: E402
from p3.git_manager import GitManager  # noqa: E402
from p3.verifier import TargetVerifier, incident_excerpt  # noqa: E402
from p3.loop import HealCycleRecord, WisdomLoop  # noqa: E402
from p3.safety import SafetyManager  # noqa: E402
from p3.loop_api import create_loop_router  # noqa: E402

# Chapter VI: Bonus imports
from bonus.classifier import TinySymptomClassifier  # noqa: E402
from bonus.consensus import SecondOpinionEngine  # noqa: E402
from bonus.pr_manager import PullRequestManager, PullRequestRecord  # noqa: E402
from bonus.bonus_api import create_bonus_router  # noqa: E402


# Progressive stage from the Makefile (p1 / p2 / p3 / bonus / full).
IOW_MODE = os.environ.get("IOW_MODE", "full").strip().lower() or "full"
if IOW_MODE not in ("p1", "p2", "p3", "bonus", "full"):
    IOW_MODE = "full"

# Part 3 only exists from `make p3` onwards. Before that the loop must not patch,
# commit or restart anything, no matter what the Observer sees.
LOOP_ENABLED = IOW_MODE in ("p3", "bonus", "full")
# The Bonus chapter only applies in the bonus stages; a bonus flag must never be
# able to divert the mandatory Wisdom Loop in `make p3`.
BONUS_ENABLED = IOW_MODE in ("bonus", "full")

# Bonus operational flags (shared with bonus router)
bonus_flags: Dict[str, bool] = {
    "human_in_the_loop": False,
    # Off by default: two prompts from a 1.5B model rarely agree, and a divergence
    # must not be able to suppress the mandatory heal.
    "second_opinion_mandatory": False,
    "fast_path_classifier": True,
}

CONFIG_FILE = os.environ.get("IOW_CONFIG_PATH", "demo_app/iow.config.yml")
TARGET_DIR = os.environ.get("IOW_TARGET_DIR", "demo_app")
REPO_PATH = os.environ.get("IOW_REPO_PATH", ".")


class InceptionOrchestrator:
    """Central orchestrator that holds instances of all IoW subsystems
    and connects Observer crash events to the Wisdom Loop.
    """

    def __init__(self, config_path: str = CONFIG_FILE):
        self.config_path = config_path
        self.auto_heal_enabled: bool = True
        self.running: bool = False
        self._config_watch_stop = threading.Event()

        # 1. Safety Manager
        self.safety_manager = SafetyManager(config_path)

        # get_section() serves the nested blocks of iow.config.yml verbatim. Reading
        # them off the safety dict used to yield {} every time, which silently made
        # every target:/analyst: setting in the file dead.
        target_cfg = self.safety_manager.get_section("target")
        analyst_cfg = self.safety_manager.get_section("analyst")
        if not target_cfg:
            logger.warning(
                "No 'target:' section in %s - falling back to built-in defaults.",
                config_path,
            )

        container_name = os.environ.get(
            "TARGET_CONTAINER", target_cfg.get("container_name", "iow_demo_target")
        )
        target_url = os.environ.get("TARGET_URL", target_cfg.get("service_url", "http://demo_app:5000"))
        # Validated, and re-applied whenever the file changes (refresh_live_settings).
        self._live_settings = self.safety_manager.read_live_settings()
        self._config_version = self.safety_manager.version
        live = self._live_settings
        probe_interval = live["probe_interval_seconds"]
        incident_debounce = live["incident_debounce_seconds"]
        self.incident_debounce = incident_debounce

        model_name = os.environ.get("OLLAMA_MODEL", analyst_cfg.get("model", "qwen2.5-coder:1.5b"))
        embedding_model = analyst_cfg.get("embedding_model", "all-MiniLM-L6-v2")
        ollama_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11436")
        # Makefile / ollama CLI use host:port; HTTP clients need a full URL.
        if ollama_host and "://" not in ollama_host:
            ollama_host = f"http://{ollama_host}"

        # 2. Observer (Part 1)
        # In GitOps mode the target is a Kubernetes pod, so container state and logs
        # must come from the cluster - not from an unrelated compose container that
        # merely shares the name.
        self.redeploy_mode = (
            os.environ.get("IOW_REDEPLOY_MODE") or "docker"
        ).strip().lower()
        self.docker_monitor: Any = DockerMonitor(container_name=container_name)
        if self.redeploy_mode == "gitops":
            try:
                from bonus.gitops import KubeTargetMonitor
                self.docker_monitor = KubeTargetMonitor()
                logger.info(
                    "GitOps mode: observing Kubernetes workload '%s' instead of a "
                    "compose container.", self.docker_monitor.container_name,
                )
            except Exception as e:
                logger.warning(
                    "GitOps mode requested but the Kubernetes target is unavailable "
                    "(%s) - falling back to the Docker container.", e,
                )

        self.log_streamer = LogStreamer(
            docker_monitor=self.docker_monitor,
            error_patterns=live["error_patterns"],
            incident_debounce_seconds=incident_debounce,
        )
        self.http_probe = HttpProbeManager(
            service_url=target_url,
            probe_paths=live["probe_urls"],
            interval_seconds=probe_interval,
            timeout_seconds=live["probe_timeout_seconds"],
        )
        self.event_manager = EventManager(
            docker_monitor=self.docker_monitor,
            log_streamer=self.log_streamer,
            http_probe=self.http_probe,
            docker_poll_seconds=probe_interval,
            crash_window_seconds=self._crash_window(live["crash_dedup_window_seconds"]),
            suggestion_window_seconds=live["suggestion_dedup_window_seconds"],
        )

        # 3. Analyst (Part 2)
        self.chunker = AstChunker(base_dir=REPO_PATH)
        self.vector_db = ChromaVectorDB(
            persist_dir=os.environ.get("CHROMA_CACHE_DIR", "/tmp/iow/chroma_db"),
            embedding_model_name=embedding_model,
        )
        self.retriever = CodeRetriever(
            vector_db=self.vector_db,
            default_top_k=live["top_k"],
        )
        self.diagnostician = CrashDiagnostician(
            retriever=self.retriever,
            model_name=model_name,
            ollama_host=ollama_host
        )

        # 4. Wisdom Loop (Part 3)
        self.git_manager = GitManager(repo_dir=REPO_PATH)
        if self.redeploy_mode == "gitops":
            # Every commit the agent makes stamps the pod template with the code's tree
            # hash: the commit alone is the redeploy, Argo CD rolls the pod (bonus/gitops.py).
            from bonus.gitops import DEFAULT_MANIFEST
            self.git_manager.manifest_stamp = DEFAULT_MANIFEST
        # The gate refuses paths outside demo_app/ (and iow.config.yml) up front, so
        # such a patch is reported as a sanity violation rather than a git error.
        self.sanity_checker = SanityChecker(
            base_dir=REPO_PATH, path_guard=self.git_manager._is_heal_path
        )
        self.patcher = CodePatcher(
            base_dir=REPO_PATH,
            model_name=model_name,
            ollama_host=ollama_host,
            patch_strategy=live["patch_strategy"],
        )
        self.index_watcher = IndexWatcher(
            chunker=self.chunker,
            vector_db=self.vector_db,
            target_dir=TARGET_DIR,
            interval_seconds=live["watch_interval_seconds"],
        )
        self.verifier = TargetVerifier(
            docker_monitor=self.docker_monitor,
            http_probe=self.http_probe,
            event_manager=self.event_manager,
            log_streamer=self.log_streamer,
            git_manager=self.git_manager,
            boot_timeout_provider=self.safety_manager.get_boot_timeout,
        )
        self.wisdom_loop = WisdomLoop(
            git_manager=self.git_manager,
            diagnostician=self.diagnostician,
            patcher=self.patcher,
            sanity_checker=self.sanity_checker,
            verifier=self.verifier,
            docker_monitor=self.docker_monitor,
            max_attempts=3
        )

        # 5. Bonus Suite (Chapter VI)
        self.classifier = TinySymptomClassifier()
        self.consensus_engine = SecondOpinionEngine(
            retriever=self.retriever,
            model_name=model_name,
            ollama_host=ollama_host
        )
        self.pr_manager = PullRequestManager(git_manager=self.git_manager)

        # Register event subscriber for autonomous healing
        self.event_manager.add_listener(self._on_observer_event)

    def _crash_window(self, configured: float) -> float:
        """The crash dedup window; on Kubernetes it must outlast the pod's restart backoff."""
        if self.redeploy_mode != "gitops":
            return configured
        from bonus.gitops import crash_dedup_window
        window = crash_dedup_window(configured)
        if window != configured:
            logger.info(
                "GitOps mode: crash dedup window %.0fs instead of %.0fs - Kubernetes backs a "
                "crash-looping pod off up to 300 s, and one crash loop must stay one event.",
                window, configured,
            )
        return window

    def land_pull_request(self, pr_id: str, comment: Optional[str] = None) -> Tuple[int, str]:
        """Approve a PR: land it like a heal. Returns (http status, message).

        Landing patches the target and restarts it: the kill switch and single-flight
        apply (a human approval is not counted against the rate limit or hard cap), and
        it holds the loop's cycle slot so no heal can write the same tree meanwhile.
        """
        allowed, reason = self.safety_manager.acquire_exclusive_slot(f"pr-merge:{pr_id}")
        if not allowed:
            return 409, f"Landing refused by the safety bounds: {reason}"
        try:
            if not self.wisdom_loop.claim():
                return 409, "A heal cycle is running - approve the PR when it has finished."
            try:
                ok, message = self.pr_manager.merge_pr(
                    pr_id, comment=comment, on_merged=self._land_merged_pr
                )
                self._resolve_review(pr_id)
                return (200 if ok else 400), message
            finally:
                self.wisdom_loop.release()
        finally:
            self.safety_manager.release_heal_slot()

    def reject_pull_request(self, pr_id: str, comment: Optional[str] = None) -> Tuple[int, str]:
        """Reject a PR. Returns (http status, message)."""
        ok, message = self.pr_manager.reject_pr(pr_id, reason=comment)
        if ok:
            self._resolve_review(pr_id)
        return (200 if ok else 400), message

    def _resolve_review(self, pr_id: str) -> None:
        """Show a human's decision on a review PR on the heal cycle that proposed it."""
        pr = self.pr_manager.get_pr(pr_id)
        if pr is None:
            return
        landing = pr.landing or {}
        if pr.status == "merged":
            self.wisdom_loop.resolve_review(pr_id, "healed", (
                "Approved by a human and verified: the target stayed up for the grace period."
                if landing.get("healed") else "Approved by a human and landed."
            ))
        elif pr.status == "reverted":
            self.wisdom_loop.resolve_review(pr_id, "rolled_back", (
                f"Approved by a human, but the target did not stay up on it "
                f"({landing.get('status')}): rolled back to {pr.base_revision[:8]}."
            ))
        elif pr.status == "rejected":
            self.wisdom_loop.resolve_review(
                pr_id, "refused", f"Review rejected by a human: {pr.reviewer_comment}"
            )

    def _land_merged_pr(self, pr_record: Any) -> Dict[str, Any]:
        """Deploy approved code the way the loop deploys a patch, and keep it only if the
        target stays up for the grace period - otherwise roll back to the PR's base.

        The redeploy goes through the Verifier: in GitOps mode that also pushes the heal
        branch Argo CD tracks (a bare pod restart used to re-clone the old code).
        """
        grace = self.safety_manager.get_grace_period()
        logger.info(f"PR [{pr_record.pr_id}] approved - deploying it (grace period {grace}s)...")
        result = self.verifier.restart_and_verify(grace_period=grace)
        outcome: Dict[str, Any] = {
            "healed": result.is_healed,
            "status": result.status,
            "container_status": result.container_status,
        }
        if result.is_healed:
            logger.info(f"PR [{pr_record.pr_id}] landed: the target stayed up for {grace}s.")
            self.event_manager.close_incidents("crash")
            return outcome
        logger.warning(
            f"PR [{pr_record.pr_id}] failed after landing ({result.status}) - rolling back "
            f"to its base {pr_record.base_revision[:8]}."
        )
        restored = self.git_manager.rollback_to(pr_record.base_revision)
        post = self.verifier.restart_and_verify(grace_period=0.0)
        outcome.update({
            "reverted": bool(restored),
            "restored_revision": restored,
            "target_up_after_revert": post.is_healed,
            "error_log": (result.error_log or "")[:2000],
        })
        return outcome

    def gate_review_patch(self, patch_files: List[Dict[str, Any]]) -> Optional[str]:
        """The pre-disk sanity gate for a PR created through the API (None = passes)."""
        patch = StructuredPatch.from_dict({"summary": "review", "files": patch_files})
        if not patch.success:
            return "the patch contains no usable file entry"
        sanity = self.sanity_checker.check_patch(patch)
        return None if sanity.passed else "; ".join(sanity.reasons)

    def _propose_hitl_patch(
        self,
        event: ObserverEvent,
        log_excerpt: str,
        diagnosis_summary: str,
        patch_files: list,
        cycle_id: str,
    ) -> Optional[PullRequestRecord]:
        """Create a Gitea Pull Request instead of auto-applying the patch.

        Returns the PR only when a review actually exists for a human to act on;
        otherwise None, and the caller records the heal as withheld - it is never
        applied without that review.
        """
        logger.info(
            f"Human-in-the-Loop gate active for [{cycle_id}] - opening Gitea PR."
        )
        try:
            # Base the review on what the target actually runs (an uncommitted planted
            # bug included), so the PR diff is exactly the proposed fix.
            pr = self.pr_manager.create_pull_request(
                cycle_id=cycle_id,
                diagnosis_summary=diagnosis_summary,
                patch_files=patch_files,
                base_revision=self.git_manager.working_tree_commit(),
            )
        except Exception as e:
            logger.error(f"Failed to create HITL PR for [{cycle_id}]: {e}")
            return None

        if pr.remote_pr_number:
            logger.info(
                f"HITL PR [{pr.pr_id}] Gitea #{pr.remote_pr_number} created - "
                "awaiting human review."
            )
            return pr

        logger.warning(
            f"HITL PR [{pr.pr_id}] was recorded locally but no remote review was "
            "opened (is Gitea up? try `make pr-bonus`)."
        )
        return None

    def _on_observer_event(self, event: ObserverEvent) -> None:
        """Autonomously triggers the Wisdom Loop when a confirmed crash event arrives."""
        if not self.auto_heal_enabled:
            logger.info(f"Observer event received [{event.signature[:8]}], but auto_heal is disabled.")
            return

        if event.type != "crash":
            logger.debug(f"Observer event [{event.type}] received; only 'crash' triggers auto-healing.")
            return

        # A container that is gone (removed, daemon down) or a pod that never started
        # the app (gitops: source fetch, image pull) is not a code failure: no patch can
        # fix it, so do not spend a heal slot - or a commit - on it. The Observer keeps
        # watching and picks the target up when it returns.
        target_state = self.docker_monitor.inspect()
        if target_state.status in ("not_found", "daemon_unavailable", "not_started") \
                or getattr(target_state, "stopped", False):
            logger.warning(
                "Crash signal [%s] while the target is '%s' (%s) - not something a code "
                "patch can fix; waiting for the target to come back.",
                event.signature[:8], target_state.status, target_state.error_reason or "-",
            )
            return

        # Check safety guardrails before starting background heal
        allowed, reason = self.safety_manager.acquire_heal_slot(event.signature)
        if not allowed:
            logger.warning(f"Auto-heal blocked by safety bounds: {reason}")
            return

        # Run healing in dedicated thread so event bus is never blocked
        threading.Thread(
            target=self._run_autonomous_heal_thread,
            args=(event,),
            name=f"Heal-{event.signature[:8]}",
            daemon=True
        ).start()

    @staticmethod
    def _diagnosis_from_consensus(report: Any) -> DiagnosticReport:
        """A diagnosis only when the two opinions agree; anything else is an explicit failure -
        a divergence, or no usable opinion at all (the model unreachable is said as such)."""
        chunks = list(report.opinion_a.get("candidate_chunks") or [])
        if not report.consensus_reached:
            why = report.consensus_summary
            if report.diverged:
                mode, why = "diverged", f"{why} Only an agreement triggers the patch."
            else:
                modes = {report.opinion_a.get("mode"), report.opinion_b.get("mode")}
                mode = "unavailable" if modes == {"unavailable"} else "unusable"
            return DiagnosticReport(
                success=False,
                summary="",
                files=[],
                candidate_chunks=chunks,
                raw_response="second_opinion_consensus",
                error_message=why,
                mode=mode,
                timestamp=time.time(),
            )
        # The root cause (opinion A), not the vote's bookkeeping: this summary becomes
        # the heal commit's subject line.
        root_cause = str(report.opinion_a.get("summary") or "").strip()
        agreed = ", ".join(report.agreed_files)
        return DiagnosticReport(
            success=True,
            summary=(
                f"{root_cause} (Second opinion agreed on {agreed}.)"
                if root_cause else report.consensus_summary
            ),
            files=list(report.agreed_files),
            candidate_chunks=chunks,
            raw_response="second_opinion_consensus",
            timestamp=time.time(),
        )

    def _consensus_diagnosis(
        self,
        log_excerpt: str,
        exit_code: Optional[int] = None,
        status: str = "crashed",
        exclude_files: Sequence[str] = (),
    ) -> DiagnosticReport:
        """The Analyst's diagnose_crash, but two prompts must agree on the suspect file."""
        report = self.consensus_engine.evaluate_consensus(
            log_excerpt, exclude_files=exclude_files
        )
        return self._diagnosis_from_consensus(report)

    def _read_repo_file(self, rel_path: str) -> Optional[str]:
        """Exact current text of a repository file (None when it does not exist)."""
        full = os.path.join(REPO_PATH, rel_path)
        if not os.path.isfile(full):
            return None
        try:
            with open(full, "r", encoding="utf-8", errors="replace", newline="") as f:
                return f.read()
        except OSError:
            return None

    def _crash_context(self, event: ObserverEvent) -> str:
        """The log text the Analyst should see for this crash: its latest error report.

        A log-pattern event already carries its traceback. A container exit or a dead
        probe usually fires first, before the traceback has even been streamed - so
        let the log settle and read the target's recent output from the daemon.
        Either way the excerpt is cut down to the most recent error report, so an
        older traceback still in the buffer can never steer the diagnosis.
        """
        excerpt = (
            event.details.get("context_excerpt")
            or event.details.get("matched_line")
            or event.details.get("log_excerpt")
            or ""
        )
        lines: List[str] = excerpt.splitlines()
        if event.source in ("docker", "http_probe"):
            time.sleep(self.incident_debounce + 1.0)
            fetch = getattr(self.docker_monitor, "fetch_logs", None)
            fresh = fetch(tail=120) if callable(fetch) else ""
            lines = (fresh or "").splitlines() or self.log_streamer.get_recent_logs(200)
        # Keeps the lines naming the request that raised it: the Verifier replays that
        # request after every redeploy (it used to be trimmed away with the rest).
        header = event.summary if event.source != "log" and event.summary else None
        return incident_excerpt(lines, header=header) or excerpt or event.summary or ""

    def run_heal_cycle(self, event: ObserverEvent, grace_period: float) -> None:
        """HITL-aware heal used by the observer thread and the Loop API trigger."""
        sig = event.signature

        if not LOOP_ENABLED:
            logger.info(
                "Crash [%s] observed, but the Wisdom Loop is locked in mode '%s'. "
                "Run `make p3` (or `make bonus` / `make run`) to enable healing.",
                sig[:8], IOW_MODE,
            )
            return

        log_excerpt = self._crash_context(event)
        event.details["context_excerpt"] = log_excerpt

        # Nothing a patch can fix - a target stopped on purpose, or a crash that is already
        # gone: asked once, before the classifier, the second opinion or a review PR spend
        # anything on it. With review on, a heal of an old event used to go to the model and
        # open a review PR proposing a change to working code (seen live: Gitea #14).
        refused = self.wisdom_loop.nothing_to_patch(event)
        if refused is not None:
            if refused.status == "not_needed":
                self.event_manager.close_incidents("crash")
            return

        # --- Bonus: tiny classifier fast path -------------------------------------
        # A known symptom reuses its verified patch and skips the LLM entirely.
        fast_path_patch = None
        fast_path_symptom: Optional[str] = None
        if BONUS_ENABLED and bonus_flags.get("fast_path_classifier", True):
            match = self.classifier.classify(
                log_excerpt, raw_signature=sig, read_file=self._read_repo_file
            )
            if match.matched and match.cached_patch:
                candidate = StructuredPatch.from_dict(match.cached_patch)
                if candidate.success and candidate.files:
                    fast_path_patch = candidate
                    fast_path_symptom = match.symptom_id
                    logger.info(
                        "BONUS FAST PATH: symptom [%s] matched at confidence %.2f - "
                        "applying the cached patch and skipping the LLM entirely.",
                        match.symptom_id, match.confidence,
                    )
                else:
                    logger.warning(
                        "Classifier matched [%s] but its cached patch is unusable; "
                        "falling back to the model.", match.symptom_id,
                    )

        # --- Bonus: second opinion -------------------------------------------------
        preset_diagnosis = None
        consensus_diverged = False
        if (
            BONUS_ENABLED
            and fast_path_patch is None
            and bonus_flags.get("second_opinion_mandatory", False)
            and self.consensus_engine
        ):
            try:
                report = self.consensus_engine.evaluate_consensus(
                    log_excerpt, exclude_files=self.git_manager.protected_paths
                )
                if report.consensus_reached:
                    # Reuse the agreed answer instead of paying for it twice.
                    preset_diagnosis = self._diagnosis_from_consensus(report)
                elif report.diverged:
                    consensus_diverged = True
                    logger.warning(
                        f"Second Opinion DIVERGED for [{sig[:8]}] - routing to a HITL PR."
                    )
                else:
                    # No usable opinion at all (the model is down, or both answers were
                    # unusable): nothing disagreed. The loop reports the Analyst's explicit
                    # failure on every attempt, as without the second opinion.
                    logger.warning(f"Second Opinion produced no vote for [{sig[:8]}]: {report.error_message}")
            except Exception as e:
                logger.warning(f"Consensus evaluation failed: {e}")

        # --- Bonus: human-in-the-loop gate ----------------------------------------
        hitl_required = BONUS_ENABLED and (
            bonus_flags.get("human_in_the_loop", False) or consensus_diverged
        )

        if hitl_required:
            # A human must see this patch before it lands (HITL on), or the two opinions
            # disagreed ("only an agreement triggers the patch"). Either way nothing is
            # applied autonomously: the patch goes to review or it is withheld.
            why = (
                "human-in-the-loop review is on"
                if bonus_flags.get("human_in_the_loop", False)
                else "the second opinion diverged"
            )
            cycle_id = (sig[:8] if sig else "manual") + "-" + str(int(time.time()))
            proposed: Optional[PullRequestRecord] = None
            detail = ""
            try:
                patch: Optional[StructuredPatch] = None
                summary = ""
                # A review PR gets the same pre-disk gate as an applied patch.
                if fast_path_patch is not None:
                    # Bonus 3 holds under review too: a known symptom on exactly the code its
                    # verified patch was learned on is proposed without asking the model
                    # (it used to go to the model while the classifier counted a bypass).
                    if self.sanity_checker.check_patch(fast_path_patch).passed:
                        patch = fast_path_patch
                        summary = (
                            f"{fast_path_patch.summary} (known symptom {fast_path_symptom}: the "
                            "classifier's verified patch, the model was not asked)"
                        )
                    else:
                        logger.warning("The classifier's cached patch no longer passes the sanity "
                                       "gate - asking the model instead.")
                if patch is None:
                    diagnosis = preset_diagnosis or self.diagnostician.diagnose_crash(
                        log_excerpt,
                        exit_code=event.details.get("exit_code"),
                        status=event.details.get("status") or "crashed",
                        exclude_files=self.git_manager.protected_paths,
                    )
                    if not diagnosis.success:
                        detail = f"diagnosis failed: {diagnosis.error_message}"
                    else:
                        candidate = self.patcher.generate_patch(diagnosis, log_excerpt)
                        if not (candidate.success and candidate.files):
                            detail = f"patch generation failed: {candidate.error_message}"
                        else:
                            sanity = self.sanity_checker.check_patch(candidate)
                            if sanity.passed:
                                patch, summary = candidate, diagnosis.summary
                            else:
                                detail = "sanity check refused the patch: " + "; ".join(sanity.reasons)
                if patch is not None:
                    patch_files = [
                        {"path": f.path, "op": f.op, "content": f.content}
                        for f in patch.files
                    ]
                    proposed = self._propose_hitl_patch(
                        event, log_excerpt, summary, patch_files, cycle_id
                    )
                    if not proposed:
                        detail = (
                            "the Gitea review PR could not be opened "
                            "(is the forge up? `make pr-bonus`)"
                        )
            except Exception as e:
                logger.error(f"HITL patch proposal failed: {e}")
                detail = f"unexpected error: {e}"

            if proposed:
                self.wisdom_loop.record_refused_cycle(
                    event,
                    f"Patch sent for human review because {why}; it lands only when the "
                    "PR is approved (Bonus tab / Gitea).",
                    status="awaiting_review",
                    review_pr=proposed.pr_id,
                    pre_loop_hash=proposed.base_revision,
                )
            else:
                logger.warning(
                    "Heal [%s] withheld: %s, and %s. Nothing was applied.", sig[:8], why, detail
                )
                self.wisdom_loop.record_refused_cycle(
                    event, f"Patch withheld because {why}; {detail}. Nothing was applied."
                )
            return

        logger.info(f"Starting autonomous Wisdom Loop for crash signature [{sig[:8]}]...")
        # Second-opinion mode votes on every attempt, not only the first: a retry that
        # patched on one opinion would break "only an agreement triggers the patch".
        second_opinion = BONUS_ENABLED and bonus_flags.get("second_opinion_mandatory", False)
        result = self.wisdom_loop.execute_heal(
            event,
            grace_period=grace_period,
            preset_diagnosis=preset_diagnosis,
            fast_path_patch=fast_path_patch,
            diagnose_fn=self._consensus_diagnosis if second_opinion else None,
            check_first=False,                       # asked above, before the bonus gates
        )
        logger.info(f"Wisdom Loop finished with status: {result.status.upper()}")
        if result.status in ("healed", "not_needed"):
            # The target was verified healthy: this incident is over, and the same crash
            # coming back is a new one (the per-signature cooldown still applies).
            self.event_manager.close_incidents("crash")

        if not result.attempts:
            return
        last_att = result.attempts[-1]

        # A cached patch that did not hold up loses trust; a verified heal is learned
        # together with the code it was applied to (it may only replace that again).
        pre_loop = result.pre_loop_hash
        self.classifier.learn_from_cycle(
            result, log_excerpt, raw_signature=sig, used_symptom=fast_path_symptom,
            pre_loop_file=lambda path: self.git_manager.read_blob(pre_loop, path),
        )

        if result.status == "healed" and BONUS_ENABLED:
            self._open_verified_heal_pr(result, last_att)

    def _open_verified_heal_pr(self, result: Any, last_att: Any) -> None:
        """Surface a real Gitea PR after a verified auto-heal."""
        try:
            patch_obj = last_att.patch
            summary = "Verified auto-heal"
            changed_paths: List[str] = []
            if isinstance(patch_obj, dict):
                summary = str(patch_obj.get("summary") or summary)
                changed_paths = [
                    str(f.get("path"))
                    for f in (patch_obj.get("files") or [])
                    if isinstance(f, dict) and f.get("path")
                ]
            pr = self.pr_manager.open_verified_heal_pr(
                cycle_id=result.cycle_id,
                diagnosis_summary=summary,
                files_changed=changed_paths,
                commit_sha=result.final_commit_hash or last_att.commit_hash,
            )
            if pr and pr.remote_html_url:
                logger.info(f"Verified heal PR ready for review: {pr.remote_html_url}")
            elif pr:
                logger.info(
                    f"Verified heal PR recorded locally [{pr.pr_id}] (remote open pending)."
                )
        except Exception as e:
            logger.warning(f"Post-heal Gitea PR open failed: {e}")

    def _run_autonomous_heal_thread(self, event: ObserverEvent) -> None:
        """Worker thread for autonomous heal cycle."""
        sig = event.signature
        grace_period = self.safety_manager.get_grace_period()
        try:
            self.run_heal_cycle(event, grace_period)
        except Exception as e:
            logger.error(f"Unexpected error in autonomous heal thread: {e}")
        finally:
            self.safety_manager.release_heal_slot(sig)

    def startup(self) -> None:
        """Starts background monitors, probes, and indexes codebase."""
        if self.running:
            return
        self.running = True
        logger.info("Initializing Inception-of-Wisdom orchestrator...")

        # 0. A heal cycle the previous agent did not live to finish (stopped or crashed
        # mid-cycle) left an unverified patch in the target's tree. Put the pre-loop
        # state back first - before anything indexes, observes or heals that tree.
        recovered = None
        try:
            recovered = self.wisdom_loop.recover_interrupted_cycle(redeploy=False)
            if recovered is not None:
                logger.warning("Interrupted heal cycle handled on start-up: %s", recovered.failure_reason)
        except Exception as e:
            logger.error(f"Could not recover an interrupted heal cycle: {e}")

        # 1. Reconcile the persistent index with the tree, then keep watching it.
        # The store is never wiped: unchanged chunks are skipped, changed symbols are
        # replaced in place, and symbols that disappeared are pruned.
        try:
            logger.info("Reconciling target codebase with the persistent ChromaDB index...")
            stats = self.index_watcher.sync_once(force_full=True)
            logger.info(
                "ChromaDB in sync: %d chunk(s) embedded, %d unchanged, %d pruned "
                "across %d watched file(s).",
                stats.get("added", 0), stats.get("skipped", 0),
                stats.get("pruned", 0), stats.get("files_changed", 0),
            )
        except Exception as e:
            logger.warning(f"Could not index codebase at startup: {e}")

        # 2. Watch mode (subject requirement): incremental updates as files change.
        try:
            self.index_watcher.start()
        except Exception as e:
            logger.warning(f"Could not start index watch mode: {e}")

        # iow.config.yml is re-read on every change, with or without a dashboard open.
        self._config_watch_stop.clear()
        threading.Thread(
            target=self._config_watch_loop, name="ConfigWatch", daemon=True
        ).start()

        # 3. Start Part 1 Observer (event_manager starts log_streamer + http_probe once).
        # After a recovery the target is first restarted on the restored code - in the
        # background, so the dashboard answers meanwhile; observing starts afterwards,
        # or the recovered code's own crash would be refused as a concurrent heal and
        # sent into cooldown.
        if recovered is not None and recovered.rollback_check is not None:
            threading.Thread(
                target=self._redeploy_then_observe, args=(recovered,),
                name="RecoveryRedeploy", daemon=True,
            ).start()
        else:
            self._start_observer()

    def refresh_live_settings(self) -> Dict[str, Any]:
        """Re-apply iow.config.yml's target:/analyst: settings after an edit (no restart).

        The file promises that its keys are live; only the safety bounds used to be -
        a new probe URL, error pattern or dedup window waited for an agent restart.
        Returns the settings that changed.
        """
        self.safety_manager.reload_config()
        if self.safety_manager.version == self._config_version:
            return {}
        self._config_version = self.safety_manager.version
        new = self.safety_manager.read_live_settings(previous=self._live_settings)
        changed = {k: v for k, v in new.items() if self._live_settings.get(k) != v}
        self._live_settings = new
        if not changed:
            return {}
        self.http_probe.set_probe_paths(new["probe_urls"])
        self.http_probe.interval_seconds = new["probe_interval_seconds"]
        self.http_probe.timeout_seconds = new["probe_timeout_seconds"]
        self.event_manager.docker_poll_seconds = new["probe_interval_seconds"]
        self.event_manager.crash_window_seconds = self._crash_window(new["crash_dedup_window_seconds"])
        self.event_manager.suggestion_window_seconds = new["suggestion_dedup_window_seconds"]
        self.log_streamer.incident_debounce_seconds = new["incident_debounce_seconds"]
        self.incident_debounce = new["incident_debounce_seconds"]
        self.log_streamer.set_error_patterns(new["error_patterns"])
        self.index_watcher.interval_seconds = new["watch_interval_seconds"]
        self.retriever.default_top_k = new["top_k"]
        self.patcher.patch_strategy = new["patch_strategy"]
        logger.info(
            "Applied iow.config.yml without a restart: %s",
            ", ".join(f"{k}={v}" for k, v in changed.items()),
        )
        return changed

    def _config_watch_loop(self) -> None:
        while not self._config_watch_stop.wait(2.0):
            try:
                self.refresh_live_settings()
            except Exception as e:
                logger.warning(f"Could not apply iow.config.yml changes: {e}")

    def _start_observer(self) -> None:
        try:
            self.event_manager.start()
            logger.info("Part 1 Observer services started successfully.")
        except Exception as e:
            logger.error(f"Error starting observer background services: {e}")

    def _redeploy_then_observe(self, recovered: HealCycleRecord) -> None:
        try:
            self.wisdom_loop.redeploy_recovered(recovered)
            logger.info("Target restarted on the recovered pre-loop code: %s", recovered.post_rollback)
        except Exception as e:
            logger.error(f"Could not restart the target after the recovery: {e}")
        finally:
            if self.running:
                self._start_observer()

    def shutdown(self) -> None:
        """Stops all background monitors and threads."""
        self.running = False
        self._config_watch_stop.set()
        logger.info("Shutting down Inception-of-Wisdom orchestrator...")
        try:
            self.index_watcher.stop()
            # event_manager.stop() already stops the probe and the streamer.
            self.event_manager.stop()
        except Exception as e:
            logger.error(f"Error shutting down background services: {e}")

    def get_overview(self) -> Dict[str, Any]:
        """Returns high-level system summary for the top dashboard status bar."""
        docker_status = self.docker_monitor.inspect()
        target_healthy = self.http_probe.is_target_healthy()
        latest_probes = self.http_probe.get_latest_results()
        safety_status = self.safety_manager.get_status()

        return {
            "mode": IOW_MODE,
            "orchestrator": {
                "running": self.running,
                "auto_heal_enabled": self.auto_heal_enabled,
                "loop_enabled": LOOP_ENABLED,
                "bonus_enabled": BONUS_ENABLED,
                "redeploy_mode": self.redeploy_mode,
            },
            "target": {
                "container_name": self.docker_monitor.container_name,
                "status": docker_status.status,
                "exit_code": docker_status.exit_code,
                "restart_count": docker_status.restart_count,
                "is_crash": docker_status.is_crash,
                "reason": docker_status.error_reason,
                "stopped": getattr(docker_status, "stopped", False),
                "http_healthy": target_healthy,
                "http_detail": self.http_probe.get_health_detail(),
                "latest_probes": latest_probes
            },
            "analyst": {
                "chunks_count": self.vector_db.count(),
                "embedding_model": self.vector_db.embedding_model_name,
                "llm_model": self.diagnostician.model_name,
                "watch_mode": self.index_watcher.get_status(),
            },
            "wisdom_loop": {
                "active": self.wisdom_loop.is_active(),
                "history_count": len(self.wisdom_loop.history),
                "git_branch": self.git_manager.get_current_branch(),
                "git_head": self.git_manager.get_current_head(),
                "safety": safety_status
            },
            "bonus": {
                "flags": bonus_flags,
                "gitea": self.pr_manager.remote_status(),
                "classifier": self.classifier.get_stats(),
                "prs_total": len(self.pr_manager.prs),
                "prs_pending": len(self.pr_manager.list_prs(status_filter="pending_review"))
            }
        }


# Global orchestrator instance
orchestrator = InceptionOrchestrator()

# Observing is always on; healing waits for the stage that unlocks Part 3.
orchestrator.auto_heal_enabled = LOOP_ENABLED


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    orchestrator.startup()
    yield
    # Shutdown: close SSE subscribers first so Ctrl+C does not cancel open streams loudly
    shutdown_sse = getattr(getattr(app.state, "observer_router", None), "shutdown_sse", None)
    if callable(shutdown_sse):
        try:
            shutdown_sse()
        except Exception:
            pass
    try:
        await asyncio.sleep(0.05)
    except Exception:
        pass
    orchestrator.shutdown()


def create_app() -> FastAPI:
    """Creates and configures the main FastAPI application."""
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed.")

    app = FastAPI(
        title="Inception of Wisdom (IoW) Dashboard",
        description="Autonomous Self-Healing Agent: Observer, Analyst, and Wisdom Loop",
        version="1.0.0",
        lifespan=lifespan
    )

    # Wire API routers
    observer_router = create_observer_router(
        docker_monitor=orchestrator.docker_monitor,
        log_streamer=orchestrator.log_streamer,
        http_probe=orchestrator.http_probe,
        event_manager=orchestrator.event_manager
    )
    if observer_router:
        app.state.observer_router = observer_router
        app.include_router(observer_router)
        # Subject / IoC-compatible SSE path: GET /events
        stream_handler = getattr(observer_router, "stream_events", None)
        if stream_handler is not None:
            app.add_api_route("/events", stream_handler, methods=["GET"], tags=["Observer"])

    analyst_router = create_analyst_router(
        vector_db=orchestrator.vector_db,
        retriever=orchestrator.retriever,
        diagnostician=orchestrator.diagnostician,
        chunker=orchestrator.chunker,
        target_dir=TARGET_DIR,
        index_watcher=orchestrator.index_watcher,
    )
    if analyst_router:
        app.include_router(analyst_router)

    loop_router = create_loop_router(
        wisdom_loop=orchestrator.wisdom_loop,
        safety_manager=orchestrator.safety_manager,
        event_manager=orchestrator.event_manager,
        heal_executor=orchestrator.run_heal_cycle,
        loop_enabled=LOOP_ENABLED,
        mode=IOW_MODE,
    )
    if loop_router:
        app.include_router(loop_router)

    bonus_router = create_bonus_router(
        classifier=orchestrator.classifier,
        consensus_engine=orchestrator.consensus_engine,
        pr_manager=orchestrator.pr_manager,
        flags=bonus_flags,
        merge_executor=orchestrator.land_pull_request,
        reject_executor=orchestrator.reject_pull_request,
        patch_gate=orchestrator.gate_review_patch,
        enabled=BONUS_ENABLED,
        mode=IOW_MODE,
    )
    if bonus_router:
        app.include_router(bonus_router)

    # High-level overview endpoint
    @app.get("/api/overview")
    def get_overview() -> Dict[str, Any]:
        return orchestrator.get_overview()

    # Toggle autonomous healing
    @app.post("/api/auto-heal/toggle")
    def toggle_auto_heal(enable: Optional[bool] = None) -> Dict[str, Any]:
        wanted = (not orchestrator.auto_heal_enabled) if enable is None else enable
        if wanted and not LOOP_ENABLED:
            # Part 3 is locked before `make p3`: switching auto-heal on here would only
            # burn safety slots (rate limit / hard cap) on heals that never run.
            raise HTTPException(
                status_code=409,
                detail=f"The Wisdom Loop is locked in mode '{IOW_MODE}' - start `make p3` or later.",
            )
        orchestrator.auto_heal_enabled = wanted
        logger.info(f"Auto-heal toggled to: {orchestrator.auto_heal_enabled}")
        return {"auto_heal_enabled": orchestrator.auto_heal_enabled}

    # Browsers ask for it on every load; a 404 there was the only console error left.
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Any:
        return Response(status_code=204)

    # Serve index.html template on root path
    @app.get("/", response_class=HTMLResponse)
    def serve_dashboard():
        template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
        mode = IOW_MODE
        if os.path.isfile(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Progressive dashboard: inject make p1/p2/p3/bonus mode for tab unlock
            inject = f'<script>window.IOW_MODE = "{mode}";</script>\n</head>'
            if "</head>" in content and "window.IOW_MODE" not in content:
                content = content.replace("</head>", inject, 1)
            elif "window.IOW_MODE" in content:
                content = content.replace(
                    'window.IOW_MODE = window.IOW_MODE || "full";',
                    f'window.IOW_MODE = "{mode}";',
                    1,
                )
            return HTMLResponse(content=content)
        return HTMLResponse(
            content=f"""<!DOCTYPE html>
<html>
<head><title>Inception of Wisdom</title></head>
<body style="font-family: sans-serif; background: #0f172a; color: #f8fafc; padding: 40px;">
  <h1>Inception of Wisdom (IoW)</h1>
  <p>Mode: {mode}. Dashboard template missing; API endpoints are active.</p>
</body>
</html>"""
        )

    return app


# Application entry point for uvicorn (uvicorn dashboard.app:app)
app = None
if FastAPI is not None:
    app = create_app()
