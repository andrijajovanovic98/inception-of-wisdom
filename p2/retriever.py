"""
Inception-of-Wisdom (IoW) - Part 2: Analyst
Code Retriever: Top-k Semantic Retrieval & Traceback Symbol Extraction
"""

from __future__ import annotations

import re
import os
import logging
from typing import List, Dict, Optional, Any, Tuple

from p2.db import ChromaVectorDB

logger = logging.getLogger("p2.retriever")

# Regex to extract file, line number, and function name from Python tracebacks
TRACEBACK_FRAME_REGEX = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+([a-zA-Z0-9_]+))?')
EXCEPTION_LINE_REGEX = re.compile(r'([a-zA-Z0-9_]*(?:Error|Exception|CRITICAL|Fatal)):\s*(.*)')


class CodeRetriever:
    """Provides semantic and symbol-guided retrieval over the ChromaDB index.
    Subject requirement: Free-text top-k retrieval with similarity score,
    and identifier extraction from error logs to feed relevant chunks to the LLM.
    """

    def __init__(self, vector_db: ChromaVectorDB, default_top_k: int = 3):
        self.vector_db = vector_db
        self.default_top_k = default_top_k

    def retrieve_by_query(
        self,
        query: str,
        top_k: Optional[int] = None,
        file_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Free-text semantic retrieval over the vector index.
        Subject requirement: Returns top-k chunks closest to query with similarity scores.
        """
        k = top_k if top_k is not None else self.default_top_k
        if not query or not query.strip():
            return []

        results = self.vector_db.query_similar(
            query_text=query.strip(),
            top_k=k,
            file_path_filter=file_path
        )
        return results

    def extract_traceback_symbols(self, log_text: str) -> Dict[str, Any]:
        """Extracts files, line numbers, function names, and exception types from error logs.
        Subject hint: 'extract identifiers from the error log, look them up in the symbol list'.
        """
        if not log_text:
            return {"files": [], "lines": [], "functions": [], "error_type": None, "error_msg": None}

        files: List[str] = []
        lines_list: List[int] = []
        functions: List[str] = []
        error_type: Optional[str] = None
        error_msg: Optional[str] = None

        frames: List[Tuple[str, int]] = []
        for line in log_text.splitlines():
            clean_line = line.strip()

            # Match traceback stack frames
            tb_match = TRACEBACK_FRAME_REGEX.search(clean_line)
            if tb_match:
                raw_file = tb_match.group(1)
                # Normalize relative path (e.g. /app/demo_app/app.py -> demo_app/app.py or app.py)
                norm_file = os.path.basename(raw_file)
                if norm_file not in files:
                    files.append(norm_file)

                line_num = int(tb_match.group(2))
                lines_list.append(line_num)
                if (norm_file, line_num) not in frames:
                    frames.append((norm_file, line_num))

                func_name = tb_match.group(3)
                if func_name and func_name not in ["<module>", "<lambda>"] and func_name not in functions:
                    functions.append(func_name)

            # Match final exception line
            exc_match = EXCEPTION_LINE_REGEX.search(clean_line)
            if exc_match:
                error_type = exc_match.group(1)
                error_msg = exc_match.group(2)

        return {
            "files": files,
            "lines": lines_list,
            "frames": frames,
            "functions": functions,
            "error_type": error_type,
            "error_msg": error_msg
        }

    def _lookup_traceback_chunks(
        self,
        extracted: Dict[str, Any],
        query_embedding: Optional[List[List[float]]],
        search_query: str,
    ) -> List[Dict[str, Any]]:
        """Look the traceback's identifiers up in the index itself.

        Subject hint: "extract identifiers from the error log, look them up in the
        symbol list of the index, and feed only the relevant chunks to the model".
        Each hit keeps its real similarity score, so ranking stays comparable.
        """
        hits: List[Dict[str, Any]] = []
        functions = list(extracted.get("functions") or [])
        if functions:
            hits.extend(
                dict(c, match="symbol") for c in self.vector_db.query_similar(
                    search_query, top_k=len(functions) * 2,
                    where={"symbol_name": {"$in": functions}},
                    query_embedding=query_embedding,
                )
            )

        frames = list(extracted.get("frames") or [])
        if frames:
            indexed = self.vector_db.list_indexed_files()
            # Only frames that point into the indexed tree (skip site-packages etc).
            for base, line_no in frames[-6:]:
                paths = [p for p in indexed if os.path.basename(p) == base]
                if len(paths) != 1:
                    continue
                hits.extend(
                    dict(c, match="line") for c in self.vector_db.query_similar(
                        search_query, top_k=2,
                        where={"$and": [
                            {"file_path": paths[0]},
                            {"start_line": {"$lte": line_no}},
                            {"end_line": {"$gte": line_no}},
                        ]},
                        query_embedding=query_embedding,
                    )
                )
        return hits

    def retrieve_for_crash(
        self,
        log_excerpt: str,
        top_k: Optional[int] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Dual-strategy retrieval for crash diagnosis:
        1. Extract specific symbol and file identifiers from traceback.
        2. Combine with semantic retrieval of the error message.
        Returns: (ranked_chunks, extracted_symbols)
        """
        k = top_k if top_k is not None else self.default_top_k
        extracted = self.extract_traceback_symbols(log_excerpt)

        # Build search query from log
        query_parts = []
        if extracted.get("error_type"):
            query_parts.append(f"{extracted['error_type']}: {extracted.get('error_msg', '')}")
        if extracted.get("functions"):
            query_parts.append(f"Function: {' '.join(extracted['functions'])}")

        # Fallback query if no structured traceback was detected
        if not query_parts:
            # Use the last few lines of the log excerpt
            tail_lines = [
                line.strip() for line in log_excerpt.splitlines() if line.strip()
            ][-5:]
            search_query = " ".join(tail_lines)
        else:
            search_query = " ".join(query_parts)

        search_query = search_query.strip()
        if not search_query:
            return [], extracted

        # Semantic retrieval + direct index lookups of the traceback identifiers,
        # sharing one query embedding.
        query_embedding = self.vector_db.compute_embeddings([search_query]) or None
        semantic_results = self.vector_db.query_similar(
            search_query, top_k=k * 2, query_embedding=query_embedding
        )
        merged: Dict[str, Dict[str, Any]] = {}
        for chunk in semantic_results + self._lookup_traceback_chunks(
            extracted, query_embedding, search_query
        ):
            cid = chunk.get("chunk_id", "")
            previous = merged.get(cid)
            if previous is None:
                merged[cid] = chunk
            elif chunk.get("match") and not previous.get("match"):
                previous["match"] = chunk["match"]

        # Re-rank: prioritize chunks whose symbol_name, line or file was explicitly in
        # the traceback; similarity breaks ties.
        target_funcs = set(extracted.get("functions", []))
        target_files = set(extracted.get("files", []))
        frames = list(extracted.get("frames") or [])

        def ranking_key(chunk: Dict[str, Any]) -> float:
            score = float(chunk.get("similarity", 0.0) or 0.0)
            symbol = chunk.get("symbol_name", "")
            file_name = os.path.basename(chunk.get("file_path", ""))

            # The exact line the traceback points at is the strongest evidence.
            if any(
                base == file_name
                and int(chunk.get("start_line", 0)) <= line_no <= int(chunk.get("end_line", 0))
                for base, line_no in frames
            ):
                score += 2.5
            # Huge boost if function was directly mentioned in traceback frame
            if symbol in target_funcs:
                score += 2.0
            # Moderate boost if file matches traceback frame
            if file_name in target_files:
                score += 1.0

            return score

        ranked = sorted(merged.values(), key=ranking_key, reverse=True)
        curated_chunks = ranked[:k]

        logger.info(
            f"Retrieved {len(curated_chunks)} candidate chunks for crash "
            f"(symbols: files={extracted.get('files')}, funcs={extracted.get('functions')})"
        )

        return curated_chunks, extracted
