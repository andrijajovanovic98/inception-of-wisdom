"""Part 3 - Patcher: full-file JSON patches, region rewrites for small models."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from typing import List, Optional, Tuple

from p2.diagnostician import DiagnosticReport
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
        # Unchanged twice: handed on as it is - the sanity gate refuses a no-op.
        stubborn = ScriptedPatcher(self.dir, ["```python\n" + unchanged + "```\n"] * 2)
        again = stubborn.generate_patch(_diag(), NAME_ERROR)
        self.assertEqual(len(stubborn.prompts), 2)
        self.assertEqual(again.files[0].content, BIG_APP if BIG_APP.endswith("\n") else BIG_APP + "\n")

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
        self.assertIsNone(patch.strategy)
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


if __name__ == "__main__":
    unittest.main()
