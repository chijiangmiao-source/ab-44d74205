"""推导表核心：128 格、代次管理、单格候选裁决。

裁决流程：
1. 解析候选内容（常量 / 公式 / 清空）；
2. 依据“修改前”的反向活动依赖求受影响闭包；
3. 在独立暂存区按实际进入的条件分支重算并替换依赖边；
4. 全部暂存成功 → 受影响项原子发布为同一新代次，其余项值与缓存代次不变；
   任一失败 → 整体回滚，活动表、依赖边、缓存值、缓存代次均保持修改前状态，
   并给出从候选格到首个失败点的稳定链路。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .evaluator import EvalFailure, Staging
from .formula import (CONST_RE, INT64_MAX, INT64_MIN, NUM_CELLS, FormulaError,
                      cell_index, index_cell, parse)


@dataclass
class Cell:
    id: str
    raw: str = ""
    kind: str = "empty"  # empty | const | formula
    expr: object = None
    const: int = 0
    value: int = 0
    has_value: bool = False
    generation: int = 0
    deps: list = field(default_factory=list)  # 活动（实际进入分支的）前向依赖


def _parse_content(raw: str):
    """返回 ((kind, expr, const), None) 或 (None, 错误消息)。"""
    text = (raw or "").strip()
    if not text:
        return ("empty", None, 0), None
    if CONST_RE.match(text):
        v = int(text, 10)
        if INT64_MIN <= v <= INT64_MAX:
            return ("const", None, v), None
        return None, f"常量超出 64 位整数范围：{text}"
    try:
        expr = parse(text)
    except FormulaError as e:
        return None, str(e)
    return ("formula", expr, 0), None


def _closure(start: str, reverse: dict) -> set:
    """沿反向活动依赖求传递闭包（含起点），即可能受影响的单元格集合。"""
    seen = {start}
    queue = [start]
    while queue:
        cur = queue.pop(0)
        for nxt in reverse.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _bfs_path(candidate: str, root: str, reverse: dict) -> list:
    """candidate → root 沿修改前反向活动依赖的最短稳定路径（邻居按单元格序）。"""
    if candidate == root:
        return [candidate]
    prev = {candidate: None}
    queue = [candidate]
    while queue:
        cur = queue.pop(0)
        if cur == root:
            break
        for nxt in reverse.get(cur, ()):
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    path = []
    node = root
    while node is not None:
        path.append(node)
        node = prev[node]
    path.reverse()
    return path


class Table:
    def __init__(self):
        self._lock = threading.RLock()
        self.generation = 0
        self.cells = [Cell(id=index_cell(i)) for i in range(NUM_CELLS)]

    def reset(self) -> None:
        with self._lock:
            self.generation = 0
            self.cells = [Cell(id=index_cell(i)) for i in range(NUM_CELLS)]

    # ---------------- 读取 ----------------

    def health(self) -> dict:
        with self._lock:
            return {"status": "ok", "generation": self.generation}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "generation": self.generation,
                "cells": [self._view(c) for c in self.cells],
                "edges": [
                    {"from": c.id, "to": d} for c in self.cells for d in c.deps
                ],
            }

    @staticmethod
    def _view(c: Cell) -> dict:
        return {
            "id": c.id,
            "raw": c.raw,
            "kind": c.kind,
            "value": c.value if c.has_value else None,
            "hasValue": c.has_value,
            "generation": c.generation,
            "deps": list(c.deps),
        }

    # ---------------- 候选裁决 ----------------

    def adjudicate(self, cell_id: str, raw_input: str) -> dict:
        with self._lock:
            idx = cell_index(cell_id)
            if idx < 0:
                raise ValueError(f"无效单元格：{cell_id}")
            cid = index_cell(idx)
            report = {
                "status": "rejected",
                "generation": self.generation,
                "candidate": {"cell": cid, "input": raw_input},
                "affected": [],
                "recomputed": [],
                "cacheHits": [],
                "depChanges": [],
                "error": None,
            }

            # 1. 解析候选内容
            content, perr = _parse_content(raw_input)
            if perr is not None:
                report["error"] = {"kind": "parse", "message": perr, "chain": [cid]}
                return report

            # 2. 修改前的反向活动依赖 → 受影响集
            reverse = {c.id: [] for c in self.cells}
            for c in self.cells:  # 按单元格序遍历，保证邻接表顺序稳定
                for d in c.deps:
                    reverse[d].append(c.id)
            affected = _closure(cid, reverse)
            ordered = sorted(affected, key=cell_index)

            # 3. 独立暂存区：按实际进入的条件分支重算并替换依赖边
            staging = Staging(self.cells, cid, content, affected)
            candidate_cleared = content[0] == "empty"
            for rid in ordered:
                if rid == cid and candidate_cleared:
                    continue  # 清空的候选格本身无值可算；其下游引用将触发未知引用
                try:
                    staging.eval_cell(rid, [])
                except EvalFailure as e:
                    chain = _bfs_path(cid, rid, reverse) + e.stack[1:]
                    report["error"] = {
                        "kind": e.kind,
                        "message": e.message,
                        "chain": chain,
                    }
                    return report  # 回滚：活动表未被任何修改

            # 4. 原子发布：仅全部暂存成功的受影响项，共同进入新代次
            self.generation += 1
            gen = self.generation
            kind, expr, const = content
            for rid in ordered:
                c = self.cells[cell_index(rid)]
                old_value = c.value if c.has_value else None
                old_had = c.has_value
                old_gen = c.generation
                old_deps = list(c.deps)
                if rid == cid:
                    c.raw = (raw_input or "").strip()
                    c.kind = kind
                    c.expr = expr
                    c.const = const
                if rid == cid and candidate_cleared:
                    c.value, c.has_value, c.deps = 0, False, []
                else:
                    c.value = staging.memo[rid]
                    c.has_value = True
                    c.deps = staging.new_deps.get(rid, [])
                c.generation = gen
                added = [d for d in c.deps if d not in old_deps]
                removed = [d for d in old_deps if d not in c.deps]
                report["recomputed"].append({
                    "cell": rid,
                    "hadValue": old_had,
                    "hasValue": c.has_value,
                    "oldValue": old_value,
                    "newValue": c.value if c.has_value else None,
                    "oldGeneration": old_gen,
                    "newGeneration": gen,
                    "depsBefore": old_deps,
                    "depsAfter": list(c.deps),
                    "added": added,
                    "removed": removed,
                })
                if added or removed:
                    report["depChanges"].append({
                        "cell": rid,
                        "added": added,
                        "removed": removed,
                        "depsAfter": list(c.deps),
                    })
            for c in self.cells:
                if c.id not in affected and c.has_value:
                    report["cacheHits"].append({
                        "cell": c.id,
                        "value": c.value,
                        "generation": c.generation,
                    })
            report["status"] = "published"
            report["generation"] = gen
            report["affected"] = ordered
            return report
