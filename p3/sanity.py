"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Sanity Bounds: Pre-Disk Write Verification and Safety Guardrails

Every check compares the proposal with the file it would replace, so the gate judges
what the model *changed*: a TODO the file already carried is not a new placeholder,
and a rewrite cannot dodge the shrink rule by calling itself 'create'.
"""

from __future__ import annotations

import ast
import os
import re
import logging
from typing import Callable, List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict

from p3.names import undefined_names
from p3.patcher import StructuredPatch

logger = logging.getLogger("p3.sanity")

# Forbidden placeholders where a model gets lazy and omits implementation
PLACEHOLDER_REGEXES = [
    re.compile(r"//\s*TODO", re.IGNORECASE),
    re.compile(r"#\s*TODO\b", re.IGNORECASE),
    re.compile(r"/\*.*rest of code.*\*/", re.IGNORECASE),
    re.compile(r"\.\.\.\s*#?\s*(?:rest of (?:the )?code|remaining code|unchanged)", re.IGNORECASE),
    re.compile(
        r"#\s*(?:\.\.\.\s*)?(?:keep existing code|rest of (?:the )?(?:file|code)(?: is the same| unchanged)?"
        r"|code continues here|existing code (?:here|unchanged)|same as before)",
        re.IGNORECASE,
    ),
]

# Retrieval / prompt scaffolding that must never end up inside a source file. These are
# the exact headers the Analyst and the Patcher put around the code they show the model.
RETRIEVAL_MARKER_REGEXES = [
    re.compile(r"---\s*Code Snippet\s*#?\d+\s*---", re.IGNORECASE),
    re.compile(r"#\s*File:\s*[\w\./-]+\s*\|\s*Symbol:", re.IGNORECASE),
    # "--- File: demo_app/app.py ---" and "--- File: demo_app/app.py (83 lines, 2345 characters) ---"
    re.compile(r"^-{3}\s*File:\s*\S+.*-{3}\s*$", re.IGNORECASE | re.MULTILINE),
    # The Patcher's region fence, in any comment dress ("# BEGIN REGION ---").
    re.compile(r"\b(?:BEGIN|END)\s+REGION\b", re.IGNORECASE),
    re.compile(r"^\s*Symbol:\s*\S+\s*\((?:function|method|class|module|file)\)\s*$", re.MULTILINE),
    re.compile(
        r"^(?:RELEVANT CODE CHUNKS|ALLOWED SUSPECT FILES|CURRENT SOURCE CODE|ERROR LOG EXCERPT"
        r"|CRASH DETAILS|AVAILABLE CODE CONTEXT|CODE REPOSITORY SLICES):\s*$",
        re.MULTILINE,
    ),
]

# A file reduced to one of these is a stub, not a fix.
TRIVIAL_PLACEHOLDER_TEXT = {
    "pass", "...", "todo", "# todo", "fixme", "# fixme", "placeholder", "# placeholder",
    "tbd", "raise notimplementederror", "raise notimplementederror()", "none", "null", "{}", "[]",
}


@dataclass
class SanityResult:
    """Outcome of pre-disk write sanity bounds verification."""
    passed: bool
    reasons: List[str] = field(default_factory=list)
    file_checks: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _is_trivial_python(content: str) -> bool:
    """A module whose only statements are docstrings / pass / ... / NotImplementedError."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.Pass):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring or a bare `...`
        if isinstance(node, ast.Raise):
            exc = node.exc
            name = getattr(exc, "id", None) or getattr(getattr(exc, "func", None), "id", None)
            if name == "NotImplementedError":
                continue
        return False
    return True


def is_trivial_placeholder(path: str, content: str) -> bool:
    """True when `content` is a stub standing in for real code."""
    meaningful = [
        line.strip() for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    collapsed = " ".join(meaningful).lower()
    if not meaningful:
        return True  # nothing but comments
    if collapsed in TRIVIAL_PLACEHOLDER_TEXT:
        return True
    if path.endswith(".py") and _is_trivial_python(content):
        return True
    return False


class SanityChecker:
    """Enforces strict safety guardrails on candidate patches before any disk write.
    Subject requirement:
    1. Touches at most 3 files.
    2. Does not replace a non-empty file with a trivial placeholder.
    3. Does not shrink a file by more than 60%.
    4. Does not leak retrieval markers into file content.
    Plus: every path stays inside the heal scope (path_guard), Python must parse, and
    nothing the file still uses may lose its definition.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        max_files: int = 3,
        max_shrinkage_pct: float = 60.0,
        path_guard: Optional[Callable[[str], bool]] = None,
    ):
        self.base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()
        self.max_files = max_files
        self.max_shrinkage_pct = max_shrinkage_pct
        # Returns True when a relative path may be written by the loop (heal scope,
        # not operator-owned). None = only the structural path checks below.
        self.path_guard = path_guard

    def _read_original(self, rel_path: str) -> Optional[str]:
        full_path = os.path.join(self.base_dir, rel_path)
        if not os.path.isfile(full_path):
            return None
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Could not read original file {rel_path}: {e}")
            return None

    def _path_violation(self, rel_path: str) -> Optional[str]:
        if not rel_path or os.path.isabs(rel_path):
            return f"Path '{rel_path}' must be relative to the repository."
        normalised = os.path.normpath(rel_path)
        if normalised == ".." or normalised.startswith(".." + os.sep):
            return f"Path '{rel_path}' escapes the repository."
        if self.path_guard is not None and not self.path_guard(rel_path):
            return (
                f"Path '{rel_path}' is outside the heal scope or operator-owned "
                "(the loop may only edit the target's source, never iow.config.yml)."
            )
        return None

    def check_patch(self, patch: StructuredPatch) -> SanityResult:
        """Executes all sanity checks against the candidate patch."""
        reasons: List[str] = []
        file_checks: Dict[str, Dict[str, Any]] = {}

        # Pre-check: Patch itself must be marked successful
        if not patch.success:
            return SanityResult(
                passed=False,
                reasons=[f"Patch is marked as unsuccessful: {patch.error_message}"]
            )

        # Rule 1: Maximum files touched (Subject constraint: max 3 files)
        if len(patch.files) == 0:
            return SanityResult(passed=False, reasons=["Patch contains zero files."])

        if len(patch.files) > self.max_files:
            reasons.append(
                f"Patch touches {len(patch.files)} files, which exceeds the limit of {self.max_files}."
            )

        seen_paths: set = set()
        for file_patch in patch.files:
            f_reasons: List[str] = []
            rel_path = file_patch.path
            content = file_patch.content
            op = file_patch.op

            if rel_path in seen_paths:
                f_reasons.append(f"Path '{rel_path}' appears more than once in the patch.")
            seen_paths.add(rel_path)

            violation = self._path_violation(rel_path)
            if violation:
                f_reasons.append(violation)

            original = self._read_original(rel_path)
            original_nonempty = bool(original and original.strip())

            if op == "delete":
                if original is None:
                    f_reasons.append(f"Cannot delete '{rel_path}': the file does not exist.")
                elif original_nonempty:
                    # Deleting a file is a 100% shrink of it.
                    f_reasons.append(
                        f"Deleting non-empty '{rel_path}' is a 100% shrink "
                        f"(limit {self.max_shrinkage_pct}%)."
                    )
            else:
                # Rule 2a: never write an empty file
                if not content.strip():
                    f_reasons.append("File content is completely empty or whitespace.")

                # A patch identical to the file cannot fix the crash - it only buys a
                # commit and a restart that "verify" nothing.
                elif original is not None and content == original:
                    f_reasons.append(
                        f"Proposed '{rel_path}' is identical to the file on disk: the patch "
                        "changes nothing, so the crash is still there."
                    )

                # Rule 2b: a non-empty file must not become a stub (any op that
                # overwrites an existing file counts, 'create' included).
                elif original_nonempty and is_trivial_placeholder(rel_path, content):
                    f_reasons.append(
                        f"Replaces non-empty '{rel_path}' with a trivial placeholder."
                    )

                # Rule 3: shrinkage (Subject constraint: max 60%), whatever the op says and
                # whatever the file's size - the subject names no lower bound.
                if original_nonempty and original is not None:
                    shrinkage_pct = ((len(original) - len(content)) / len(original)) * 100.0
                    if shrinkage_pct > self.max_shrinkage_pct:
                        f_reasons.append(
                            f"File shrinkage ({shrinkage_pct:.1f}%) exceeds "
                            f"the allowed {self.max_shrinkage_pct}% "
                            f"(original: {len(original)} bytes, proposed: {len(content)} bytes)."
                        )

                # Rule 4: lazy placeholders the original did not already contain
                for regex in PLACEHOLDER_REGEXES:
                    if regex.search(content) and not (original and regex.search(original)):
                        f_reasons.append(
                            f"Forbidden placeholder pattern detected matching '{regex.pattern}'."
                        )
                        break

                # Rule 5: leaked retrieval markers (new ones only)
                for regex in RETRIEVAL_MARKER_REGEXES:
                    if regex.search(content) and not (original and regex.search(original)):
                        f_reasons.append(f"Leaked retrieval marker detected matching '{regex.pattern}'.")
                        break

                # Rule 6: Python must still parse
                if rel_path.endswith(".py"):
                    try:
                        ast.parse(content, filename=rel_path)
                    except SyntaxError as e:
                        f_reasons.append(f"Python SyntaxError in proposed patch: {e.msg} (line {e.lineno})")

                # Rule 7: nothing the file still uses may lose its definition. Seen live:
                # a rewrite of the import block dropped SERVICE_NAME, and a whole
                # redeploy was spent finding the NameError the file could have shown.
                if rel_path.endswith(".py") and original is not None:
                    before, after = undefined_names(original), undefined_names(content)
                    if before is not None and after is not None and after - before:
                        lost = ", ".join(sorted(after - before))
                        f_reasons.append(
                            f"The patch leaves {lost} undefined although the file still uses "
                            "it: it removed a definition or an import (NameError at run time)."
                        )

            file_checks[rel_path] = {
                "op": op,
                "passed": len(f_reasons) == 0,
                "violations": f_reasons,
                "original_bytes": len(original) if original is not None else None,
                "proposed_bytes": 0 if op == "delete" else len(content),
            }
            reasons.extend(f_reasons)

        passed = len(reasons) == 0
        if passed:
            logger.info(f"Sanity check PASSED for patch with {len(patch.files)} files.")
        else:
            logger.warning(f"Sanity check REFUSED patch with {len(reasons)} violation(s): {reasons}")

        return SanityResult(
            passed=passed,
            reasons=reasons,
            file_checks=file_checks
        )
