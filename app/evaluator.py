"""候选暂存求值器。

在独立暂存区中对受影响单元格重算：
- 仅按实际进入的条件分支收集活动依赖（if 的未取分支不产生依赖、不触发错误）；
- 非受影响单元格直接使用已发布缓存值（缓存命中）；
- 全程进行 64 位整数溢出、除零、未知引用与环路检查；
- 求值失败抛出 EvalFailure，携带从根单元格到失败点的单元格链路。
"""
from __future__ import annotations

from .formula import INT64_MIN, INT64_MAX, Bin, If, Neg, Num, Ref, cell_index


class EvalFailure(Exception):
    """求期失败。kind ∈ {cycle, unknown_ref, div_zero, overflow}。"""

    def __init__(self, kind: str, message: str, stack: list):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.stack = list(stack)  # 从本次求值根单元格到失败点的链路


class Staging:
    """独立暂存区：候选内容视图 + 新值 memo + 新依赖边，发布前不触碰活动表。"""

    def __init__(self, cells, candidate_id: str, new_content, affected: set):
        self._cells = cells              # 活动表当前单元格（只读）
        self._candidate = candidate_id
        self._new_content = new_content  # (kind, expr, const)
        self._affected = affected
        self.memo: dict[str, int] = {}   # 受影响单元格的新值
        self.new_deps: dict[str, list] = {}  # 受影响单元格的新活动依赖边

    def _content_of(self, cid: str):
        if cid == self._candidate:
            return self._new_content
        c = self._cells[cell_index(cid)]
        return (c.kind, c.expr, c.const)

    def eval_cell(self, cid: str, stack: list) -> int:
        if cid in self.memo:
            return self.memo[cid]
        if cid in stack:
            chain = " → ".join(stack + [cid])
            raise EvalFailure("cycle", f"活动分支出现环路：{chain}", stack + [cid])
        kind, expr, const = self._content_of(cid)
        if kind == "empty":
            raise EvalFailure("unknown_ref", f"引用了未定义的单元格 {cid}", stack + [cid])
        if cid not in self._affected:
            # 缓存命中：非受影响项的值不受本次修改影响，直接沿用已发布缓存。
            return self._cells[cell_index(cid)].value
        stack = stack + [cid]
        if kind == "const":
            value, refs = const, []
        else:
            value, refs = self.eval_expr(expr, stack)
        self.memo[cid] = value
        self.new_deps[cid] = sorted(set(refs), key=cell_index)
        return value

    def eval_expr(self, node, stack: list):
        """返回 (值, 本表达式实际进入分支所引用的单元格列表)。"""
        if isinstance(node, Num):
            return node.value, []
        if isinstance(node, Ref):
            return self.eval_cell(node.cell, stack), [node.cell]
        if isinstance(node, Neg):
            v, refs = self.eval_expr(node.expr, stack)
            if v == INT64_MIN:
                raise EvalFailure("overflow", f"对 {v} 取负发生 64 位溢出", stack)
            return -v, refs
        if isinstance(node, Bin):
            lv, lr = self.eval_expr(node.left, stack)
            rv, rr = self.eval_expr(node.right, stack)
            refs = lr + rr
            if node.op == "/":
                if rv == 0:
                    raise EvalFailure("div_zero", f"除数为零：{lv} / 0", stack)
                if lv == INT64_MIN and rv == -1:
                    raise EvalFailure("overflow", f"64 位整数溢出：{lv} / -1", stack)
                q = abs(lv) // abs(rv)  # 向零取整
                v = -q if (lv < 0) != (rv < 0) else q
                return v, refs
            if node.op == "+":
                v = lv + rv
            elif node.op == "-":
                v = lv - rv
            elif node.op == "*":
                v = lv * rv
            else:  # pragma: no cover - 解析器保证不会产生
                raise AssertionError(f"未知运算符 {node.op}")
            if v < INT64_MIN or v > INT64_MAX:
                raise EvalFailure("overflow", f"64 位整数溢出：{lv} {node.op} {rv}", stack)
            return v, refs
        if isinstance(node, If):
            cv, cr = self.eval_expr(node.cond, stack)
            if cv != 0:
                tv, tr = self.eval_expr(node.yes, stack)
                return tv, cr + tr
            fv, fr = self.eval_expr(node.no, stack)
            return fv, cr + fr
        raise AssertionError("未知 AST 节点")  # pragma: no cover
