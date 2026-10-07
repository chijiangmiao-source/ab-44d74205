# 地面标定 · 推导表（Calibration Sheet）

至多 128 格的推导表：每格为有符号 64 位整数常量，或由 `+ - * /`、单元格引用
`cN` 与条件式 `if(条件, 真值, 假值)` 组成的公式。仅条件实际进入的分支产生
**活动依赖**；可提交单格候选修改并查看裁决。

纯 Python 标准库实现（无第三方依赖），由 Compose 在可配置的宿主机端口提供页面、
JSON API 与健康响应；附带名为 `verify` 的一次性可执行验收。

## 语义要点

- **分支感知**：`if` 只求值实际进入的分支；切换条件源后，新分支实际引用的
  单元格成为活动依赖，旧分支依赖被移除，结果绝不沿用旧分支缓存。
- **独立暂存区**：候选先结合*修改前的反向活动依赖*在暂存区重算；上游格从修改前
  缓存读取，并同步替换依赖边。
- **部分下游原子发布**：受影响项（候选格 + 修改前反向活动下游闭包）共同发布为
  同一新代次；其余格的值、依赖边与缓存代次保持不变。裁决返回缓存命中、重算列表、
  新增/移除依赖与受影响格的活动依赖。
- **失败回滚**：活动分支出现环路、未知引用、除零或 64 位整数溢出（含
  `INT64_MIN / -1`）时，返回从候选格到首个失败点的稳定链路（确定性，重复运行一致），
  随后活动表、依赖边、缓存值与缓存代次完全维持修改前状态。
- **整数除法**向零截断。

## 运行（Docker Compose，推荐）

```bash
docker compose up --build            # 默认宿主机端口 8080
APP_PORT=9090 docker compose up      # 自定义宿主机端口
```

- 页面： http://localhost:8080/
- 健康： http://localhost:8080/health → `{"status":"ok", ...}`

环境变量：`APP_PORT`（容器内监听口，默认 8080）、`TABLE_SIZE`（默认 128）。

## 无 Docker 时直接运行

```bash
APP_PORT=8080 python3 -m app.server
```

## 一次性验收 verify

```bash
./verify                            # 本地：自动选空闲端口，结束自行退出
docker compose run --rm verify      # 容器内运行同一脚本
```

`verify` 依次穿插执行并以退出码报告结果（0 成功 / 非 0 失败）：

1. 构建检查：Python 字节码编译（存在 docker 时另做 `docker compose config` 校验）；
2. 代码测试：条件切换、部分下游发布、新边经缓存格成环、失败链路稳定性与回滚等
   22 个单元测试；
3. 启动真实 HTTP 服务并等待 `/health` 就绪；
4. 页面与健康接口的 API/HTTP 冒烟（8 个端到端测试，覆盖条件切换、部分下游发布、
   环路/除零/未知引用/溢出回滚、400/404）。

## HTTP API

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/health` | 健康响应 |
| GET  | `/` , `/app.js` | 页面与脚本 |
| GET  | `/api/table` | 当前表：每格类型、原文、值、活动依赖、缓存代次，及反向边 |
| POST | `/api/table` | 整表载入 `{"size":128,"cells":[{"index":0,"type":"const","value":1}, ...]}`（`type:"formula"` 用 `"raw"`） |
| POST | `/api/candidates` | 提交单格候选 `{"index":3,"type":"formula","raw":"if(c0,c1,c2)"}` |

候选成功返回 200：

```json
{"ok": true, "generation": 2,
 "affected": {"generation": 2, "recomputed": [0, 4], "cache_hits": [3],
              "deps_added": {"4": [3]}, "deps_removed": {"4": [2]},
              "active_deps": {"0": [], "4": [0, 3]}}}
```

失败返回 422，状态完整回滚：

```json
{"ok": false, "generation": 1,
 "failure": {"reason": "cycle", "point": 0,
             "chain": [0, 1, 0], "chain_labels": ["c0","c1","c0"],
             "detail": "active dependency cycle: c0 -> c1 -> c0"}}
```

`failure.reason` ∈ `unknown | overflow | cycle | divzero`。

## 目录

```
app/model.py     解析、分支感知依赖分析、暂存/原子发布/回滚
app/server.py    标准库 HTTP 服务与 JSON 序列化
app/web/         单页 UI（依赖增删、缓存命中/重算、失败链路）
tests/test_model.py    代码测试
tests/http_smoke.py    HTTP/API 端到端冒烟
verify           一次性验收（构建检查 + 代码测试 + HTTP 冒烟）
Dockerfile, docker-compose.yml
```
