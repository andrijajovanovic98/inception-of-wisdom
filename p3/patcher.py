"""
Inception-of-Wisdom (IoW) — Part 3: Wisdom Loop
Patcher: Structured JSON Patch Generator (Ollama Code Repair)
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict

try:
    import httpx
except ImportError:
    httpx = None

from p2.diagnostician import DiagnosticReport

logger = logging.getLogger("p3.patcher")

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = "qwen2.5-coder:1.5b"


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
        self.ollama_host = (ollama_host or DEFAULT_OLLAMA_HOST).rstrip("/")
        self.model_name = model_name
        self.request_timeout = request_timeout

    def generate_patch(
        self,
        diagnosis: DiagnosticReport,
        log_excerpt: str
    ) -> StructuredPatch:
        """Generates a structured JSON repair patch for the suspect files."""
        now = time.time()

        if not diagnosis.files:
            return StructuredPatch(
                summary="Patch generation failed: no suspect files identified in diagnosis.",
                success=False,
                error_message="No suspect files available for patching.",
                timestamp=now
            )

        # Read current contents of all suspect files
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
                error_message="Suspect files not found on disk.",
                timestamp=now
            )

        # Build prompt for the local coder model
        prompt = self._build_patch_prompt(diagnosis.summary, log_excerpt, file_contexts)

        # Call Ollama
        raw_output, call_error = self._call_ollama(prompt)

        if call_error:
            logger.warning(f"Ollama patch generation failed ({call_error}).")
            return StructuredPatch(
                summary="Patch generation failed due to model service error.",
                success=False,
                raw_response=raw_output,
                error_message=f"Model call failed: {call_error}",
                timestamp=now
            )

        # Parse and validate JSON structure
        return self._parse_patch_response(raw_output, file_contexts, now)

    def _build_patch_prompt(
        self,
        diagnostic_summary: str,
        log_excerpt: str,
        file_contexts: Dict[str, str]
    ) -> str:
        """Constructs an unambiguous prompt enforcing full-file JSON patches."""
        files_text = ""
        for path, code in file_contexts.items():
            files_text += f"\n--- File: {path} ---\n{code}\n"

        prompt = f"""You are an autonomous software repair agent.
A containerized service crashed. Fix the error in the suspect file.

DIAGNOSIS:
{diagnostic_summary}

ERROR LOG EXCERPT:
{log_excerpt.strip()}

CURRENT SOURCE CODE:
{files_text}

CRITICAL RULES (SUBJECT CONSTRAINTS):
1. Output MUST be strictly valid JSON and NOTHING ELSE.
2. The "content" field MUST be the COMPLETE, FULL-FILE post-change code.
3. DIFFS ARE NOT ACCEPTED (do NOT use '+' or '-' line diffs, unified diffs, or git diffs).
4. PLACEHOLDERS ARE STRICTLY FORBIDDEN (do NOT write '// TODO', '...', or omit code).
5. "op" must be "modify", "create", or "delete".

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

    def _call_ollama(self, prompt: str) -> tuple[Optional[str], Optional[str]]:
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
                "temperature": 0.2,
                "num_predict": 1024
            }
        }

        try:
            with httpx.Client(timeout=self.request_timeout) as client:
                res = client.post(url, json=payload)
                if res.status_code != 200:
                    return None, f"Ollama HTTP {res.status_code}: {res.text[:200]}"
                data = res.json()
                return data.get("response", "").strip(), None
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
                error_message="Empty response from LLM",
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

        summary = parsed.get("summary", "Automatic fix").strip()
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

            # Subject rule: op in {create, modify, delete}
            if op not in ["create", "modify", "delete"]:
                op = "modify"

            # Subject constraint: Diffs are not accepted!
            if self._is_unified_diff(content):
                logger.error(f"Patch rejected: content for '{path}' is a diff, not a full file.")
                return StructuredPatch(
                    summary="Patch rejected: model returned a diff instead of a full file.",
                    success=False,
                    raw_response=raw_output,
                    error_message=f"Diff detected in '{path}'. Full-file content required by subject.",
                    timestamp=timestamp
                )

            valid_patches.append(FilePatch(
                path=path,
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

    def _is_unified_diff(self, content: str) -> bool:
        """Detects if content is accidentally formatted as a git/unified diff."""
        lines = content.strip().splitlines()
        if not lines:
            return False

        first_few = "\n".join(lines[:6])
        if "diff --git" in first_few:
            return True
        if "--- " in first_few and "+++ " in first_few:
            return True
        if any(line.startswith("@@ -") for line in lines[:15]):
            return True

        return False

