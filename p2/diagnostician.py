"""
Inception-of-Wisdom (IoW) - Part 2: Analyst
Diagnostician: Structured LLM Crash Diagnosis (Ollama JSON output)
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
    httpx = None  # type: ignore[assignment,misc]

from p2.retriever import CodeRetriever

logger = logging.getLogger("p2.diagnostician")

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11436")


def _normalize_ollama_host(host: str) -> str:
    host = (host or "").strip().rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host or "http://127.0.0.1:11436"


DEFAULT_MODEL = "qwen2.5-coder:1.5b"


@dataclass
class DiagnosticReport:
    """Represents a structured diagnosis of a target crash event."""
    success: bool
    summary: str                                      # One-paragraph explanation of the root cause
    files: List[str] = field(default_factory=list)    # List of verified suspect files
    candidate_chunks: List[Dict[str, Any]] = field(default_factory=list)
    raw_response: Optional[str] = None
    error_message: Optional[str] = None
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CrashDiagnostician:
    """Asks the local Ollama LLM (< 3B) for a structured diagnosis of a crash event.
    Subject requirement: JSON output with summary and suspect files.
    The model must cite real indexed files and must not guess.
    """

    def __init__(
        self,
        retriever: CodeRetriever,
        ollama_host: Optional[str] = None,
        model_name: str = DEFAULT_MODEL,
        request_timeout: float = 45.0
    ):
        self.retriever = retriever
        self.ollama_host = _normalize_ollama_host(ollama_host or DEFAULT_OLLAMA_HOST)
        self.model_name = model_name
        self.request_timeout = request_timeout

    def diagnose_crash(
        self,
        log_excerpt: str,
        exit_code: Optional[int] = None,
        status: str = "crashed",
        top_k: int = 3
    ) -> DiagnosticReport:
        """Runs the complete diagnostic pipeline:
        1. Retrieves relevant code chunks and extracts traceback symbols.
        2. Prompts local LLM using JSON format constraint.
        3. Validates and sanitizes model output against real indexed files.
        """
        now = time.time()

        # Step 1: Retrieval & symbol extraction
        candidate_chunks, extracted_symbols = self.retriever.retrieve_for_crash(
            log_excerpt, top_k=top_k
        )

        valid_known_files = set(extracted_symbols.get("files", []))
        for c in candidate_chunks:
            valid_known_files.add(c.get("file_path", ""))

        # Step 2: Build the prompt for the < 3B coder model
        prompt = self._build_diagnosis_prompt(
            log_excerpt=log_excerpt,
            exit_code=exit_code,
            status=status,
            candidate_chunks=candidate_chunks,
            valid_files=list(valid_known_files)
        )

        # Step 3: Call Ollama with JSON mode
        raw_output, call_error = self._call_ollama(prompt)

        if call_error:
            logger.warning(f"Ollama call failed ({call_error}). Attempting deterministic fallback...")
            return self._create_fallback_report(
                extracted_symbols, candidate_chunks, call_error, now
            )

        # Step 4: Parse and validate JSON response
        report = self._parse_and_validate_response(
            raw_output=raw_output,
            valid_files=valid_known_files,
            candidate_chunks=candidate_chunks,
            timestamp=now
        )

        return report

    def _build_diagnosis_prompt(
        self,
        log_excerpt: str,
        exit_code: Optional[int],
        status: str,
        candidate_chunks: List[Dict[str, Any]],
        valid_files: List[str]
    ) -> str:
        """Constructs a strict, concise prompt tailored for small coder models (< 3B)."""
        chunks_context = ""
        for i, chunk in enumerate(candidate_chunks, 1):
            chunks_context += (
                f"\n--- Code Snippet #{i} ---\n"
                f"File: {chunk.get('file_path')}\n"
                f"Symbol: {chunk.get('symbol_name')} ({chunk.get('symbol_type')})\n"
                f"Lines: {chunk.get('start_line')}-{chunk.get('end_line')}\n"
                f"Code:\n{chunk.get('content')}\n"
            )

        prompt = f"""You are an autonomous reliability engineer analyzing a service crash.
Given the container crash log and the retrieved source code snippets, diagnose the failure.

CRASH DETAILS:
Status: {status}
Exit Code: {exit_code}
Error Log:
{log_excerpt.strip()}

RELEVANT CODE CHUNKS:
{chunks_context}

ALLOWED SUSPECT FILES:
{json.dumps(valid_files)}

TASK:
Identify which file is responsible and explain in ONE concise paragraph what looks wrong.
CRITICAL CONSTRAINTS:
1. Output MUST be valid JSON and NOTHING ELSE.
2. In the "files" array, cite ONLY real files from the ALLOWED SUSPECT FILES list above. Do NOT invent paths.
3. The "summary" must clearly explain the root cause.

RESPONSE FORMAT (JSON):
{{
  "summary": "Concise root cause explanation paragraph.",
  "files": ["exact/path/to/file.py"]
}}
"""
        return prompt

    def _call_ollama(self, prompt: str) -> tuple[Optional[str], Optional[str]]:
        """Sends generation request to Ollama with format='json'."""
        if httpx is None:
            return None, "httpx library not installed"

        url = f"{self.ollama_host}/api/generate"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.1,  # Low temperature for deterministic output
                "num_predict": 300
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

    def _parse_and_validate_response(
        self,
        raw_output: Optional[str],
        valid_files: set[str],
        candidate_chunks: List[Dict[str, Any]],
        timestamp: float
    ) -> DiagnosticReport:
        """Validates that the LLM response is valid JSON and strictly contains real files."""
        if not raw_output:
            return DiagnosticReport(
                success=False,
                summary="",
                files=[],
                candidate_chunks=candidate_chunks,
                error_message="Ollama returned an empty response",
                timestamp=timestamp
            )

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM JSON diagnosis: {e}. Raw: {raw_output[:200]}")
            return DiagnosticReport(
                success=False,
                summary="",
                files=[],
                candidate_chunks=candidate_chunks,
                raw_response=raw_output,
                error_message=f"Invalid JSON produced by model: {e}",
                timestamp=timestamp
            )

        summary = parsed.get("summary", "").strip()
        files = parsed.get("files", [])

        if not isinstance(files, list):
            files = [files] if files else []

        # Sanitize suspect files: must exist in index (Subject: must not invent paths)
        verified_files = []
        for f in files:
            norm = str(f).strip()
            # Direct match or basename match
            for v in valid_files:
                if norm == v or os.path.basename(norm) == os.path.basename(v):
                    if v not in verified_files:
                        verified_files.append(v)

        # Subject: surface explicit failure if model returns no usable file or empty summary
        if not summary or not verified_files:
            err_msg = (
                "Diagnosis failed: model produced empty summary or unverified files. "
                "The pipeline does not guess."
            )
            return DiagnosticReport(
                success=False,
                summary=summary,
                files=verified_files,
                candidate_chunks=candidate_chunks,
                raw_response=raw_output,
                error_message=err_msg,
                timestamp=timestamp
            )

        logger.info(f"Structured diagnosis SUCCESS: files={verified_files}, summary='{summary[:80]}...'")
        return DiagnosticReport(
            success=True,
            summary=summary,
            files=verified_files,
            candidate_chunks=candidate_chunks,
            raw_response=raw_output,
            timestamp=timestamp
        )

    def _create_fallback_report(
        self,
        extracted_symbols: Dict[str, Any],
        candidate_chunks: List[Dict[str, Any]],
        reason: str,
        timestamp: float
    ) -> DiagnosticReport:
        """Deterministic fallback when LLM service is offline or unreachable."""
        suspect_files = extracted_symbols.get("files", [])
        error_type = extracted_symbols.get("error_type")
        error_msg = extracted_symbols.get("error_msg")
        funcs = extracted_symbols.get("functions", [])

        if suspect_files:
            summary = (
                f"Deterministic diagnosis: {error_type or 'Exception'} occurred in "
                f"{', '.join(funcs) if funcs else 'target'} ({error_msg or 'runtime crash'})."
            )
            return DiagnosticReport(
                success=True,
                summary=summary,
                files=suspect_files,
                candidate_chunks=candidate_chunks,
                raw_response="fallback_deterministic",
                timestamp=timestamp
            )

        return DiagnosticReport(
            success=False,
            summary="",
            files=[],
            candidate_chunks=candidate_chunks,
            error_message=f"LLM diagnosis unavailable ({reason}) and no deterministic traceback found.",
            timestamp=timestamp
        )
