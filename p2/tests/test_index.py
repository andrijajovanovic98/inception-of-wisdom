"""Part 2 - persistent ChromaDB index: incremental, never wiped, line ranges stay true.

Uses a real PersistentClient in a temp dir with a tiny deterministic embedder, so it
runs offline and fast.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from typing import List
from unittest import mock

from p2.tests import APP_PY
from p2.chunker import AstChunker
from p2.db import ChromaVectorDB
from p2.retriever import CodeRetriever
from p2.watcher import IndexWatcher


class _Vec(list):
    def tolist(self) -> list:
        return list(self)


class FakeEmbedder:
    """Deterministic 16-d bag-of-words vectors; counts how many texts it embedded."""

    def __init__(self) -> None:
        self.embedded = 0

    def encode(self, texts: List[str], normalize_embeddings: bool = True) -> _Vec:
        self.embedded += len(texts)
        out = _Vec()
        for text in texts:
            vec = [0.0] * 16
            for word in text.lower().split():
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 16] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="iow-index-")
        self.db_dir = os.path.join(self.root, "chroma")
        os.makedirs(os.path.join(self.root, "demo_app"))
        self.app = os.path.join(self.root, "demo_app", "app.py")
        with open(self.app, "w", encoding="utf-8") as f:
            f.write(APP_PY)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _open(self):
        db = ChromaVectorDB(persist_dir=self.db_dir)
        db._embedder = FakeEmbedder()
        chunker = AstChunker(base_dir=self.root)
        watcher = IndexWatcher(chunker, db, os.path.join(self.root, "demo_app"), interval_seconds=0.5)
        return db, watcher

    def test_restart_never_pays_the_indexing_cost_again(self) -> None:
        db, watcher = self._open()
        watcher.sync_once(force_full=True)
        first = db.count()
        self.assertGreater(first, 0)

        db2, watcher2 = self._open()                # a restarted agent
        stats = watcher2.sync_once(force_full=True)
        self.assertEqual(db2.count(), first)        # persisted, not wiped
        self.assertEqual(stats["added"], 0)
        self.assertEqual(db2._embedder.embedded, 0)

    def test_moved_code_keeps_true_line_ranges_without_reembedding(self) -> None:
        db, watcher = self._open()
        watcher.sync_once(force_full=True)
        with open(self.app, "w", encoding="utf-8") as f:
            f.write(APP_PY.replace(
                "app = Flask(__name__)\n", "app = Flask(__name__)\nMAX_BODY = 1024\n\n\n"))
        db._embedder.embedded = 0
        watcher.sync_once()
        main = db._collection.get(ids=["demo_app/app.py:module:__main__"], include=["metadatas"])
        start = main["metadatas"][0]["start_line"]
        with open(self.app, encoding="utf-8") as f:
            actual = [i + 1 for i, line in enumerate(f) if line.startswith('if __name__')][0]
        self.assertEqual(start, actual)
        # Only the edited preamble was re-embedded; the moved chunks were not.
        self.assertEqual(db._embedder.embedded, 1)

    def test_removed_symbol_is_pruned(self) -> None:
        db, watcher = self._open()
        watcher.sync_once(force_full=True)
        with open(self.app, "w", encoding="utf-8") as f:
            f.write(APP_PY.replace('def index():\n    return "ok"\n', ""))
        watcher.sync_once()
        ids = db._collection.get(include=[])["ids"]
        self.assertNotIn("demo_app/app.py:function:index", ids)

    def test_an_emptied_file_keeps_no_chunks(self) -> None:
        """A9: its chunks used to survive and be served back to the Analyst."""
        db, watcher = self._open()
        watcher.sync_once(force_full=True)
        with open(self.app, "w", encoding="utf-8") as f:
            f.write("")
        watcher.sync_once()
        self.assertEqual(db._collection.get(where={"file_path": "demo_app/app.py"}, include=[])["ids"], [])

    def test_the_embedding_model_is_loaded_from_the_local_cache_first(self) -> None:
        """A2: the agent downloaded a second copy into a directory nobody prefetched."""
        from p2 import db as db_module
        answers = [OSError("not cached"), "model"]
        with mock.patch.object(db_module, "SentenceTransformer", side_effect=answers) as st:
            self.assertEqual(ChromaVectorDB(persist_dir=self.db_dir)._load_embedder(), "model")
        self.assertEqual(st.call_args_list[0].kwargs, {"local_files_only": True})
        self.assertEqual(st.call_args_list[1].kwargs, {})              # the one-time download

    def test_traceback_line_is_looked_up_in_the_index(self) -> None:
        db, watcher = self._open()
        watcher.sync_once(force_full=True)
        with open(self.app, encoding="utf-8") as f:
            main_line = [i + 1 for i, line in enumerate(f) if "app.run" in line][0]
        log = (
            "Traceback (most recent call last):\n"
            f'  File "/app/app.py", line {main_line}, in <module>\n'
            "ValueError: invalid literal for int() with base 10: 'abc'\n"
        )
        chunks, _ = CodeRetriever(db, default_top_k=2).retrieve_for_crash(log)
        self.assertEqual(chunks[0]["symbol_name"], "__main__")


if __name__ == "__main__":
    unittest.main()
