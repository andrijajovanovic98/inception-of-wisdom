"""Part 2 - Analyst: logical chunks cover the file, diagnosis never guesses."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from p2.tests import APP_PY
from p2.chunker import AstChunker
from p2.diagnostician import CrashDiagnostician
from p2.retriever import CodeRetriever

VALID = {"demo_app/app.py", "demo_app/config.py"}


class _NoDb:
    """The parser never touches the index."""


def _parse(raw):
    diag = CrashDiagnostician(retriever=CodeRetriever(_NoDb()))  # type: ignore[arg-type]
    return diag._parse_and_validate_response(raw, set(VALID), [], 0.0)


class ChunkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="iow-chunk-")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _chunks(self, source: str):
        path = os.path.join(self.dir, "app.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        return AstChunker(self.dir).chunk_file(path)

    def test_every_code_line_is_in_some_chunk(self) -> None:
        chunks = self._chunks(APP_PY)
        covered: set = set()
        for chunk in chunks:
            covered.update(range(chunk.start_line, chunk.end_line + 1))
        code_lines = [
            i + 1 for i, line in enumerate(APP_PY.splitlines())
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual([n for n in code_lines if n not in covered], [])

    def test_main_block_is_its_own_logical_chunk(self) -> None:
        names = {c.symbol_name: c for c in self._chunks(APP_PY)}
        self.assertIn("__main__", names)
        self.assertIn('int(os.environ.get("PORT", 5000))', names["__main__"].content)
        self.assertIn("module_preamble", names)
        self.assertIn("index", names)

    def test_ids_are_stable_across_edits(self) -> None:
        before = {c.chunk_id for c in self._chunks(APP_PY)}
        after = {c.chunk_id for c in self._chunks(APP_PY.replace('"ok"', '"changed"'))}
        self.assertEqual(before, after)

    def test_syntax_error_falls_back_to_one_chunk(self) -> None:
        chunks = self._chunks("def broken(:\n    pass\n")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].symbol_type, "file")


class DiagnosisParsingTests(unittest.TestCase):
    def test_valid_answer(self) -> None:
        report = _parse(json.dumps({"summary": "Flask import removed.", "files": ["demo_app/app.py"]}))
        self.assertTrue(report.success)
        self.assertEqual(report.files, ["demo_app/app.py"])

    def test_basename_resolves_to_the_indexed_path(self) -> None:
        report = _parse(json.dumps({"summary": "x", "files": ["config.py"]}))
        self.assertEqual(report.files, ["demo_app/config.py"])

    def test_invented_path_is_dropped_and_fails_explicitly(self) -> None:
        report = _parse(json.dumps({"summary": "x", "files": ["src/made_up.py"]}))
        self.assertFalse(report.success)
        self.assertEqual(report.mode, "unusable")

    def test_null_summary_is_an_explicit_failure_not_an_exception(self) -> None:
        report = _parse(json.dumps({"summary": None, "files": ["demo_app/app.py"]}))
        self.assertFalse(report.success)

    def test_non_object_json_is_an_explicit_failure(self) -> None:
        report = _parse(json.dumps(["demo_app/app.py"]))
        self.assertFalse(report.success)
        self.assertEqual(report.mode, "unusable")

    def test_non_string_file_entries_are_ignored(self) -> None:
        report = _parse(json.dumps({"summary": "x", "files": [{"p": 1}, 3, "demo_app/app.py"]}))
        self.assertEqual(report.files, ["demo_app/app.py"])

    def test_an_answer_cut_off_after_a_complete_files_list_is_salvaged(self) -> None:
        """Seen live: 'Expecting ',' delimiter ... char 324' - the token budget ran out."""
        cut = '{"files": ["demo_app/app.py"], "summary": "The jsonify import is missing, so every route'
        report = _parse(cut)
        self.assertTrue(report.success)
        self.assertEqual(report.files, ["demo_app/app.py"])
        self.assertTrue(report.summary.startswith("The jsonify import is missing"))

    def test_a_cut_off_files_list_is_not_guessed(self) -> None:
        self.assertFalse(_parse('{"summary": "x", "files": ["demo_app/app.py", "demo_ap').success)
        self.assertFalse(_parse('{"files": ["src/invented.py"], "summary": "cut her').success)

    def test_invalid_json_and_empty_reply(self) -> None:
        self.assertFalse(_parse("{not json").success)
        self.assertFalse(_parse("").success)


class _OneChunkRetriever:
    def retrieve_for_crash(self, log, top_k=3):
        chunk = {
            "chunk_id": "demo_app/app.py:module:__main__", "file_path": "demo_app/app.py",
            "symbol_name": "__main__", "symbol_type": "module",
            "start_line": 91, "end_line": 94, "content": 'port = int(os.environ["IOW_DEMO_PORT"])',
        }
        return [chunk], {}


class ScriptedDiagnostician(CrashDiagnostician):
    def __init__(self, answers):
        super().__init__(retriever=_OneChunkRetriever())  # type: ignore[arg-type]
        self.answers = list(answers)
        self.prompts: list = []

    def _call_ollama(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0), None


class DiagnosisRetryTests(unittest.TestCase):
    def test_a_quote_that_cut_the_json_short_gets_one_more_ask(self) -> None:
        """Seen live: '..."summary": "... os.environ["}' - no files, grammar closed early."""
        diag = ScriptedDiagnostician([
            '{"summary": "The crash is due to a missing variable read with os.environ["}',
            json.dumps({"files": ["demo_app/app.py"], "summary": "IOW_DEMO_PORT is not set."}),
        ])
        report = diag.diagnose_crash("KeyError: 'IOW_DEMO_PORT'")
        self.assertTrue(report.success)
        self.assertEqual(report.files, ["demo_app/app.py"])
        self.assertEqual(len(diag.prompts), 2)
        self.assertIn("PREVIOUS ANSWER WAS UNUSABLE", diag.prompts[1])

    def test_synthetic_chunk_names_are_not_shown_as_identifiers(self) -> None:
        diag = ScriptedDiagnostician([json.dumps({"files": ["demo_app/app.py"], "summary": "x"})])
        diag.diagnose_crash("KeyError: 'IOW_DEMO_PORT'")
        self.assertNotIn("__main__ (module)", diag.prompts[0])
        self.assertIn('the if __name__ == "__main__": block', diag.prompts[0])

    def test_files_the_caller_cannot_patch_are_never_offered(self) -> None:
        """Seen live: the model blamed the operator-owned iow.config.yml twice."""
        class WithConfig(_OneChunkRetriever):
            def retrieve_for_crash(self, log, top_k=3):
                chunks, info = super().retrieve_for_crash(log, top_k)
                config = dict(chunks[0], chunk_id="demo_app/iow.config.yml:file:iow.config.yml",
                              file_path="demo_app/iow.config.yml", symbol_type="file")
                return [config] + chunks, info

        diag = ScriptedDiagnostician([json.dumps({"files": ["demo_app/app.py"], "summary": "x"})])
        diag.retriever = WithConfig()  # type: ignore[assignment]
        report = diag.diagnose_crash("KeyError: 'IOW_DEMO_PORT'", exclude_files=["demo_app/iow.config.yml"])
        self.assertTrue(report.success)
        self.assertNotIn("iow.config.yml", diag.prompts[0])

    def test_an_unreachable_model_is_not_retried(self) -> None:
        class Down(ScriptedDiagnostician):
            def _call_ollama(self, prompt):
                self.prompts.append(prompt)
                return None, "connection refused"

        diag = Down([])
        report = diag.diagnose_crash("KeyError: 'IOW_DEMO_PORT'")
        self.assertEqual(report.mode, "unavailable")
        self.assertEqual(len(diag.prompts), 1)


class TracebackExtractionTests(unittest.TestCase):
    def test_frames_functions_and_exception(self) -> None:
        log = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3.11/site-packages/flask/app.py", line 880, in full_dispatch_request\n'
            '  File "/app/app.py", line 78, in trigger_crash\n'
            "RuntimeError: Intentional target service crash\n"
        )
        info = CodeRetriever(_NoDb()).extract_traceback_symbols(log)  # type: ignore[arg-type]
        self.assertIn(("app.py", 78), info["frames"])
        self.assertIn("trigger_crash", info["functions"])
        self.assertEqual(info["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
