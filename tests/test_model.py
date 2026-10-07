"""Unit tests for the branch-aware staging / publication protocol."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.model import (  # noqa: E402
    INT64_MAX,
    INT64_MIN,
    CellKind,
    EvalError,
    FailReason,
    Table,
)


def load(table: Table, entries: list) -> None:
    table.reset(entries)


class ConditionSwitchTests(unittest.TestCase):
    def setUp(self):
        self.t = Table(16)
        load(self.t, [
            {"index": 0, "type": "const", "value": 1},   # condition source
            {"index": 1, "type": "const", "value": 7},
            {"index": 2, "type": "const", "value": 10},
            {"index": 3, "type": "const", "value": 20},
            {"index": 4, "type": "formula", "raw": "if(c0, c2, c3)"},
        ])

    def test_initial_active_branch_only(self):
        self.assertEqual(self.t.cells[4].value, 10)
        self.assertEqual(set(self.t.cells[4].deps), {0, 2})
        self.assertEqual(self.t.cells[4].generation, 1)

    def test_switch_shows_new_branch_refs_and_diff(self):
        result = self.t.submit_candidate(0, "const", value=0)
        self.assertTrue(result.ok, result.failure)
        aff = result.affected
        # Candidate and the single active downstream are republished together.
        self.assertEqual(aff.recomputed, [0, 4])
        # The new branch target is read from cache; old branch target is not.
        self.assertEqual(aff.cache_hits, [3])
        self.assertEqual(aff.deps_added, {4: [3]})
        self.assertEqual(aff.deps_removed, {4: [2]})
        self.assertEqual(aff.active_deps[4], [0, 3])

        # New value actually comes from the newly entered branch.
        self.assertEqual(self.t.cells[4].value, 20)
        self.assertEqual(set(self.t.cells[4].deps), {0, 3})

        # Stale-branch cell keeps its value and cache generation.
        self.assertEqual(self.t.cells[2].value, 10)
        self.assertEqual(self.t.cells[2].generation, 1)
        # New-branch cache cell was not republished either.
        self.assertEqual(self.t.cells[3].generation, 1)
        # Affected items share the new generation.
        self.assertEqual(self.t.cells[0].generation, 2)
        self.assertEqual(self.t.cells[4].generation, 2)

    def test_switch_back_does_not_reuse_old_branch_cache(self):
        self.t.submit_candidate(0, "const", value=0)
        self.assertEqual(self.t.cells[4].value, 20)
        result = self.t.submit_candidate(0, "const", value=5)
        self.assertTrue(result.ok)
        self.assertEqual(self.t.cells[4].value, 10)
        self.assertEqual(set(self.t.cells[4].deps), {0, 2})
        self.assertEqual(result.affected.deps_added, {4: [2]})
        self.assertEqual(result.affected.deps_removed, {4: [3]})

    def test_dead_branch_is_not_evaluated_at_load(self):
        # Division by zero sits in the non-entered branch: load must succeed.
        t = Table(8)
        load(t, [
            {"index": 0, "type": "const", "value": 1},
            {"index": 1, "type": "formula", "raw": "if(c0, 42, 1/0)"},
        ])
        self.assertEqual(t.cells[1].value, 42)
        self.assertEqual(set(t.cells[1].deps), {0})


class PartialDownstreamTests(unittest.TestCase):
    def setUp(self):
        self.t = Table(16)
        load(self.t, [
            {"index": 0, "type": "const", "value": 5},
            {"index": 1, "type": "const", "value": 9},
            {"index": 2, "type": "formula", "raw": "c0 + 1"},     # downstream
            {"index": 3, "type": "formula", "raw": "c1 * 2"},     # independent
            {"index": 4, "type": "formula", "raw": "c2 + c3"},    # joins both
        ])

    def test_only_active_downstream_republished(self):
        result = self.t.submit_candidate(0, "const", value=6)
        self.assertTrue(result.ok, result.failure)
        # c0 -> c2 -> c4 are the active reverse closure. c1/c3 are not in it.
        self.assertEqual(result.affected.recomputed, [0, 2, 4])
        # c4 reads c3 from the untouched cache.
        self.assertEqual(result.affected.cache_hits, [3])
        self.assertEqual(self.t.cells[2].value, 7)
        self.assertEqual(self.t.cells[4].value, 25)       # 7 + 18
        self.assertEqual(self.t.cells[3].value, 18)

        gen = self.t.generation
        self.assertEqual(gen, 2)
        for cid in (0, 2, 4):
            self.assertEqual(self.t.cells[cid].generation, gen)
        # Unaffected items keep both value and cache generation.
        for cid in (1, 3):
            self.assertEqual(self.t.cells[cid].generation, 1)
        self.assertEqual(self.t.cells[3].value, 18)

    def test_new_branch_reads_independent_formula_from_cache(self):
        # A newly entered branch referencing a formula cell that has no
        # dependency on the modified region: its value is unchanged, so it is
        # served from cache and keeps its cache generation; only the new edge
        # is recorded on the candidate cell.
        t = Table(16)
        load(t, [
            {"index": 0, "type": "const", "value": 1},
            {"index": 1, "type": "const", "value": 4},
            {"index": 2, "type": "formula", "raw": "if(c0, 5, c3)"},
            {"index": 3, "type": "formula", "raw": "c1 + 1"},
            {"index": 4, "type": "formula", "raw": "c3 * 10"},
        ])
        # Initially c2 does not reference c3; c4 is downstream of c3 only.
        self.assertEqual(set(t.cells[2].deps), {0})
        result = t.submit_candidate(0, "const", value=0)
        self.assertTrue(result.ok, result.failure)
        # Only the condition source and its active downstream c2 are published.
        self.assertEqual(result.affected.recomputed, [0, 2])
        self.assertEqual(t.cells[2].value, 5)
        # c3/c4 keep value, edges and their original cache generation.
        self.assertEqual(t.cells[3].value, 5)
        self.assertEqual(t.cells[4].value, 50)
        self.assertEqual(t.cells[3].generation, 1)
        self.assertEqual(t.cells[4].generation, 1)
        # The newly entered edge is still shown as an added dependency.
        self.assertEqual(set(t.cells[2].deps), {0, 3})
        self.assertEqual(result.affected.deps_added, {2: [3]})
        self.assertEqual(result.affected.deps_removed, {})
        # c3 is served whole from the pre-change cache (its own upstream c1
        # is not re-read during staging).
        self.assertEqual(result.affected.cache_hits, [3])

    def test_new_edge_closing_cycle_through_cached_cell_rolls_back(self):
        # c3 is NOT an old downstream of c2 (the edge is inactive before the
        # switch).  Activating c2 -> c3 closes a cycle c2 -> c3 -> c2; the
        # candidate must fail even though c3 would otherwise be a cache hit.
        t = Table(16)
        load(t, [
            {"index": 0, "type": "const", "value": 1},
            {"index": 1, "type": "const", "value": 4},
            {"index": 2, "type": "formula", "raw": "if(c0, 5, c3)"},
            {"index": 3, "type": "formula", "raw": "c1 + c2"},
        ])
        before = {c.idx: (c.value, tuple(sorted(c.deps)), c.generation)
                  for c in t.cells}
        result = t.submit_candidate(0, "const", value=0)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.CYCLE)
        chain = result.failure.chain
        self.assertEqual(chain[0], 0)
        # Chain starts at the candidate and closes the c2 <-> c3 loop.
        self.assertEqual(chain[0:2], [0, 2])
        self.assertIn(3, chain)
        self.assertEqual(chain[-1], 2)
        self.assertEqual(t.generation, 1)
        for cid, (value, deps, gen) in before.items():
            cell = t.cells[cid]
            self.assertEqual(cell.value, value)
            self.assertEqual(tuple(sorted(cell.deps)), deps)
            self.assertEqual(cell.generation, gen)


class FailureRollbackTests(unittest.TestCase):
    def setUp(self):
        self.t = Table(16)
        load(self.t, [
            {"index": 0, "type": "const", "value": 5},
            {"index": 1, "type": "formula", "raw": "c0 + 1"},
        ])

    def _assert_unchanged(self, gen=1):
        self.assertEqual(self.t.generation, gen)
        self.assertEqual(self.t.cells[0].kind, CellKind.CONST)
        self.assertEqual(self.t.cells[0].value, 5)
        self.assertEqual(self.t.cells[1].value, 6)
        self.assertEqual(set(self.t.cells[1].deps), {0})
        for cell in self.t.cells:
            self.assertLessEqual(cell.generation, gen)

    def test_cycle_candidate_is_rolled_back_with_chain(self):
        result = self.t.submit_candidate(0, "formula", raw="c1")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.CYCLE)
        # Stable chain starts at the candidate and closes the loop.
        self.assertEqual(result.failure.chain[0], 0)
        self.assertEqual(result.failure.chain[-1], 0)
        self.assertEqual(result.failure.chain, [0, 1, 0])
        self.assertEqual(result.failure.point, 0)
        self._assert_unchanged()

    def test_failure_chain_is_stable_across_runs(self):
        chains = []
        for _ in range(3):
            r = self.t.submit_candidate(0, "formula", raw="c1")
            chains.append(r.failure.chain)
        self.assertTrue(all(c == chains[0] for c in chains))
        self._assert_unchanged()

    def test_unknown_reference_chain(self):
        result = self.t.submit_candidate(0, "formula", raw="c2 + 1")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.UNKNOWN)
        self.assertEqual(result.failure.chain, [0, 2])
        self.assertEqual(result.failure.point, 2)
        self._assert_unchanged()

    def test_overflow_chain(self):
        result = self.t.submit_candidate(0, "formula",
                                         raw=f"{INT64_MAX} + 1")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.OVERFLOW)
        self.assertEqual(result.failure.chain, [0])
        self._assert_unchanged()

    def test_multiply_overflow(self):
        result = self.t.submit_candidate(0, "formula",
                                         raw=f"{INT64_MAX} * 2")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.OVERFLOW)
        self._assert_unchanged()

    def test_division_by_zero_chain(self):
        result = self.t.submit_candidate(0, "formula", raw="1 / 0")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.DIVZERO)
        self.assertEqual(result.failure.chain, [0])
        self._assert_unchanged()

    def test_div_minus_one_overflow(self):
        result = self.t.submit_candidate(0, "formula",
                                         raw=f"({INT6_MIN_RAW}) / -1")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.OVERFLOW)


INT6_MIN_RAW = str(INT64_MIN)


class SwitchIntoFailingBranchTests(unittest.TestCase):
    def setUp(self):
        self.t = Table(8)
        load(self.t, [
            {"index": 0, "type": "const", "value": 1},
            {"index": 1, "type": "formula", "raw": "if(c0, 42, 1/0)"},
        ])

    def test_switching_into_dead_failing_branch_rolls_back(self):
        before = {c.idx: (c.value, tuple(sorted(c.deps)), c.generation)
                  for c in self.t.cells}
        result = self.t.submit_candidate(0, "const", value=0)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.DIVZERO)
        # Chain runs from candidate cell through the downstream formula.
        self.assertEqual(result.failure.chain[0], 0)
        self.assertIn(1, result.failure.chain)
        # Everything (including c0's value and cache generations) is restored.
        for cid, (value, deps, gen) in before.items():
            cell = self.t.cells[cid]
            self.assertEqual(cell.value, value)
            self.assertEqual(tuple(sorted(cell.deps)), deps)
            self.assertEqual(cell.generation, gen)
        self.assertEqual(self.t.generation, 1)
        self.assertEqual(self.t.cells[0].value, 1)
        self.assertEqual(self.t.cells[1].value, 42)


class LoadTests(unittest.TestCase):
    def test_failed_load_restores_previous_state(self):
        t = Table(8)
        t.reset([{"index": 0, "type": "const", "value": 42}])
        with self.assertRaises(EvalError):
            t.reset([
                {"index": 1, "type": "formula", "raw": "c2"},
                {"index": 2, "type": "formula", "raw": "c1"},
            ])
        self.assertEqual(t.cells[0].value, 42)
        self.assertEqual(t.cells[1].kind, CellKind.EMPTY)
        self.assertEqual(t.generation, 1)

    def test_candidate_on_empty_cell(self):
        t = Table(8)
        t.reset([{"index": 0, "type": "const", "value": 42}])
        result = t.submit_candidate(5, "formula", raw="c0 + 8")
        self.assertTrue(result.ok, result.failure)
        self.assertEqual(result.affected.recomputed, [5])
        self.assertEqual(t.cells[5].value, 50)
        self.assertEqual(t.cells[5].generation, 2)
        self.assertEqual(t.cells[0].generation, 1)
        self.assertEqual(result.affected.cache_hits, [0])

    def test_self_loop_chain(self):
        t = Table(4)
        t.reset([{"index": 0, "type": "const", "value": 1}])
        result = t.submit_candidate(1, "formula", raw="c1 + 1")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.reason, FailReason.CYCLE)
        self.assertEqual(result.failure.chain, [1, 1])
        self.assertEqual(t.cells[1].kind, CellKind.EMPTY)
        self.assertEqual(t.generation, 1)


class ArithmeticTests(unittest.TestCase):
    def test_truncating_division(self):
        t = Table(8)
        cases = [("7 / 2", 3), ("-7 / 2", -3), ("7 / -2", -3),
                 ("-7 / -2", 3), (f"{INT64_MIN} / 2", INT64_MIN // 2)]
        for i, (raw, expected) in enumerate(cases):
            t.reset([{"index": i, "type": "formula", "raw": raw}])
            self.assertEqual(t.cells[i].value, expected, raw)

    def test_signed_literals(self):
        t = Table(2)
        t.reset([{"index": 0, "type": "formula", "raw": "-5"}])
        self.assertEqual(t.cells[0].value, -5)
        t.reset([{"index": 0, "type": "formula", "raw": f"-{INT64_MAX} - 1"}])
        self.assertEqual(t.cells[0].value, INT64_MIN)
        with self.assertRaises(EvalError):
            t.reset([{"index": 0, "type": "formula", "raw": "-"}])

    def test_table_size_bounded(self):
        with self.assertRaises(ValueError):
            Table(129)

    def test_constant_range_check(self):
        t = Table(2)
        with self.assertRaises(EvalError):
            t.submit_candidate(0, "const", value=INT64_MAX + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
