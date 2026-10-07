"""一次性验收的 HTTP 冒烟与场景验收（对运行中的服务执行真实 HTTP 请求）。

覆盖：页面/健康冒烟、条件切换、部分下游发布、失败回滚（环路/未知引用/除零/溢出）。
以退出码报告结果：0 通过，1 失败。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASSES = []
FAILURES = []


def check(name, cond, detail=""):
    (PASSES if cond else FAILURES).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"  [{mark}] {name}"
    if not cond and detail:
        line += f"  | {detail}"
    print(line, flush=True)


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def req(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, payload=None):
        return self.req("POST", path, payload)

    def candidate(self, cell, inp):
        code, body = self.post("/api/candidate", {"cell": cell, "input": inp})
        return code, json.loads(body)

    def state(self):
        code, body = self.get("/api/state")
        assert code == 200, f"GET /api/state -> {code}"
        return json.loads(body)

    def reset(self):
        code, _ = self.post("/api/reset")
        assert code == 200, "reset 失败"


def cells_by_id(st):
    return {c["id"]: c for c in st["cells"]}


def wait_health(client, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            code, body = client.get("/health")
            if code == 200 and json.loads(body).get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ---------------- 冒烟 ----------------

def smoke(client):
    print("· 页面与健康接口冒烟", flush=True)
    code, body = client.get("/health")
    ok = code == 200 and json.loads(body).get("status") == "ok"
    check("GET /health 返回 200 且 status=ok", ok, f"code={code} body={body[:120]!r}")

    code, body = client.get("/")
    text = body.decode("utf-8", "replace")
    check("GET / 返回 200 的页面", code == 200 and "推导表" in text and 'id="grid"' in text,
          f"code={code}")
    code, _ = client.get("/no-such-path")
    check("未知路径返回 404", code == 404, f"code={code}")


# ---------------- 场景一：条件切换 ----------------

def scenario_condition_switch(client):
    print("· 场景：切换条件源（新分支引用 / 依赖增删 / 不沿用旧分支缓存）", flush=True)
    client.reset()
    for cell, inp in [("A1", "10"), ("A2", "20"), ("C1", "0"), ("B1", "if(C1, A1, A2)")]:
        code, rep = client.candidate(cell, inp)
        check(f"准备 {cell} 发布成功", code == 200 and rep["status"] == "published",
              f"code={code} rep={rep.get('error')}")
    st = cells_by_id(client.state())
    check("B1 初始取 false 分支值 20", st["B1"]["value"] == 20, f"value={st['B1']['value']}")
    check("B1 初始活动依赖为 [C1, A2]", st["B1"]["deps"] == ["C1", "A2"],
          f"deps={st['B1']['deps']}")

    code, rep = client.candidate("C1", "1")
    check("切换 C1=1 发布成功", code == 200 and rep["status"] == "published",
      f"code={code} err={rep.get('error')}")
    check("受影响项为 [B1, C1]", rep["affected"] == ["B1", "C1"],
          f"affected={rep['affected']}")
    rec = {x["cell"]: x for x in rep["recomputed"]}
    b1 = rec.get("B1", {})
    check("B1 重算为新分支值 10（非旧分支缓存）",
          b1.get("oldValue") == 20 and b1.get("newValue") == 10, f"B1={b1}")
    check("B1 新分支实际引用 [A1, C1]", b1.get("depsAfter") == ["A1", "C1"],
          f"depsAfter={b1.get('depsAfter')}")
    check("B1 依赖新增 [A1]、移除 [A2]",
          b1.get("added") == ["A1"] and b1.get("removed") == ["A2"],
          f"added={b1.get('added')} removed={b1.get('removed')}")
    check("depChanges 展示 B1 的依赖变更",
          any(d["cell"] == "B1" and d["added"] == ["A1"] and d["removed"] == ["A2"]
              for d in rep["depChanges"]), f"depChanges={rep['depChanges']}")
    hits = {h["cell"]: h for h in rep["cacheHits"]}
    check("缓存命中包含 A1/A2 且代次不变",
          hits.get("A1", {}).get("generation") == 1 and hits.get("A2", {}).get("generation") == 2,
          f"hits={hits}")
    st = cells_by_id(client.state())
    check("B1/C1 共同进入新代次，A1/A2 代次保持",
          st["B1"]["generation"] == rep["generation"] == st["C1"]["generation"]
          and st["A1"]["generation"] == 1 and st["A2"]["generation"] == 2,
          f"gens: B1={st['B1']['generation']} C1={st['C1']['generation']} "
          f"A1={st['A1']['generation']} A2={st['A2']['generation']}")


# ---------------- 场景二：部分下游发布 ----------------

def scenario_partial_downstream(client):
    print("· 场景：部分活动下游引用 → 受影响项共同发布，其余保持不变", flush=True)
    client.reset()
    for cell, inp in [("A1", "2"), ("B1", "A1 + 1"), ("C1", "0"),
                      ("D1", "if(C1, A1, 100)"), ("E1", "7")]:
        code, rep = client.candidate(cell, inp)
        check(f"准备 {cell} 发布成功", code == 200, f"code={code} err={rep.get('error')}")
    before = cells_by_id(client.state())
    gen_before = client.state()["generation"]

    code, rep = client.candidate("A1", "5")
    check("修改 A1=5 发布成功", code == 200 and rep["status"] == "published",
          f"code={code} err={rep.get('error')}")
    check("受影响项仅 [A1, B1]（D1 未实际引用 A1）", rep["affected"] == ["A1", "B1"],
          f"affected={rep['affected']}")
    st = cells_by_id(client.state())
    check("A1/B1 共同发布为新代次", st["A1"]["generation"] == rep["generation"]
          == st["B1"]["generation"] and rep["generation"] == gen_before + 1,
          f"gen={rep['generation']} before={gen_before}")
    check("B1 重算为 6", st["B1"]["value"] == 6, f"B1={st['B1']['value']}")
    check("D1 值与缓存代次保持不变",
          st["D1"]["value"] == before["D1"]["value"]
          and st["D1"]["generation"] == before["D1"]["generation"],
          f"D1={st['D1']}")
    check("E1/C1 值与缓存代次保持不变",
          st["E1"]["generation"] == before["E1"]["generation"]
          and st["C1"]["generation"] == before["C1"]["generation"],
          f"E1={st['E1']} C1={st['C1']}")
    hit_cells = {h["cell"] for h in rep["cacheHits"]}
    check("缓存命中展示包含 C1/D1/E1", {"C1", "D1", "E1"} <= hit_cells,
          f"hits={sorted(hit_cells)}")


# ---------------- 场景三：失败回滚 ----------------

def _rollback_case(client, name, setup, cell, inp, kind, chain):
    print(f"· 场景：失败回滚 — {name}", flush=True)
    client.reset()
    for c, i in setup:
        code, rep = client.candidate(c, i)
        check(f"准备 {c} 发布成功", code == 200, f"code={code} err={rep.get('error')}")
    before = client.state()

    code, rep = client.candidate(cell, inp)
    err = rep.get("error") or {}
    check(f"{name}：候选被拒绝（HTTP 409）", code == 409 and rep["status"] == "rejected",
          f"code={code} status={rep.get('status')}")
    check(f"{name}：错误类型为 {kind}", err.get("kind") == kind, f"err={err}")
    check(f"{name}：链路 {chain}", err.get("chain") == chain,
          f"chain={err.get('chain')}")
    check(f"{name}：链路自候选格出发", (err.get("chain") or [None])[0] == cell,
          f"chain={err.get('chain')}")

    code2, rep2 = client.candidate(cell, inp)
    check(f"{name}：链路稳定（重复提交结果一致）",
          code2 == 409 and rep2.get("error") == err, f"err2={rep2.get('error')}")

    after = client.state()
    check(f"{name}：活动表/依赖边/缓存值/缓存代次均保持修改前",
          before == after,
          "state 发生变化" if before != after else "")


def scenario_failures(client):
    _rollback_case(client, "环路",
                   [("A1", "1"), ("B1", "A1 + 1")],
                   "A1", "B1 + 1", "cycle", ["A1", "B1", "A1"])
    _rollback_case(client, "未知引用",
                   [],
                   "D4", "E9 + 1", "unknown_ref", ["D4", "E9"])
    _rollback_case(client, "除零",
                   [("A5", "1"), ("B5", "7"), ("C5", "B5 / A5")],
                   "A5", "0", "div_zero", ["A5", "C5"])
    _rollback_case(client, "64位溢出",
                   [("A7", "1"), ("B7", "9223372036854775806"), ("C7", "B7 + A7")],
                   "A7", "2", "overflow", ["A7", "C7"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="http://127.0.0.1:8080")
    ap.add_argument("--wait", type=int, default=60, help="等待健康检查通过的秒数")
    args = ap.parse_args()

    client = Client(args.addr)
    print(f"验收目标：{args.addr}", flush=True)
    if not wait_health(client, args.wait):
        print("  [FAIL] 服务健康检查未通过（超时）", flush=True)
        sys.exit(1)
    check("服务健康检查通过", True)

    smoke(client)
    scenario_condition_switch(client)
    scenario_partial_downstream(client)
    scenario_failures(client)

    print(f"\n验收结果：{len(PASSES)} 项通过，{len(FAILURES)} 项失败", flush=True)
    if FAILURES:
        print("失败项：", flush=True)
        for f in FAILURES:
            print(f"  - {f}", flush=True)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
