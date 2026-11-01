"""Part 3 - pre-disk sanity gate."""

from __future__ import annotations

import re
import unittest

from p3.tests.helpers import TempRepo
from p3.git_manager import GitManager
from p3.patcher import FilePatch, StructuredPatch
from p3.sanity import SanityChecker


def _patch(*files: FilePatch) -> StructuredPatch:
    return StructuredPatch(summary="fix", files=list(files))


class SanityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.original = self.repo.read("demo_app/app.py")
        gm = GitManager(repo_dir=self.repo.path)
        self.gate = SanityChecker(base_dir=self.repo.path, path_guard=gm._is_heal_path)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def assertRefused(self, patch: StructuredPatch, fragment: str) -> None:
        result = self.gate.check_patch(patch)
        self.assertFalse(result.passed, "patch should have been refused")
        self.assertTrue(
            any(fragment in reason for reason in result.reasons),
            f"no reason mentions {fragment!r}: {result.reasons}",
        )

    def test_minimal_fix_passes(self) -> None:
        fixed = self.original.replace('"ok"', '"fixed"')
        self.assertTrue(self.gate.check_patch(_patch(FilePatch("demo_app/app.py", "modify", fixed))).passed)

    def test_more_than_three_files(self) -> None:
        files = [FilePatch(f"demo_app/f{i}.py", "create", "x = 1\n") for i in range(4)]
        self.assertRefused(_patch(*files), "exceeds the limit of 3")

    def test_exactly_three_files_pass(self) -> None:
        """T4: the subject refuses a patch that touches *more than* three files. A mutation run
        refused three as well, and every suite stayed green."""
        files = [FilePatch(f"demo_app/f{i}.py", "create", "x = 1\n") for i in range(3)]
        result = self.gate.check_patch(_patch(*files))
        self.assertTrue(result.passed, result.reasons)

    def test_a_patch_without_files_is_refused(self) -> None:
        """T4: nothing to apply is not a fix; the gate says so itself instead of passing it on."""
        result = self.gate.check_patch(_patch())
        self.assertFalse(result.passed)
        self.assertIn("zero files", " ".join(result.reasons))

    def test_shrink_over_sixty_percent(self) -> None:
        self.assertRefused(
            _patch(FilePatch("demo_app/app.py", "modify", self.original[:60])), "shrinkage"
        )

    def test_a_shrink_of_exactly_sixty_percent_passes(self) -> None:
        """T4: "shrinks a file by more than 60 %" - 60 % itself is allowed, 61 % is not."""
        self.repo.write("demo_app/data.txt", "a" * 99 + "\n")                      # 100 bytes
        exact = self.gate.check_patch(_patch(FilePatch("demo_app/data.txt", "modify", "b" * 39 + "\n")))
        self.assertTrue(exact.passed, exact.reasons)                               # 40 bytes: 60.0 %
        self.assertRefused(_patch(FilePatch("demo_app/data.txt", "modify", "b" * 38 + "\n")), "shrinkage")

    def test_a_small_file_may_not_shrink_by_more_than_sixty_percent_either(self) -> None:
        # run.sh is 19 bytes: the subject's 60 % rule names no size floor (80 B was exempt).
        self.assertRefused(_patch(FilePatch("demo_app/run.sh", "modify", "echo\n")), "shrinkage")

    def test_create_cannot_dodge_the_shrink_rule(self) -> None:
        self.assertRefused(
            _patch(FilePatch("demo_app/app.py", "create", "import os\nx = 1\n")), "shrinkage"
        )

    def test_deleting_a_non_empty_file_is_a_full_shrink(self) -> None:
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "delete", "")), "100% shrink")

    def test_deleting_a_missing_file(self) -> None:
        self.assertRefused(_patch(FilePatch("demo_app/nope.py", "delete", "")), "does not exist")

    def test_trivial_placeholder_even_for_a_small_file(self) -> None:
        # notes.txt is below the shrink threshold - the placeholder rule still applies.
        self.assertRefused(_patch(FilePatch("demo_app/notes.txt", "modify", "TODO")), "placeholder")
        self.assertRefused(
            _patch(FilePatch("demo_app/app.py", "modify", '"""Stub."""\npass\n')), "placeholder"
        )

    def test_new_todo_is_refused_but_an_existing_one_is_not(self) -> None:
        lazy = self.original + "\n# TODO: rest of code unchanged\n"
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", lazy)), "placeholder")
        self.repo.write("demo_app/app.py", self.original + "# TODO keep this note\n")
        kept = self.original + "# TODO keep this note\nX = 2\n"
        self.assertTrue(self.gate.check_patch(_patch(FilePatch("demo_app/app.py", "modify", kept))).passed)

    def test_leaked_patch_prompt_header(self) -> None:
        leaked = "--- File: demo_app/app.py (13 lines, 230 characters) ---\n" + self.original
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", leaked)), "retrieval marker")

    def test_leaked_snippet_header(self) -> None:
        leaked = self.original + "\n# --- Code Snippet #2 ---\n"
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", leaked)), "retrieval marker")

    def test_the_analysts_chunk_headers_are_leaked_markers_too(self) -> None:
        """L24: the header around each retrieved chunk and the crash block above it passed as
        comments, and "Lines: 1-20" passed bare: it parses, as an annotation."""
        for line in ("# File: demo_app/app.py", "# Part: function index", "# Lines: 1-20", "Lines: 1-20",
                     "# Code:", "# Status: crashed", "# Exit Code: 1", "# Error Log:",
                     "# SUMMARY: restored the import"):
            with self.subTest(line=line):
                leaked = self.original.rstrip("\n") + "\n" + line + "\n"
                self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", leaked)), "retrieval marker")

    def test_a_comment_that_only_looks_like_a_header_passes(self) -> None:
        """L24: the rule is about the prompts' exact lines, not about the words."""
        for line in ("# Parts of this file are generated", "# Status: see the wiki", "# Lines are short",
                     "# The file: app.py"):
            with self.subTest(line=line):
                kept = self.original.replace('"ok"', '"fixed"').rstrip("\n") + "\n" + line + "\n"
                result = self.gate.check_patch(_patch(FilePatch("demo_app/app.py", "modify", kept)))
                self.assertTrue(result.passed, result.reasons)

    def test_echoed_region_fence_is_a_leaked_marker(self) -> None:
        """Seen live: the 1.5B model wrapped its answer in '# BEGIN REGION ---'."""
        leaked = "# BEGIN REGION ---\n" + self.original + "# END REGION ---\n"
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", leaked)), "retrieval marker")

    def test_prompt_scaffolding_is_refused_also_as_a_comment(self) -> None:
        """L19: in a Python file a leaked prompt line survives the parse check only as a
        comment, and that is the form the rule missed - the region header, the facts, the
        retry notes, "--- File: ... ---" and every section header passed behind a '#'.
        The lines are taken from the patch prompts themselves."""
        from p3.patcher import CodePatcher
        patcher = CodePatcher(base_dir=self.repo.path)
        region = patcher._build_region_prompt(
            "demo_app/app.py", 8, 9, "def index():\n", "diagnosis", "log", refusal_hint="refused",
            goal="the service must start",
            facts="\nFACT (read from the code): demo_app/app.py uses jsonify but never imports it.\n",
        )
        full = patcher._build_patch_prompt(
            "diagnosis", "log", {"demo_app/app.py": self.original}, refusal_hint="refused"
        )
        scaffolding = {
            line.strip() for prompt in (region, full) for line in prompt.splitlines()
            if line.strip().startswith(("THE REGION", "FACT (", "YOUR PREVIOUS", "--- File:"))
            or re.fullmatch(r"[A-Z][A-Z ()\"',]+:", line.strip())
        }
        self.assertGreaterEqual(len(scaffolding), 10)
        for line in sorted(scaffolding):
            for comment in ("# ", "## ", "// "):
                leaked = self.original.replace(
                    "app = Flask(__name__)", f"{comment}{line}\napp = Flask(__name__)"
                )
                self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", leaked)), "retrieval marker")
        # An ordinary comment is not scaffolding.
        fine = self.original.replace("app = Flask(__name__)", "# Goal: keep serving.\napp = Flask(__name__)")
        self.assertTrue(self.gate.check_patch(_patch(FilePatch("demo_app/app.py", "modify", fine))).passed)

    def test_path_outside_heal_scope(self) -> None:
        self.assertRefused(_patch(FilePatch("p3/agent.py", "modify", "AGENT = 2\n")), "heal scope")
        self.assertRefused(_patch(FilePatch("demo_app/../p3/agent.py", "modify", "x=1\n")), "heal scope")
        self.assertRefused(_patch(FilePatch("/etc/passwd", "modify", "x\n")), "relative")

    def test_operator_config_is_off_limits(self) -> None:
        self.assertRefused(
            _patch(FilePatch("demo_app/iow.config.yml", "modify", "kill_switch: true\n")), "operator-owned"
        )

    def test_python_must_parse(self) -> None:
        broken = self.original.replace("def index():", "def index(:")
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", broken)), "SyntaxError")

    def test_a_definition_the_file_still_uses_may_not_disappear(self) -> None:
        """Seen live: a rewrite of the import block dropped SERVICE_NAME, still used below."""
        dropped = self.original.replace("import os\n", "")
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", dropped)), "leaves os undefined")

    def test_a_value_set_on_some_paths_only_is_refused(self) -> None:
        """J7: the value read where it may not be set yet - a NameError after the redeploy."""
        guarded = self.original + (
            '\nif __name__ == "__main__":\n'
            '    if "PORT" in os.environ:\n'
            '        port = int(os.environ["PORT"])\n'
            '    print(port)\n'
        )
        self.assertRefused(_patch(FilePatch("demo_app/app.py", "modify", guarded)), "reads port on line")
        defaulted = guarded.replace('    if "PORT"', '    port = 5000\n    if "PORT"')
        result = self.gate.check_patch(_patch(FilePatch("demo_app/app.py", "modify", defaulted)))
        self.assertTrue(result.passed, result.reasons)

    def test_a_partial_fix_of_an_already_broken_file_passes(self) -> None:
        broken = self.original.replace("from flask import Flask", "# from flask import Flask")
        self.repo.write("demo_app/app.py", broken)
        unrelated = broken.replace('"ok"', '"fixed"')     # Flask was undefined before, too
        result = self.gate.check_patch(_patch(FilePatch("demo_app/app.py", "modify", unrelated)))
        self.assertTrue(result.passed, result.reasons)

    def test_a_patch_that_changes_nothing_is_refused(self) -> None:
        """Seen live: the model returned the crashing function unchanged."""
        self.assertRefused(
            _patch(FilePatch("demo_app/app.py", "modify", self.original)), "changes nothing"
        )

    def test_duplicate_paths(self) -> None:
        fixed = self.original.replace('"ok"', '"a"')
        self.assertRefused(
            _patch(
                FilePatch("demo_app/app.py", "modify", fixed),
                FilePatch("demo_app/app.py", "modify", fixed),
            ),
            "more than once",
        )


if __name__ == "__main__":
    unittest.main()
