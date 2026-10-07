"""引擎单元测试：条件切换、部分下游发布、失败回滚及解析/求值细节。"""
import unittest

from app.engine import Table
from app.formula import INT64_MAX, INT64_MIN, FormulaError, parse


def cells_by_id(state):
    return {c["id"]: c for c in state["cells"]}


class TestParser(unittest.TestCase):
    def test_const_and_precedence(self):
        t = Table()
        self.assertEqual(t.adjudicate("A1", "2 + 3 * 4")["status"], "published")
        self.assertEqual(cells_by_id(t.snapshot())["A1"]["value"], 14)
        t.adjudicate("A2", "(2 + 3) * 4")
        self.assertEqual(cells_by_id(t.snapshot())["A2"]["value"], 20)

    def test_int64_bounds_literals(self):
        t = Table()
        self.assertEqual(t.adjudicate("A1", str(INT64_MAX))["status"], "published")
        self.assertEqual(t.adjudicate("A2", str(INT64_MIN))["status"], "published")
        self.assertEqual(cells_by_id(t.snapshot())["A2"]["value"], INT64_MIN)
        r = t.adjudicate("A3", str(INT64_MAX + 1))
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["kind"], "parse")

    def test_parse_errors(self):
        t = Table()
        for bad in ["A1 +", "if(1, 2)", "Z9 + 1", "1.5", "A17 + 1", ""]:
            if bad == "":
                continue  # 空串表示清空，单独测试
            r = t.adjudicate("B1", bad)
            self.assertEqual(r["status"], "rejected", bad)
            self.assertEqual(r["error"]["kind"], "parse", bad)
            self.assertEqual(r["error"]["chain"], ["B1"], bad)

    def test_case_insensitive_refs_and_if(self):
        t = Table()
        t.adjudicate("a1", "7")
        r = t.adjudicate("B1", "IF(1, a1, 9)")
        self.assertEqual(r["status"], "published")
        self.assertEqual(cells_by_id(t.snapshot())["B1"]["value"], 7)


class TestConditionSwitching(unittest.TestCase):
    """切换条件源：新分支实际引用、依赖增删、不得沿用旧分支缓存。"""

    def test_switch_branch(self):
        t = Table()
        t.adjudicate("A1", "10")
        t.adjudicate("A2", "20")
        t.adjudicate("C1", "0")
        r = t.adjudicate("B1", "if(C1, A1, A2)")
        self.assertEqual(r["status"], "published")
        st = cells_by_id(t.snapshot())
        self.assertEqual(st["B1"]["value"], 20)
        self.assertEqual(st["B1"]["deps"], ["C1", "A2"])  # 条件 + 实际进入的 false 分支

        r = t.adjudicate("C1", "1")  # 切换条件源
        self.assertEqual(r["status"], "published")
        self.assertEqual(r["affected"], ["B1", "C1"])
        rec = {x["cell"]: x for x in r["recomputed"]}
        # 新分支实际引用的单元格（按单元格序排列）
        self.assertEqual(rec["B1"]["depsAfter"], ["A1", "C1"])
        self.assertEqual(rec["B1"]["added"], ["A1"])
        self.assertEqual(rec["B1"]["removed"], ["A2"])
        self.assertEqual(rec["B1"]["oldValue"], 20)
        self.assertEqual(rec["B1"]["newValue"], 10)  # 重算得到新分支值，非旧分支缓存
        # 依赖变更展示
        self.assertEqual(r["depChanges"],
                         [{"cell": "B1", "added": ["A1"], "removed": ["A2"],
                           "depsAfter": ["A1", "C1"]}])
        # 缓存命中：未受影响项沿用旧代次
        hits = {h["cell"]: h for h in r["cacheHits"]}
        self.assertEqual(hits["A1"]["generation"], 1)
        self.assertEqual(hits["A2"]["generation"], 2)
        st = cells_by_id(t.snapshot())
        self.assertEqual(st["B1"]["value"], 10)
        self.assertEqual(st["B1"]["generation"], r["generation"])
        self.assertEqual(st["A2"]["generation"], 2)

    def test_inactive_branch_errors_are_ignored(self):
        t = Table()
        # 未进入分支中的未知引用 / 除零 / 溢出均不触发
        self.assertEqual(t.adjudicate("A1", "if(1, 5, B9 + 1)")["status"], "published")
        self.assertEqual(t.adjudicate("A2", "if(0, 1 / 0, 7)")["status"], "published")
        big = str(INT64_MAX)
        self.assertEqual(t.adjudicate("A3", f"if(1, 3, {big} + {big})")["status"], "published")
        st = cells_by_id(t.snapshot())
        self.assertEqual((st["A1"]["value"], st["A2"]["value"], st["A3"]["value"]), (5, 7, 3))
        self.assertEqual(st["A1"]["deps"], [])  # 未取分支不产生依赖


class TestPartialDownstreamPublish(unittest.TestCase):
    """修改仅被部分活动下游引用：受影响项共同进入新代次，其余保持不变。"""

    def test_partial_downstream(self):
        t = Table()
        t.adjudicate("A1", "2")                    # 代次 1
        t.adjudicate("B1", "A1 + 1")               # 代次 2，活动依赖 A1
        t.adjudicate("C1", "0")                    # 代次 3
        t.adjudicate("D1", "if(C1, A1, 100)")      # 代次 4，未实际引用 A1
        t.adjudicate("E1", "7")                    # 代次 5，完全无关
        r = t.adjudicate("A1", "5")                # 代次 6
        self.assertEqual(r["status"], "published")
        self.assertEqual(r["affected"], ["A1", "B1"])  # D1 非活动下游，不受影响
        st = cells_by_id(t.snapshot())
        # 受影响项共同发布为新代次
        self.assertEqual(st["A1"]["generation"], 6)
        self.assertEqual(st["B1"]["generation"], 6)
        self.assertEqual(st["B1"]["value"], 6)
        # 其余项的值与缓存代次保持不变
        self.assertEqual((st["D1"]["value"], st["D1"]["generation"]), (100, 4))
        self.assertEqual((st["E1"]["value"], st["E1"]["generation"]), (7, 5))
        self.assertEqual((st["C1"]["value"], st["C1"]["generation"]), (0, 3))
        hit_cells = {h["cell"] for h in r["cacheHits"]}
        self.assertTrue({"C1", "D1", "E1"} <= hit_cells)

    def test_transitive_affected(self):
        t = Table()
        t.adjudicate("A1", "1")
        t.adjudicate("B1", "A1 * 2")
        t.adjudicate("C1", "B1 * 3")
        t.adjudicate("D1", "9")
        r = t.adjudicate("A1", "2")
        self.assertEqual(r["affected"], ["A1", "B1", "C1"])
        st = cells_by_id(t.snapshot())
        self.assertEqual((st["B1"]["value"], st["C1"]["value"]), (4, 12))
        self.assertEqual(st["D1"]["generation"], 4)


class TestFailureRollback(unittest.TestCase):
    """活动分支失败：稳定链路 + 修改前状态完整保持。"""

    def _assert_preserved(self, t, before, report, kind, chain):
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["error"]["kind"], kind)
        self.assertEqual(report["error"]["chain"], chain)
        after = t.snapshot()
        self.assertEqual(before, after)  # 活动表、依赖边、缓存值、缓存代次均不变

    def test_cycle(self):
        t = Table()
        t.adjudicate("A1", "1")
        t.adjudicate("B1", "A1 + 1")
        before = t.snapshot()
        r = t.adjudicate("A1", "B1 + 1")
        self._assert_preserved(t, before, r, "cycle", ["A1", "B1", "A1"])

    def test_unknown_ref(self):
        t = Table()
        before = t.snapshot()
        r = t.adjudicate("D4", "E9 + 1")
        self._assert_preserved(t, before, r, "unknown_ref", ["D4", "E9"])

    def test_unknown_ref_via_downstream(self):
        t = Table()
        t.adjudicate("A1", "3")
        t.adjudicate("B1", "A1 * 2")
        before = t.snapshot()
        r = t.adjudicate("A1", "")  # 清空被活动下游引用的单元格
        self._assert_preserved(t, before, r, "unknown_ref", ["A1", "B1", "A1"])

    def test_div_zero(self):
        t = Table()
        t.adjudicate("A5", "1")
        t.adjudicate("B5", "7")
        t.adjudicate("C5", "B5 / A5")
        before = t.snapshot()
        r = t.adjudicate("A5", "0")
        self._assert_preserved(t, before, r, "div_zero", ["A5", "C5"])

    def test_overflow(self):
        t = Table()
        t.adjudicate("A7", "1")
        t.adjudicate("B7", str(INT64_MAX - 1))
        t.adjudicate("C7", "B7 + A7")
        before = t.snapshot()
        r = t.adjudicate("A7", "2")
        self._assert_preserved(t, before, r, "overflow", ["A7", "C7"])

    def test_overflow_ops(self):
        t = Table()
        t.adjudicate("A1", str(INT64_MIN))
        for cell, formula in [("B1", "A1 - 1"), ("B2", "-A1"), ("B3", "A1 / -1"),
                              ("B4", "3037000500 * 3037000500")]:
            r = t.adjudicate(cell, formula)
            self.assertEqual(r["status"], "rejected", formula)
            self.assertEqual(r["error"]["kind"], "overflow", formula)

    def test_chain_is_stable(self):
        t = Table()
        t.adjudicate("A5", "1")
        t.adjudicate("C5", "10 / A5")
        r1 = t.adjudicate("A5", "0")
        r2 = t.adjudicate("A5", "0")
        self.assertEqual(r1["error"], r2["error"])  # 同样的失败给出同样的链路

    def test_state_usable_after_rejection(self):
        t = Table()
        t.adjudicate("A1", "1")
        t.adjudicate("A1", "B9")          # 拒绝：未知引用
        r = t.adjudicate("A1", "42")      # 随后正常候选仍可发布
        self.assertEqual(r["status"], "published")
        self.assertEqual(cells_by_id(t.snapshot())["A1"]["value"], 42)

    def test_clear_without_downstream(self):
        t = Table()
        t.adjudicate("A1", "5")
        r = t.adjudicate("A1", "")
        self.assertEqual(r["status"], "published")
        st = cells_by_id(t.snapshot())
        self.assertFalse(st["A1"]["hasValue"])
        self.assertEqual(st["A1"]["kind"], "empty")


if __name__ == "__main__":
    unittest.main()
