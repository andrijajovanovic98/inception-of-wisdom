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

    def test_a_file_that_failed_to_index_is_tried_again(self) -> None:
        """A12: the watcher remembered a file's mtime even when its indexing failed (the store
        busy, the embedder not ready), so the file looked unchanged and was never looked at
        again: the Analyst went on retrieving the code from before the patch."""
        db, watcher = self._open()
        real, calls = db.sync_chunks, []

        def flaky(chunks):
            calls.append(len(chunks))
            if len(calls) == 1:
                return {"added": 0, "skipped": 0, "failed": len(chunks), "pruned": 0}
            return real(chunks)

        db.sync_chunks = flaky                                   # type: ignore[method-assign]
        first = watcher.sync_once()
        self.assertEqual((first["failed"] > 0, db.count()), (True, 0))
        second = watcher.sync_once()                             # nothing changed on disk
        self.assertEqual(second["files_changed"], 1)
        self.assertGreater(db.count(), 0)
        self.assertEqual(watcher.sync_once()["files_changed"], 0)   # indexed now: done

    def test_a_file_that_cannot_be_read_does_not_block_the_index(self) -> None:
        """A13: a null byte makes ast.parse raise ValueError, not SyntaxError. The chunker let
        it out, so every watch pass failed on that file and none of the other changes reached
        the index (the startup sync too)."""
        with open(os.path.join(self.root, "demo_app", "blob.py"), "wb") as f:
            f.write(b"\xff\xfe\x00bad\x80\x81")
        chunker = AstChunker(base_dir=self.root)
        self.assertEqual(chunker.chunk_file(os.path.join(self.root, "demo_app", "blob.py")), [])
        db, watcher = self._open()

        class Unreadable(AstChunker):                            # whatever else a file can do to it
            def chunk_file(self, full_path: str):
                if full_path.endswith("odd.py"):
                    raise OSError("boom")
                return super().chunk_file(full_path)

        with open(os.path.join(self.root, "demo_app", "odd.py"), "w", encoding="utf-8") as odd:
            odd.write("def odd():\n    return 1\n")
        watcher.chunker = Unreadable(base_dir=self.root)
        stats = watcher.sync_once(force_full=True)
        self.assertGreater(stats["added"], 0)                    # app.py got in
        self.assertEqual({c["file_path"] for c in db.query_similar("index", top_k=10)},
                         {"demo_app/app.py"})

    def test_retrieval_returns_exactly_top_k_with_scores(self) -> None:
        """T4: subject V.2, "the top-k chunks ... with their similarity score"."""
        db, watcher = self._open()
        watcher.sync_once(force_full=True)
        retriever = CodeRetriever(db, default_top_k=2)
        self.assertEqual(len(retriever.retrieve_by_query("start the server")), 2)    # the default
        chunks = retriever.retrieve_by_query("start the server", top_k=1)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(0.0 <= chunks[0]["similarity"] <= 1.0)

    def test_the_line_a_traceback_names_outranks_a_closer_chunk(self) -> None:
        """T4 for A4: the traceback's own line wins over a chunk that merely reads similar."""
        db, watcher = self._open()
        watcher.sync_once(force_full=True)
        retriever = CodeRetriever(db, default_top_k=1)
        message = 'ValueError: app.run(port=int(os.environ.get("PORT", 5000)))'
        by_similarity = retriever.retrieve_by_query(message, top_k=3)
        self.assertEqual(by_similarity[-1]["symbol_name"], "index")
        log = f'Traceback (most recent call last):\n  File "/app/app.py", line 9, in <lambda>\n{message}'
        chunks, _ = retriever.retrieve_for_crash(log)
        self.assertEqual(chunks[0]["symbol_name"], "index")      # lines 8-9 of the fixture

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
