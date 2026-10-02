"""
Inception-of-Wisdom (IoW) - Chapter VI: Bonus Part
Tiny Symptom Classifier & Known-Crash Patch Cache: Skips LLM on Known-Symptom Crashes
"""

from __future__ import annotations

import os
import re
import json
import time
import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

logger = logging.getLogger("bonus.classifier")

DEFAULT_STORE_PATH = os.environ.get("IOW_CLASSIFIER_STORE", "/tmp/iow/classifier_store.json")

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

        # 1. Traceback frames: the deepest frame in the application's own code. The very
        # last frame is often the stdlib ('<frozen os>' for os.environ[...]), which would
        # make every KeyError on any environment variable look like the same symptom.
        frames = TRACEBACK_FRAME_REGEX.findall(log_excerpt)
        app_frames = [
            f for f in frames
            if not f[0].startswith("<") and "site-packages" not in f[0] and "/lib/python" not in f[0]
        ]
        frames = app_frames or frames
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

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _patch_files(patch: Optional[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
        return [
            (str(f.get("path")), str(f.get("op", "modify")), str(f.get("content", "")))
            for f in (patch or {}).get("files") or []
            if isinstance(f, dict) and f.get("path")
        ]

    def _fits_code(self, entry: Dict[str, Any], read_file: Callable[[str], Optional[str]]) -> bool:
        """Whether the files the cached patch replaces are exactly the ones it was learned on.

        A cached patch is complete post-change files. Replayed onto a file that has
        changed since, it would silently revert every unrelated change - and the sanity
        gate cannot tell that apart from a fix. An entry without that record cannot
        show that it fits, so it never skips the model.
        """
        pre_images = entry.get("pre_images")
        paths = {path for path, _, _ in self._patch_files(entry.get("patch"))}
        if not isinstance(pre_images, dict) or not paths or set(pre_images) != paths:
            return False
        for path, digest in pre_images.items():
            current = read_file(path)
            if (None if current is None else self._digest(current)) != digest:
                return False
        return True

    def classify(
        self,
        log_excerpt: str,
        raw_signature: Optional[str] = None,
        read_file: Optional[Callable[[str], Optional[str]]] = None,
    ) -> ClassifierMatch:
        """Evaluates incoming crash against the learned knowledge base.
        Returns a ClassifierMatch with cached_patch if a known fix is found with high confidence.

        read_file(path) returns the current text of a repository file (None when it
        does not exist). With it, a known symptom only skips the model when its cached
        patch was learned on exactly the code that is there now.
        """
        features = self.extract_features(log_excerpt, raw_signature)
        symptom_key = features.raw_signature

        def learned_elsewhere(key: str, confidence: float) -> ClassifierMatch:
            logger.info(
                f"Classifier knows symptom [{key}], but its cached patch was learned on "
                "different code - routing to the LLM instead of overwriting the file."
            )
            return ClassifierMatch(
                matched=False,
                confidence=confidence,
                symptom_id=key,
                explanation=(
                    "Known symptom, but the file differs from the one its cached patch "
                    "was learned on; routing to the LLM."
                ),
            )

        # Exact match check
        if symptom_key in self.entries:
            entry = self.entries[symptom_key]
            confidence = entry.get("confidence", 1.0)
            if confidence >= self.confidence_threshold and read_file is not None \
                    and not self._fits_code(entry, read_file):
                return learned_elsewhere(symptom_key, confidence)
            if confidence >= self.confidence_threshold:
                self.total_bypasses += 1
                entry["times_applied"] = entry.get("times_applied", 0) + 1
                entry["last_applied_at"] = time.time()
                self._save_store()

                logger.info(
                    f"🎯 Classifier MATCH (Exact)! Key=[{symptom_key}], "
                    f"Error=[{features.error_type} in {features.offending_file}], "
                    f"Confidence={confidence:.2f} -> Bypassing LLM generation"
                )
                expl = (
                    f"Exact match on verified symptom: {features.error_type} "
                    f"in {features.offending_file}:{features.offending_func}"
                )
                return ClassifierMatch(
                    matched=True,
                    confidence=confidence,
                    symptom_id=symptom_key,
                    cached_patch=entry.get("patch"),
                    explanation=expl,
                )

        # Fuzzy / Nearest-Neighbor match check across learned entries
        best_match_key = None
        best_score = 0.0
        best_entry = None

        for k, entry in self.entries.items():
            stored_feat = entry.get("features", {})
            score = 0.0

            # Matching exception type: +40%
            err_type = stored_feat.get("error_type")
            if err_type == features.error_type and features.error_type != "UnknownException":
                score += 0.40

            # Matching target file: +30%
            stored_file = stored_feat.get("offending_file")
            if stored_file and stored_file == features.offending_file:
                score += 0.30

            # Matching function/scope: +20%
            stored_func = stored_feat.get("offending_func")
            if stored_func and stored_func == features.offending_func:
                score += 0.20

            # Matching normalized message: +10%
            stored_msg = stored_feat.get("normalized_message")
            if stored_msg and stored_msg == features.normalized_message:
                score += 0.10

            if score > best_score:
                best_score = score
                best_match_key = k
                best_entry = entry

        if best_score >= self.confidence_threshold and best_entry is not None \
                and best_match_key is not None and read_file is not None \
                and not self._fits_code(best_entry, read_file):
            return learned_elsewhere(best_match_key, best_score)

        if best_score >= self.confidence_threshold and best_entry is not None:
            self.total_bypasses += 1
            best_entry["times_applied"] = best_entry.get("times_applied", 0) + 1
            best_entry["last_applied_at"] = time.time()
            self._save_store()

            logger.info(
                f"🎯 Classifier MATCH (Fuzzy {best_score:.2f})! Key=[{best_match_key}], "
                f"Error=[{features.error_type}] -> Bypassing LLM generation"
            )
            fuzzy_expl = (
                f"Fuzzy match (score {best_score:.2f}) on verified symptom: "
                f"{features.error_type} in {features.offending_file}"
            )
            return ClassifierMatch(
                matched=True,
                confidence=best_score,
                symptom_id=best_match_key,
                cached_patch=best_entry.get("patch"),
                explanation=fuzzy_expl,
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
        raw_signature: Optional[str] = None,
        pre_contents: Optional[Dict[str, Optional[str]]] = None,
    ) -> str:
        """Reinforcement: stores or updates a verified patch for this symptom.

        pre_contents: each patched file as it was before the heal (None = it did not
        exist). The patch is only ever replayed onto exactly that code (see classify).
        """
        features = self.extract_features(log_excerpt, raw_signature)
        symptom_key = features.raw_signature
        pre_images = (
            {path: (None if text is None else self._digest(text)) for path, text in pre_contents.items()}
            if pre_contents is not None else None
        )

        existing = self.entries.get(symptom_key)
        if existing:
            if self._patch_files(existing.get("patch")) != self._patch_files(patch_dict):
                # A different verified fix replaces the cached one: it starts with the
                # trust of a fresh verification, not with what the old patch had left.
                existing["confidence"] = 1.0
            else:
                existing["confidence"] = min(1.0, existing.get("confidence", 0.9) + 0.05)
            existing["success_count"] = existing.get("success_count", 0) + 1
            existing["patch"] = patch_dict
            existing["pre_images"] = pre_images
            existing["last_success_at"] = time.time()
        else:
            self.entries[symptom_key] = {
                "symptom_id": symptom_key,
                "features": features.to_dict(),
                "patch": patch_dict,
                "pre_images": pre_images,
                "confidence": 1.0,
                "success_count": 1,
                "failure_count": 0,
                "times_applied": 0,
                "created_at": time.time(),
                "last_success_at": time.time()
            }

        self._save_store()
        logger.info(
            f"✨ Learned verified crash symptom [{symptom_key}]: "
            f"{features.error_type} in {features.offending_file}"
        )
        return symptom_key

    def learn_from_cycle(
        self,
        cycle: Any,
        log_excerpt: str,
        raw_signature: Optional[str],
        used_symptom: Optional[str],
        pre_loop_file: Callable[[str], Optional[str]],
    ) -> None:
        """Learn from a finished Wisdom Loop cycle (a HealCycleRecord).

        The entry whose cached patch was used (used_symptom) loses trust when that
        attempt did not hold up - refused by the sanity gate or failed verification.
        It is attempt 1, not the last of three: only checking the last attempt meant
        no cached patch was ever penalised. A verified heal is stored together with
        what each patched file was before the cycle (pre_loop_file).
        """
        attempts = list(getattr(cycle, "attempts", None) or [])
        fast = next(
            (a for a in attempts if getattr(a, "source", "") == "classifier_fast_path"), None
        )
        if used_symptom and fast is not None and not (fast.verification or {}).get("is_healed"):
            self.record_failure(used_symptom)
        if getattr(cycle, "status", "") == "healed" and attempts and attempts[-1].patch:
            patch = attempts[-1].patch
            pre_contents = {path: pre_loop_file(path) for path, _, _ in self._patch_files(patch)}
            self.record_successful_heal(
                log_excerpt, patch, raw_signature=raw_signature, pre_contents=pre_contents
            )

    def record_failure(self, symptom_id: str) -> None:
        """Penalizes a symptom pattern if its cached patch failed verification."""
        if symptom_id in self.entries:
            entry = self.entries[symptom_id]
            entry["failure_count"] = entry.get("failure_count", 0) + 1
            entry["confidence"] = max(0.0, entry.get("confidence", 1.0) - 0.25)
            conf = entry["confidence"]
            logger.warning(
                f"Penalized classifier symptom [{symptom_id}]: confidence reduced to {conf:.2f}"
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
