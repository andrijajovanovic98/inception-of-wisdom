"""
Inception-of-Wisdom (IoW) — Part 3: Wisdom Loop
Sanity Bounds: Pre-Disk Write Verification and Safety Guardrails
"""

from __future__ import annotations

import ast
import os
import re
import logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict

from p3.patcher import StructuredPatch

logger = logging.getLogger("p3.sanity")

# Forbidden placeholders where a model gets lazy and omits implementation
PLACEHOLDER_REGEXES = [
    re.compile(r"//\s*TODO", re.IGNORECASE),
    re.compile(r"#\s*TODO\b", re.IGNORECASE),
    re.compile(r"/\*.*rest of code.*\*/", re.IGNORECASE),
    re.compile(r"\.\.\.\s*#?\s*(?:rest of code|remaining code|unchanged)", re.IGNORECASE),
    re.compile(r"#\s*(?:keep existing code|rest of file is the same|code continues here)", re.IGNORECASE),
]

# Retrieval markers that may accidentally leak from prompt into code
RETRIEVAL_MARKER_REGEXES = [
    re.compile(r"---\s*Code Snippet\s*#?\d+\s*---", re.IGNORECASE),
    re.compile(r"#\s*File:\s*[\w\./-]+\s*\|\s*Symbol:", re.IGNORECASE),
    re.compile(r"---\s*File:\s*[\w\./-]+\s*---", re.IGNORECASE),
]


@dataclass
class SanityResult:
    """Outcome of pre-disk write sanity bounds verification."""
    passed: bool
    reasons: List[str] = field(default_factory=list)
    file_checks: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SanityChecker:
    """Enforces strict safety guardrails on candidate patches before any disk write.
    Subject requirement:
    1. Touches at most 3 files.
    2. Does not replace a non-empty file with a trivial placeholder.
    3. Does not shrink a file by more than 60%.
    4. Does not leak retrieval markers into file content.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        max_files: int = 3,
        max_shrinkage_pct: float = 60.0
    ):
        self.base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()
        self.max_files = max_files
        self.max_shrinkage_pct = max_shrinkage_pct

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

        for file_patch in patch.files:
            f_reasons = []
            rel_path = file_patch.path
            full_path = os.path.join(self.base_dir, rel_path)
            content = file_patch.content
            op = file_patch.op

            # Rule 2: Non-empty check on create/modify
            if op in ["create", "modify"] and not content.strip():
                f_reasons.append("File content is completely empty or whitespace.")

            # Rule 3: File shrinkage check (Subject constraint: max 60% shrinkage)
            if op == "modify" and os.path.isfile(full_path):
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        original_content = f.read()
                    orig_len = len(original_content)
                    new_len = len(content)

                    # Only evaluate shrinkage if original file is substantial (> 80 chars)
                    if orig_len > 80:
                        shrinkage_pct = ((orig_len - new_len) / orig_len) * 100.0
                        if shrinkage_pct > self.max_shrinkage_pct:
                            f_reasons.append(
                                f"File shrinkage ({shrinkage_pct:.1f}%) exceeds "
                                f"the allowed {self.max_shrinkage_pct}% "
                                f"(original: {orig_len} bytes, proposed: {new_len} bytes)."
                            )
                except Exception as e:
                    logger.warning(f"Could not read original file {rel_path} for shrinkage check: {e}")

            # Rule 4: Trivial placeholder protection (Subject constraint: no placeholders)
            for regex in PLACEHOLDER_REGEXES:
                if regex.search(content):
                    f_reasons.append(f"Forbidden placeholder pattern detected matching '{regex.pattern}'.")
                    break

            # Rule 5: Leakage of retrieval markers (Subject constraint: no retrieval markers)
            for regex in RETRIEVAL_MARKER_REGEXES:
                if regex.search(content):
                    f_reasons.append(f"Leaked retrieval marker detected matching '{regex.pattern}'.")
                    break

            # Rule 6: Python Syntax verification
            if rel_path.endswith(".py") and op in ["create", "modify"]:
                try:
                    ast.parse(content, filename=rel_path)
                except SyntaxError as e:
                    f_reasons.append(f"Python SyntaxError in proposed patch: {e.msg} (line {e.lineno})")

            file_checks[rel_path] = {
                "op": op,
                "passed": len(f_reasons) == 0,
                "violations": f_reasons
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
