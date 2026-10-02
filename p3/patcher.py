"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Patcher: Structured JSON Patch Generator (Ollama Code Repair)

The model is the only source of a fix. There is no canned answer for any particular
bug.

A sub-3B model cannot reliably echo a whole 100-line file back as one JSON string: it
cuts the file off, or runs out of time. So when the crash points into a larger file,
the model rewrites only the logical chunk the traceback points at (the same AST
chunks the Analyst indexes - subject hint: "use the index to do as much of the work as
you can"). The patcher splices that region back in, so the patch that reaches the
sanity gate, the disk and git is still the complete post-change file. Small files, and
crashes that point nowhere specific, still get a full-file rewrite; a truncated one is
retried once with an explicit length requirement and otherwise fails honestly.
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment,misc]

from p2.chunker import AstChunker
from p2.diagnostician import DiagnosticReport, _normalize_ollama_host
from p3.names import undefined_names

logger = logging.getLogger("p3.patcher")

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11436")
DEFAULT_MODEL = "qwen2.5-coder:1.5b"

# A full-file rewrite that keeps less than this share of the original is almost
# certainly a truncated generation rather than a deliberate deletion.
TRUNCATION_KEEP_RATIO = 0.5

# Files shorter than this are cheap enough to rewrite in full.
REGION_MIN_FILE_LINES = 30
# Lines around a syntax error when the file cannot be parsed into chunks.
REGION_SYNTAX_WINDOW = 8
# A rewritten region that keeps less than this share of its lines was cut off.
REGION_KEEP_RATIO = 0.4

_FRAME_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)')
# An echoed fence ("--- BEGIN REGION ---", "# END REGION ---", ...) is scaffolding, not code.
_REGION_MARKER_RE = re.compile(
    r"^[ \t]*(?:#|//)?[ \t]*-*[ \t]*(?:BEGIN|END)[ \t]+REGION\b.*(?:\n|$)",
    re.MULTILINE | re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)```", re.DOTALL)
_SUMMARY_RE = re.compile(r"SUMMARY:\s*(.+)", re.IGNORECASE)
_NAME_ERROR_RE = re.compile(r"NameError: name '([A-Za-z_]\w*)' is not defined")


def _unbound_name(log_excerpt: str, text: str) -> Optional[str]:
    """The name of the latest NameError, if this file binds it nowhere.

    Nowhere means no import, def, class, assignment or argument: the name is simply
    missing - a dropped import or definition - so the fix belongs with the imports
    rather than in the function that tripped over it.
    """
    names = _NAME_ERROR_RE.findall(log_excerpt or "")
    missing = undefined_names(text) if names else None
    return names[-1] if missing and names[-1] in missing else None


# When the target's ports and environment matter to a fix.
_TARGET_FACTS_RE = re.compile(
    r"os\.environ|getenv|KeyError|environment variable|not answering HTTP", re.IGNORECASE
)


UNCHANGED_NOTE = (
    "\nYOUR PREVIOUS ANSWER WAS IDENTICAL TO THE REGION. The region fails exactly as it "
    "is: change the code that causes the failure.\n"
)


def _same_code(a: str, b: str) -> bool:
    """The same lines, trailing whitespace and blank edges aside."""
    return [line.rstrip() for line in a.strip("\n").splitlines()] == \
        [line.rstrip() for line in b.strip("\n").splitlines()]


def _needs_target_facts(text: str) -> bool:
    """Whether the code or log is about environment variables or a service nobody reaches.

    Only then does the prompt carry the container's ports and environment: for an import
    or syntax error that would just be one more thing for a small model to act on.
    """
    return bool(_TARGET_FACTS_RE.search(text or ""))


@dataclass
class FilePatch:
    """Represents a single file modification in the structured patch."""
    path: str
    op: str              # 'create', 'modify', 'delete'
    content: str         # Complete post-change file content. Diffs are strictly rejected.

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredPatch:
    """Subject requirement: Structured JSON patch with summary and files array.
    Diffs are NOT accepted. Content must be the complete post-change file.
    """
    summary: str
    files: List[FilePatch] = field(default_factory=list)
    success: bool = True
    raw_response: Optional[str] = None
    error_message: Optional[str] = None
    timestamp: float = 0.0
    # How the model was asked: "full-file" or "region <path> L<a>-<b> (<why>)".
    strategy: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "success": self.success,
            "files": [f.to_dict() for f in self.files],
            "error_message": self.error_message,
            "timestamp": self.timestamp,
            "strategy": self.strategy,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StructuredPatch":
        """Rebuild a patch from its serialised form (classifier fast path)."""
        files: List[FilePatch] = []
        for item in data.get("files") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            files.append(FilePatch(
                path=path,
                op=str(item.get("op", "modify")),
                content=str(item.get("content", "")),
            ))
        return cls(
            summary=str(data.get("summary", "Cached patch")),
            files=files,
            success=bool(files),
            timestamp=time.time(),
        )


def _undefined_block(rel_path: str, text: str) -> str:
    """A fact read from the code, not asked of the model: names used but never defined.

    Seen live: with an import line of two names commented out, the traceback names only
    the first; the model restored that one, and a whole redeploy was spent learning
    about the second.
    """
    missing = sorted(undefined_names(text) or [])
    if not missing:
        return ""
    return (
        f"\nFACT (read from the code): {rel_path} uses {', '.join(missing)} but never "
        "imports or defines it. Each of these needs an import or a definition.\n"
    )


def _goal_block(goal: Optional[str], subject: str) -> str:
    """What "fixed" means, stated to the model - and why an unchanged answer is wrong.

    Without it a small model reads an intentional-looking `raise` as intended and
    hands the code back untouched.
    """
    lines = ["", "GOAL:"]
    if goal:
        lines.append(goal)
    lines.append(f"{subject} fails exactly as it is, so returning it unchanged is NOT a fix.")
    return "\n".join(lines) + "\n"


class CodePatcher:
    """Generates structured code repairs using the local Ollama coder model (< 3B).
    Enforces full-file JSON patches and strictly rejects unified diffs.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        ollama_host: Optional[str] = None,
        model_name: str = DEFAULT_MODEL,
        request_timeout: float = 180.0
    ):
        self.base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()
        self.ollama_host = _normalize_ollama_host(ollama_host or DEFAULT_OLLAMA_HOST)
        self.model_name = model_name
        self.request_timeout = request_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate_patch(
        self,
        diagnosis: DiagnosticReport,
        log_excerpt: str,
        refusal_hint: Optional[str] = None,
        goal: Optional[str] = None,
        temperature: float = 0.1,
        facts: str = "",
    ) -> StructuredPatch:
        """Generates a structured JSON repair patch for the suspect files.

        refusal_hint carries why the previous attempt was rejected, so a retry is
        never a byte-identical request. goal states what "fixed" means for this crash
        (e.g. the failing request must answer 2xx), which is what the Verifier checks.
        temperature is the model's sampling temperature (the loop raises it on retries).
        facts describes the target's container (ports, environment); it is shown only
        when the code or the log is about environment variables or an unreachable service.
        """
        now = time.time()

        if not diagnosis.files:
            return StructuredPatch(
                summary="Patch generation failed: no suspect files identified in diagnosis.",
                success=False,
                error_message="No suspect files available for patching.",
                timestamp=now
            )

        file_contexts: Dict[str, str] = {}
        for rel_path in diagnosis.files:
            full_path = os.path.join(self.base_dir, rel_path)
            if os.path.isfile(full_path):
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        file_contexts[rel_path] = f.read()
                except Exception as e:
                    logger.error(f"Failed to read file {rel_path}: {e}")

        if not file_contexts:
            return StructuredPatch(
                summary="Patch generation failed: suspect files could not be read from disk.",
                success=False,
                error_message=(
                    "Suspect files not found on disk: "
                    f"{', '.join(diagnosis.files)} (relative to {self.base_dir})"
                ),
                timestamp=now
            )

        # The crash points into a larger file: have the model rewrite that region.
        for rel_path, text in file_contexts.items():
            if text.count("\n") + 1 < REGION_MIN_FILE_LINES:
                continue
            region = self._find_region(rel_path, text, log_excerpt, diagnosis)
            if region is not None:
                return self._generate_region_patch(
                    rel_path, text, region, diagnosis.summary, log_excerpt, refusal_hint, now,
                    goal=goal, temperature=temperature, facts=facts,
                )

        target = facts if _needs_target_facts(" ".join(file_contexts.values()) + log_excerpt) else ""
        prompt = self._build_patch_prompt(
            diagnosis.summary, log_excerpt, file_contexts, refusal_hint, goal=goal, facts=target
        )
        raw_output, call_error = self._call_ollama(prompt, temperature=temperature)

        if call_error:
            logger.warning(f"Ollama patch generation failed ({call_error}).")
            return StructuredPatch(
                summary="Patch generation failed: the local model could not be reached.",
                success=False,
                raw_response=raw_output,
                error_message=f"Model call failed: {call_error}",
                timestamp=now
            )

        patch = self._parse_patch_response(raw_output, file_contexts, now)
        if not patch.success:
            return patch

        # One stricter retry when the model truncated the file instead of rewriting it.
        truncated = self._truncated_files(patch, file_contexts)
        if not truncated:
            return patch

        logger.warning(
            "Model returned truncated file(s) %s - retrying once with an explicit "
            "length requirement.", [t[0] for t in truncated]
        )
        retry_prompt = self._build_patch_prompt(
            diagnosis.summary, log_excerpt, file_contexts,
            refusal_hint=self._truncation_hint(truncated),
        )
        retry_raw, retry_err = self._call_ollama(retry_prompt, temperature=temperature)
        if not retry_err:
            retry_patch = self._parse_patch_response(retry_raw, file_contexts, now)
            if retry_patch.success and not self._truncated_files(retry_patch, file_contexts):
                return retry_patch
            if retry_patch.success:
                truncated = self._truncated_files(retry_patch, file_contexts)
                patch = retry_patch

        return StructuredPatch(
            summary="Patch rejected: the model returned an incomplete file.",
            success=False,
            raw_response=patch.raw_response,
            error_message=self._truncation_hint(truncated),
            timestamp=now,
        )

    # ------------------------------------------------------------------
    # Region-scoped rewrite
    # ------------------------------------------------------------------
    def _find_region(
        self,
        rel_path: str,
        text: str,
        log_excerpt: str,
        diagnosis: DiagnosticReport,
    ) -> Optional[Tuple[int, int, str]]:
        """(start_line, end_line, why) of the chunk the crash points at, or None.

        Chunks are recomputed from the file as it is on disk now, so their line
        ranges are exact even if the index is a few seconds behind.
        """
        base = os.path.basename(rel_path)
        line_hint: Optional[int] = None
        for match in _FRAME_RE.finditer(log_excerpt or ""):
            if os.path.basename(match.group(1)) == base:
                line_hint = int(match.group(2))  # the deepest frame in this file wins

        total = text.count("\n") + 1
        chunks = []
        if rel_path.endswith(".py"):
            chunks = [
                c for c in AstChunker(self.base_dir)._chunk_python_ast(rel_path, text)
                if c.symbol_type not in ("class", "file")
            ]

        # A name the file binds nowhere ("NameError: name 'jsonify' is not defined")
        # is a missing import or definition. Given only the function that tripped over
        # it, the model can do nothing but hand it back unchanged (seen live: two
        # refused no-op attempts, then a rollback) - show it the imports instead.
        missing = _unbound_name(log_excerpt, text)
        if missing:
            preamble = next((c for c in chunks if c.symbol_name == "module_preamble"), None)
            if preamble is not None:
                return preamble.start_line, preamble.end_line, f"imports, '{missing}' is never defined"

        if line_hint is not None and 1 <= line_hint <= total:
            containing = [c for c in chunks if c.start_line <= line_hint <= c.end_line]
            if containing:
                best = min(containing, key=lambda c: c.end_line - c.start_line)
                return best.start_line, best.end_line, f"{best.symbol_name}, traceback line {line_hint}"
            if not chunks:
                # Unparseable file (the syntax error itself): a window around it.
                start = max(1, line_hint - REGION_SYNTAX_WINDOW)
                end = min(total, line_hint + REGION_SYNTAX_WINDOW)
                return start, end, f"around line {line_hint} (file does not parse)"

        # No usable frame: the chunk the Analyst ranked first in this file.
        by_name = {c.symbol_name: c for c in chunks}
        for candidate in diagnosis.candidate_chunks or []:
            if candidate.get("file_path") == rel_path and candidate.get("symbol_name") in by_name:
                chunk = by_name[candidate["symbol_name"]]
                return chunk.start_line, chunk.end_line, f"{chunk.symbol_name}, top retrieved chunk"
        return None

    def _generate_region_patch(
        self,
        rel_path: str,
        text: str,
        region: Tuple[int, int, str],
        diagnostic_summary: str,
        log_excerpt: str,
        refusal_hint: Optional[str],
        now: float,
        goal: Optional[str] = None,
        temperature: float = 0.1,
        facts: str = "",
    ) -> StructuredPatch:
        start, end, why = region
        lines = text.splitlines(keepends=True)
        original_region = "".join(lines[start - 1:end])
        strategy = f"region {rel_path} L{start}-{end} ({why})"
        logger.info(f"Asking the model to rewrite {strategy}.")

        prompt = self._build_region_prompt(
            rel_path, start, end, original_region, diagnostic_summary, log_excerpt, refusal_hint,
            goal=goal,
            facts=_undefined_block(rel_path, text)
            + (facts if _needs_target_facts(original_region + log_excerpt) else ""),
        )
        # Plain code, not JSON: escaping two dozen lines of Python into one JSON string
        # is exactly where a 1.5B model gives up and answers with the two lines it
        # changed. The agent itself packs the answer into the structured JSON patch.
        raw_output, call_error = self._call_ollama(prompt, json_mode=False, temperature=temperature)
        if call_error:
            return StructuredPatch(
                summary="Patch generation failed: the local model could not be reached.",
                success=False, raw_response=raw_output, strategy=strategy,
                error_message=f"Model call failed: {call_error}", timestamp=now,
            )

        new_region, summary, error = self._parse_region_response(raw_output, original_region)
        if not error and new_region is not None and _same_code(new_region, original_region):
            # Handed back unchanged: the sanity gate would refuse it and the whole attempt
            # would be spent (seen live, even at a raised temperature). Ask once more,
            # more freely, before that happens.
            logger.info(f"The model returned {strategy} unchanged - asking once more.")
            again, again_error = self._call_ollama(
                prompt + UNCHANGED_NOTE, json_mode=False, temperature=min(0.9, temperature + 0.3)
            )
            if not again_error:
                retried, retried_summary, retried_error = self._parse_region_response(
                    again, original_region
                )
                if not retried_error and retried is not None and not _same_code(retried, original_region):
                    new_region, summary, raw_output = retried, retried_summary, again
        if error:
            return StructuredPatch(
                summary="Patch rejected.", success=False, raw_response=raw_output,
                strategy=strategy, error_message=f"{error} (region L{start}-{end} of {rel_path})",
                timestamp=now,
            )

        assert new_region is not None
        if not new_region.endswith("\n"):
            new_region += "\n"
        new_text = "".join(lines[:start - 1]) + new_region + "".join(lines[end:])
        logger.info(f"Model rewrote {strategy}: '{summary}'")
        return StructuredPatch(
            summary=summary,
            files=[FilePatch(path=rel_path, op="modify", content=new_text)],
            success=True,
            raw_response=raw_output,
            strategy=strategy,
            timestamp=now,
        )

    def _build_region_prompt(
        self,
        rel_path: str,
        start: int,
        end: int,
        region: str,
        diagnostic_summary: str,
        log_excerpt: str,
        refusal_hint: Optional[str],
        goal: Optional[str] = None,
        facts: str = "",
    ) -> str:
        retry_block = ""
        if refusal_hint:
            retry_block = (
                "\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n"
                f"{refusal_hint}\n"
                "Fix that specific problem in this attempt.\n"
            )
        n_lines = region.count("\n") or 1
        return f"""You are an autonomous software repair agent.
A containerized service crashed. Fix the error by rewriting ONE region of {rel_path}.

DIAGNOSIS:
{diagnostic_summary}

ERROR LOG EXCERPT:
{log_excerpt.strip()}
{_goal_block(goal, "The region below")}{facts}
THE REGION TO REWRITE ({rel_path}, lines {start}-{end}, {n_lines} lines):
```python
{region.rstrip()}
```
{retry_block}
RULES:
1. First line: SUMMARY: <one sentence - what you changed and why>
2. Then the corrected region in ONE ```python code block, and nothing after it.
3. The block must contain every line of the region, in order, with the same
   indentation. Copy the lines you do not change verbatim.
4. Change only what causes the crash, with the smallest possible edit. If the right
   code is already in the region (for example commented out), reuse it exactly.
   Do not add code from other parts of the file.
5. No diffs, no "..." and no placeholders.
"""

    def _parse_region_response(
        self, raw_output: Optional[str], original_region: str
    ) -> Tuple[Optional[str], str, Optional[str]]:
        """(new_region, summary, error). Accepts a fenced code block (asked for) or JSON."""
        if not raw_output:
            return None, "", "Empty response from the local model."

        content: Any = None
        raw_summary: Any = None
        fence = _CODE_FENCE_RE.search(raw_output)
        if fence:
            content = fence.group(1)
            summary_match = _SUMMARY_RE.search(raw_output[:fence.start()])
            raw_summary = summary_match.group(1) if summary_match else None
        else:
            try:
                parsed = json.loads(raw_output)
            except json.JSONDecodeError:
                return None, "", "The model returned neither a ```python code block nor JSON."
            if not isinstance(parsed, dict):
                return None, "", "Expected a code block (or a JSON object with 'content')."
            raw_summary = parsed.get("summary")
            content = parsed.get("content")
            if not isinstance(content, str):
                # Tolerate the full patch schema: {"files": [{"content": ...}]}.
                files = parsed.get("files")
                if isinstance(files, list) and files and isinstance(files[0], dict):
                    content = files[0].get("content")
        if not isinstance(content, str) or not content.strip():
            return None, "", "The model returned no content for the region."
        content = _REGION_MARKER_RE.sub("", content).strip("\n")
        if self._is_unified_diff(content):
            return None, "", "The model returned a diff instead of the rewritten region."

        orig_lines = original_region.count("\n") or 1
        new_lines = content.count("\n") + 1
        if orig_lines >= 8 and new_lines < orig_lines * REGION_KEEP_RATIO:
            return None, "", (
                f"The rewritten region came back with {new_lines} lines but the original has "
                f"{orig_lines}; it was cut off instead of rewritten in full"
            )

        summary = "Automatic fix"
        if isinstance(raw_summary, str) and raw_summary.strip():
            summary = raw_summary.strip()
        return content, summary, None

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------
    def _build_patch_prompt(
        self,
        diagnostic_summary: str,
        log_excerpt: str,
        file_contexts: Dict[str, str],
        refusal_hint: Optional[str] = None,
        goal: Optional[str] = None,
        facts: str = "",
    ) -> str:
        """Constructs an unambiguous prompt enforcing full-file JSON patches."""
        files_text = ""
        facts = "".join(_undefined_block(path, code) for path, code in file_contexts.items()) + facts
        for path, code in file_contexts.items():
            line_count = code.count("\n") + 1
            files_text += (
                f"\n--- File: {path} ({line_count} lines, "
                f"{len(code)} characters) ---\n{code}\n"
            )

        retry_block = ""
        if refusal_hint:
            retry_block = (
                "\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n"
                f"{refusal_hint}\n"
                "Fix that specific problem in this attempt.\n"
            )

        prompt = f"""You are an autonomous software repair agent.
A containerized service crashed. Fix the error in the suspect file.

DIAGNOSIS:
{diagnostic_summary}

ERROR LOG EXCERPT:
{log_excerpt.strip()}
{_goal_block(goal, "The current code")}{facts}
CURRENT SOURCE CODE:
{files_text}
{retry_block}
CRITICAL RULES (SUBJECT CONSTRAINTS):
1. Output MUST be strictly valid JSON and NOTHING ELSE.
2. The "content" field MUST be the COMPLETE post-change file, from its first line to
   its last. Copy every line you are not changing verbatim. The result must have
   roughly the same number of lines as the original shown above.
3. Keep ALL existing imports, routes, helpers and docstrings. Change ONLY the
   lines that cause the failure described in the diagnosis.
4. DIFFS ARE NOT ACCEPTED (no '+' / '-' unified diffs, no "@@" hunks).
5. PLACEHOLDERS ARE FORBIDDEN (no TODO, no "...", no "rest of code unchanged").
6. "op" must be "modify", "create", or "delete".
7. Patch at most 3 files.

RESPONSE JSON SCHEMA:
{{
  "summary": "Brief explanation of the bug fix",
  "files": [
    {{
      "path": "exact/relative/path.py",
      "op": "modify",
      "content": "COMPLETE PYTHON SOURCE CODE OF THE FIXED FILE"
    }}
  ]
}}
"""
        return prompt

    # ------------------------------------------------------------------
    # Truncation detection
    # ------------------------------------------------------------------
    def _truncated_files(
        self,
        patch: StructuredPatch,
        file_contexts: Dict[str, str],
    ) -> List[Tuple[str, int, int]]:
        """Files whose proposed content is far shorter than the original they replace.

        Returns (path, original_lines, proposed_lines) for each suspicious entry.
        """
        suspicious: List[Tuple[str, int, int]] = []
        for fp in patch.files:
            if fp.op != "modify":
                continue
            original = file_contexts.get(fp.path) or self._read_from_disk(fp.path)
            if not original:
                continue
            orig_lines = original.count("\n") + 1
            new_lines = fp.content.count("\n") + 1
            if orig_lines < 10:
                continue
            if new_lines < orig_lines * TRUNCATION_KEEP_RATIO:
                suspicious.append((fp.path, orig_lines, new_lines))
        return suspicious

    def _truncation_hint(self, truncated: List[Tuple[str, int, int]]) -> str:
        if not truncated:
            return "The model returned an incomplete file."
        parts = [
            f"'{path}' came back with {new} lines but the original has {orig}; "
            "the file was cut off instead of rewritten in full"
            for path, orig, new in truncated
        ]
        return "; ".join(parts)

    def _read_from_disk(self, rel_path: str) -> str:
        full_path = os.path.join(self.base_dir, rel_path)
        if not os.path.isfile(full_path):
            return ""
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Model call & parsing
    # ------------------------------------------------------------------
    def _call_ollama(
        self, prompt: str, json_mode: bool = True, temperature: float = 0.1
    ) -> Tuple[Optional[str], Optional[str]]:
        """Sends a code repair request to Ollama (JSON mode unless asked for plain code)."""
        if httpx is None:
            return None, "httpx library not installed"

        url = f"{self.ollama_host}/api/generate"
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 4096
            }
        }
        if json_mode:
            payload["format"] = "json"

        try:
            with httpx.Client(timeout=self.request_timeout) as client:
                res = client.post(url, json=payload)
                if res.status_code != 200:
                    return None, f"Ollama HTTP {res.status_code}: {res.text[:200]}"
                data = res.json()
                return (data.get("response", "") or "").strip(), None
        except Exception as e:
            return None, str(e)

    def _parse_patch_response(
        self,
        raw_output: Optional[str],
        file_contexts: Dict[str, str],
        timestamp: float
    ) -> StructuredPatch:
        """Validates JSON structure, verifies full-file content, and rejects diffs."""
        if not raw_output:
            return StructuredPatch(
                summary="Model produced empty patch output.",
                success=False,
                raw_response=raw_output,
                error_message="Empty response from the local model.",
                timestamp=timestamp
            )

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode patch JSON from model: {e}")
            return StructuredPatch(
                summary="Model produced malformed JSON.",
                success=False,
                raw_response=raw_output,
                error_message=f"JSONDecodeError: {e}",
                timestamp=timestamp
            )

        if not isinstance(parsed, dict):
            return StructuredPatch(
                summary="Model produced JSON that is not a patch object.",
                success=False,
                raw_response=raw_output,
                error_message="Expected a JSON object with 'summary' and 'files'.",
                timestamp=timestamp
            )

        summary = str(parsed.get("summary", "Automatic fix")).strip()
        raw_files = parsed.get("files", [])
        if not isinstance(raw_files, list):
            raw_files = [raw_files] if raw_files else []

        valid_patches: List[FilePatch] = []

        for item in raw_files:
            if not isinstance(item, dict):
                continue

            path = str(item.get("path", "")).strip()
            op = str(item.get("op", "modify")).lower().strip()
            content = str(item.get("content", ""))

            if not path:
                continue

            if op not in ("create", "modify", "delete"):
                op = "modify"

            # Subject constraint: Diffs are not accepted.
            if self._is_unified_diff(content):
                logger.error(f"Patch rejected: content for '{path}' is a diff, not a full file.")
                return StructuredPatch(
                    summary="Patch rejected: model returned a diff instead of a full file.",
                    success=False,
                    raw_response=raw_output,
                    error_message=f"Diff detected in '{path}'. Full-file content required by subject.",
                    timestamp=timestamp
                )

            # Map the model's path onto a real suspect path when that is unambiguous.
            canonical = self._canonical_path(path, file_contexts)
            valid_patches.append(FilePatch(
                path=canonical or path,
                op=op,
                content=content
            ))

        if not valid_patches:
            return StructuredPatch(
                summary="Patch rejected: no valid files specified in patch.",
                success=False,
                raw_response=raw_output,
                error_message="No valid file patches extracted from model output.",
                timestamp=timestamp
            )

        logger.info(f"Generated structured patch with {len(valid_patches)} files: '{summary}'")
        return StructuredPatch(
            summary=summary,
            files=valid_patches,
            success=True,
            raw_response=raw_output,
            timestamp=timestamp
        )

    def _canonical_path(self, path: str, file_contexts: Dict[str, str]) -> Optional[str]:
        """Resolve a model-written path to a real suspect path, when unambiguous.

        Only exact and whole-segment suffix matches count - never a bare 'endswith'
        against an unrelated file name.
        """
        if path in file_contexts:
            return path
        needle = path.lstrip("./")
        matches = [
            key for key in file_contexts
            if key == needle or key.endswith("/" + needle) or needle.endswith("/" + key)
        ]
        if len(matches) == 1:
            return matches[0]
        base_matches = [
            key for key in file_contexts
            if os.path.basename(key) == os.path.basename(needle)
        ]
        if len(base_matches) == 1:
            return base_matches[0]
        return None

    def _is_unified_diff(self, content: str) -> bool:
        """Detects if content is accidentally formatted as a git/unified diff."""
        lines = content.strip().splitlines()
        if not lines:
            return False

        first_few = lines[:6]
        if any("diff --git" in ln for ln in first_few):
            return True
        if (any(ln.startswith("--- ") for ln in first_few)
                and any(ln.startswith("+++ ") for ln in first_few)):
            return True
        if any(line.startswith("@@ -") for line in lines[:15]):
            return True

        return False
