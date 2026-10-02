"""Names a Python file uses but never defines - read with ast, before any model runs.

Scope-insensitive on purpose: a name bound anywhere in the file counts as defined.
That can miss an undefined name but never invents one, so a check built on it never
refuses a correct patch.
"""

from __future__ import annotations

import ast
import builtins
from typing import Optional, Set

# Names every module has without defining them.
_MODULE_NAMES = {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__",
    "__builtins__", "__path__", "__cached__", "__annotations__",
}
_KNOWN = set(dir(builtins)) | _MODULE_NAMES


def undefined_names(text: str) -> Optional[Set[str]]:
    """Names the file reads but binds nowhere, or None when that cannot be told.

    It cannot be told for a file that does not parse, or one with a star import (the
    star may provide anything).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    bound: Set[str] = set()
    loaded: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return None
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Name):
            (loaded if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif type(node).__name__ in ("MatchAs", "MatchStar", "MatchMapping"):
            # match/case captures: MatchAs/MatchStar .name, MatchMapping .rest
            captured = getattr(node, "name", None) or getattr(node, "rest", None)
            if captured:
                bound.add(captured)
    return loaded - bound - _KNOWN
