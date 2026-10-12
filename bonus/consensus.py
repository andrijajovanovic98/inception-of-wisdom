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
from typing import Optional, List, Dict, Any, Sequence, Set
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
    # Not reached because the opinions pointed at different files - as opposed to no usable
    # opinion at all (the model unreachable, or both answers unusable), where nothing disagreed.
    diverged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ModelUnavailable(Exception):
    """The local model could not be asked (connection refused, timeout, HTTP error)."""


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
        request_timeout: float = 120.0
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
        """Queries the local Ollama instance with specialized system instructions and temperature.

        Raises ModelUnavailable when the model cannot be asked: swallowed into "no answer",
        an unreachable model read as two opinions that disagreed (seen live).
        """
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
                if resp.status != 200:
                    raise ModelUnavailable(f"Ollama HTTP {resp.status}")
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("response", "")
        except urllib.error.HTTPError as e:
            raise ModelUnavailable(f"Ollama HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ModelUnavailable(str(getattr(e, "reason", None) or e)) from e
        except ValueError as e:                                   # not JSON: an unusable answer
            logger.warning(f"Unreadable answer from Ollama in the consensus engine: {e}")
            return None

    def _opinion(
        self, prompt: str, system_prompt: str, temperature: float, context_chunks: List[Dict[str, Any]]
    ) -> DiagnosticReport:
        """One opinion, validated like the Analyst's diagnosis - or its explicit failure."""
        try:
            raw_resp = self._query_llm_prompt(prompt, system_prompt, temperature=temperature)
        except ModelUnavailable as e:
            logger.warning(f"Ollama generation failed in consensus engine: {e}")
            return self.base_diagnostician._create_unavailable_report({}, context_chunks, str(e), time.time())
        valid_files = {c.get("file_path", "") for c in context_chunks}
        return self.base_diagnostician._parse_and_validate_response(
            raw_resp, valid_files, context_chunks, time.time()
        )

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
        return self._opinion(prompt, system_prompt, 0.0, context_chunks)

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
        return self._opinion(prompt, system_prompt, 0.3, context_chunks)

    def evaluate_consensus(
        self,
        log_excerpt: str,
        top_k: Optional[int] = None,
        exclude_files: Sequence[str] = (),
    ) -> ConsensusReport:
        """Runs dual independent evaluations and checks if consensus is reached.

        exclude_files: indexed files the caller may never patch (the operator-owned
        iow.config.yml) - two opinions agreeing on one would only waste an attempt.
        """
        logger.info("Running Second Opinion dual-prompt consensus evaluation...")

        # 1. Retrieve candidate context
        context_chunks, _ = self.retriever.retrieve_for_crash(log_excerpt, top_k=top_k)
        if exclude_files:
            excluded = set(exclude_files)
            context_chunks = [c for c in context_chunks if c.get("file_path") not in excluded]

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
        if not files_a and not files_b:
            # Nothing to compare: neither prompt produced a usable diagnosis (the model is
            # unreachable, or both answers cited nothing real). That is the Analyst's explicit
            # failure, twice - not two opinions that disagreed (seen live with the model
            # stopped: "Opinion A suggested [], whereas Opinion B suggested []").
            consensus_summary = (
                "No vote: neither prompt produced a usable diagnosis "
                f"(A: {opinion_a.error_message}; B: {opinion_b.error_message})"
            )
            logger.warning(f"[KO] {consensus_summary}")
            return ConsensusReport(
                consensus_reached=False,
                agreed_files=[],
                consensus_summary=consensus_summary,
                opinion_a=opinion_a.to_dict(),
                opinion_b=opinion_b.to_dict(),
                confidence=0.0,
                error_message=consensus_summary,
            )

        def says(label: str, files: Set[str], opinion: DiagnosticReport) -> str:
            if files:
                return f"Opinion {label} suggested {sorted(files)}"
            return f"Opinion {label} produced no usable diagnosis ({opinion.error_message})"

        consensus_summary = (
            f"Consensus FAILED: the two opinions did not agree on a suspect file. "
            f"{says('A', files_a, opinion_a)}, whereas {says('B', files_b, opinion_b)}. "
            f"Blind patching aborted to prevent hallucinations."
        )
        logger.warning(f"[KO] {consensus_summary}")
        return ConsensusReport(
            consensus_reached=False,
            agreed_files=[],
            consensus_summary=consensus_summary,
            opinion_a=opinion_a.to_dict(),
            opinion_b=opinion_b.to_dict(),
            confidence=0.30,
            error_message="The opinions did not agree on a suspect file.",
            diverged=True,
        )
