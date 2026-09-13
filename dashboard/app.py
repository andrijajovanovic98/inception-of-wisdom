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
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("iow.dashboard")

# Enforce local cache directories in /tmp/ to protect user quota
os.environ.setdefault("HF_HOME", "/tmp/iow/hf-cache")
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", "/tmp/iow/hf-cache/sentence_transformers")
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
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]
    HTTPException = Exception  # type: ignore[assignment,misc]
    HTMLResponse = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]
    FileResponse = None  # type: ignore[assignment,misc]
    StaticFiles = None  # type: ignore[assignment,misc]

# Part 1: Observer imports
from p1.docker_monitor import DockerMonitor  # noqa: E402
from p1.log_streamer import LogStreamer, DEFAULT_ERROR_PATTERNS  # noqa: E402
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
from p3.verifier import TargetVerifier  # noqa: E402
from p3.loop import WisdomLoop  # noqa: E402
from p3.safety import SafetyManager  # noqa: E402
from p3.loop_api import create_loop_router  # noqa: E402

# Chapter VI: Bonus imports
from bonus.classifier import TinySymptomClassifier  # noqa: E402
from bonus.consensus import SecondOpinionEngine  # noqa: E402
from bonus.pr_manager import PullRequestManager  # noqa: E402
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
        probe_interval = float(target_cfg.get("probe_interval_seconds", 3.0))
        probe_timeout = float(target_cfg.get("probe_timeout_seconds", 2.0))
        probe_paths = list(target_cfg.get("probe_urls", ["/", "/healthz"]))
        error_patterns = list(
            target_cfg.get("error_patterns", DEFAULT_ERROR_PATTERNS)
        )
        incident_debounce = float(target_cfg.get("incident_debounce_seconds", 1.0))
        crash_window = float(target_cfg.get("crash_dedup_window_seconds", 60.0))
        suggestion_window = float(target_cfg.get("suggestion_dedup_window_seconds", 300.0))
        watch_interval = float(analyst_cfg.get("watch_interval_seconds", 3.0))

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
            error_patterns=error_patterns,
            incident_debounce_seconds=incident_debounce,
        )
        self.http_probe = HttpProbeManager(
            service_url=target_url,
            probe_paths=probe_paths,
            interval_seconds=probe_interval,
            timeout_seconds=probe_timeout,
        )
        self.event_manager = EventManager(
            docker_monitor=self.docker_monitor,
            log_streamer=self.log_streamer,
            http_probe=self.http_probe,
            docker_poll_seconds=probe_interval,
            crash_window_seconds=crash_window,
            suggestion_window_seconds=suggestion_window,
        )

        # 3. Analyst (Part 2)
        self.chunker = AstChunker(base_dir=REPO_PATH)
        self.vector_db = ChromaVectorDB(
            persist_dir=os.environ.get("CHROMA_CACHE_DIR", "/tmp/iow/chroma_db"),
            embedding_model_name=embedding_model,
        )
        self.retriever = CodeRetriever(
            vector_db=self.vector_db,
            default_top_k=int(analyst_cfg.get("top_k", 3))
        )
        self.diagnostician = CrashDiagnostician(
            retriever=self.retriever,
            model_name=model_name,
            ollama_host=ollama_host
        )

        # 4. Wisdom Loop (Part 3)
        self.git_manager = GitManager(repo_dir=REPO_PATH)
        self.sanity_checker = SanityChecker(base_dir=REPO_PATH)
        self.patcher = CodePatcher(
            base_dir=REPO_PATH,
            model_name=model_name,
            ollama_host=ollama_host
        )
        self.index_watcher = IndexWatcher(
            chunker=self.chunker,
            vector_db=self.vector_db,
            target_dir=TARGET_DIR,
            interval_seconds=watch_interval,
        )
        self.verifier = TargetVerifier(
            docker_monitor=self.docker_monitor,
            http_probe=self.http_probe,
            event_manager=self.event_manager,
            log_streamer=self.log_streamer,
            git_manager=self.git_manager,
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

    def _on_pr_merged(self, pr_record) -> None:
        """Restart target container after a HITL PR is approved and merged."""
        logger.info(f"PR [{pr_record.pr_id}] merged - restarting target container...")
        try:
            self.docker_monitor.restart_target()
            logger.info("Target container restarted after PR merge.")
        except Exception as e:
            logger.error(f"Failed to restart target after PR merge: {e}")

    def _propose_hitl_patch(
        self,
        event: ObserverEvent,
        log_excerpt: str,
        diagnosis_summary: str,
        patch_files: list,
        cycle_id: str,
    ) -> bool:
        """Create a Gitea Pull Request instead of auto-applying the patch.

        Returns True only when a review actually exists for a human to act on; the
        caller falls back to the Wisdom Loop otherwise, so a crash is never silently
        dropped because the forge was down.
        """
        logger.info(
            f"Human-in-the-Loop gate active for [{cycle_id}] - opening Gitea PR."
        )
        try:
            pr = self.pr_manager.create_pull_request(
                cycle_id=cycle_id,
                diagnosis_summary=diagnosis_summary,
                patch_files=patch_files,
                base_revision=self.git_manager.get_current_head(),
            )
        except Exception as e:
            logger.error(f"Failed to create HITL PR for [{cycle_id}]: {e}")
            return False

        if pr.remote_pr_number:
            logger.info(
                f"HITL PR [{pr.pr_id}] Gitea #{pr.remote_pr_number} created - "
                "awaiting human review."
            )
            return True

        logger.warning(
            f"HITL PR [{pr.pr_id}] was recorded locally but no remote review was "
            "opened (is Gitea up? try `make pr-bonus`)."
        )
        return False

    def _on_observer_event(self, event: ObserverEvent) -> None:
        """Autonomously triggers the Wisdom Loop when a confirmed crash event arrives."""
        if not self.auto_heal_enabled:
            logger.info(f"Observer event received [{event.signature[:8]}], but auto_heal is disabled.")
            return

        if event.type != "crash":
            logger.debug(f"Observer event [{event.type}] received; only 'crash' triggers auto-healing.")
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

    def run_heal_cycle(self, event: ObserverEvent, grace_period: float) -> None:
        """HITL-aware heal used by the observer thread and the Loop API trigger."""
        sig = event.signature
        log_excerpt = (
            event.details.get("context_excerpt")
            or event.details.get("matched_line")
            or event.details.get("log_excerpt")
            or event.summary
            or ""
        )

        if not LOOP_ENABLED:
            logger.info(
                "Crash [%s] observed, but the Wisdom Loop is locked in mode '%s'. "
                "Run `make p3` (or `make bonus` / `make run`) to enable healing.",
                sig[:8], IOW_MODE,
            )
            return

        # --- Bonus: tiny classifier fast path -------------------------------------
        # A known symptom reuses its verified patch and skips the LLM entirely.
        fast_path_patch = None
        if BONUS_ENABLED and bonus_flags.get("fast_path_classifier", True):
            match = self.classifier.classify(log_excerpt, raw_signature=sig)
            if match.matched and match.cached_patch:
                candidate = StructuredPatch.from_dict(match.cached_patch)
                if candidate.success and candidate.files:
                    fast_path_patch = candidate
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
                report = self.consensus_engine.evaluate_consensus(log_excerpt, top_k=3)
                if report.consensus_reached:
                    # Reuse the agreed answer instead of paying for it twice.
                    preset_diagnosis = DiagnosticReport(
                        success=True,
                        summary=report.consensus_summary,
                        files=list(report.agreed_files),
                        candidate_chunks=[],
                        raw_response="second_opinion_consensus",
                        timestamp=time.time(),
                    )
                else:
                    consensus_diverged = True
                    logger.warning(
                        f"Second Opinion DIVERGED for [{sig[:8]}] - routing to a HITL PR."
                    )
            except Exception as e:
                logger.warning(f"Consensus evaluation failed: {e}")

        # --- Bonus: human-in-the-loop gate ----------------------------------------
        hitl_required = BONUS_ENABLED and (
            bonus_flags.get("human_in_the_loop", False) or consensus_diverged
        )

        if hitl_required:
            cycle_id = (sig[:8] if sig else "manual") + "-" + str(int(time.time()))
            proposed = False
            try:
                diagnosis = preset_diagnosis or self.diagnostician.diagnose_crash(log_excerpt)
                if diagnosis.success:
                    patch = self.patcher.generate_patch(diagnosis, log_excerpt)
                    if patch.success and patch.files:
                        patch_files = [
                            {"path": f.path, "op": f.op, "content": f.content}
                            for f in patch.files
                        ]
                        proposed = self._propose_hitl_patch(
                            event, log_excerpt, diagnosis.summary, patch_files, cycle_id
                        )
                    else:
                        logger.warning(
                            f"HITL gate active but patch generation failed: {patch.error_message}"
                        )
                else:
                    logger.warning(
                        f"HITL gate active but diagnosis failed: {diagnosis.error_message}"
                    )
            except Exception as e:
                logger.error(f"HITL patch proposal failed: {e}")

            if proposed:
                return
            # The review PR could not be opened (no forge, model failure, …). Falling
            # through is the safe answer: the crash still gets a chance to be healed
            # instead of disappearing silently.
            logger.warning(
                "Could not open a review PR for [%s]; running the Wisdom Loop instead.",
                sig[:8],
            )

        logger.info(f"Starting autonomous Wisdom Loop for crash signature [{sig[:8]}]...")
        result = self.wisdom_loop.execute_heal(
            event,
            grace_period=grace_period,
            preset_diagnosis=preset_diagnosis,
            fast_path_patch=fast_path_patch,
        )
        logger.info(f"Wisdom Loop finished with status: {result.status.upper()}")

        if not result.attempts:
            return
        last_att = result.attempts[-1]

        if result.status == "healed":
            if last_att.patch:
                self.classifier.record_successful_heal(
                    log_excerpt, last_att.patch, raw_signature=sig
                )
            if BONUS_ENABLED:
                self._open_verified_heal_pr(result, last_att)
        elif last_att.source == "classifier_fast_path":
            # The cached patch did not hold up - lower its confidence so the next
            # crash of this shape goes back to the model.
            match_id = self.classifier.extract_features(
                log_excerpt, raw_signature=sig
            ).raw_signature
            self.classifier.record_failure(match_id)

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

        # 3. Start Part 1 Observer (event_manager starts log_streamer + http_probe once)
        try:
            self.event_manager.start()
            logger.info("Part 1 Observer services started successfully.")
        except Exception as e:
            logger.error(f"Error starting observer background services: {e}")

    def shutdown(self) -> None:
        """Stops all background monitors and threads."""
        self.running = False
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
        on_pr_merged=orchestrator._on_pr_merged,
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
        if enable is not None:
            orchestrator.auto_heal_enabled = enable
        else:
            orchestrator.auto_heal_enabled = not orchestrator.auto_heal_enabled
        logger.info(f"Auto-heal toggled to: {orchestrator.auto_heal_enabled}")
        return {"auto_heal_enabled": orchestrator.auto_heal_enabled}

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
