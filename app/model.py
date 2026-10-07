"""Spreadsheet core model.

A table holds at most 128 cells.  Each cell is either a signed 64-bit integer
constant or a formula built from ``+ - * /``, cell references and the
conditional form ``if(cond, truthy, falsy)``.

Only the branch actually entered by an ``if`` contributes *active*
dependencies.  Integer arithmetic is checked: division by zero,
``INT64_MIN / -1`` and any ``+ - * /`` result outside the signed 64-bit range
fails the candidate.

Candidate modification protocol
-------------------------------

1. The candidate is parsed and evaluated in an **isolated staging area**; the
   live table is untouched until publication.
2. Active dependencies are discovered while evaluating the branches that are
   actually entered; upstream cells are read from the pre-modification cache.
3. The affected set grows to a fixed point, starting from the candidate and
   the reverse active closure of the pre-modification edges and adding every
   formula cell reached through a newly active edge (plus its old downstream).
4. Every affected cell is recomputed and published together under one new
   cache generation; all other cells keep value, edges and cache generation.
5. On any failure (active cycle, unknown reference, division by zero,
   overflow) nothing is published and a stable cell-to-cell chain from the
   candidate cell to the first failure point is reported; the live table,
   edges, cached values and generations are restored to the pre-change state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
MAX_CELLS = 128


class CellKind(str, Enum):
    EMPTY = "empty"
    CONST = "const"
    FORMULA = "formula"


class FailReason(str, Enum):
    UNKNOWN = "unknown"
    OVERFLOW = "overflow"
    CYCLE = "cycle"
    DIVZERO = "divzero"


class EvalError(Exception):
    """Error while parsing a formula or storing a constant."""


# --------------------------------------------------------------------------- #
# Formula AST / parsing
# --------------------------------------------------------------------------- #


class Node:
    pass


@dataclass(frozen=True)
class Num(Node):
    value: int


@dataclass(frozen=True)
class Ref(Node):
    target: int


@dataclass(frozen=True)
class Bin(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class If(Node):
    cond: Node
    yes: Node
    no: Node


class _Parser:
    """Recursive descent parser.

    expr   := if ( expr , expr , expr ) | arith
    arith  := term (('+' | '-') term)*
    term   := factor (('*' | '/') factor)*
    factor := integer | cellref | '(' expr ')'
    cellref:= 'c' digits          (0..127)
    """

    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self.n = len(text)

    def parse(self) -> Node:
        node = self._expr()
        self._skip_ws()
        if self.i != self.n:
            raise EvalError(f"unexpected character {self.text[self.i]!r} at {self.i}")
        return node

    def _skip_ws(self) -> None:
        while self.i < self.n and self.text[self.i].isspace():
            self.i += 1

    def _peek(self) -> str:
        self._skip_ws()
        return self.text[self.i] if self.i < self.n else ""

    def _accept(self, ch: str) -> bool:
        if self._peek() == ch:
            self.i += 1
            return True
        return False

    def _expect(self, ch: str) -> None:
        if not self._accept(ch):
            raise EvalError(f"expected {ch!r} at position {self.i}")

    def _expr(self) -> Node:
        self._skip_ws()
        if self.text.startswith("if", self.i) and (
            self.i + 2 >= self.n or not self.text[self.i + 2].isalnum()
        ):
            return self._if()
        return self._arith()

    def _if(self) -> If:
        self.i += 2
        self._expect("(")
        cond = self._expr()
        self._expect(",")
        yes = self._expr()
        self._expect(",")
        no = self._expr()
        self._expect(")")
        return If(cond, yes, no)

    def _arith(self) -> Node:
        node = self._term()
        while True:
            ch = self._peek()
            if ch in ("+", "-"):
                self.i += 1
                node = Bin(ch, node, self._term())
            else:
                return node

    def _term(self) -> Node:
        node = self._factor()
        while True:
            ch = self._peek()
            if ch in ("*", "/"):
                self.i += 1
                node = Bin(ch, node, self._factor())
            else:
                return node

    def _factor(self) -> Node:
        self._skip_ws()
        if self.i >= self.n:
            raise EvalError("unexpected end of formula")
        ch = self.text[self.i]
        if ch == "(":
            self.i += 1
            node = self._expr()
            self._expect(")")
            return node
        if ch == "-" and self.i + 1 < self.n and self.text[self.i + 1].isdigit():
            self.i += 1  # signed literal, e.g. -1 / -9223372036854775808
            num = self._number()
            value = -num.value
            if not INT64_MIN <= value <= INT64_MAX:
                raise EvalError("integer literal outside signed 64-bit range")
            return Num(value)
        if ch.isdigit():
            num = self._number()
            if num.value > INT64_MAX:
                raise EvalError("integer literal outside signed 64-bit range")
            return num
        if ch in ("c", "C"):
            return self._reference()
        raise EvalError(f"unexpected character {ch!r} at {self.i}")

    def _number(self) -> Num:
        start = self.i
        while self.i < self.n and self.text[self.i].isdigit():
            self.i += 1
        return Num(int(self.text[start:self.i]))

    def _reference(self) -> Ref:
        start = self.i
        self.i += 1
        digits = self.i
        while self.i < self.n and self.text[self.i].isdigit():
            self.i += 1
        if self.i == digits:
            raise EvalError(f"invalid reference at position {start}")
        idx = int(self.text[digits:self.i])
        if not 0 <= idx < MAX_CELLS:
            raise EvalError(f"cell reference out of range: c{idx}")
        return Ref(idx)


def parse_formula(text: str) -> Node:
    return _Parser(text).parse()


def all_refs(node: Node) -> List[int]:
    """Every reference syntactically present (pre-evaluation edges)."""
    if isinstance(node, Num):
        return []
    if isinstance(node, Ref):
        return [node.target]
    if isinstance(node, Bin):
        return all_refs(node.left) + all_refs(node.right)
    if isinstance(node, If):
        return all_refs(node.cond) + all_refs(node.yes) + all_refs(node.no)
    raise TypeError(node)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class FailureChain:
    """Stable chain candidate -> ... -> first failure point."""

    chain: List[int]
    point: Optional[int]
    reason: FailReason
    detail: str


@dataclass
class AffectedReport:
    generation: int = 0
    recomputed: List[int] = field(default_factory=list)
    cache_hits: List[int] = field(default_factory=list)
    deps_added: Dict[int, List[int]] = field(default_factory=dict)
    deps_removed: Dict[int, List[int]] = field(default_factory=dict)
    active_deps: Dict[int, List[int]] = field(default_factory=dict)


@dataclass
class CandidateResult:
    ok: bool
    generation: int
    affected: Optional[AffectedReport] = None
    failure: Optional[FailureChain] = None


# --------------------------------------------------------------------------- #
# Table
# --------------------------------------------------------------------------- #


@dataclass
class Cell:
    idx: int
    kind: CellKind = CellKind.EMPTY
    raw: str = ""
    ast: Optional[Node] = None
    value: Optional[int] = None
    error: Optional[str] = None
    generation: int = 0
    deps: FrozenSet[int] = frozenset()

    def snapshot(self) -> tuple:
        return (self.kind, self.raw, self.ast, self.value,
                self.error, self.generation, self.deps)

    def restore(self, snap: tuple) -> None:
        (self.kind, self.raw, self.ast, self.value,
         self.error, self.generation, self.deps) = snap


class Table:
    def __init__(self, size: int = MAX_CELLS):
        if not 1 <= size <= MAX_CELLS:
            raise ValueError("table size out of range")
        self.size = size
        self.cells: List[Cell] = [Cell(i) for i in range(size)]
        self.generation = 0

    # -- snapshots --------------------------------------------------------- #

    def _snapshot(self) -> Tuple[int, List[tuple]]:
        return self.generation, [c.snapshot() for c in self.cells]

    def _restore(self, snap: Tuple[int, List[tuple]]) -> None:
        gen, rows = snap
        self.generation = gen
        for cell, row in zip(self.cells, rows):
            cell.restore(row)

    # -- loading ----------------------------------------------------------- #

    def reset(self, entries: Sequence[dict]) -> None:
        """Bulk load ``[{index, type: const|formula, value|raw}, ...]``.

        The whole load is staged; on failure the previous state is restored.
        """
        saved = self._snapshot()
        fresh = [Cell(i) for i in range(self.size)]
        old_cells = self.cells
        self.cells = fresh
        try:
            for entry in entries:
                idx = int(entry["index"])
                if not 0 <= idx < self.size:
                    raise EvalError(f"cell index out of range: {idx}")
                kind = entry.get("type", "formula")
                cell = fresh[idx]
                if kind == "const":
                    value = int(entry["value"])
                    if not INT64_MIN <= value <= INT64_MAX:
                        raise EvalError("constant outside signed 64-bit range")
                    cell.kind = CellKind.CONST
                    cell.raw = str(value)
                    cell.ast = Num(value)
                    cell.value = value
                elif kind == "formula":
                    raw = str(entry["raw"])
                    cell.kind = CellKind.FORMULA
                    cell.raw = raw
                    cell.ast = parse_formula(raw)
                    cell.deps = frozenset(all_refs(cell.ast))
                else:
                    raise EvalError(f"unknown cell type: {kind}")
            self.evaluate_all()
        except _StageFailure as exc:
            self.cells = old_cells
            self._restore(saved)
            raise EvalError(exc.chain.detail) from exc
        except Exception:
            self.cells = old_cells
            self._restore(saved)
            raise

    def evaluate_all(self) -> None:
        """Populate caches for every formula cell (initial load)."""
        roots = {c.idx for c in self.cells if c.kind is CellKind.FORMULA}
        stager = _Stager(self, candidate=None, cand_ast=None,
                         recompute=roots)
        for cid in sorted(roots):
            stager.eval_cell(cid)
        self.generation = 1
        for cid, (value, deps) in stager.memo.items():
            cell = self.cells[cid]
            cell.value = value
            cell.error = None
            cell.deps = deps
            cell.generation = 1
        for cell in self.cells:
            if cell.kind is CellKind.CONST:
                cell.generation = 1

    # -- graph helpers ----------------------------------------------------- #

    def _reverse_edges(self) -> Dict[int, List[int]]:
        rev: Dict[int, List[int]] = {i: [] for i in range(self.size)}
        for cell in self.cells:
            for dep in cell.deps:
                rev[dep].append(cell.idx)
        return {k: sorted(v) for k, v in rev.items()}

    @staticmethod
    def _closure(seeds: Iterable[int], edges: Dict[int, List[int]]) -> set:
        seen: set = set()
        stack = list(seeds)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(edges.get(cur, ()))
        return seen

    @staticmethod
    def _shortest_path(edges: Dict[int, List[int]],
                       src: int, dst: int) -> List[int]:
        """Deterministic shortest path (sorted neighbours); fallback direct."""
        if src == dst:
            return [src]
        parent = {src: src}
        queue = [src]
        head = 0
        while head < len(queue):
            cur = queue[head]
            head += 1
            for nxt in edges.get(cur, ()):
                if nxt not in parent:
                    parent[nxt] = cur
                    if nxt == dst:
                        path = [dst]
                        while path[-1] != src:
                            path.append(parent[path[-1]])
                        path.reverse()
                        return path
                    queue.append(nxt)
        return [src, dst]

    # -- candidate --------------------------------------------------------- #

    def submit_candidate(self, idx: int, kind: str,
                         value: Optional[int] = None,
                         raw: Optional[str] = None) -> CandidateResult:
        if not 0 <= idx < self.size:
            raise EvalError(f"cell index out of range: {idx}")
        if kind == "const":
            ivalue = int(value)  # type: ignore[arg-type]
            if not INT64_MIN <= ivalue <= INT64_MAX:
                raise EvalError("constant outside signed 64-bit range")
            cand_ast: Node = Num(ivalue)
            cand_kind = CellKind.CONST
            cand_raw = str(ivalue)
        elif kind == "formula":
            if raw is None:
                raise EvalError("formula candidate requires raw text")
            cand_ast = parse_formula(raw)
            cand_kind = CellKind.FORMULA
            cand_raw = raw
        else:
            raise EvalError(f"unknown candidate type: {kind}")

        saved = self._snapshot()
        old_reverse = self._reverse_edges()
        # Affected = candidate plus every pre-change active downstream cell.
        affected = {idx} | self._closure([idx], old_reverse)

        try:
            # Evaluate the affected cells in the isolated staging area.
            # References to anything outside the set are served from the
            # pre-change cache and still recorded as dependency edges.
            stager = _Stager(self, idx, cand_ast, affected)
            order = [idx] + sorted(c for c in affected if c != idx)
            for cid in order:
                try:
                    stager.eval_cell(cid)
                except _StageFailure as sf:
                    chain = sf.chain.chain
                    if cid != idx and (not chain or chain[0] != idx):
                        prefix = self._shortest_path(old_reverse, idx, cid)
                        sf.chain.chain = prefix[:-1] + chain
                    raise

            # The prospective graph uses staged edges for affected cells and
            # pre-change edges everywhere else.  A newly active edge may close
            # a cycle through an (otherwise cached) outside cell.
            cycle = self._find_cycle(idx, affected, stager)
            if cycle is not None:
                raise _StageFailure(FailureChain(
                    cycle, cycle[-1], FailReason.CYCLE,
                    "active dependency cycle: " + " -> ".join(f"c{x}" for x in cycle)))

            report = self._build_report(idx, affected, stager)
            self._publish(idx, cand_kind, cand_raw, cand_ast, affected, stager)
            return CandidateResult(True, self.generation, affected=report)
        except _StageFailure as sf:
            self._restore(saved)
            return CandidateResult(False, saved[0], failure=sf.chain)

    def _find_cycle(self, root: int, affected: set,
                    stager: "_Stager") -> Optional[List[int]]:
        """Return a deterministic chain ``root -> ... -> cycle`` or ``None``.

        Edges of affected cells come from the staging area; every other cell
        keeps its pre-change active edges.  Neighbours are visited in sorted
        order, so the reported chain is stable across identical runs.
        """
        def edges_of(cid: int) -> List[int]:
            if cid in stager.memo:
                return sorted(stager.memo[cid][1])
            return sorted(self.cells[cid].deps)

        color: Dict[int, int] = {}
        path: List[int] = []

        def dfs(node_id: int) -> Optional[List[int]]:
            color[node_id] = 1
            path.append(node_id)
            for nxt in edges_of(node_id):
                c = color.get(nxt, 0)
                if c == 0:
                    found = dfs(nxt)
                    if found is not None:
                        return found
                elif c == 1:
                    return path[path.index(nxt):] + [nxt]
            path.pop()
            color[node_id] = 2
            return None

        all_edges = {cid: edges_of(cid) for cid in range(self.size)}
        cycle_seed: Optional[List[int]] = None
        for nid in sorted(affected):
            if color.get(nid, 0) == 0:
                cycle_seed = dfs(nid)
                if cycle_seed is not None:
                    break
        if cycle_seed is None:
            return None
        # Prefix with the shortest stable path from the candidate when the
        # cycle does not already begin at the candidate.
        if cycle_seed[0] == root:
            return cycle_seed
        prefix = self._shortest_path(all_edges, root, cycle_seed[0])
        return prefix[:-1] + cycle_seed

    def _build_report(self, idx: int, affected: set,
                      stager: "_Stager") -> AffectedReport:
        report = AffectedReport(generation=self.generation + 1)
        for cid in sorted(affected):
            _value, deps = stager.memo[cid]
            report.active_deps[cid] = sorted(deps)
            old_deps = self.cells[cid].deps
            added = sorted(deps - old_deps)
            removed = sorted(old_deps - deps)
            if added:
                report.deps_added[cid] = added
            if removed:
                report.deps_removed[cid] = removed
        report.recomputed = sorted(affected)
        report.cache_hits = sorted(stager.cache_reads - affected)
        return report

    def _publish(self, idx: int, kind: CellKind, raw: str, ast: Node,
                 affected: set, stager: "_Stager") -> None:
        self.generation += 1
        gen = self.generation
        for cid in sorted(affected):
            value, deps = stager.memo[cid]
            cell = self.cells[cid]
            if cid == idx:
                cell.kind = kind
                cell.raw = raw
                cell.ast = ast
            cell.value = value
            cell.error = None
            cell.deps = deps
            cell.generation = gen


# --------------------------------------------------------------------------- #
# Isolated staging evaluator
# --------------------------------------------------------------------------- #


class _StageFailure(Exception):
    def __init__(self, chain: FailureChain):
        super().__init__(chain.detail)
        self.chain = chain


class _Stager:
    """Evaluates cells in isolation.

    Cells in ``recompute`` are evaluated (and their active edges recorded);
    every other reference is served directly from the pre-change cache.
    """

    def __init__(self, table: Table, candidate: Optional[int],
                 cand_ast: Optional[Node], recompute: set):
        self.t = table
        self.candidate = candidate
        self.cand_ast = cand_ast
        self.recompute = recompute
        self.memo: Dict[int, Tuple[int, FrozenSet[int]]] = {}
        self.onpath: List[int] = []
        self.onset: set = set()
        self.cache_reads: set = set()

    def eval_cell(self, cid: int) -> None:
        if cid in self.memo:
            return
        if cid in self.onset:
            start = self.onpath.index(cid)
            chain = list(self.onpath) + [cid]
            cycle = self.onpath[start:] + [cid]
            raise _StageFailure(FailureChain(
                chain, cid, FailReason.CYCLE,
                "active dependency cycle: " + " -> ".join(f"c{x}" for x in cycle)))
        ast = self.cand_ast if cid == self.candidate else self.t.cells[cid].ast
        if ast is None:
            raise _StageFailure(FailureChain(
                list(self.onpath) + [cid], cid, FailReason.UNKNOWN,
                f"unknown reference: c{cid} is empty"))
        self.onset.add(cid)
        self.onpath.append(cid)
        used: set = set()
        try:
            value = self._eval(ast, used)
        finally:
            self.onpath.pop()
            self.onset.discard(cid)
        self.memo[cid] = (value, frozenset(used))

    def _eval(self, node: Node, used: set) -> int:
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Ref):
            used.add(node.target)
            return self._eval_ref(node.target, used)
        if isinstance(node, If):
            cond = self._eval(node.cond, used)
            branch = node.yes if cond != 0 else node.no
            return self._eval(branch, used)
        if isinstance(node, Bin):
            left = self._eval(node.left, used)
            right = self._eval(node.right, used)
            return self._binop(node.op, left, right)
        raise TypeError(node)

    def _eval_ref(self, target: int, used: set) -> int:
        if not 0 <= target < self.t.size:
            raise _StageFailure(FailureChain(
                list(self.onpath), None, FailReason.UNKNOWN,
                f"unknown reference: c{target}"))
        if target == self.candidate or target in self.recompute:
            self.eval_cell(target)
            return self.memo[target][0]
        # Serve the pre-change cached value; never recompute upstream cells.
        cell = self.t.cells[target]
        if cell.kind is CellKind.EMPTY or cell.value is None:
            raise _StageFailure(FailureChain(
                list(self.onpath) + [target], target, FailReason.UNKNOWN,
                f"unknown reference: c{target} has no value"))
        self.cache_reads.add(target)
        return cell.value

    def _binop(self, op: str, left: int, right: int) -> int:
        if op == "+":
            result = left + right
        elif op == "-":
            result = left - right
        elif op == "*":
            result = left * right
        else:  # '/'
            if right == 0:
                owner = self.onpath[-1] if self.onpath else None
                raise _StageFailure(FailureChain(
                    list(self.onpath), owner, FailReason.DIVZERO,
                    f"division by zero while evaluating c{owner}: {left} / 0"))
            # Truncation toward zero; INT64_MIN / -1 overflows.
            if left == INT64_MIN and right == -1:
                result = INT64_MAX + 1
            else:
                q = abs(left) // abs(right)
                result = q if (left < 0) == (right < 0) else -q
        if not INT64_MIN <= result <= INT64_MAX:
            owner = self.onpath[-1] if self.onpath else None
            raise _StageFailure(FailureChain(
                list(self.onpath), owner, FailReason.OVERFLOW,
                f"int64 overflow while evaluating c{owner}: "
                f"{left} {op} {right}"))
        return result
