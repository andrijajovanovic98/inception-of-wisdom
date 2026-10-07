"""Part 3 - Patcher: full-file JSON patches, region rewrites for small models."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from typing import List, Optional, Tuple

from p2.diagnostician import DiagnosticReport
from p3.exits import stops_in_a_new_way
from p3.patcher import CodePatcher

BIG_APP = "\n".join(
    [
        '"""Demo target."""', "import os", "# from flask import Flask, jsonify", "",
        "app = Flask(__name__)", "", "",
    ]
    + [f"def route_{i}():\n    return {i}\n\n" for i in range(12)]
    + ['if __name__ == "__main__":', '    app.run(port=int(os.environ.get("PORT", 5000)))', ""]
)


class ScriptedPatcher(CodePatcher):
    """A patcher whose model answers are scripted; records the prompts it was given."""

    def __init__(self, base_dir: str, answers: List[str]) -> None:
        super().__init__(base_dir=base_dir)
        self.answers = list(answers)
        self.prompts: List[str] = []

    def _call_ollama(
        self, prompt: str, json_mode: bool = True, temperature: float = 0.1
    ) -> Tuple[Optional[str], Optional[str]]:
        self.prompts.append(prompt)
        return self.answers.pop(0), None


def _diag(files=("demo_app/app.py",)) -> DiagnosticReport:
    return DiagnosticReport(success=True, summary="The Flask import is commented out.", files=list(files))


NAME_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/app/app.py", line 5, in <module>\n'
    "    app = Flask(__name__)\n"
    "NameError: name 'Flask' is not defined\n"
)


class PatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="iow-patcher-")
        os.makedirs(os.path.join(self.dir, "demo_app"))
        self._write("demo_app/app.py", BIG_APP)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, rel: str, text: str) -> None:
        with open(os.path.join(self.dir, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def test_large_file_gets_a_region_rewrite_spliced_into_the_full_file(self) -> None:
        fixed_preamble = (
            '"""Demo target."""\nimport os\nfrom flask import Flask, jsonify\n\napp = Flask(__name__)'
        )
        answer = json.dumps({"summary": "restore import", "content": fixed_preamble})
        patcher = ScriptedPatcher(self.dir, [answer])
        patch = patcher.generate_patch(_diag(), NAME_ERROR)
        self.assertTrue(patch.success, patch.error_message)
        self.assertIn("region demo_app/app.py L1-5", patch.strategy or "")
        # Only the region went to the model...
        self.assertNotIn("def route_11", patcher.prompts[0])
        # ...but the patch carries the complete post-change file.
        content = patch.files[0].content
        expected = BIG_APP.replace("# from flask import Flask, jsonify", "from flask import Flask, jsonify")
        self.assertEqual(content, expected)

    def test_region_answer_as_a_python_code_block(self) -> None:
        """What the model is actually asked for: SUMMARY line + fenced code."""
        fixed = '"""Demo target."""\nimport os\nfrom flask import Flask, jsonify\n\napp = Flask(__name__)\n'
        answer = "SUMMARY: Restored the Flask import.\n```python\n" + fixed + "```\n"
        patch = ScriptedPatcher(self.dir, [answer]).generate_patch(_diag(), NAME_ERROR)
        self.assertTrue(patch.success, patch.error_message)
        self.assertEqual(patch.summary, "Restored the Flask import.")
        self.assertIn("from flask import Flask, jsonify\n\napp = Flask", patch.files[0].content)
        self.assertIn("def route_11", patch.files[0].content)

    def test_a_region_handed_back_unchanged_is_asked_for_once_more(self) -> None:
        """Seen live: two attempts in a row spent on the model returning the code as it was."""
        unchanged = (
            '"""Demo target."""\nimport os\n# from flask import Flask, jsonify\n\napp = Flask(__name__)\n'
        )
        fixed = unchanged.replace("# from flask", "from flask")
        patcher = ScriptedPatcher(self.dir, [
            "SUMMARY: nothing\n```python\n" + unchanged + "```\n",
            "SUMMARY: Restored the import.\n```python\n" + fixed + "```\n",
        ])
        patch = patcher.generate_patch(_diag(), NAME_ERROR)
        self.assertEqual(len(patcher.prompts), 2)
        self.assertIn("IDENTICAL TO THE REGION", patcher.prompts[1])
        self.assertIn("from flask import Flask, jsonify\n\napp", patch.files[0].content)
        # Unchanged every time: handed on as it is - the sanity gate refuses a no-op.
        stubborn = ScriptedPatcher(self.dir, ["```python\n" + unchanged + "```\n"] * 3)
        again = stubborn.generate_patch(_diag(), NAME_ERROR)
        self.assertEqual(len(stubborn.prompts), 3)
        self.assertEqual(again.files[0].content, BIG_APP if BIG_APP.endswith("\n") else BIG_APP + "\n")

    def test_a_fix_that_only_moves_the_crash_is_asked_for_again(self) -> None:
        """Seen live on `make break-env`: 4 of 6 rewrites of the startup block added exit(1) or
        raise ValueError, or started the server only when the variable was set."""
        app = BIG_APP.replace('int(os.environ.get("PORT", 5000))', 'int(os.environ["IOW_DEMO_PORT"])')
        self._write("demo_app/app.py", app)
        log = (
            "Traceback (most recent call last):\n"
            f'  File "/app/app.py", line {len(app.splitlines())}, in <module>\n'
            "KeyError: 'IOW_DEMO_PORT'\n"
        )
        main = 'if __name__ == "__main__":\n'
        exits = main + '    if "IOW_DEMO_PORT" not in os.environ:\n        exit(1)\n' \
            '    app.run(port=int(os.environ["IOW_DEMO_PORT"]))\n'
        guarded = main + '    if "IOW_DEMO_PORT" in os.environ:\n' \
            '        app.run(port=int(os.environ["IOW_DEMO_PORT"]))\n'
        fixed = main + '    app.run(port=int(os.environ.get("IOW_DEMO_PORT", 5000)))\n'
        answers = [f"SUMMARY: x\n```python\n{body}```\n" for body in (exits, guarded, fixed)]
        patcher = ScriptedPatcher(self.dir, answers)
        patch = patcher.generate_patch(_diag(), log)
        self.assertTrue(patch.success, patch.error_message)
        self.assertEqual(len(patcher.prompts), 3)
        self.assertIn("adds exit()", patcher.prompts[1])
        self.assertIn("app.run(...) no longer runs every time", patcher.prompts[2])
        self.assertIn('os.environ.get("IOW_DEMO_PORT", 5000)', patch.files[0].content)
        # Still stopping after every ask: refused before it costs a redeploy.
        stubborn = ScriptedPatcher(self.dir, [answers[0]] * 3)
        refused = stubborn.generate_patch(_diag(), log)
        self.assertFalse(refused.success)
        self.assertIn("does not fix the crash", refused.error_message or "")

    def test_a_fix_that_reads_a_value_it_may_not_set_is_asked_for_again(self) -> None:
        """J7: one rewrite of the break-env startup block set `port` only when the variable
        was there - a NameError that only verification found, after a redeploy."""
        app = BIG_APP.replace('int(os.environ.get("PORT", 5000))', 'int(os.environ["IOW_DEMO_PORT"])')
        self._write("demo_app/app.py", app)
        log = (
            "Traceback (most recent call last):\n"
            f'  File "/app/app.py", line {len(app.splitlines())}, in <module>\n'
            "KeyError: 'IOW_DEMO_PORT'\n"
        )
        main = 'if __name__ == "__main__":\n'
        only_if = main + '    if "IOW_DEMO_PORT" in os.environ:\n' \
            '        port = int(os.environ["IOW_DEMO_PORT"])\n    app.run(port=port)\n'
        fixed = main + '    app.run(port=int(os.environ.get("IOW_DEMO_PORT", 5000)))\n'
        answers = [f"SUMMARY: x\n```python\n{body}```\n" for body in (only_if, fixed)]
        patcher = ScriptedPatcher(self.dir, answers)
        patch = patcher.generate_patch(_diag(), log)
        self.assertTrue(patch.success, patch.error_message)
        self.assertEqual(len(patcher.prompts), 2)
        self.assertIn("WOULD FAIL WITH A NameError", patcher.prompts[1])
        self.assertIn("reads port on line", patcher.prompts[1])
        self.assertIn('os.environ.get("IOW_DEMO_PORT", 5000)', patch.files[0].content)

    def test_full_file_strategy_has_the_model_write_the_whole_json_patch(self) -> None:
        """analyst.patch_strategy: full_file (J1) - no region, even in a large file."""
        full = BIG_APP.replace("# from flask import Flask, jsonify", "from flask import Flask, jsonify")
        answer = {"summary": "restore the import",
                  "files": [{"path": "demo_app/app.py", "op": "modify", "content": full}]}
        patcher = ScriptedPatcher(self.dir, [json.dumps(answer)])
        patcher.patch_strategy = "full_file"
        patch = patcher.generate_patch(_diag(), NAME_ERROR)
        self.assertTrue(patch.success, patch.error_message)
        self.assertEqual(patch.strategy, "full-file")
        self.assertIn("CURRENT SOURCE CODE", patcher.prompts[0])
        self.assertIn("def route_11", patcher.prompts[0])
        self.assertEqual(patch.files[0].content, full)

    def test_a_full_file_fix_that_raises_is_retried_then_refused(self) -> None:
        self._write("demo_app/app.py", "import os\nport = int(os.environ['P'])\nprint(port)\n")
        stopping = "import os\nif 'P' not in os.environ:\n    raise SystemExit(1)\n" \
            "port = int(os.environ['P'])\nprint(port)\n"
        answer = {"summary": "x", "files": [{"path": "demo_app/app.py", "op": "modify", "content": stopping}]}
        patcher = ScriptedPatcher(self.dir, [json.dumps(answer)] * 2)
        patch = patcher.generate_patch(_diag(), "KeyError: 'P'\n")
        self.assertFalse(patch.success)
        self.assertEqual(len(patcher.prompts), 2)
        self.assertIn("adds raise", patcher.prompts[1])
        self.assertIn("does not fix the crash", patch.error_message or "")

    def test_the_retry_of_a_cut_off_file_keeps_the_goal_and_the_facts(self) -> None:
        """Q3: the stricter retry rebuilt its prompt without them."""
        small = "import os\n" + "".join(f"X{i} = {i}\n" for i in range(12)) + "PORT = int(os.environ['P'])\n"
        self._write("demo_app/app.py", small)
        cut = {"summary": "x",
               "files": [{"path": "demo_app/app.py", "op": "modify", "content": "import os\n"}]}
        whole = {"summary": "x", "files": [{"path": "demo_app/app.py", "op": "modify",
                                            "content": small.replace("['P']", ".get('P', 5000)")}]}
        patcher = ScriptedPatcher(self.dir, [json.dumps(cut), json.dumps(whole)])
        facts = "\nFACT (read from the target's container): the service is reached on port 5000.\n"
        patch = patcher.generate_patch(_diag(), "KeyError: 'P'\n", goal="the service must start", facts=facts)
        self.assertTrue(patch.success, patch.error_message)
        self.assertIn("cut off", patcher.prompts[1])
        for prompt in patcher.prompts:
            self.assertIn("the service must start", prompt)
            self.assertIn("reached on port 5000", prompt)

    def test_echoed_fence_lines_are_stripped_from_the_region(self) -> None:
        fixed = (
            '# BEGIN REGION ---\n"""Demo target."""\nimport os\nfrom flask import Flask, jsonify\n\n'
            "app = Flask(__name__)\n# END REGION ---\n"
        )
        answer = "SUMMARY: fix\n```python\n" + fixed + "```\n"
        patch = ScriptedPatcher(self.dir, [answer]).generate_patch(_diag(), NAME_ERROR)
        self.assertTrue(patch.success, patch.error_message)
        self.assertNotIn("REGION", patch.files[0].content)

    def test_cut_off_region_is_rejected(self) -> None:
        self._write("demo_app/app.py", BIG_APP.replace(
            "def route_3():\n    return 3\n",
            "def route_3():\n" + "".join(f"    x{i} = {i}\n" for i in range(10)) + "    return 3\n",
        ))
        # Not a NameError: an unbound name would send the model to the imports instead.
        log = '  File "/app/app.py", line 30, in route_3\n    x5 = 5\nValueError: boom\n'
        answer = json.dumps({"summary": "x", "content": "def route_3():\n    return 3"})
        patcher = ScriptedPatcher(self.dir, [answer])
        patch = patcher.generate_patch(_diag(), log)
        self.assertFalse(patch.success)
        self.assertIn("cut off", patch.error_message or "")

    def test_syntax_error_rewrites_a_window_around_the_line(self) -> None:
        self._write("demo_app/app.py", BIG_APP.replace("def route_5():", "def route_5(:"))
        broken_line = [
            i + 1 for i, line in enumerate(BIG_APP.splitlines()) if line.startswith("def route_5")
        ][0]
        log = f'  File "/app/app.py", line {broken_line}\n    def route_5(:\nSyntaxError: invalid syntax\n'
        patcher = ScriptedPatcher(self.dir, [json.dumps({"summary": "fix", "content": "x = 1"})])
        patcher.generate_patch(_diag(), log)
        self.assertIn(f"lines {broken_line - 8}-{broken_line + 8}", patcher.prompts[0])

    def test_the_target_is_described_only_when_the_fix_is_about_its_environment(self) -> None:
        """Seen live: a correct fallback chose port 8080 for a service probed on 5000."""
        facts = "\nFACT (read from the target's container): the service is reached on port 5000.\n"
        app = BIG_APP.replace('int(os.environ.get("PORT", 5000))', 'int(os.environ["IOW_DEMO_PORT"])')
        self._write("demo_app/app.py", app)
        line = len(app.splitlines())
        log = (
            "Traceback (most recent call last):\n"
            f'  File "/app/app.py", line {line}, in <module>\n'
            "KeyError: 'IOW_DEMO_PORT'\n"
        )
        fixed = '    app.run(port=int(os.environ.get("IOW_DEMO_PORT", 5000)))'
        answer = f"SUMMARY: fallback\n```python\nif __name__ == \"__main__\":\n{fixed}\n```"
        env = ScriptedPatcher(self.dir, [answer])
        env.generate_patch(_diag(), log, facts=facts)
        self.assertIn("reached on port 5000", env.prompts[0])
        imports = ScriptedPatcher(self.dir, [json.dumps({"summary": "x", "content": "import os"})])
        imports.generate_patch(_diag(), NAME_ERROR, facts=facts)       # an import error: not shown
        self.assertNotIn("reached on port", imports.prompts[0])

    def test_a_name_the_file_never_binds_is_fixed_where_the_imports_are(self) -> None:
        """Seen live: NameError for jsonify inside a route - the fix is the import line."""
        app = BIG_APP.replace("# from flask import Flask, jsonify", "from flask import Flask")
        app = app.replace("def route_7():\n    return 7", "def route_7():\n    return jsonify(7)")
        self._write("demo_app/app.py", app)
        line = [i + 1 for i, text in enumerate(app.splitlines()) if "jsonify(7)" in text][0]
        log = (
            "Traceback (most recent call last):\n"
            f'  File "/app/app.py", line {line}, in route_7\n'
            "    return jsonify(7)\n"
            "NameError: name 'jsonify' is not defined\n"
        )
        preamble = '"""Demo target."""\nimport os\nfrom flask import Flask, jsonify\n\napp = Flask(__name__)'
        patcher = ScriptedPatcher(self.dir, [json.dumps({"summary": "import it", "content": preamble})])
        patch = patcher.generate_patch(_diag(), log)
        self.assertTrue(patch.success, patch.error_message)
        self.assertIn("L1-5 (imports, 'jsonify' is never defined)", patch.strategy or "")
        self.assertNotIn("def route_7", patcher.prompts[0])
        self.assertEqual(patch.files[0].content,
                         app.replace("from flask import Flask\n", "from flask import Flask, jsonify\n"))

    def test_a_bound_name_keeps_the_chunk_the_traceback_points_at(self) -> None:
        app = BIG_APP.replace("# from flask import Flask, jsonify", "from flask import Flask")
        app = app.replace("def route_7():\n    return 7", "def route_7():\n    return route_3()")
        self._write("demo_app/app.py", app)
        line = [i + 1 for i, text in enumerate(app.splitlines()) if "return route_3()" in text][0]
        log = f'  File "/app/app.py", line {line}, in route_7\nNameError: name \'route_3\' is not defined\n'
        patcher = ScriptedPatcher(self.dir, [json.dumps({"summary": "x", "content": "x"})])
        patcher.generate_patch(_diag(), log)
        self.assertIn("def route_7", patcher.prompts[0])

    def test_the_model_is_told_every_name_the_file_never_defines(self) -> None:
        """Seen live: the traceback names only Flask; jsonify cost a second redeploy."""
        patcher = ScriptedPatcher(self.dir, [json.dumps({"summary": "x", "content": "x"})])
        patcher.generate_patch(_diag(), NAME_ERROR)
        self.assertIn("uses Flask but never imports or defines it", patcher.prompts[0])

    def test_small_file_is_rewritten_in_full(self) -> None:
        self._write("demo_app/app.py", "import os\nprint(os.getcwd())\n")
        answer = {
            "summary": "fix",
            "files": [{"path": "demo_app/app.py", "op": "modify", "content": "import os\n"}],
        }
        patcher = ScriptedPatcher(self.dir, [json.dumps(answer)])
        patch = patcher.generate_patch(_diag(), NAME_ERROR)
        self.assertTrue(patch.success)
        self.assertEqual(patch.strategy, "full-file")
        self.assertIn("CURRENT SOURCE CODE", patcher.prompts[0])

    def test_diffs_are_rejected(self) -> None:
        self._write("demo_app/app.py", "import os\nprint(1)\n")
        diff = "--- a/demo_app/app.py\n+++ b/demo_app/app.py\n@@ -1,2 +1,2 @@\n-print(1)\n+print(2)\n"
        answer = {
            "summary": "x",
            "files": [{"path": "demo_app/app.py", "op": "modify", "content": diff}],
        }
        patch = ScriptedPatcher(self.dir, [json.dumps(answer)]).generate_patch(_diag(), NAME_ERROR)
        self.assertFalse(patch.success)
        self.assertIn("Diff", patch.error_message or "")

    def test_model_path_is_mapped_onto_the_suspect_file(self) -> None:
        self._write("demo_app/app.py", "import os\nprint(1)\n")
        answer = {
            "summary": "x",
            "files": [{"path": "app.py", "op": "MODIFY", "content": "import os\nprint(2)\n"}],
        }
        patch = ScriptedPatcher(self.dir, [json.dumps(answer)]).generate_patch(_diag(), NAME_ERROR)
        self.assertEqual((patch.files[0].path, patch.files[0].op), ("demo_app/app.py", "modify"))


class StopsInANewWayTests(unittest.TestCase):
    MAIN = 'import os\nif __name__ == "__main__":\n    log("start")\n    app.run(port=PORT)\n'

    def test_a_fix_that_keeps_the_program_running_passes(self) -> None:
        guarded = self.MAIN.replace("    app.run", "    try:\n        app.run").replace(
            "(port=PORT)\n", "(port=PORT)\n    except OSError:\n        log('busy')\n")
        self.assertIsNone(stops_in_a_new_way(self.MAIN, guarded))     # try still always runs
        quiet = self.MAIN.replace('    log("start")\n', '    if DEBUG:\n        log("start")\n')
        self.assertIsNone(stops_in_a_new_way(self.MAIN, quiet))       # only the last call counts
        before = "def f():\n    raise RuntimeError('x')\n"
        self.assertIsNone(stops_in_a_new_way(before, "def f():\n    return 1\n"))
        self.assertIsNone(stops_in_a_new_way("def f(:\n", "def f(): raise X\n"))  # syntax check's job

    def test_a_new_stop_or_a_service_that_may_not_start_is_reported(self) -> None:
        self.assertIn("adds sys.exit()", stops_in_a_new_way(
            self.MAIN, self.MAIN.replace('    log("start")\n', '    sys.exit(1)\n')) or "")
        self.assertIn("adds raise", stops_in_a_new_way(
            self.MAIN, self.MAIN.replace('    log("start")\n', '    raise ValueError("no")\n')) or "")
        gone = "import os\nlog('start')\n"
        self.assertIn("app.run(...) no longer runs", stops_in_a_new_way(self.MAIN, gone) or "")


if __name__ == "__main__":
    unittest.main()
