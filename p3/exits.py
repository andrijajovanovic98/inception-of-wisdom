"""Ways a Python file stops the program - read with ast, before anything is deployed.

A "fix" that turns a crash into a deliberate stop (raise ValueError, exit(1)), or that
starts the service only when the missing value is there, moves the failure instead of
removing it: the redeployed target still does not serve. Seen live on `make break-env`:
four of six rewrites of the startup block did exactly that, and each one cost a whole
redeploy before verification could tell.
"""

from __future__ import annotations

import ast
from collections import Counter
from typing import List, Optional

# Calls that end the process.
_EXIT_CALLS = {"exit", "quit", "sys.exit", "os._exit", "os.abort"}
# Blocks whose body always runs (try / with), unlike an if / loop branch.
_ALWAYS_RUN = tuple(
    t for t in (ast.Try, getattr(ast, "TryStar", None), ast.With, ast.AsyncWith) if t is not None
)


def _dotted(node: Optional[ast.AST]) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _stops(tree: ast.AST) -> Counter:
    """Every raise and process-ending call, counted by kind."""
    found: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            found["raise"] += 1
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in _EXIT_CALLS:
                found[f"{name}()"] += 1
    return found


def _main_body(tree: ast.Module) -> Optional[List[ast.stmt]]:
    """The body of `if __name__ == "__main__":`, if the file has one."""
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and _dotted(node.test.left) == "__name__":
            return node.body
    return None


def _unconditional_calls(body: List[ast.stmt]) -> List[str]:
    """Calls a block always makes: its own statements, and those of try / with blocks."""
    names: List[str] = []
    for stmt in body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            name = _dotted(stmt.value.func)
            if name:
                names.append(name)
        elif isinstance(stmt, _ALWAYS_RUN):
            names.extend(_unconditional_calls(stmt.body))  # type: ignore[attr-defined]
    return names


def stops_in_a_new_way(before: str, after: str) -> Optional[str]:
    """Why `after` stops the program where `before` did not, or None.

    Two ways a fix can move a crash instead of removing it:
    * it adds a raise or an exit call (exit(1), sys.exit(...), os._exit(...));
    * the call the startup block ends with (app.run(...)) is no longer made on every
      run - it moved under a condition, or it is gone - so the service only starts
      when the missing value happens to be there.
    A file that does not parse is left to the syntax check (None).
    """
    try:
        old_tree, new_tree = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return None
    added = _stops(new_tree) - _stops(old_tree)
    if added:
        return f"it adds {', '.join(sorted(added))}, so the program still stops"
    old_main, new_main = _main_body(old_tree), _main_body(new_tree)
    old_calls = _unconditional_calls(old_main) if old_main else []
    new_calls = _unconditional_calls(new_main) if new_main else []
    if old_calls and old_calls[-1] not in new_calls:
        return (
            f"{old_calls[-1]}(...) no longer runs every time the program starts, so the service "
            "still does not start when the value is missing"
        )
    return None
