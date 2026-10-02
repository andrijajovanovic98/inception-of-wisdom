"""
Inception-of-Wisdom (IoW) - Part 2: Analyst
Diagnostician: Structured LLM Crash Diagnosis (Ollama JSON output)
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
from typing import List, Dict, Optional, Any, Sequence
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

RETRY_NOTE = (
    "\nYOUR PREVIOUS ANSWER WAS UNUSABLE (invalid JSON, or no file from the allowed list).\n"
    'Answer again with BOTH keys: "files" first, then a plain-words "summary" without any\n'
    "double quotes or code inside it.\n"
)


_FILES_RE = re.compile(r'"files"\s*:\s*(\[[^\]]*\])', re.DOTALL)
_SUMMARY_RE = re.compile(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)', re.DOTALL)


def _salvage_cut_json(raw: str) -> Optional[Dict[str, Any]]:
    """Recover {"files", "summary"} from an answer cut off mid-JSON.

    Only a *complete* files array counts, and every path in it is still checked
    against the index afterwards - nothing is guessed, a truncated tail is dropped.
    """
    files_match = _FILES_RE.search(raw or "")
    summary_match = _SUMMARY_RE.search(raw or "")
    if not files_match or not summary_match:
        return None
    try:
        files = json.loads(files_match.group(1))
    except json.JSONDecodeError:
        return None
    summary = summary_match.group(1).replace('\\"', '"').strip()
    if not isinstance(files, list) or not summary:
        return None
    return {"files": files, "summary": summary}


def _describe_chunk(chunk: Dict[str, Any]) -> str:
    """How a chunk is named to the model.

    Synthetic chunk names (module_preamble, module_block_L40) are index bookkeeping,
    not identifiers - shown raw, a small model "fixed" a crash by calling
    module_preamble() as if it were a function.
    """
    name = str(chunk.get("symbol_name") or "")
    kind = str(chunk.get("symbol_type") or "")
    if kind == "module":
        if name == "__main__":
            return 'the if __name__ == "__main__": block (startup code)'
        return "module-level code (imports, globals and startup statements)"
    if kind == "file":
        return "the whole file"
    return f"{kind} {name}"


@dataclass
class DiagnosticReport:
    """Represents a structured diagnosis of a target crash event."""
    success: bool
    summary: str                                      # One-paragraph explanation of the root cause
    files: List[str] = field(default_factory=list)    # List of verified suspect files
    candidate_chunks: List[Dict[str, Any]] = field(default_factory=list)
    raw_response: Optional[str] = None
    error_message: Optional[str] = None
    # 'llm'          - the local model produced and validated this diagnosis
    # 'unavailable'  - the model could not be reached; nothing was inferred
    # 'unusable'     - the model answered but cited nothing real
    mode: str = "llm"
    # Deterministic traceback facts, shown for context but never treated as a diagnosis.
    traceback_hint: Optional[Dict[str, Any]] = None
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
        request_timeout: float = 120.0
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
        top_k: Optional[int] = None,
        exclude_files: Optional[Sequence[str]] = None,
    ) -> DiagnosticReport:
        """Runs the complete diagnostic pipeline.

        exclude_files: indexed files the caller can never act on (the Wisdom Loop
        passes the operator-owned iow.config.yml) - offering them to the model would
        only waste an attempt on a file that may not be patched.

        1. Retrieves relevant code chunks and extracts traceback symbols.
        2. Prompts local LLM using JSON format constraint.
        3. Validates and sanitizes model output against real indexed files.
        """
        now = time.time()

        # Step 1: Retrieval & symbol extraction
        candidate_chunks, extracted_symbols = self.retriever.retrieve_for_crash(
            log_excerpt, top_k=top_k
        )

        # Subject: "The diagnosis must cite real files from the index. It must not
        # invent paths." Only indexed paths are offered to the model - never the bare
        # basenames scraped out of a traceback, which are not real index entries.
        if exclude_files:
            excluded = set(exclude_files)
            candidate_chunks = [c for c in candidate_chunks if c.get("file_path") not in excluded]
        valid_known_files = {
            c.get("file_path", "") for c in candidate_chunks if c.get("file_path")
        }

        if not valid_known_files:
            return DiagnosticReport(
                success=False,
                summary="",
                files=[],
                candidate_chunks=candidate_chunks,
                error_message=(
                    "Diagnosis failed: retrieval returned no indexed code for this crash, "
                    "so there is no real file to cite."
                ),
                mode="unusable",
                traceback_hint=extracted_symbols,
                timestamp=now,
            )

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
            logger.error(
                "Local model unreachable (%s). The Analyst does not guess - "
                "surfacing an explicit failure.", call_error
            )
            return self._create_unavailable_report(
                extracted_symbols, candidate_chunks, call_error, now
            )

        # Step 4: Parse and validate JSON response
        report = self._parse_and_validate_response(
            raw_output=raw_output,
            valid_files=valid_known_files,
            candidate_chunks=candidate_chunks,
            timestamp=now
        )

        # One more ask when the answer was unusable. The classic small-model slip: a
        # bare quote inside the summary ("os.environ["...) ends the string, and the JSON
        # grammar closes the object before "files" is ever written.
        if not report.success and report.mode == "unusable" and report.raw_response is not None:
            retry_raw, retry_error = self._call_ollama(prompt + RETRY_NOTE)
            if not retry_error:
                retried = self._parse_and_validate_response(
                    raw_output=retry_raw,
                    valid_files=valid_known_files,
                    candidate_chunks=candidate_chunks,
                    timestamp=now,
                )
                if retried.success:
                    return retried

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
                f"Part: {_describe_chunk(chunk)}\n"
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
3. The "summary" must clearly explain the root cause in plain words. Do not put code or
   double quotes inside it.

RESPONSE FORMAT (JSON, "files" first):
{{
  "files": ["exact/path/to/file.py"],
  "summary": "Concise root cause explanation paragraph."
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
                # A one-paragraph summary plus the file list; 300 cut answers off
                # mid-JSON on a chatty small model.
                "num_predict": 768
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
                mode="unusable",
                timestamp=timestamp
            )

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as e:
            salvaged = _salvage_cut_json(raw_output)
            if salvaged is not None:
                logger.warning(
                    f"Diagnosis JSON was cut off ({e}); using its complete 'files' list and "
                    "the summary written so far."
                )
                parsed = salvaged
            else:
                logger.error(f"Failed to parse LLM JSON diagnosis: {e}. Raw: {raw_output[:200]}")
                return DiagnosticReport(
                    success=False,
                    summary="",
                    files=[],
                    candidate_chunks=candidate_chunks,
                    raw_response=raw_output,
                    error_message=f"Invalid JSON produced by model: {e}",
                    mode="unusable",
                    timestamp=timestamp
                )

        if not isinstance(parsed, dict):
            return DiagnosticReport(
                success=False,
                summary="",
                files=[],
                candidate_chunks=candidate_chunks,
                raw_response=raw_output,
                error_message="Diagnosis failed: the model did not return a JSON object.",
                mode="unusable",
                timestamp=timestamp
            )

        # A small model answers null, numbers or nested objects often enough that
        # none of it may reach .strip() unchecked.
        raw_summary = parsed.get("summary")
        summary = raw_summary.strip() if isinstance(raw_summary, str) else ""
        files = parsed.get("files", [])

        if not isinstance(files, list):
            files = [files] if files else []
        files = [f for f in files if isinstance(f, str)]

        # Sanitize suspect files: each answer must resolve to exactly one indexed path
        # (Subject: must cite real files, must not invent paths).
        verified_files: List[str] = []
        for f in files:
            resolved = self._resolve_indexed_path(str(f).strip(), valid_files)
            if resolved and resolved not in verified_files:
                verified_files.append(resolved)

        # Subject: surface explicit failure if model returns no usable file or empty summary
        if not summary or not verified_files:
            err_msg = (
                "Diagnosis failed: the model produced an empty summary or cited no file "
                "from the index. The pipeline does not guess."
            )
            return DiagnosticReport(
                success=False,
                summary=summary,
                files=verified_files,
                candidate_chunks=candidate_chunks,
                raw_response=raw_output,
                error_message=err_msg,
                mode="unusable",
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

    def _resolve_indexed_path(self, answer: str, valid_files: set) -> Optional[str]:
        """Map one model answer onto a single real indexed path, or nothing.

        An ambiguous basename (two files called app.py) resolves to nothing rather
        than silently selecting both, which used to report one file twice.
        """
        if not answer:
            return None
        if answer in valid_files:
            return answer

        needle = answer.lstrip("./")
        suffix_matches = [
            v for v in valid_files
            if v == needle or v.endswith("/" + needle) or needle.endswith("/" + v)
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0]

        base_matches = [
            v for v in valid_files
            if os.path.basename(v) == os.path.basename(needle)
        ]
        if len(base_matches) == 1:
            return base_matches[0]

        if base_matches:
            logger.warning(
                "Model cited '%s', which matches %d indexed files (%s) - ambiguous, "
                "dropping it rather than guessing.",
                answer, len(base_matches), base_matches,
            )
        else:
            logger.warning("Model cited '%s', which is not in the index - dropped.", answer)
        return None

    def _create_unavailable_report(
        self,
        extracted_symbols: Dict[str, Any],
        candidate_chunks: List[Dict[str, Any]],
        reason: str,
        timestamp: float
    ) -> DiagnosticReport:
        """Explicit failure when the local model cannot be reached.

        The traceback facts are attached for the dashboard, but this is never
        reported as a successful diagnosis: the subject requires the Analyst to
        surface an explicit failure mode rather than guess a culprit.
        """
        return DiagnosticReport(
            success=False,
            summary="",
            files=[],
            candidate_chunks=candidate_chunks,
            error_message=(
                f"Diagnosis unavailable: the local LLM at {self.ollama_host} could not be "
                f"reached ({reason}). Start it (`make setup` for the host runtime, or the "
                "iow_ollama container) and retry - the pipeline does not guess a culprit."
            ),
            mode="unavailable",
            traceback_hint=extracted_symbols,
            timestamp=timestamp
        )
