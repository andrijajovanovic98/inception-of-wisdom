"""
Inception-of-Wisdom (IoW) - Chapter VI: Bonus Part
Second Opinion Diagnostician: Dual-Prompt Consensus Voting Engine
Subject requirement: 'Add a second opinion mode where two prompts vote on the
same diagnosis and only an agreement triggers the patch.'
"""

from __future__ import annotations

import os
import json
import time
import logging
import urllib.request
import urllib.error
from typing import Optional, List, Dict, Any, Set
from dataclasses import dataclass, asdict

from p2.diagnostician import CrashDiagnostician, DiagnosticReport, _normalize_ollama_host
from p2.retriever import CodeRetriever

logger = logging.getLogger("bonus.consensus")

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:1.5b")
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11436")


@dataclass
class ConsensusReport:
    """Outcome of dual-prompt consensus evaluation."""
    consensus_reached: bool
    agreed_files: List[str]
    consensus_summary: str
    opinion_a: Dict[str, Any]
    opinion_b: Dict[str, Any]
    confidence: float
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SecondOpinionEngine:
    """Subject Bonus requirement:
    Executes two independent diagnostic evaluations with differing analytical angles
    and prompts. Only when both independent opinions agree on the suspect file(s)
    does the system proceed with patching.
    """

    def __init__(
        self,
        retriever: CodeRetriever,
        model_name: str = DEFAULT_MODEL,
        ollama_host: str = DEFAULT_OLLAMA_HOST,
        request_timeout: float = 45.0
    ):
        self.retriever = retriever
        self.model_name = model_name
        self.ollama_host = _normalize_ollama_host(ollama_host)
        self.request_timeout = request_timeout

        # Base diagnostician for standard calls
        self.base_diagnostician = CrashDiagnostician(
            retriever=retriever,
            model_name=model_name,
            ollama_host=self.ollama_host,
            request_timeout=request_timeout
        )

    def _query_llm_prompt(self, prompt: str, system_prompt: str, temperature: float = 0.0) -> Optional[str]:
        """Queries the local Ollama instance with specialized system instructions and temperature."""
        url = f"{self.ollama_host}/api/generate"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": temperature,
                "top_p": 0.9,
                "num_predict": 512
            }
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                if resp.status == 200:
                    raw_data = resp.read().decode("utf-8")
                    data = json.loads(raw_data)
                    return data.get("response", "")
                return None
        except Exception as e:
            logger.warning(f"Ollama generation failed in consensus engine: {e}")
            return None

    def get_opinion_a(self, log_excerpt: str, context_chunks: List[Dict[str, Any]]) -> DiagnosticReport:
        """Opinion A: Conservative, localized stack-frame analyzer (Zero-shot, temperature 0.0)."""
        system_prompt = (
            "You are a conservative runtime diagnostic auditor. Focus strictly on the bottom-most stack "
            "frame, explicit exception messages, and direct syntax/runtime crashes. Output strict JSON: "
            '{"summary": "...", "files": ["relative/file/path"]}. CITE ONLY REAL FILES FROM CONTEXT.'
        )

        chunks_text = "\n\n".join([
            f"--- File: {c['file_path']} (lines {c['start_line']}-{c['end_line']}) ---\n{c['content']}"
            for c in context_chunks
        ])

        prompt = (
            f"CRASH LOG:\n{log_excerpt}\n\nAVAILABLE CODE CONTEXT:\n{chunks_text}\n\n"
            "Identify suspect file:"
        )
        raw_resp = self._query_llm_prompt(prompt, system_prompt, temperature=0.0)

        valid_files = {c.get("file_path", "") for c in context_chunks}
        return self.base_diagnostician._parse_and_validate_response(
            raw_resp, valid_files, context_chunks, time.time()
        )

    def get_opinion_b(self, log_excerpt: str, context_chunks: List[Dict[str, Any]]) -> DiagnosticReport:
        """Opinion B: Architectural and caller-contract analyzer (Temperature 0.3)."""
        system_prompt = (
            "You are a systems architecture reliability auditor. Analyze the broader caller context, "
            "unhandled preconditions, and functional contracts that led to this crash. "
            'Output strict JSON: {"summary": "...", "files": ["relative/file/path"]}. CITE ONLY REAL FILES.'
        )

        chunks_text = "\n\n".join([
            f"--- File: {c['file_path']} (lines {c['start_line']}-{c['end_line']}) ---\n{c['content']}"
            for c in context_chunks
        ])

        prompt = (
            f"INCIDENT TRACE:\n{log_excerpt}\n\nCODE REPOSITORY SLICES:\n{chunks_text}\n\n"
            "Identify offending file:"
        )
        raw_resp = self._query_llm_prompt(prompt, system_prompt, temperature=0.3)

        valid_files = {c.get("file_path", "") for c in context_chunks}
        return self.base_diagnostician._parse_and_validate_response(
            raw_resp, valid_files, context_chunks, time.time()
        )

    def evaluate_consensus(
        self,
        log_excerpt: str,
        top_k: int = 3
    ) -> ConsensusReport:
        """Runs dual independent evaluations and checks if consensus is reached."""
        logger.info("⚖️ Running Second Opinion dual-prompt consensus evaluation...")

        # 1. Retrieve candidate context
        context_chunks, _ = self.retriever.retrieve_for_crash(log_excerpt, top_k=top_k)

        # 2. Get Opinion A and Opinion B
        opinion_a = self.get_opinion_a(log_excerpt, context_chunks)
        opinion_b = self.get_opinion_b(log_excerpt, context_chunks)

        files_a: Set[str] = set(opinion_a.files)
        files_b: Set[str] = set(opinion_b.files)

        # 3. Consensus Check
        common_files = list(files_a.intersection(files_b))

        if common_files:
            # Full or partial agreement on target file
            consensus_summary = (
                f"Consensus REACHED: Both independent analytical opinions agreed that {common_files} "
                f"is the root cause location. Primary cause: {opinion_a.summary}"
            )
            confidence = 0.95 if files_a == files_b else 0.80
            logger.info(f"[OK] Consensus REACHED on files: {common_files} (confidence: {confidence})")

            return ConsensusReport(
                consensus_reached=True,
                agreed_files=common_files,
                consensus_summary=consensus_summary,
                opinion_a=opinion_a.to_dict(),
                opinion_b=opinion_b.to_dict(),
                confidence=confidence
            )
        else:
            # Disagreement!
            consensus_summary = (
                f"Consensus FAILED: Independent opinions disagreed on suspect files. "
                f"Opinion A suggested {list(files_a)}, whereas Opinion B suggested {list(files_b)}. "
                f"Blind patching aborted to prevent hallucinations."
            )
            logger.warning(f"[KO] Consensus FAILED: {consensus_summary}")

            return ConsensusReport(
                consensus_reached=False,
                agreed_files=[],
                consensus_summary=consensus_summary,
                opinion_a=opinion_a.to_dict(),
                opinion_b=opinion_b.to_dict(),
                confidence=0.30,
                error_message="Models diverged on suspect file identification."
            )
