"""
Inception-of-Wisdom (IoW) — Chapter VI: Bonus Part
Tiny Symptom Classifier & Known-Crash Patch Cache: Skips LLM on Known-Symptom Crashes
"""

from __future__ import annotations

import os
import re
import json
import time
import hashlib
import logging
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, asdict, field

logger = logging.getLogger("bonus.classifier")

DEFAULT_STORE_PATH = os.environ.get("IOW_CLASSIFIER_STORE", "/tmp/iow_cache/classifier_store.json")

# Traceback parsing regexes
TRACEBACK_FRAME_REGEX = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+([a-zA-Z0-9_]+))?')
EXCEPTION_LINE_REGEX = re.compile(r'([a-zA-Z0-9_]*(?:Error|Exception|CRITICAL|Fatal)):\s*(.*)')


@dataclass
class SymptomFeatures:
    """Normalized feature vector extracted from a crash log."""
    error_type: str
    offending_file: Optional[str]
    offending_line: Optional[int]
    offending_func: Optional[str]
    normalized_message: str
    raw_signature: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ClassifierMatch:
    """Result of classifying an incoming crash event."""
    matched: bool
    confidence: float
    symptom_id: Optional[str] = None
    cached_patch: Optional[Dict[str, Any]] = None
    explanation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TinySymptomClassifier:
    """Subject Bonus requirement:
    'Train a tiny classifier on past events to skip the LLM entirely on known-symptom crashes.'
    Maintains a learned store of verified crash symptoms and their verified solutions.
    """

    def __init__(
        self,
        store_path: Optional[str] = None,
        confidence_threshold: float = 0.85
    ):
        self.store_path = os.path.abspath(store_path or DEFAULT_STORE_PATH)
        self.confidence_threshold = confidence_threshold
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.total_bypasses: int = 0

        self._load_store()

    def _load_store(self) -> None:
        """Loads cached symptoms from disk if present."""
        if not os.path.isfile(self.store_path):
            self.entries = {}
            return

        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.entries = data.get("entries", {})
                self.total_bypasses = data.get("total_bypasses", 0)
                logger.info(f"Loaded {len(self.entries)} learned crash symptoms from {self.store_path}")
        except Exception as e:
            logger.error(f"Failed to load classifier store from {self.store_path}: {e}")
            self.entries = {}

    def _save_store(self) -> None:
        """Persists learned symptoms atomically to disk."""
        try:
            os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
            tmp_file = f"{self.store_path}.tmp.{os.getpid()}"
            payload = {
                "updated_at": time.time(),
                "total_entries": len(self.entries),
                "total_bypasses": self.total_bypasses,
                "entries": self.entries
            }
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_file, self.store_path)
            logger.debug(f"Saved {len(self.entries)} classifier entries to {self.store_path}")
        except Exception as e:
            logger.error(f"Failed to save classifier store to {self.store_path}: {e}")

    def extract_features(self, log_excerpt: str, raw_signature: Optional[str] = None) -> SymptomFeatures:
        """Extracts structured, normalized symptom features from crash log."""
        if not log_excerpt:
            sig = raw_signature or "unknown"
            return SymptomFeatures(
                error_type="UnknownError",
                offending_file=None,
                offending_line=None,
                offending_func=None,
                normalized_message="",
                raw_signature=sig
            )

        error_type = "UnknownException"
        norm_msg = ""
        offending_file = None
        offending_line = None
        offending_func = None

        # 1. Traceback frames (last frame is most specific to the error)
        frames = TRACEBACK_FRAME_REGEX.findall(log_excerpt)
        if frames:
            last_frame = frames[-1]
            offending_file = last_frame[0]
            try:
                offending_line = int(last_frame[1])
            except ValueError:
                offending_line = None
            offending_func = last_frame[2] if len(last_frame) > 2 and last_frame[2] else None

        # 2. Exception line
        for line in reversed(log_excerpt.splitlines()):
            m = EXCEPTION_LINE_REGEX.search(line)
            if m:
                error_type = m.group(1).strip()
                raw_msg = m.group(2).strip()
                # Normalize message: strip memory addresses (0x7f...), variable numbers
                norm_msg = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', raw_msg)
                norm_msg = re.sub(r'\b\d+\b', 'NUM', norm_msg)
                break

        # Generate deterministic composite symptom key
        composite = f"{error_type}:{offending_file}:{offending_func}:{norm_msg}"
        sig = hashlib.sha256(composite.encode("utf-8")).hexdigest()[:16]

        return SymptomFeatures(
            error_type=error_type,
            offending_file=offending_file,
            offending_line=offending_line,
            offending_func=offending_func,
            normalized_message=norm_msg,
            raw_signature=sig
        )

    def classify(self, log_excerpt: str, raw_signature: Optional[str] = None) -> ClassifierMatch:
        """Evaluates incoming crash against the learned knowledge base.
        Returns a ClassifierMatch with cached_patch if a known fix is found with high confidence.
        """
        features = self.extract_features(log_excerpt, raw_signature)
        symptom_key = features.raw_signature

        # Exact match check
        if symptom_key in self.entries:
            entry = self.entries[symptom_key]
            confidence = entry.get("confidence", 1.0)
            if confidence >= self.confidence_threshold:
                self.total_bypasses += 1
                entry["times_applied"] = entry.get("times_applied", 0) + 1
                entry["last_applied_at"] = time.time()
                self._save_store()

                logger.info(
                    f"🎯 Classifier MATCH (Exact)! Key=[{symptom_key}], Error=[{features.error_type} in {features.offending_file}], "
                    f"Confidence={confidence:.2f} -> Bypassing LLM generation"
                )
                return ClassifierMatch(
                    matched=True,
                    confidence=confidence,
                    symptom_id=symptom_key,
                    cached_patch=entry.get("patch"),
                    explanation=f"Exact match on verified symptom: {features.error_type} in {features.offending_file}:{features.offending_func}"
                )

        # Fuzzy / Nearest-Neighbor match check across learned entries
        best_match_key = None
        best_score = 0.0
        best_entry = None

        for k, entry in self.entries.items():
            stored_feat = entry.get("features", {})
            score = 0.0

            # Matching exception type: +40%
            if stored_feat.get("error_type") == features.error_type and features.error_type != "UnknownException":
                score += 0.40

            # Matching target file: +30%
            if stored_feat.get("offending_file") and stored_feat.get("offending_file") == features.offending_file:
                score += 0.30

            # Matching function/scope: +20%
            if stored_feat.get("offending_func") and stored_feat.get("offending_func") == features.offending_func:
                score += 0.20

            # Matching normalized message: +10%
            if stored_feat.get("normalized_message") and stored_feat.get("normalized_message") == features.normalized_message:
                score += 0.10

            if score > best_score:
                best_score = score
                best_match_key = k
                best_entry = entry

        if best_score >= self.confidence_threshold and best_entry is not None:
            self.total_bypasses += 1
            best_entry["times_applied"] = best_entry.get("times_applied", 0) + 1
            best_entry["last_applied_at"] = time.time()
            self._save_store()

            logger.info(
                f"🎯 Classifier MATCH (Fuzzy {best_score:.2f})! Key=[{best_match_key}], "
                f"Error=[{features.error_type}] -> Bypassing LLM generation"
            )
            return ClassifierMatch(
                matched=True,
                confidence=best_score,
                symptom_id=best_match_key,
                cached_patch=best_entry.get("patch"),
                explanation=f"Fuzzy match (score {best_score:.2f}) on verified symptom: {features.error_type} in {features.offending_file}"
            )

        # No confident match
        return ClassifierMatch(
            matched=False,
            confidence=best_score,
            symptom_id=best_match_key,
            explanation="No known symptom pattern above confidence threshold; routing to LLM."
        )

    def record_successful_heal(
        self,
        log_excerpt: str,
        patch_dict: Dict[str, Any],
        raw_signature: Optional[str] = None
    ) -> str:
        """Reinforcement: stores or updates a verified patch for this symptom."""
        features = self.extract_features(log_excerpt, raw_signature)
        symptom_key = features.raw_signature

        existing = self.entries.get(symptom_key)
        if existing:
            existing["confidence"] = min(1.0, existing.get("confidence", 0.9) + 0.05)
            existing["success_count"] = existing.get("success_count", 0) + 1
            existing["patch"] = patch_dict
            existing["last_success_at"] = time.time()
        else:
            self.entries[symptom_key] = {
                "symptom_id": symptom_key,
                "features": features.to_dict(),
                "patch": patch_dict,
                "confidence": 1.0,
                "success_count": 1,
                "failure_count": 0,
                "times_applied": 0,
                "created_at": time.time(),
                "last_success_at": time.time()
            }

        self._save_store()
        logger.info(f"✨ Learned verified crash symptom [{symptom_key}]: {features.error_type} in {features.offending_file}")
        return symptom_key

    def record_failure(self, symptom_id: str) -> None:
        """Penalizes a symptom pattern if its cached patch failed verification."""
        if symptom_id in self.entries:
            entry = self.entries[symptom_id]
            entry["failure_count"] = entry.get("failure_count", 0) + 1
            entry["confidence"] = max(0.0, entry.get("confidence", 1.0) - 0.25)
            logger.warning(
                f"Penalized classifier symptom [{symptom_id}]: confidence reduced to {entry['confidence']:.2f}"
            )
            # Remove if completely untrusted
            if entry["confidence"] < 0.40:
                logger.info(f"Evicting untrusted symptom [{symptom_id}] from classifier store.")
                del self.entries[symptom_id]
            self._save_store()

    def get_stats(self) -> Dict[str, Any]:
        """Returns statistics for the Dashboard."""
        return {
            "total_learned_symptoms": len(self.entries),
            "total_fast_path_bypasses": self.total_bypasses,
            "confidence_threshold": self.confidence_threshold,
            "store_path": self.store_path,
            "symptoms": [
                {
                    "symptom_id": k,
                    "error_type": v.get("features", {}).get("error_type"),
                    "file": v.get("features", {}).get("offending_file"),
                    "func": v.get("features", {}).get("offending_func"),
                    "confidence": v.get("confidence"),
                    "times_applied": v.get("times_applied", 0),
                    "success_count": v.get("success_count", 0)
                }
                for k, v in self.entries.items()
            ]
        }

