"""
Inception-of-Wisdom (IoW) - Part 2: Analyst
Index Watcher: incremental watch mode over the target source tree.

Subject requirement: "The index must be persistent across restarts, must update
incrementally as files change (watch mode), and must never wipe and rebuild on every
start."

The watcher polls mtimes rather than pulling in an inotify dependency: it re-chunks
only the files that actually changed, upserts them in place, prunes the symbols they
no longer contain, and drops files that disappeared. A peer who has already paid the
indexing cost never pays it again.
"""

from __future__ import annotations

import os
import logging
import threading
from typing import Dict, List, Optional, Tuple

from p2.chunker import AstChunker, CodeChunk, DEFAULT_IGNORE_DIRS, SKIP_EXTENSIONS
from p2.db import ChromaVectorDB

logger = logging.getLogger("p2.watcher")

DEFAULT_INTERVAL_SECONDS = 3.0


class IndexWatcher:
    """Keeps the ChromaDB index in step with the target source tree."""

    def __init__(
        self,
        chunker: AstChunker,
        vector_db: ChromaVectorDB,
        target_dir: str,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ):
        self.chunker = chunker
        self.vector_db = vector_db
        self.target_dir = target_dir
        self.interval_seconds = max(0.5, float(interval_seconds))

        self._mtimes: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_result: Dict[str, int] = {}

    # ------------------------------------------------------------------
    def _scan(self) -> Dict[str, float]:
        """Current mtime of every indexable file under the target tree."""
        snapshot: Dict[str, float] = {}
        root_dir = os.path.abspath(self.target_dir)
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [
                d for d in dirs
                if d not in DEFAULT_IGNORE_DIRS and not d.startswith(".")
            ]
            for filename in files:
                if filename.startswith(".") or filename.endswith(SKIP_EXTENSIONS):
                    continue
                full_path = os.path.join(root, filename)
                try:
                    snapshot[full_path] = os.path.getmtime(full_path)
                except OSError:
                    continue
        return snapshot

    def _diff(self, snapshot: Dict[str, float]) -> Tuple[List[str], List[str]]:
        """(changed_or_new_paths, removed_paths) since the previous scan."""
        changed = [
            path for path, mtime in snapshot.items()
            if self._mtimes.get(path) != mtime
        ]
        removed = [path for path in self._mtimes if path not in snapshot]
        return changed, removed

    # ------------------------------------------------------------------
    def sync_once(self, force_full: bool = False) -> Dict[str, int]:
        """Bring the index up to date. Returns counters for the dashboard."""
        with self._lock:
            snapshot = self._scan()
            changed: List[str]
            removed: List[str]
            if force_full:
                changed, removed = list(snapshot.keys()), []
            else:
                changed, removed = self._diff(snapshot)

            result: Dict[str, int] = {
                "files_changed": len(changed),
                "files_removed": len(removed),
                "added": 0,
                "skipped": 0,
                "failed": 0,
                "pruned": 0,
            }

            if changed:
                chunks: List[CodeChunk] = []
                for path in changed:
                    chunks.extend(self.chunker.chunk_file(path))
                if chunks:
                    stats = self.vector_db.sync_chunks(chunks)
                    for key in ("added", "skipped", "failed", "pruned"):
                        result[key] = int(stats.get(key, 0))

            if removed or force_full:
                known_rel = [
                    os.path.relpath(path, self.chunker.base_dir)
                    for path in snapshot
                ]
                result["files_removed_chunks"] = self.vector_db.prune_missing_files(known_rel)

            self._mtimes = snapshot
            self._last_result = result

        if changed or removed:
            logger.info(
                "Watch mode: %d file(s) changed, %d removed -> "
                "%d chunk(s) embedded, %d unchanged, %d pruned.",
                result["files_changed"], result["files_removed"],
                result["added"], result["skipped"], result["pruned"],
            )
        return result

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Begin polling the target tree in the background."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="AnalystIndexWatcher",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Index watch mode started on '%s' (every %.1fs).",
            self.target_dir, self.interval_seconds,
        )

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sync_once()
            except Exception as e:
                logger.warning(f"Index watch pass failed: {e}")
            self._stop_event.wait(self.interval_seconds)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the watcher thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("Index watch mode stopped.")

    def get_status(self) -> Dict[str, object]:
        """Watcher state for the Analyst tab."""
        with self._lock:
            return {
                "active": bool(self._thread and self._thread.is_alive()),
                "interval_seconds": self.interval_seconds,
                "watched_files": len(self._mtimes),
                "target_dir": self.target_dir,
                "last_pass": dict(self._last_result),
            }
