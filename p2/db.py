"""
Inception-of-Wisdom (IoW) - Part 2: Analyst
ChromaDB PersistentClient & Local all-MiniLM-L6-v2 Embeddings
"""

from __future__ import annotations

import os
import logging
from typing import List, Optional, Dict, Any

from p2.chunker import CodeChunk

logger = logging.getLogger("p2.db")

# 42 quota protection: every model cache lives under /tmp/iow. The embedding model is
# resolved through HF_HOME only - the same cache `make setup` prefetches into - so the
# agent never downloads a second copy into some other directory.
CACHE_BASE_DIR = os.environ.get("IOW_CACHE_DIR", "/tmp/iow")
os.environ.setdefault("HF_HOME", os.path.join(CACHE_BASE_DIR, "hf-cache"))
os.environ.setdefault("TORCH_HOME", os.path.join(CACHE_BASE_DIR, "hf-cache", "torch"))
# Must be set before chromadb import / client init (IoC-style)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    chromadb = None  # type: ignore[assignment,misc]
    Settings = None  # type: ignore[assignment,misc]

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None  # type: ignore[assignment,misc]


class ChromaVectorDB:
    """Manages the persistent ChromaDB vector store and local offline embeddings.
    Subject requirement: ChromaDB PersistentClient mode, incremental updates,
    offline all-MiniLM-L6-v2 embeddings, never wipe on restart.
    """

    def __init__(
        self,
        persist_dir: str = ".chroma",
        collection_name: str = "iow_code_chunks",
        embedding_model_name: str = "all-MiniLM-L6-v2"
    ):
        self.persist_dir = os.path.abspath(persist_dir)
        self.collection_name = collection_name
        self.embedding_model_name = embedding_model_name

        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None
        self._embedder: Optional[Any] = None

    def _ensure_initialized(self) -> bool:
        """Initializes the PersistentClient and offline embedding model."""
        if self._collection is not None:
            return True

        if chromadb is None:
            logger.error("The 'chromadb' package is not installed.")
            return False

        try:
            os.makedirs(self.persist_dir, exist_ok=True)
            # Subject requirement: PersistentClient mode so database persists across restarts
            # Disable anonymized PostHog telemetry (avoids noisy capture() ERROR spam)
            client_kwargs: Dict[str, Any] = {"path": self.persist_dir}
            if Settings is not None:
                client_kwargs["settings"] = Settings(anonymized_telemetry=False)
            self._client = chromadb.PersistentClient(**client_kwargs)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
            logger.info(
                f"ChromaDB PersistentClient initialized at {self.persist_dir} "
                f"(collection='{self.collection_name}', items={self._collection.count()})"
            )
        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB PersistentClient: {e}")
            return False

        # Load local embedding model
        if self._embedder is None and SentenceTransformer is not None:
            try:
                self._embedder = self._load_embedder()
                logger.info("Local embedding model loaded successfully.")
            except Exception as e:
                logger.warning(f"Could not load SentenceTransformer model ({e}). Embedding will be delayed.")

        return True

    def _load_embedder(self) -> Any:
        """Load the embedding model, from the local cache first.

        local_files_only keeps a cached model fully offline (no hub round-trip); only
        a genuinely empty cache falls back to the one-time download.
        """
        logger.info(
            f"Loading local embedding model '{self.embedding_model_name}' "
            f"(HF_HOME={os.environ.get('HF_HOME')})..."
        )
        try:
            return SentenceTransformer(self.embedding_model_name, local_files_only=True)
        except Exception as cached_err:
            logger.info(
                f"Embedding model not in the local cache yet ({cached_err}); "
                "downloading it once."
            )
            return SentenceTransformer(self.embedding_model_name)

    def compute_embeddings(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Computes embeddings locally using all-MiniLM-L6-v2."""
        if not texts:
            return []

        if self._embedder is None:
            if SentenceTransformer is not None:
                try:
                    self._embedder = self._load_embedder()
                except Exception as e:
                    logger.error(f"Failed to load embedder on demand: {e}")
                    return None
            else:
                logger.error("sentence_transformers library is not installed.")
                return None

        try:
            embeddings = self._embedder.encode(texts, normalize_embeddings=True)
            return embeddings.tolist()
        except Exception as e:
            logger.error(f"Error computing embeddings: {e}")
            return None

    @staticmethod
    def _metadata(chunk: CodeChunk) -> Dict[str, Any]:
        return {
            "file_path": chunk.file_path,
            "symbol_name": chunk.symbol_name,
            "symbol_type": chunk.symbol_type,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "sha256": chunk.sha256,
        }

    def index_chunks(self, chunks: List[CodeChunk]) -> Dict[str, int]:
        """Incrementally indexes code chunks into ChromaDB.
        Skips chunks whose SHA-256 hash has not changed (zero re-embedding cost).
        """
        if not self._ensure_initialized() or self._collection is None:
            return {"added": 0, "skipped": 0, "failed": len(chunks)}

        if not chunks:
            return {"added": 0, "skipped": 0, "failed": 0}

        chunk_ids = [c.chunk_id for c in chunks]

        # Retrieve existing metadata to check for unchanged hashes
        try:
            existing = self._collection.get(ids=chunk_ids, include=["metadatas"])
            existing_map: Dict[str, Dict[str, Any]] = {}
            for i, cid in enumerate(existing.get("ids", [])):
                meta = existing.get("metadatas", [])[i] if existing.get("metadatas") else {}
                existing_map[cid] = dict(meta or {})
        except Exception as e:
            logger.warning(f"Could not check existing chunks: {e}. Proceeding with all chunks.")
            existing_map = {}

        to_embed_chunks: List[CodeChunk] = []
        moved_chunks: List[CodeChunk] = []
        skipped_count = 0

        for chunk in chunks:
            prev = existing_map.get(chunk.chunk_id)
            if prev is None or prev.get("sha256") != chunk.sha256:
                to_embed_chunks.append(chunk)
                continue
            skipped_count += 1
            if (prev.get("start_line"), prev.get("end_line")) != (chunk.start_line, chunk.end_line):
                # Same code, new position (lines inserted above it): the embedding is
                # still right, but the line range drives traceback lookups.
                moved_chunks.append(chunk)

        if moved_chunks:
            try:
                self._collection.update(
                    ids=[c.chunk_id for c in moved_chunks],
                    metadatas=[self._metadata(c) for c in moved_chunks],
                )
                logger.info(f"Updated line ranges of {len(moved_chunks)} moved chunk(s) (no re-embedding).")
            except Exception as e:
                logger.warning(f"Could not update moved chunk metadata: {e}")

        if not to_embed_chunks:
            logger.info(
                f"Incremental index: all {skipped_count} chunks are up to date. "
                "No embeddings needed."
            )
            return {"added": 0, "skipped": skipped_count, "failed": 0}

        logger.info(
            f"Indexing {len(to_embed_chunks)} new/modified chunks "
            f"(skipped {skipped_count} unchanged)..."
        )

        # Prepare texts for embedding: symbol name + content
        texts_to_embed = [
            f"# File: {c.file_path} | Symbol: {c.symbol_name} ({c.symbol_type})\n{c.content}"
            for c in to_embed_chunks
        ]

        embeddings = self.compute_embeddings(texts_to_embed)

        ids = [c.chunk_id for c in to_embed_chunks]
        documents = [c.content for c in to_embed_chunks]
        metadatas = [self._metadata(c) for c in to_embed_chunks]

        try:
            if embeddings:
                self._collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    documents=documents,
                    metadatas=metadatas
                )
            else:
                # Fallback to Chroma's default embedder if local embedder is unavailable
                self._collection.upsert(
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas
                )

            logger.info(f"Successfully upserted {len(to_embed_chunks)} chunks into ChromaDB.")
            return {"added": len(to_embed_chunks), "skipped": skipped_count, "failed": 0}

        except Exception as e:
            logger.error(f"Failed to upsert chunks into ChromaDB: {e}")
            return {"added": 0, "skipped": skipped_count, "failed": len(to_embed_chunks)}

    def prune_missing_chunks(self, chunks: List[CodeChunk]) -> int:
        """Delete rows for symbols that no longer exist in the files just indexed.

        Without this an edited or removed function survives in the index forever and
        retrieval can serve pre-patch code back to the Analyst.
        """
        if not self._ensure_initialized() or self._collection is None:
            return 0

        wanted: Dict[str, set] = {}
        for chunk in chunks:
            wanted.setdefault(chunk.file_path, set()).add(chunk.chunk_id)

        removed = 0
        for file_path, keep_ids in wanted.items():
            try:
                existing = self._collection.get(
                    where={"file_path": file_path}, include=[]
                )
            except Exception as e:
                logger.warning(f"Could not list existing chunks for {file_path}: {e}")
                continue

            stale = [cid for cid in existing.get("ids", []) if cid not in keep_ids]
            if not stale:
                continue
            try:
                self._collection.delete(ids=stale)
                removed += len(stale)
                logger.info(
                    f"Pruned {len(stale)} superseded chunk(s) from {file_path}."
                )
            except Exception as e:
                logger.error(f"Failed pruning stale chunks for {file_path}: {e}")

        return removed

    def sync_chunks(self, chunks: List[CodeChunk]) -> Dict[str, int]:
        """Index chunks and drop whatever they replaced (incremental, never a wipe)."""
        stats = self.index_chunks(chunks)
        stats["pruned"] = self.prune_missing_chunks(chunks)
        return stats

    def sync_directory_chunks(
        self,
        chunks: List[CodeChunk],
        known_files: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Full-tree sync: index, prune superseded symbols, drop deleted files."""
        stats = self.sync_chunks(chunks)
        if known_files is not None:
            stats["files_removed"] = self.prune_missing_files(known_files)
        return stats

    def prune_missing_files(self, existing_files: List[str]) -> int:
        """Delete rows whose source file has disappeared from the target tree."""
        if not self._ensure_initialized() or self._collection is None:
            return 0
        keep = set(existing_files)
        try:
            everything = self._collection.get(include=["metadatas"])
        except Exception as e:
            logger.warning(f"Could not enumerate the index for file pruning: {e}")
            return 0

        ids = everything.get("ids", []) or []
        metas = everything.get("metadatas", []) or []
        stale = [
            cid for cid, meta in zip(ids, metas)
            if (meta or {}).get("file_path") not in keep
        ]
        if not stale:
            return 0
        try:
            self._collection.delete(ids=stale)
            logger.info(f"Pruned {len(stale)} chunk(s) from files no longer on disk.")
            return len(stale)
        except Exception as e:
            logger.error(f"Failed pruning chunks of deleted files: {e}")
            return 0

    def query_similar(
        self,
        query_text: str,
        top_k: int = 3,
        file_path_filter: Optional[str] = None,
        where: Optional[Dict[str, Any]] = None,
        query_embedding: Optional[List[List[float]]] = None,
    ) -> List[Dict[str, Any]]:
        """Queries the vector index for the top-k chunks closest to query_text.

        `where` narrows the search to matching metadata (symbol / file / line range)
        and `query_embedding` reuses an embedding the caller already computed.
        """
        if not self._ensure_initialized() or self._collection is None:
            return []
        if top_k <= 0:
            return []

        where_clause = where
        if file_path_filter:
            file_clause = {"file_path": file_path_filter}
            where_clause = {"$and": [where, file_clause]} if where else file_clause

        query_emb = query_embedding or self.compute_embeddings([query_text])

        try:
            if query_emb:
                results = self._collection.query(
                    query_embeddings=query_emb,
                    n_results=top_k,
                    where=where_clause,
                    include=["documents", "metadatas", "distances"]
                )
            else:
                results = self._collection.query(
                    query_texts=[query_text],
                    n_results=top_k,
                    where=where_clause,
                    include=["documents", "metadatas", "distances"]
                )

            formatted: List[Dict[str, Any]] = []
            if results and results.get("ids") and results["ids"][0]:
                for i, cid in enumerate(results["ids"][0]):
                    doc = results["documents"][0][i] if results.get("documents") else ""
                    meta = results["metadatas"][0][i] if results.get("metadatas") else {}
                    # Cosine distance to similarity: similarity = 1 - distance
                    dist = results["distances"][0][i] if results.get("distances") else 0.0
                    similarity = max(0.0, round(1.0 - dist, 4))

                    formatted.append({
                        "chunk_id": cid,
                        "file_path": meta.get("file_path", "unknown"),
                        "symbol_name": meta.get("symbol_name", "unknown"),
                        "symbol_type": meta.get("symbol_type", "unknown"),
                        "start_line": meta.get("start_line", 0),
                        "end_line": meta.get("end_line", 0),
                        "content": doc,
                        "similarity": similarity,
                        "similarity_score": similarity,  # IoC-compatible field name
                        "score": similarity,             # dashboard badge field
                        "distance": round(dist, 4)
                    })

            return formatted

        except Exception as e:
            logger.error(f"Error querying ChromaDB: {e}")
            return []

    def list_indexed_files(self) -> List[str]:
        """Every distinct file path currently in the index (the real citable paths)."""
        if not self._ensure_initialized() or self._collection is None:
            return []
        try:
            everything = self._collection.get(include=["metadatas"])
        except Exception as e:
            logger.warning(f"Could not list indexed files: {e}")
            return []
        paths = {
            str((meta or {}).get("file_path"))
            for meta in everything.get("metadatas", []) or []
            if (meta or {}).get("file_path")
        }
        return sorted(paths)

    def count(self) -> int:
        """Returns the total number of indexed chunks in the collection."""
        if not self._ensure_initialized() or self._collection is None:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def delete_by_file(self, rel_path: str) -> int:
        """Deletes all chunks associated with a specific file path."""
        if not self._ensure_initialized() or self._collection is None:
            return 0
        try:
            self._collection.delete(where={"file_path": rel_path})
            logger.info(f"Deleted chunks for file {rel_path} from ChromaDB.")
            return 1
        except Exception as e:
            logger.error(f"Failed to delete chunks for {rel_path}: {e}")
            return 0
