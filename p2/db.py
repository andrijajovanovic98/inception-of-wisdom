"""
Inception-of-Wisdom (IoW) — Part 2: Analyst
ChromaDB PersistentClient & Local all-MiniLM-L6-v2 Embeddings
"""

from __future__ import annotations

import os
import logging
from typing import List, Optional, Dict, Any

from p2.chunker import CodeChunk

logger = logging.getLogger("p2.db")

# User constraint & 42 quota protection: Route model caches to /tmp
CACHE_BASE_DIR = os.environ.get("IOW_CACHE_DIR", "/tmp/iow_cache")
os.environ.setdefault("HF_HOME", os.path.join(CACHE_BASE_DIR, "hf"))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", os.path.join(CACHE_BASE_DIR, "embeddings"))
os.environ.setdefault("TORCH_HOME", os.path.join(CACHE_BASE_DIR, "torch"))
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
                emb_cache = os.path.join(CACHE_BASE_DIR, "embeddings")
                os.makedirs(emb_cache, exist_ok=True)
                logger.info(
                    f"Loading local embedding model '{self.embedding_model_name}' (cache={emb_cache})..."
                )
                self._embedder = SentenceTransformer(
                    self.embedding_model_name,
                    cache_folder=emb_cache
                )
                logger.info("Local embedding model loaded successfully.")
            except Exception as e:
                logger.warning(f"Could not load SentenceTransformer model ({e}). Embedding will be delayed.")

        return True

    def compute_embeddings(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Computes embeddings locally using all-MiniLM-L6-v2."""
        if not texts:
            return []

        if self._embedder is None:
            if SentenceTransformer is not None:
                try:
                    emb_cache = os.path.join(CACHE_BASE_DIR, "embeddings")
                    self._embedder = SentenceTransformer(
                        self.embedding_model_name,
                        cache_folder=emb_cache
                    )
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
            existing_map = {}
            for i, cid in enumerate(existing.get("ids", [])):
                meta = existing.get("metadatas", [])[i] if existing.get("metadatas") else {}
                existing_map[cid] = meta.get("sha256", "")
        except Exception as e:
            logger.warning(f"Could not check existing chunks: {e}. Proceeding with all chunks.")
            existing_map = {}

        to_embed_chunks: List[CodeChunk] = []
        skipped_count = 0

        for chunk in chunks:
            prev_hash = existing_map.get(chunk.chunk_id)
            if prev_hash == chunk.sha256:
                skipped_count += 1
            else:
                to_embed_chunks.append(chunk)

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
        metadatas = [
            {
                "file_path": c.file_path,
                "symbol_name": c.symbol_name,
                "symbol_type": c.symbol_type,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "sha256": c.sha256
            }
            for c in to_embed_chunks
        ]

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

    def sync_chunks(self, chunks: List[CodeChunk]) -> Dict[str, int]:
        """Alias for index_chunks (dashboard/orchestrator compatibility)."""
        return self.index_chunks(chunks)

    def query_similar(
        self,
        query_text: str,
        top_k: int = 3,
        file_path_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Queries the vector index for the top-k chunks closest to query_text."""
        if not self._ensure_initialized() or self._collection is None:
            return []

        where_clause = None
        if file_path_filter:
            where_clause = {"file_path": file_path_filter}

        query_emb = self.compute_embeddings([query_text])

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
