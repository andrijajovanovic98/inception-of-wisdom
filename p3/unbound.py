"""Names a Python file may read before they are set - read with ast, before anything is deployed.

A rewrite that sets a value on one path only (`port = ...` inside an `if` with no `else`, or
inside a `try` whose `except` falls through) passes every other check and fails at run time
with a NameError or UnboundLocalError, after a whole redeploy. Seen live on `make break-env`:
one of nine rewrites left `port` unbound when IOW_DEMO_PORT was missing.

Flow-sensitive, and lenient wherever Python cannot be read statically: a loop body counts as
having run at least once, a statement that only assigns a constant cannot raise, and a
construct this module does not model (match, a star import) makes it answer None. It can
miss a name that may be unbound; it is meant never to report one that is always set.
"""

from __future__ import annotations

import ast
import builtins
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from p3.exits import _EXIT_CALLS, _dotted

_BUILTINS = frozenset(dir(builtins))
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)

Names = FrozenSet[str]
Reads = List[Tuple[str, int]]
# (names surely set when the block falls through - None when it never does,
#  the sets at each `break`, the sets at each `continue`)
Outcome = Tuple[Optional[Names], List[Names], List[Names]]


class _CannotTell(Exception):
    """A construct this check does not model."""


def _reads(node: ast.AST) -> Tuple[Reads, Set[str]]:
    """(name, line) of every name an expression reads now, and the names its walruses set.

    A lambda body runs later, and a comprehension's own variables are its own.
    """
    reads: Reads = []
    walrus: Set[str] = set()

    def visit(n: ast.AST, hidden: FrozenSet[str]) -> None:
        if isinstance(n, ast.Lambda):
            return
        if isinstance(n, _COMPREHENSIONS):
            own = {t.id for g in n.generators for t in ast.walk(g.target) if isinstance(t, ast.Name)}
            for child in ast.iter_child_nodes(n):
                visit(child, hidden | own)
            return
        if isinstance(n, ast.NamedExpr):
            if isinstance(n.target, ast.Name):
                walrus.add(n.target.id)
            visit(n.value, hidden)
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in hidden:
            reads.append((n.id, n.lineno))
        for child in ast.iter_child_nodes(n):
            visit(child, hidden)

    visit(node, frozenset())
    return reads, walrus


def _targets(node: ast.AST) -> Tuple[Set[str], Reads]:
    """Names an assignment target binds, and what it reads (`a.b = ...` reads a)."""
    if isinstance(node, ast.Name):
        return {node.id}, []
    if isinstance(node, (ast.Tuple, ast.List)):
        names: Set[str] = set()
        reads: Reads = []
        for elt in node.elts:
            n, r = _targets(elt)
            names |= n
            reads += r
        return names, reads
    if isinstance(node, ast.Starred):
        return _targets(node.value)
    return set(), _reads(node)[0]


def _bound_in(body: List[ast.stmt]) -> Set[str]:
    """Every name a scope's own statements bind (not the bodies of nested scopes)."""
    bound: Set[str] = set()

    def visit(n: ast.AST) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            return
        if isinstance(n, ast.Lambda) or isinstance(n, _COMPREHENSIONS):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        for child in ast.iter_child_nodes(n):
            visit(child)

    for stmt in body:
        visit(stmt)
    return bound


def _declared(body: List[ast.stmt]) -> Set[str]:
    """Names a function declares global or nonlocal (they are not its own)."""
    names: Set[str] = set()
    for node in body:
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
                continue
            if isinstance(n, (ast.Global, ast.Nonlocal)):
                names.update(n.names)
    return names


def _cannot_raise(stmt: ast.stmt) -> bool:
    """A plain assignment of a constant (`port = 5000`): nothing in it can fail."""
    return isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) \
        and all(isinstance(t, ast.Name) for t in stmt.targets)


def _always_true(test: ast.expr) -> bool:
    return isinstance(test, ast.Constant) and bool(test.value)


def _meet(sets: List[Optional[Names]]) -> Optional[Names]:
    """What is surely set where these paths join (None when no path gets there)."""
    live = [s for s in sets if s is not None]
    if not live:
        return None
    result = live[0]
    for s in live[1:]:
        result = result & s
    return result


class _Scope:
    """Definite assignment over the statements of one scope."""

    def __init__(self, own: Set[str], path: str) -> None:
        self.own = own
        self.path = path
        self.unset: Dict[str, int] = {}
        self.nested: List[Tuple[str, ast.AST]] = []

    def _use(self, reads: Reads, surely: Names) -> None:
        for name, line in reads:
            if name in self.own and name not in surely:
                self.unset.setdefault(name, line)

    def block(self, body: List[ast.stmt], surely: Names) -> Outcome:
        breaks: List[Names] = []
        continues: List[Names] = []
        current: Optional[Names] = surely
        for stmt in body:
            if current is None:
                break                                   # unreachable from here on
            current, b, c = self.statement(stmt, current)
            breaks += b
            continues += c
        return current, breaks, continues

    def statement(self, stmt: ast.stmt, surely: Names) -> Outcome:  # noqa: C901 - one case per kind
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts: List[ast.AST] = list(stmt.decorator_list)
            if isinstance(stmt, ast.ClassDef):
                parts += list(stmt.bases) + [k.value for k in stmt.keywords]
            else:
                parts += [d for d in stmt.args.defaults + stmt.args.kw_defaults if d is not None]
            for part in parts:
                self._use(_reads(part)[0], surely)
            self.nested.append((f"{self.path}.{stmt.name}" if self.path else stmt.name, stmt))
            return surely | {stmt.name}, [], []
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = stmt.value
            reads, walrus = _reads(value) if value is not None else ([], set())
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            bound: Set[str] = set()
            for target in targets:
                names, target_reads = _targets(target)
                reads += target_reads
                if isinstance(stmt, ast.AugAssign):
                    reads += [(n, stmt.lineno) for n in names]      # x += 1 reads x first
                if value is not None:
                    bound |= names
            self._use(reads, surely)
            return surely | bound | walrus, [], []
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names = set()
            for alias in stmt.names:
                if alias.name == "*":
                    raise _CannotTell("star import")
                names.add(alias.asname or alias.name.split(".")[0])
            return surely | names, [], []
        if isinstance(stmt, ast.If):
            reads, walrus = _reads(stmt.test)
            self._use(reads, surely)
            entry = surely | walrus
            body_end, b1, c1 = self.block(stmt.body, entry)
            else_end, b2, c2 = self.block(stmt.orelse, entry)
            return _meet([body_end, else_end]), b1 + b2, c1 + c2
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            if isinstance(stmt, ast.While):
                reads, walrus = _reads(stmt.test)
                entry = surely | walrus
            else:
                reads, walrus = _reads(stmt.iter)
                names, target_reads = _targets(stmt.target)
                reads += target_reads
                entry = surely | walrus | names
            self._use(reads, surely)
            body_end, breaks, continues = self.block(stmt.body, entry)
            # Lenient: the body counts as having run at least once.
            natural = _meet([body_end] + list(continues))
            if isinstance(stmt, ast.While) and _always_true(stmt.test):
                natural = None                          # `while True:` only ends by break
            loop_else: Optional[Names] = None
            outer_breaks: List[Names] = []
            outer_continues: List[Names] = []
            if natural is not None:
                loop_else, outer_breaks, outer_continues = self.block(stmt.orelse, natural)
            return _meet([loop_else] + list(breaks)), outer_breaks, outer_continues
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            bound = set()
            for item in stmt.items:
                reads, walrus = _reads(item.context_expr)
                self._use(reads, surely | bound)
                bound |= walrus
                if item.optional_vars is not None:
                    names, target_reads = _targets(item.optional_vars)
                    self._use(target_reads, surely | bound)
                    bound |= names
            return self.block(stmt.body, surely | bound)
        if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
            body: List[ast.stmt] = stmt.body  # type: ignore[attr-defined]
            # Anything in the body may raise, except leading assignments of constants.
            start = set(surely)
            for first in body:
                if not _cannot_raise(first):
                    break
                start |= _targets(first.targets[0])[0]  # type: ignore[attr-defined]
            body_end, breaks, continues = self.block(body, surely)
            ends: List[Optional[Names]] = []
            if body_end is not None:
                else_end, b, c = self.block(stmt.orelse, body_end)  # type: ignore[attr-defined]
                ends.append(else_end)
                breaks, continues = breaks + b, continues + c
            for handler in stmt.handlers:  # type: ignore[attr-defined]
                if handler.type is not None:
                    self._use(_reads(handler.type)[0], frozenset(start))
                entry = frozenset(start | ({handler.name} if handler.name else set()))
                handler_end, b, c = self.block(handler.body, entry)
                ends.append(handler_end)
                breaks, continues = breaks + b, continues + c
            after = _meet(ends)
            final: List[ast.stmt] = stmt.finalbody  # type: ignore[attr-defined]
            if final:
                fin_end, b, c = self.block(final, frozenset(start))
                breaks, continues = breaks + b, continues + c
                if after is not None and fin_end is not None:
                    after = after | (fin_end - frozenset(start))
                elif fin_end is None:
                    after = None
            return after, breaks, continues
        if isinstance(stmt, ast.Return):
            if stmt.value is not None:
                self._use(_reads(stmt.value)[0], surely)
            return None, [], []
        if isinstance(stmt, ast.Raise):
            for raised in (stmt.exc, stmt.cause):
                if raised is not None:
                    self._use(_reads(raised)[0], surely)
            return None, [], []
        if isinstance(stmt, ast.Break):
            return None, [surely], []
        if isinstance(stmt, ast.Continue):
            return None, [], [surely]
        if isinstance(stmt, ast.Delete):
            gone: Set[str] = set()
            for target in stmt.targets:
                names, target_reads = _targets(target)
                self._use(target_reads + [(n, stmt.lineno) for n in names], surely)
                gone |= names
            return surely - gone, [], []
        if type(stmt).__name__ == "Match":
            raise _CannotTell("match statement")
        if isinstance(stmt, ast.Expr):
            reads, walrus = _reads(stmt.value)
            self._use(reads, surely)
            if isinstance(stmt.value, ast.Call) and _dotted(stmt.value.func) in _EXIT_CALLS:
                return None, [], []                     # exit(1) ends the program here
            return surely | walrus, [], []
        # assert, global, nonlocal, pass, ...: whatever they read, nothing they bind.
        reads, walrus = _reads(stmt)
        self._use(reads, surely)
        return surely | walrus, [], []


def _function_scope(path: str, node: ast.AST) -> _Scope:
    args = node.args  # type: ignore[attr-defined]
    params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    params |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    body: List[ast.stmt] = node.body  # type: ignore[attr-defined]
    scope = _Scope((_bound_in(body) | params) - _declared(body), path)
    scope.block(body, frozenset(params))
    return scope


def reads_before_set(text: str) -> Optional[Dict[Tuple[str, str], int]]:
    """{(scope, name): line} for every name read where it may not be set yet, or None.

    None when it cannot be told: the file does not parse, or uses a construct this check
    does not model. A module-level name that is also a builtin falls back to the builtin,
    so it is never reported there.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    found: Dict[Tuple[str, str], int] = {}
    try:
        module = _Scope(_bound_in(tree.body) - _BUILTINS, "")
        module.block(tree.body, frozenset())
        pending = list(module.nested)
        for name, line in module.unset.items():
            found[("<module>", name)] = line
        while pending:
            path, node = pending.pop()
            if isinstance(node, ast.ClassDef):
                # A class body runs in its own namespace; its methods are scopes of their own.
                for stmt in node.body:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        pending.append((f"{path}.{stmt.name}", stmt))
                continue
            scope = _function_scope(path, node)
            pending += scope.nested
            for name, line in scope.unset.items():
                found[(path, name)] = line
    except _CannotTell:
        return None
    return found


def unset_in_a_new_way(before: str, after: str) -> Optional[str]:
    """Why `after` may read a name before it is set where `before` did not, or None."""
    old, new = reads_before_set(before), reads_before_set(after)
    if old is None or new is None:
        return None
    added = sorted((line, scope, name) for (scope, name), line in new.items() if (scope, name) not in old)
    if not added:
        return None
    line, scope, name = added[0]
    where = "" if scope == "<module>" else f" in {scope}()"
    more = f" (and {len(added) - 1} more)" if len(added) > 1 else ""
    return (
        f"reads {name} on line {line}{where}, where it may not be set yet: it is set only on "
        f"some paths, so the program fails with a NameError there{more}"
    )
