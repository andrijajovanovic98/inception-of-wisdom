"""
Inception-of-Wisdom (IoW) - Part 2: Analyst
AST Chunker: Logical Code Decomposition for Semantic Indexing
"""

from __future__ import annotations

import ast
import os
import hashlib
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger("p2.chunker")

# Binary and generated files carry no retrievable meaning and would be embedded as
# replacement-character noise.
SKIP_EXTENSIONS = (
    ".pyc", ".pyo", ".so", ".o", ".a", ".bin", ".onnx", ".safetensors", ".pt", ".pth",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
    ".zip", ".gz", ".tar", ".whl", ".sqlite3", ".db", ".lock", ".woff", ".woff2",
)

DEFAULT_IGNORE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", ".chroma", "chroma_db",
    ".pytest_cache", ".cache", "build", "dist", "docu", "docs", "node_modules",
}


@dataclass
class CodeChunk:
    """Represents a logical code unit (function, class, method, or module preamble)."""
    chunk_id: str
    file_path: str              # Relative path, e.g. "demo_app/app.py"
    symbol_name: str            # e.g. "calculate_summary", "healthcheck"
    symbol_type: str            # "function", "class", "method", "module"
    start_line: int
    end_line: int
    content: str
    sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AstChunker:
    """Decomposes Python source code into logical AST-based chunks.
    Subject requirement: Logical chunking (not fixed-size), preserving functions and classes.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()

    def chunk_file(self, full_path: str) -> List[CodeChunk]:
        """Parses a single file and extracts its logical code chunks."""
        if not os.path.isfile(full_path):
            logger.warning(f"File not found for chunking: {full_path}")
            return []

        rel_path = os.path.relpath(full_path, self.base_dir)

        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                source_code = f.read()
        except Exception as e:
            logger.error(f"Failed to read file {rel_path}: {e}")
            return []

        if not source_code.strip():
            return []

        # Only Python files undergo AST parsing
        if full_path.endswith(".py"):
            return self._chunk_python_ast(rel_path, source_code)
        else:
            return self._chunk_generic_file(rel_path, source_code)

    def _chunk_python_ast(self, rel_path: str, source_code: str) -> List[CodeChunk]:
        """Extracts AST nodes (functions, classes, methods) from Python source code."""
        chunks: List[CodeChunk] = []
        lines = source_code.splitlines(keepends=True)
        total_lines = len(lines)

        try:
            tree = ast.parse(source_code, filename=rel_path)
        except SyntaxError as e:
            logger.warning(
                f"SyntaxError while parsing AST for {rel_path}: {e}. "
                "Falling back to single chunk."
            )
            return self._chunk_generic_file(rel_path, source_code)

        # Top-level statements that are not defs (imports, constants, app = Flask(...),
        # the __main__ block, ...) are grouped into contiguous module blocks, so every
        # line of the file lives in some chunk. A crash in `if __name__ == "__main__":`
        # (the classic bad environment variable) must be retrievable too.
        module_blocks: List[List[ast.stmt]] = []
        current_block: List[ast.stmt] = []

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if current_block:
                    module_blocks.append(current_block)
                    current_block = []
            else:
                current_block.append(node)
                continue

            # Functions
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chunk = self._extract_node_chunk(rel_path, lines, node, symbol_type="function")
                if chunk:
                    chunks.append(chunk)

            # Classes
            elif isinstance(node, ast.ClassDef):
                # Also extract methods inside the class for fine-grained retrieval
                for sub_node in node.body:
                    if isinstance(sub_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_chunk = self._extract_node_chunk(
                            rel_path, lines, sub_node,
                            symbol_type="method",
                            class_name=node.name
                        )
                        if method_chunk:
                            chunks.append(method_chunk)

                # Extract the full class chunk
                class_chunk = self._extract_node_chunk(rel_path, lines, node, symbol_type="class")
                if class_chunk:
                    chunks.append(class_chunk)

        if current_block:
            module_blocks.append(current_block)

        first_def_line = min(
            (self._first_line(n) for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))),
            default=total_lines + 1,
        )

        for block in module_blocks:
            start_line = self._first_line(block[0])
            # The block that precedes every def also owns the file header
            # (shebang, comments, module docstring).
            if start_line < first_def_line:
                start_line = 1
            end_line = getattr(block[-1], "end_lineno", None) or total_lines
            text = "".join(lines[start_line - 1:end_line]).strip()
            if not text:
                continue
            if start_line == 1:
                name = "module_preamble"
            elif self._is_main_guard(block[0]):
                name = "__main__"
            else:
                name = f"module_block_L{start_line}"
            chunks.append(CodeChunk(
                chunk_id=f"{rel_path}:module:{name}",
                file_path=rel_path,
                symbol_name=name,
                symbol_type="module",
                start_line=start_line,
                end_line=end_line,
                content=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            ))

        # Fallback if no functions or classes were found
        if not chunks:
            return self._chunk_generic_file(rel_path, source_code)

        chunks.sort(key=lambda c: (c.start_line, c.symbol_type != "class"))
        return self._disambiguate_ids(chunks)

    @staticmethod
    def _first_line(node: ast.stmt) -> int:
        decorators = getattr(node, "decorator_list", None) or []
        lines = [getattr(d, "lineno", node.lineno) for d in decorators] + [node.lineno]
        return min(lines)

    @staticmethod
    def _is_main_guard(node: ast.stmt) -> bool:
        """`if __name__ == "__main__":`"""
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            return False
        left = node.test.left
        return isinstance(left, ast.Name) and left.id == "__name__"

    @staticmethod
    def _disambiguate_ids(chunks: List[CodeChunk]) -> List[CodeChunk]:
        """Ensure ids are unique within a file (e.g. two same-named defs)."""
        seen: Dict[str, int] = {}
        for chunk in chunks:
            count = seen.get(chunk.chunk_id, 0)
            seen[chunk.chunk_id] = count + 1
            if count:
                chunk.chunk_id = f"{chunk.chunk_id}#{count}"
        return chunks

    def _extract_node_chunk(
        self,
        rel_path: str,
        lines: List[str],
        node: ast.AST,
        symbol_type: str,
        class_name: Optional[str] = None
    ) -> Optional[CodeChunk]:
        """Extracts the slice of source lines corresponding to an AST node."""
        start_line = getattr(node, "lineno", 1)
        end_line = getattr(node, "end_lineno", len(lines))

        # Include any leading decorators
        if hasattr(node, "decorator_list") and node.decorator_list:
            first_decorator = node.decorator_list[0]
            dec_line = getattr(first_decorator, "lineno", start_line)
            start_line = min(start_line, dec_line)

        chunk_lines = lines[start_line - 1:end_line]
        chunk_content = "".join(chunk_lines).strip()
        if not chunk_content:
            return None

        symbol_name = getattr(node, "name", "anonymous")
        if class_name:
            symbol_name = f"{class_name}.{symbol_name}"

        sha = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
        # Identity is the symbol, not its current text: an edited function keeps its
        # id so upsert replaces it in place instead of leaving a stale twin behind.
        chunk_id = f"{rel_path}:{symbol_type}:{symbol_name}"

        return CodeChunk(
            chunk_id=chunk_id,
            file_path=rel_path,
            symbol_name=symbol_name,
            symbol_type=symbol_type,
            start_line=start_line,
            end_line=end_line,
            content=chunk_content,
            sha256=sha
        )

    def _chunk_generic_file(self, rel_path: str, source_code: str) -> List[CodeChunk]:
        """Fallback chunker for non-Python files (e.g. YAML, text)."""
        content = source_code.strip()
        if not content:
            return []
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        total_lines = len(source_code.splitlines())
        return [
            CodeChunk(
                chunk_id=f"{rel_path}:file:{os.path.basename(rel_path)}",
                file_path=rel_path,
                symbol_name=os.path.basename(rel_path),
                symbol_type="file",
                start_line=1,
                end_line=total_lines,
                content=content,
                sha256=sha
            )
        ]

    def chunk_directory(
        self,
        directory: str,
        ignore_dirs: Optional[set] = None
    ) -> List[CodeChunk]:
        """Walks a directory and returns all extracted chunks across eligible source files."""
        ignored = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
        all_chunks: List[CodeChunk] = []

        target_dir = os.path.abspath(directory)
        for root, dirs, files in os.walk(target_dir):
            # Modify dirs in place to prune ignored folders
            dirs[:] = [d for d in dirs if d not in ignored and not d.startswith(".")]

            for filename in files:
                if filename.startswith(".") or filename.endswith(SKIP_EXTENSIONS):
                    continue
                full_path = os.path.join(root, filename)
                chunks = self.chunk_file(full_path)
                all_chunks.extend(chunks)

        return all_chunks
