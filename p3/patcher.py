"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Patcher: Structured JSON Patch Generator (Ollama Code Repair)

The model is the only source of a fix. There is no canned answer for any particular
bug: when the model truncates a file the patcher asks it again with a stricter prompt,
and when it still fails the attempt fails honestly.
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment,misc]

from p2.diagnostician import DiagnosticReport, _normalize_ollama_host

logger = logging.getLogger("p3.patcher")

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11436")
DEFAULT_MODEL = "qwen2.5-coder:1.5b"

# A full-file rewrite that keeps less than this share of the original is almost
# certainly a truncated generation rather than a deliberate deletion.
TRUNCATION_KEEP_RATIO = 0.5


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "success": self.success,
            "files": [f.to_dict() for f in self.files],
            "error_message": self.error_message,
            "timestamp": self.timestamp
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


class CodePatcher:
    """Generates structured code repairs using the local Ollama coder model (< 3B).
    Enforces full-file JSON patches and strictly rejects unified diffs.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        ollama_host: Optional[str] = None,
        model_name: str = DEFAULT_MODEL,
        request_timeout: float = 60.0
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
    ) -> StructuredPatch:
        """Generates a structured JSON repair patch for the suspect files.

        refusal_hint carries why the previous attempt was rejected, so a retry is
        never a byte-identical request.
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

        prompt = self._build_patch_prompt(
            diagnosis.summary, log_excerpt, file_contexts, refusal_hint
        )
        raw_output, call_error = self._call_ollama(prompt)

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
        retry_raw, retry_err = self._call_ollama(retry_prompt)
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
    # Prompting
    # ------------------------------------------------------------------
    def _build_patch_prompt(
        self,
        diagnostic_summary: str,
        log_excerpt: str,
        file_contexts: Dict[str, str],
        refusal_hint: Optional[str] = None,
    ) -> str:
        """Constructs an unambiguous prompt enforcing full-file JSON patches."""
        files_text = ""
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
    def _call_ollama(self, prompt: str) -> Tuple[Optional[str], Optional[str]]:
        """Sends code repair request to Ollama with JSON mode."""
        if httpx is None:
            return None, "httpx library not installed"

        url = f"{self.ollama_host}/api/generate"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 4096
            }
        }

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
