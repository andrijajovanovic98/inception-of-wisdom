"""
Inception-of-Wisdom (IoW) - Part 1: Observer
HTTP Probe: Endpoint Health Monitoring (Hard Crash vs. Soft Suggestion)
"""

import time
import logging
import threading
from typing import List, Dict, Optional, Callable, Any
from dataclasses import dataclass, asdict

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment,misc]

logger = logging.getLogger("p1.http_probe")
# Probe GETs every few seconds - keep them out of the dashboard terminal
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class ProbeResult:
    """Snapshot of a single HTTP probe execution."""
    url: str
    status_code: Optional[int] = None
    response_time_ms: float = 0.0
    is_healthy: bool = False
    is_crash: bool = False           # 5xx, connection refused, timeout -> triggers auto-heal
    is_suggestion: bool = False      # 4xx (missing route) -> flagged for human, never patched
    error_message: Optional[str] = None
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HttpProbeManager:
    """Probes configured HTTP endpoints on the target service.
    Separates hard signals (crashes) from soft signals (suggestions).
    """

    def __init__(
        self,
        service_url: str = "http://localhost:5000",
        probe_paths: Optional[List[str]] = None,
        interval_seconds: float = 3.0,
        timeout_seconds: float = 2.0,
        on_crash: Optional[Callable[[ProbeResult], Any]] = None,
        on_suggestion: Optional[Callable[[ProbeResult], Any]] = None
    ):
        self.service_url = service_url.rstrip("/")
        self.probe_paths = probe_paths if probe_paths is not None else ["/", "/healthz"]
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds

        self.on_crash = on_crash
        self.on_suggestion = on_suggestion

        # Latest results mapped by full url
        self._latest_results: Dict[str, ProbeResult] = {}
        self._lock = threading.Lock()

        # While a deliberate restart is in flight the target is expected to be
        # briefly unreachable; probing continues but must not raise crash events.
        self._suppress_until: float = 0.0

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Starts the background HTTP probing thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.debug("HttpProbeManager worker is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._probe_loop,
            name="HttpProbeWorker",
            daemon=True
        )
        self._thread.start()
        logger.info(
            f"HttpProbeManager started for {self.service_url} "
            f"paths={self.probe_paths} (interval={self.interval_seconds}s)"
        )

    def stop(self, timeout: float = 2.0) -> None:
        """Stops the background HTTP probe thread cleanly."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("HttpProbeManager stopped.")

    def probe_all_once(self) -> List[ProbeResult]:
        """Runs a single round of probes synchronously across all configured paths."""
        results: List[ProbeResult] = []
        for path in self.probe_paths:
            full_url = f"{self.service_url}{path if path.startswith('/') else '/' + path}"
            result = self._probe_single_url(full_url)
            results.append(result)

            with self._lock:
                self._latest_results[full_url] = result

            if time.time() < self._suppress_until:
                continue

            # Fire appropriate callbacks based on signal classification
            if result.is_crash and self.on_crash:
                try:
                    self.on_crash(result)
                except Exception as e:
                    logger.error(f"Error in on_crash callback: {e}")
            elif result.is_suggestion and self.on_suggestion:
                try:
                    self.on_suggestion(result)
                except Exception as e:
                    logger.error(f"Error in on_suggestion callback: {e}")

        return results

    def _probe_single_url(self, url: str) -> ProbeResult:
        """Executes a single HTTP GET request and classifies the outcome."""
        now = time.time()
        start_time = time.perf_counter()

        if httpx is None:
            return ProbeResult(
                url=url,
                is_healthy=False,
                is_crash=True,
                error_message="The 'httpx' python library is not installed",
                timestamp=now
            )

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(url)
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                status_code = response.status_code

                # 2xx Success: Healthy
                if 200 <= status_code < 300:
                    return ProbeResult(
                        url=url,
                        status_code=status_code,
                        response_time_ms=round(duration_ms, 2),
                        is_healthy=True,
                        is_crash=False,
                        is_suggestion=False,
                        timestamp=now
                    )

                # 4xx Client Error: Soft Suggestion (Subject: Flagged for human, NEVER auto-healed)
                elif 400 <= status_code < 500:
                    return ProbeResult(
                        url=url,
                        status_code=status_code,
                        response_time_ms=round(duration_ms, 2),
                        is_healthy=False,
                        is_crash=False,
                        is_suggestion=True,
                        error_message=f"HTTP {status_code} on {url}: functional gap / missing route",
                        timestamp=now
                    )

                # 5xx Server Error: Hard Crash (Subject: Triggers Wisdom Loop)
                elif status_code >= 500:
                    return ProbeResult(
                        url=url,
                        status_code=status_code,
                        response_time_ms=round(duration_ms, 2),
                        is_healthy=False,
                        is_crash=True,
                        is_suggestion=False,
                        error_message=f"HTTP {status_code} server error on {url}",
                        timestamp=now
                    )

                # 1xx / 3xx: not a crash and not a functional gap. A redirect is a
                # deliberate answer from a live service, so it must never auto-heal.
                else:
                    return ProbeResult(
                        url=url,
                        status_code=status_code,
                        response_time_ms=round(duration_ms, 2),
                        is_healthy=False,
                        is_crash=False,
                        is_suggestion=False,
                        error_message=f"HTTP {status_code} on {url}: unexpected non-2xx response",
                        timestamp=now
                    )

        except httpx.ConnectError:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return ProbeResult(
                url=url,
                status_code=None,
                response_time_ms=round(duration_ms, 2),
                is_healthy=False,
                is_crash=True,
                is_suggestion=False,
                error_message=f"Connection refused to {url} (target down)",
                timestamp=now
            )

        except httpx.TimeoutException:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return ProbeResult(
                url=url,
                status_code=None,
                response_time_ms=round(duration_ms, 2),
                is_healthy=False,
                is_crash=True,
                is_suggestion=False,
                error_message=f"HTTP request timed out after {self.timeout_seconds}s on {url}",
                timestamp=now
            )

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return ProbeResult(
                url=url,
                status_code=None,
                response_time_ms=round(duration_ms, 2),
                is_healthy=False,
                is_crash=True,
                is_suggestion=False,
                error_message=f"Probe failure on {url}: {str(e)}",
                timestamp=now
            )

    def _probe_loop(self) -> None:
        """Background continuous probing loop."""
        while not self._stop_event.is_set():
            self.probe_all_once()
            # Sleep with fine granularity to respond promptly to stop_event
            elapsed = 0.0
            step = 0.2
            while elapsed < self.interval_seconds and not self._stop_event.is_set():
                time.sleep(step)
                elapsed += step

    def get_latest_results(self) -> Dict[str, Dict[str, Any]]:
        """Returns a snapshot dictionary of the most recent probe for each path."""
        with self._lock:
            return {url: res.to_dict() for url, res in self._latest_results.items()}

    def suppress_events_for(self, seconds: float) -> None:
        """Stop raising crash/suggestion events for a moment (deliberate restart)."""
        self._suppress_until = max(self._suppress_until, time.time() + max(0.0, seconds))

    def resume_events(self) -> None:
        """End any active suppression window immediately."""
        self._suppress_until = 0.0

    def is_target_healthy(self) -> bool:
        """True when no probe reports a hard failure.

        A 4xx is a *suggestion* - a functional gap for a human to decide on - so a
        deliberately missing route on the probe list must not make a perfectly
        healthy service read as down.
        """
        with self._lock:
            if not self._latest_results:
                return False
            if not any(res.is_healthy for res in self._latest_results.values()):
                return False
            return not any(res.is_crash for res in self._latest_results.values())

    def get_health_detail(self) -> Dict[str, Any]:
        """Health broken down by signal class, for the dashboard."""
        with self._lock:
            results = list(self._latest_results.values())
        return {
            "healthy": self.is_target_healthy(),
            "probes_total": len(results),
            "probes_ok": sum(1 for r in results if r.is_healthy),
            "probes_failing": sum(1 for r in results if r.is_crash),
            "probes_suggesting": sum(1 for r in results if r.is_suggestion),
        }
