"""
Inception-of-Wisdom (IoW) - Dashboard & Central Application
Integrates Part 1 (Observer), Part 2 (Analyst), and Part 3 (Wisdom Loop) into a unified FastAPI service.
"""

from __future__ import annotations

import os
import logging
import asyncio
import threading
from typing import Optional, Dict, Any
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
from p1.log_streamer import LogStreamer  # noqa: E402
from p1.http_probe import HttpProbeManager  # noqa: E402
from p1.event_manager import EventManager, ObserverEvent  # noqa: E402
from p1.observer_api import create_observer_router  # noqa: E402

# Part 2: Analyst imports
from p2.chunker import AstChunker  # noqa: E402
from p2.db import ChromaVectorDB  # noqa: E402
from p2.retriever import CodeRetriever  # noqa: E402
from p2.diagnostician import CrashDiagnostician  # noqa: E402
from p2.analyst_api import create_analyst_router  # noqa: E402

# Part 3: Wisdom Loop imports
from p3.patcher import CodePatcher  # noqa: E402
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
        cfg = self.safety_manager.reload_config()

        target_cfg = cfg.get("target", {})
        analyst_cfg = cfg.get("analyst", {})

        container_name = os.environ.get(
            "TARGET_CONTAINER", target_cfg.get("container_name", "iow_demo_target")
        )
        target_url = os.environ.get("TARGET_URL", target_cfg.get("service_url", "http://demo_app:5000"))
        probe_interval = float(target_cfg.get("probe_interval_seconds", 3.0))
        error_patterns = target_cfg.get("error_patterns", ["Traceback", "CRITICAL", "Error", "Exception"])

        model_name = os.environ.get("OLLAMA_MODEL", analyst_cfg.get("model", "qwen2.5-coder:1.5b"))
        embedding_model = analyst_cfg.get("embedding_model", "all-MiniLM-L6-v2")
        ollama_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11436")
        # Makefile / ollama CLI use host:port; HTTP clients need a full URL.
        if ollama_host and "://" not in ollama_host:
            ollama_host = f"http://{ollama_host}"

        # 2. Observer (Part 1)
        self.docker_monitor = DockerMonitor(container_name=container_name)
        self.log_streamer = LogStreamer(
            docker_monitor=self.docker_monitor,
            error_patterns=error_patterns
        )
        self.http_probe = HttpProbeManager(
            service_url=target_url,
            probe_paths=target_cfg.get("probe_urls", ["/", "/healthz"]),
            interval_seconds=probe_interval
        )
        self.event_manager = EventManager(
            docker_monitor=self.docker_monitor,
            log_streamer=self.log_streamer,
            http_probe=self.http_probe
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
        self.verifier = TargetVerifier(
            docker_monitor=self.docker_monitor,
            http_probe=self.http_probe,
            event_manager=self.event_manager,
            log_streamer=self.log_streamer
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

    def _run_autonomous_heal_thread(self, event: ObserverEvent) -> None:
        """Worker thread for autonomous heal cycle."""
        sig = event.signature
        grace_period = self.safety_manager.get_grace_period()
        log_excerpt = event.details.get("log_excerpt", "")

        # Bonus: Check classifier for Fast Path bypass
        match = self.classifier.classify(log_excerpt, raw_signature=sig)
        if match.matched and match.cached_patch:
            logger.info(
                f"🎯 BONUS FAST PATH: Symptom [{match.symptom_id}] matched "
                "by Tiny Classifier! Applying cached patch."
            )

        logger.info(f"⚡ Starting autonomous Wisdom Loop for crash signature [{sig[:8]}]...")
        try:
            result = self.wisdom_loop.execute_heal(event, grace_period=grace_period)
            logger.info(f"Wisdom Loop finished with status: {result.status.upper()}")

            # Bonus reinforcement: record successful heal to classifier
            if result.status == "healed" and result.attempts:
                last_att = result.attempts[-1]
                if last_att.patch:
                    self.classifier.record_successful_heal(log_excerpt, last_att.patch, raw_signature=sig)
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

        # 1. Initial codebase AST index
        try:
            logger.info("Indexing target codebase into local ChromaDB...")
            chunks = self.chunker.chunk_directory(TARGET_DIR)
            self.vector_db.sync_chunks(chunks)
            logger.info(f"ChromaDB synced with {len(chunks)} code chunks.")
        except Exception as e:
            logger.warning(f"Could not index codebase at startup: {e}")

        # 2. Start Part 1 streaming and probes
        try:
            self.log_streamer.start()
            self.http_probe.start()
            self.event_manager.start()
            logger.info("Part 1 Observer services started successfully.")
        except Exception as e:
            logger.error(f"Error starting observer background services: {e}")

    def shutdown(self) -> None:
        """Stops all background monitors and threads."""
        self.running = False
        logger.info("Shutting down Inception-of-Wisdom orchestrator...")
        try:
            self.event_manager.stop()
            self.http_probe.stop()
            self.log_streamer.stop()
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
                "auto_heal_enabled": self.auto_heal_enabled
            },
            "target": {
                "container_name": self.docker_monitor.container_name,
                "status": docker_status.status,
                "exit_code": docker_status.exit_code,
                "restart_count": docker_status.restart_count,
                "is_crash": docker_status.is_crash,
                "http_healthy": target_healthy,
                "latest_probes": latest_probes
            },
            "analyst": {
                "chunks_count": self.vector_db.count(),
                "embedding_model": self.vector_db.embedding_model_name,
                "llm_model": self.diagnostician.model_name
            },
            "wisdom_loop": {
                "active": self.wisdom_loop.is_active(),
                "history_count": len(self.wisdom_loop.history),
                "git_branch": self.git_manager.get_current_branch(),
                "git_head": self.git_manager.get_current_head(),
                "safety": safety_status
            },
            "bonus": {
                "classifier": self.classifier.get_stats(),
                "prs_total": len(self.pr_manager.prs),
                "prs_pending": len(self.pr_manager.list_prs(status_filter="pending_review"))
            }
        }


# Global orchestrator instance
orchestrator = InceptionOrchestrator()

# Progressive dashboard mode from Makefile (p1/p2/p3/bonus/full)
IOW_MODE = os.environ.get("IOW_MODE", "full").strip().lower() or "full"
if IOW_MODE == "p1":
    orchestrator.auto_heal_enabled = False


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
        target_dir=TARGET_DIR
    )
    if analyst_router:
        app.include_router(analyst_router)

    loop_router = create_loop_router(
        wisdom_loop=orchestrator.wisdom_loop,
        safety_manager=orchestrator.safety_manager,
        event_manager=orchestrator.event_manager
    )
    if loop_router:
        app.include_router(loop_router)

    bonus_router = create_bonus_router(
        classifier=orchestrator.classifier,
        consensus_engine=orchestrator.consensus_engine,
        pr_manager=orchestrator.pr_manager
    )
    if bonus_router:
        app.include_router(bonus_router)

    # High-level overview endpoint
    @app.get("/api/overview")
    async def get_overview() -> Dict[str, Any]:
        return orchestrator.get_overview()

    # Toggle autonomous healing
    @app.post("/api/auto-heal/toggle")
    async def toggle_auto_heal(enable: Optional[bool] = None) -> Dict[str, Any]:
        if enable is not None:
            orchestrator.auto_heal_enabled = enable
        else:
            orchestrator.auto_heal_enabled = not orchestrator.auto_heal_enabled
        logger.info(f"Auto-heal toggled to: {orchestrator.auto_heal_enabled}")
        return {"auto_heal_enabled": orchestrator.auto_heal_enabled}

    # Serve index.html template on root path
    @app.get("/", response_class=HTMLResponse)
    async def serve_dashboard():
        template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
        mode = os.environ.get("IOW_MODE", "full").strip().lower() or "full"
        if mode not in ("p1", "p2", "p3", "bonus", "full"):
            mode = "full"
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
