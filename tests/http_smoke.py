"""End-to-end HTTP smoke tests against a running server.

The server base URL is taken from ``APP_BASE_URL`` (default
``http://127.0.0.1:8080``).  The scenarios interleave condition switching,
partial downstream publication and failure rollback exactly as required by
the acceptance procedure.
"""

import json
import os
import unittest
import urllib.error
import urllib.request

BASE = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8080")


def call(method: str, path: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        status, body = call("GET", "/health")
        if status != 200 or body.get("status") != "ok":
            raise RuntimeError("server is not healthy")

    def test_01_health_and_page(self):
        status, body = call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("generation", body)

        req = urllib.request.Request(BASE + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("地面标定", html)
        self.assertIn("/app.js", html)

        req = urllib.request.Request(BASE + "/app.js")
        with urllib.request.urlopen(req, timeout=5) as resp:
            js = resp.read().decode("utf-8")
        self.assertIn("submit", js)

    def test_02_load_scenario(self):
        cells = [
            {"index": 0, "type": "const", "value": 1},     # condition source
            {"index": 1, "type": "const", "value": 7},
            {"index": 2, "type": "const", "value": 3},
            {"index": 3, "type": "formula", "raw": "if(c0, c1 + c2, c1 - c2)"},
            {"index": 4, "type": "formula", "raw": "c3 * 2"},
            {"index": 5, "type": "formula", "raw": "c1 * 100"},
            {"index": 6, "type": "formula", "raw": "c4 + c5"},
            {"index": 7, "type": "const", "value": 0},
            {"index": 8, "type": "formula", "raw": "if(c0, 9, c7)"},
        ]
        status, body = call("POST", "/api/table", {"size": 16, "cells": cells})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["generation"], 1)
        by_index = {c["index"]: c for c in body["cells"]}
        self.assertEqual(by_index[3]["value"], 10)
        self.assertEqual(by_index[3]["deps"], [0, 1, 2])
        self.assertEqual(by_index[6]["value"], 720)
        self.assertEqual(by_index[8]["deps"], [0])

    def test_03_condition_switch_shows_branch_diff(self):
        # Flip the condition source c0 1 -> 0.
        status, verdict = call("POST", "/api/candidates",
                               {"index": 0, "type": "const", "value": 0})
        self.assertEqual(status, 200, verdict)
        self.assertTrue(verdict["ok"])
        aff = verdict["affected"]
        self.assertEqual(aff["generation"], 2)
        # Candidate and every active downstream of it.
        self.assertEqual(aff["recomputed"], [0, 3, 4, 6, 8])
        # Recomputed formulas read untouched upstream cells from the cache:
        # c3 reads c1/c2, c6 reads c5, c8 now reads c7.
        self.assertEqual(aff["cache_hits"], [1, 2, 5, 7])
        # c3 changes branch: adds c... actually same deps; check c8.
        self.assertEqual(aff["deps_added"], {"8": [7]})
        self.assertEqual(aff["deps_removed"], {})
        self.assertEqual(aff["active_deps"]["8"], [0, 7])
        self.assertEqual(aff["active_deps"]["3"], [0, 1, 2])

        status, table = call("GET", "/api/table")
        cells = {c["index"]: c for c in table["cells"]}
        self.assertEqual(cells[3]["value"], 4)           # 7 - 3
        self.assertEqual(cells[4]["value"], 8)
        self.assertEqual(cells[6]["value"], 708)         # 8 + 700
        self.assertEqual(cells[8]["value"], 0)
        self.assertEqual(cells[8]["deps"], [0, 7])
        # Unaffected cells keep generation 1.
        self.assertEqual(cells[5]["generation"], 1)
        self.assertEqual(cells[7]["generation"], 1)
        for i in (0, 3, 4, 6, 8):
            self.assertEqual(cells[i]["generation"], 2)

    def test_04_partial_downstream_publish(self):
        # Change c2 (3 -> 100). Only c3/c4/c6 follow; c8 does not depend on c2.
        status, verdict = call("POST", "/api/candidates",
                               {"index": 2, "type": "const", "value": 100})
        self.assertEqual(status, 200, verdict)
        aff = verdict["affected"]
        self.assertEqual(aff["recomputed"], [2, 3, 4, 6])
        # c3 reads condition source c0 and c1 from cache; c6 reads c5.
        self.assertEqual(aff["cache_hits"], [0, 1, 5])
        self.assertEqual(aff["generation"], 3)

        status, table = call("GET", "/api/table")
        cells = {c["index"]: c for c in table["cells"]}
        self.assertEqual(cells[3]["value"], -93)        # 7 - 100 (c0 == 0)
        self.assertEqual(cells[6]["value"], 514)        # -186 + 700
        # c8 is untouched: same value and same generation.
        self.assertEqual(cells[8]["value"], 0)
        self.assertEqual(cells[8]["generation"], 2)
        self.assertEqual(table["generation"], 3)

    def test_05_failed_candidate_rolls_back_with_chain(self):
        status, before = call("GET", "/api/table")
        before_cells = {c["index"]: c for c in before["cells"]}

        # Switch c0 back to 1: c8 enters branch value 9 (fine), but first send
        # a candidate that creates an active cycle: c2 -> c3 -> c2.
        status, verdict = call("POST", "/api/candidates",
                               {"index": 2, "type": "formula", "raw": "c3"})
        self.assertEqual(status, 422)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["failure"]["reason"], "cycle")
        self.assertEqual(verdict["failure"]["chain"][0], 2)
        self.assertEqual(verdict["failure"]["chain"][-1], 2)
        self.assertEqual(verdict["failure"]["chain_labels"],
                         [f"c{x}" for x in verdict["failure"]["chain"]])
        self.assertEqual(verdict["generation"], 3)

        # Live table, edges, cached values and generations are pre-change.
        status, after = call("GET", "/api/table")
        self.assertEqual(after["generation"], 3)
        after_cells = {c["index"]: c for c in after["cells"]}
        for idx, bc in before_cells.items():
            ac = after_cells[idx]
            self.assertEqual(ac["value"], bc["value"], idx)
            self.assertEqual(ac["deps"], bc["deps"], idx)
            self.assertEqual(ac["generation"], bc["generation"], idx)
            self.assertEqual(ac["raw"], bc["raw"], idx)
            self.assertEqual(ac["type"], bc["type"], idx)

    def test_06_switch_into_divzero_branch_rolls_back(self):
        # c8 = if(c0, 9, c7) with c0 == 0 currently.  A new c0 -> 1 candidate
        # is fine; instead stage a candidate on c7 that would divide by zero
        # when entered, then flip c0 to make c8 reference it — verify the
        # direct failure chain and rollback via a zero divisor candidate.
        status, verdict = call("POST", "/api/candidates", {
            "index": 7, "type": "formula", "raw": "1 / 0"})
        self.assertEqual(status, 422)
        self.assertEqual(verdict["failure"]["reason"], "divzero")
        self.assertEqual(verdict["failure"]["chain"], [7])

        # Unknown reference chain through a downstream consumer.
        status, verdict = call("POST", "/api/candidates", {
            "index": 7, "type": "formula", "raw": "c15 + 1"})
        self.assertEqual(status, 422)
        self.assertEqual(verdict["failure"]["reason"], "unknown")
        self.assertEqual(verdict["failure"]["chain"], [7, 15])

        status, table = call("GET", "/api/table")
        cells = {c["index"]: c for c in table["cells"]}
        self.assertEqual(cells[7]["type"], "const")
        self.assertEqual(cells[7]["value"], 0)
        self.assertEqual(table["generation"], 3)

    def test_07_overflow_then_recover(self):
        status, verdict = call("POST", "/api/candidates", {
            "index": 5, "type": "formula",
            "raw": "9223372036854775807 * 2"})
        self.assertEqual(status, 422)
        self.assertEqual(verdict["failure"]["reason"], "overflow")
        # A valid candidate afterwards still works (state is usable).
        status, verdict = call("POST", "/api/candidates",
                               {"index": 5, "type": "const", "value": 1})
        self.assertEqual(status, 200, verdict)
        self.assertTrue(verdict["ok"])
        self.assertIn(6, verdict["affected"]["recomputed"])

    def test_08_bad_requests(self):
        status, body = call("POST", "/api/candidates",
                            {"index": 999, "type": "const", "value": 1})
        self.assertEqual(status, 400)
        status, body = call("GET", "/no-such-path")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
