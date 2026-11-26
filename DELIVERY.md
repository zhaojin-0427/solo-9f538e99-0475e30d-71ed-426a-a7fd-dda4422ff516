# 交付说明（DELIVERY）

---

# 第二轮：厘米测绘稿 → 整米最优取整单（2026-11-26）

本轮在第一轮“共边核验 / 签收 / 制图供数”平台上，按新要求增加了**厘米坐标下/上
取整、保共边、绕开库中宗地、最优 + 字典序平局、分组不指数枚举、整单原子入库**
能力。第一轮全部功能与 29 个用例保持不变，新增 26 个用例，**合计 55 passed**。

## A. 要求 → 实现对照

| 要求 | 实现位置 | 说明 / 验证 |
| --- | --- | --- |
| `/docs` 提交宗地号 + 整数厘米轴对齐矩形 + 须保留共边的宗地对 | `POST /api/rounding/orders`；`app/schemas.py::RoundingOrderIn` | `/docs` 可见并可直接 Try it out；兼容 `rect_cm/rect/bbox`、4 角点 `ring`、宗地号→矩形映射、并行 `parcel_nos+rectangles`、对象/数组式宗地对；另挂 `/api/orders/round-cm`、`/api/rounding` 等别名，并让老 `POST /api/orders` 识别 `"unit":"cm"` 取整报文 |
| 每条边只能向下或向上取整米 | `app/rounding.py::edge_choices` | `floor(c/100)` 与 `ceil(c/100)`；负坐标按数学 floor/ceil（如 -105cm→-2m/-1m）；恰为整米只有一个选择 |
| 取整后正面积 | `rectangle_candidates` | 严格 `L<R 且 B<T`，否则该候选丢弃；退化厘米稿也可能“向外取整”出正面积，故不提前 422 |
| 指定宗地对仍有正长度共边 | `rects_shared_edge_length` + 分量内约束；库内任意形状用 `geometry.shared_segments` | 严格正长度，单点接触不算（有专项无解用例 + 正长度对照）；支持“本单↔本单”“本单↔库内宗地”“两宗库内地”三种对 |
| 本单之间、与库中未改单宗地不得正面积叠压 | 候选级 `has_interior_overlap` + 分量枚举 `rects_positive_overlap` + 落库前整单冲突校验 | 边界共边合法；库内回读沿用 bbox 候选规划，只回读可能相交的外环 |
| 各边位移绝对值之和最小 | `Candidate.cost_cm`，分量内选 `(cost, lex)` 最小 | 位移用厘米整数 `|cm-100m|`；用米表示只相差 100 倍常数，不影响选解；响应同时给 `total_cost_cm` 与 `total_displacement_m` |
| 平局：宗地号排序，逐宗左、下、右、上字典序最小 | `solve_rounding` 的 `lex_tuple` / 全局 `lex_key` | 有“四余 50 全向下”和“A/Z 交错编号”专项用例 |
| 候选范围互不相交、组间无共边要求时组合数不随组数指数增长 | 耦合图连通分量 + 逐分量 `itertools.product` | `combinations_checked=Σ_分量∏候选数`；60 独立组实查 240 组、500 组 54ms；响应给 `groups/combinations_checked/naive_joint_combinations` 可核对 |
| 按入库时现行宗地图求解 | `service.submit_rounding_order` | 在同一 `BEGIN IMMEDIATE` 写事务内读现行库 → 求解 → 落库，写锁串行化并发整单 |
| 沿用整单改界规则 | `service.persist_validated_order`（从 `apply_order` 抽出的事务体内核） | 取整结果构造为整米 `OrderIn`，复用冲突整单退回、upsert、共边版本对账/签收失效、orders 记录 |
| 成功返回整米坐标 | 响应 `rectangles[].rect/ring/bbox` 等 | 全部 `int`；GET 宗地复核库内即整米环 |
| 无解照旧 | `409 no_feasible_rounding` + 回滚 | 不写 parcels/orders；有用例核对坏单 404、原环不变、已签收 feed 仍在 |
| 写入失败照旧 | 同一事务 `except: ROLLBACK` | monkeypatch 共边阶段写故障，验证两宗旧环与 orders 计数不变 |
| 沿用旧 SQLite 卷 | **未改 `app/db.py` / 表结构 / `docker-compose.yml` 卷** | 仍是 `cadastre-data:/data` → `/data/cadastre.db`；旧卷直接被新代码打开 |
| docker compose 启动 | `Dockerfile` `COPY app ./app` 自动含 `rounding.py/solver.py` | compose YAML 解析通过；沙箱无 docker，未实跑构建（见 D 节） |

额外提供 `POST /api/rounding/solve`（及 `/api/rounding/plan`、`/api/round-rectangles`
别名）做不落库 dry-run，始终 200 并带 `feasible`，便于先看最优解与组合统计。

## B. 算法与“不指数”的正确性说明

1. 每宗矩形 4 条边各 ≤2 个取值 → ≤16 个候选，先过正面积/坐标范围，再过
   “与每个库内障碍无正面积叠压”“与固定库内共边对保持共边”两个一元过滤。
2. 建无向耦合图：两宗本单宗地之间，若有共边保留要求，或**存在**一对候选会正面积
   叠压，则连边（先用候选并集包络做 O(1) 排除）。
3. 连通分量之间：没有共边要求，且任意候选对都不会叠压，因此约束完全解耦；总代价
   为各分量代价之和。逐分量穷举 `(总位移, 分量字典序键)` 最小者，再合并。
4. 全局字典序可由“各分量取各自字典序最小”直接合并得到：全局最早出现的差异坐标
   必属于某个分量，而该分量已在其代价最优解集内取了字典序最小（有随机差分测试背书）。
5. `tests/test_rounding.py` 中 3 个随机种子 ×30 例，把分解求解器与**不做分解的全
   笛卡尔积暴力枚举**逐例比对最优矩形、代价、字典序键与可行性，全部一致。

## C. 本轮实测（均为本环境真实执行）

依赖仍装在 `/tmp/pylibs`（fastapi 0.165.0 / pydantic 2.14.4 / pytest 9.2.0 / httpx）。

```bash
PYTHONPATH=/tmp/pylibs python3 -m pytest -q
# 55 passed, 1 warning in ~8s     （第一轮 29 + 本轮 26）

PYTHONPATH=/tmp/pylibs python3 scripts/trial.py
# 全部试跑断言通过（第一轮 25 条叙述断言无回归）

PYTHONPATH=/tmp/pylibs python3 scripts/trial_rounding.py
# 21 条断言全部通过，覆盖：
#  保共边 forced x=11（96cm）、绕开 O 取 [509,0,510,10]、
#  余数 50 字典序全向下、60 独立组实查 240（线性）、
#  无解 409 且宗地/单/供数照旧
```

真实 uvicorn + curl（与 Docker 同一启动模块，端口 8137，临时库 `/tmp/live_round.db`）：

```
GET  /health                                   -> {"status":"ok","database":"ok"}
GET  /openapi.json                             -> 含 /api/rounding/orders,/api/rounding/solve
POST /api/orders SEED O=[510,0]-[520,10]       -> applied
POST /api/rounding/orders A/B 保共边            -> A=[0,0,11,10] B=[11,0,20,10] cost96 checked4
POST /api/rounding/orders P 绕开 O              -> P=[509,0,510,10]
GET  /api/shared-edges A/B                     -> 1 条 [[11,0],[11,10]]
POST /api/rounding/orders 无解 Z=[0,0,10,10]    -> HTTP 409 no_feasible_rounding
GET  /api/parcels/Z ; /api/orders/BAD          -> 404 ; 404
GET  /docs                                     -> 200
```

## D. docker / compose 实测边界（如实说明）

- 本沙箱**没有 `docker` / `podman` / `nerdctl` 可执行文件**（`/.dockerenv` 表明
  自身也在容器内），因此 `docker compose build/up` 本轮仍**未能真实执行**。
- 已核对：`docker-compose.yml` 通过 PyYAML 解析，服务/端口 `8000:8000`/命名卷
  `cadastre-data:/data`/`CADASTRE_DB_PATH=/data/cadastre.db`/healthcheck 不变；
  `Dockerfile` 为 `python:3.12-slim` + `pip install -r requirements.txt` +
  `COPY app ./app`，新增 `app/rounding.py`、`app/solver.py` 会自动入镜像，
  启动命令仍是上面已用等价方式实测通过的 `uvicorn app.main:app --host 0.0.0.0 --port 8000`。
- 目标机有 Docker 时：`docker compose up --build`，开 <http://localhost:8000/docs>。

## E. 本轮新增 / 改动文件

```
app/rounding.py        # 纯求解器：取整候选、几何谓词、耦合分组、最优+字典序
app/solver.py          # 命名别名（app.solver.solve / solve_rounding ...）
app/schemas.py         # RoundingOrderIn + 宽松报文归一；OrderIn 增量识别取整单
app/errors.py          # InvalidRoundingRequestError(422) / NoFeasibleRoundingError(409)
app/service.py         # 抽出 persist_validated_order；新增 submit/plan_rounding_order
app/main.py            # /api/rounding/* 路由与路径别名；/api/orders 分流
tests/test_rounding.py # 26 个用例（算法/随机差分/API/事务回滚/固定库共边）
scripts/trial_rounding.py
README.md / DELIVERY.md
```

## F. 仍未完成 / 已知限制（第二轮视角）

1. **未实跑 Docker 构建**：原因同 D，沙箱无容器运行时；只做到 YAML 解析 + 等价
   uvicorn/curl 实测。
2. **组内仍是穷举**：只保证“组数”不指数；单个强耦合组若含极多宗地，`∏候选数`
   仍可能很大。任务只要求互不相交组不指数，且候选每宗 ≤16；如未来出现数百宗
   强耦合矩形链，需要再上 CSP/动态规划。当前对链/簇状常见场景足够（随机差分
   覆盖到 4 宗全枚举）。
3. **厘米矩形模型**：本轮入口按题意只做**轴对齐矩形**；第一轮的任意正交外环能力
   仍保留在 `/api/orders`，但厘米求解器不直接处理 L 形等厘米多边形（可先取其
   bbox 或扩展候选生成）。
4. **无解状态码取舍**：依赖现状库的无解按基线“冲突”惯例返回 `409`；纯报文形态
   错误（非整数、越界厘米范围、引用未知宗地等）返回 `422/404`。另提供 dry-run
   200 + `feasible:false` 以便不依赖状态码的集成。
5. 第一轮 DELIVERY 末尾的其他限制（无鉴权、共边身份启发式匹配、无 R-tree 等）
   仍然成立。

---
---

# 第一轮交付说明（2026-11-25，原文保留）

交付时间：2026-11-25。分支 `main`，基线提交 `3f7a0fe`，本次改动均为新增文件，
尚未提交 git（按约定需要你确认后再 commit）。

---

## 一、完成的功能（对照原始要求逐条）

| 原始要求 | 实现位置 | 说明 / 验证 |
| --- | --- | --- |
| FastAPI，`/docs` 入口 | `app/main.py` | `/docs` 为 Swagger UI，资源已下载自托管到 `app/static/vendor/`，离线浏览器也能开；另有 `/redoc`、`/openapi.json` |
| SQLite 存一个测区 | `app/db.py` | `survey_area` 为 `id=1` 单例行；一个库文件 = 一个测区，库路径由 `CADASTRE_DB_PATH` 控制 |
| 按宗地号录入 / 更正外环 | `POST /api/orders`、`PUT /api/parcels/{no}/ring` | upsert 语义；一单可多宗，宗地号单内去重 |
| 地块无洞 | `app/schemas.py` | 只建模 `ring`；`holes` 可显式传 `[]`，传非空内环直接 422 |
| 边与坐标轴平行 | `app/geometry.py::normalize_ring` | 逐边检查 `x` 相等或 `y` 相等，斜边 422 |
| ±100 万米内整数坐标 | `app/schemas.py` + `geometry.py` | pydantic `StrictInt`（`1.0`/`true` 不收），再做 `[-1e6,1e6]` 范围校验 |
| 以整单替换后的图形为准 | `service.apply_order` | 先在内存构造“本单新环 + 候选旧环”的替换后状态，再统一做冲突校验 |
| 内部相交退回整单、报冲突编号 | `409 parcel_interior_conflict` | 返回 `conflicts[].parcels` 与去重排序的 `conflict_parcel_nos`；同单无关新宗地也不落库，有测试 |
| 核验员按宗地对查线段、版本、签收状态 | `GET /api/shared-edges?parcel_a=&parcel_b=`、`GET /api/parcel-pairs/{a}/{b}` | 返回 GeoJSON 线段、`version`、`state`、`sign_status(signed/unsigned/stale/removed)`、签收人/时间/凭证 |
| 凭所见版本签收 | `POST /api/shared-edges/{id}/sign`、宗地对批量签收 | 必须带 `seen_version`（兼容字段名 `version`）；版本不符 `409 stale_signoff` 并回带当前版本与最新图形 |
| 单点接触不算共边 | `geometry.shared_segments` | 只保留重叠长度 `hi > lo`（严格正长度），有对点接触测试 |
| 范围有变即使等长也要重验 | 版本对账 `reconcile_pair` | 同 id 升 `version`、清空当前签收、状态变 `stale`；有“等长 y[0,10]→y[2,12]”专项测试 |
| 范围照旧保留签收 | 同上 | 几何键完全一致时数据库行不动，`version` 与 `current_signoff_id` 原样保留，有测试 |
| 消失的停止供数 | 旧线置 `state='removed'` | 制图 feed 过滤；旧签收凭证取数 409；removed 历史行保留可查 |
| 制图只给当前已签收的线 | `GET /api/mapping/shared-boundaries` | GeoJSON；SQL `JOIN signoffs` 且要求 `state='signed'`、凭证为当前凭证、签收版本=`e.version` |
| 过期签收须拒绝 | 签收接口 409 + `GET /api/mapping/delivery/{signoff_id}` 409 | 旧版本签收、旧凭证、removed 线三种情况均拒绝，有端到端测试 |
| 改界、共边、签收状态一起生效；写入失败照旧 | 单事务 `BEGIN IMMEDIATE` | 宗地 upsert、orders、edges 版本/状态、signoffs 同一事务；异常 rollback；有 monkeypatch 写故障回滚测试 |
| 只回读外接框与本单任一新、旧外接框相交的外环 | `geometry.plan_ring_reads` + `apply_order` | 先只扫 bbox 列，再 `IN (...)` 读候选 `ring_json`；响应 `readback` 给出候选与实际回读清单，有断言 FAR 宗地不回读 |
| 计算不逐米铺格 | `app/geometry.py` | 全部为整数线段相交、区间重叠/合并、射线法点在内；坐标跨度大小与耗时无关 |
| 查询共边不读原外环 | `service.pair_edges` | 只查 `shared_edges/signoffs`；有 SQL trace 测试断言请求 SQL 不含 `parcels` |
| docker compose 启动 | `Dockerfile`、`docker-compose.yml` | 卷持久化 `/data`，带 healthcheck；说明见下“实测边界” |
| 交付前核对改动、试跑、写明结果 | `tests/`、`scripts/trial.py`、本文件、README | 见下 |

### 接口额外补充（不影响主链路）

- `GET /` 导航、`GET /health`、`GET /api/survey-area`
- `GET /api/parcels`、`GET /api/parcels/{no}`、`GET /api/parcels/{no}/shared-edges`
- `GET /api/orders`、`GET /api/orders/{order_no}`（含每单回读与对账明细）
- `POST /api/parcel-pairs/{a}/{b}/sign` 宗地对批量原子签收

---

## 二、实测结果（均为本环境真实执行）

### 1. 单元 + 端到端测试：29/29 通过

命令：

```bash
PYTHONPATH=/tmp/pylibs python3 -m pytest -v
```

结果（末尾汇总）：

```
29 passed, 1 warning in 0.78s
```

覆盖：环规范化/共线合并、斜边/非整数/超范围/折回/自交拒绝、相邻共边、
单点不算共边、面积重叠、十字穿越、包含、完全重叠、T 接、部分重叠、L 形、
候选 bbox 规划，以及 11 个 API/事务场景（挪界+过期签收全时序、等长挪段、
保留签收、消失停供、冲突整单退回、坏环整单拒绝、回读范围、查询不读外环、
宗地对双线段批量签收原子性、写故障回滚、文档与静态资源）。

> 唯一 warning 是 starlette/httpx 测试客户端的弃用提示，与功能无关。

### 2. 叙述式试跑：25 条断言全部通过

```bash
PYTHONPATH=/tmp/pylibs python3 scripts/trial.py
# exit=0，末尾输出：全部试跑断言通过。
```

实测覆盖你点名的两件事：

- **同单挪界**：一单内同时把 `A=[0,0]-[10,10]` 扩到 x=12、`B` 缩到从 x=12 起，
  共边 `x=12,y[0,10]`（等长平移）。返回 `shared_edges.updated=[{edge_id:1,
  old_version:1, version:2}]`；同 id 查询为 `version=2 / sign_status=stale`。
- **过期签收**：仍按所见 `version=1` 调签收 → 真实响应：

```json
{
  "error": "stale_signoff",
  "message": "签收被拒绝：核验员所见版本 v1 已过期，当前版本为 v2，请按最新图形重新核验后签收",
  "edge_id": 1, "seen_version": 1, "current_version": 2
}
```

HTTP `409`；旧 `signoff_id` 调 `/api/mapping/delivery/{id}` 也是 `409`；
重签 v2 后 feed 恢复 1 条且坐标为 `[[12,0],[12,10]]`。

冲突单的真实 `409` 响应：

```json
{
  "error": "parcel_interior_conflict",
  "conflicts": [{"conflict_id": "C1", "parcels": ["A", "B"], "reason": "interior_overlap"}],
  "conflict_parcel_nos": ["A", "B"]
}
```

随后读 `A` 仍是上一单图形、同单新宗地 `G` 为 404、`GET /api/orders/TRIAL-BAD-CONFLICT` 为 404。

### 3. 真实 uvicorn + curl（与 Docker 同一启动模块）

```bash
CADASTRE_DB_PATH=/tmp/live_cadastre.db \
PYTHONPATH=/tmp/pylibs:/workspace \
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8123
```

实测状态：

```
GET /health                              -> {"status":"ok","database":"ok"}
GET /docs                                -> 200，<title>...Swagger UI</title>
POST /api/orders (HTTP-O1)               -> 200，edges_created 1
GET  /api/shared-edges?A&B               -> v1 unsigned [[10,0],[10,10]]
POST .../sign {"seen_version":1}         -> 200
POST /api/orders (同单挪界 HTTP-O2)       -> 200，updated [(1, 1, 2)]，readback=['A','B']
POST .../sign {"seen_version":1}         -> 409 stale_signoff seen 1 current 2
GET  /api/mapping/shared-boundaries      -> feature_count=0
POST .../sign {"seen_version":2}         -> 200
GET  /api/mapping/shared-boundaries      -> feature_count=1 coords=[[12,0],[12,10]]
```

自托管文档与宗地对批量签收在 8124 端口复验：

```
/docs 引用 /static/vendor/swagger-ui-bundle.js 与 swagger-ui.css
swagger-ui-bundle.js -> 200 application/javascript (1.8 MB)
swagger-ui.css       -> 200 text/css (206 KB)
POST /api/parcel-pairs/A/B/sign（两条边各 v1）-> signed_count=2，feed=2
```

### 4. Docker / compose 的实测边界（如实说明）

- 本沙箱 **没有 `docker` / `podman` / `nerdctl` 可执行文件**，因此
  `docker compose build` 与 `docker compose up` **未能在本环境真实执行**。
- 已完成的核对：`docker-compose.yml` 通过 PyYAML 解析，服务名/端口 `8000:8000`/
  命名卷 `/data`/healthcheck 结构正确；`Dockerfile` 使用 `python:3.12-slim`、
  `pip install -r requirements.txt`、复制 `app/`、启动命令
  `uvicorn app.main:app --host 0.0.0.0 --port 8000`，与上面实测通过的启动方式一致；
  `app/static/vendor` 会随 `COPY app ./app` 进入镜像。
- 目标机器具备 Docker 时的启动方式就是 `docker compose up --build`。

### 5. 依赖安装说明（本沙箱特殊情况）

系统 Python 3.11.2 未带 pip 且无 `ensurepip`，本次实测依赖是用 get-pip 引导到
`/tmp/pylibs`，再 `pip install --target /tmp/pylibs fastapi "uvicorn[standard]"
pytest httpx`（实际版本 fastapi 0.165.0 / pydantic 2.14.4 / pytest 9.2.0）。
仓库 `requirements.txt` / `requirements-dev.txt` 是标准安装方式，Docker 构建不受影响。

---

## 三、未完成事项与已知限制

1. **未实跑 Docker 构建**：原因如上（沙箱无容器运行时）；只做到配置解析 + 等价 uvicorn 实测。
2. **无身份认证 / 权限 / HTTPS**：任务未要求；`verifier` 只做非空记录，不校验核验员身份。
3. **不支持宗地删除 / 注销**：原文只要求“录入或更正外环”。“消失”指共边消失
   （`removed` 留痕并停供），不是删除宗地；宗地历史图形未做版本回溯，
   但每单的处理结果存在 `orders.result_json`，签收历史存在 `signoffs`。
4. **共边身份延续是启发式匹配**：规则为“同向 + 延伸区间严格正重叠 + 端点曼哈顿
   位移全局贪心最小”。极端的一对多 / 大范围重排可能被归类成 `removed + created`
   而非 `updated`；两种归类都不会错误保留旧签收（安全方向正确），差别仅在 edge_id 与展示。
5. **候选 bbox 扫描在 Python 侧做 O(n)**：只取外接框四列、不读外环，宗地量级在
   一个测区通常够用；若到数十万宗，可把相交条件下沉成 SQL 或加 R-tree 索引。
6. **并发模型面向单机适量写入**：SQLite + `BEGIN IMMEDIATE` 能串行化写事务，
   不适合多实例同时写同一个网络盘库；compose 当前是单副本 + 命名卷。
7. **`/redoc` 仍引用 CDN 资源**：只有任务点名的 `/docs` 做了完全自托管；
   若浏览器环境完全离线且需要 ReDoc，需要同样下载 redoc 资源。
8. **未做接口限流 / 分页**：`/api/orders` 最近 100 条；宗地与共边列表当前全量返回。

---

## 四、交付物清单

```
Dockerfile docker-compose.yml .dockerignore
requirements.txt requirements-dev.txt pytest.ini .gitignore
README.md DELIVERY.md
app/ (__init__, main, schemas, service, geometry, db, errors, config
      + static/vendor 自托管 Swagger UI)
scripts/trial.py
tests/ (conftest + 2 个测试文件，29 用例)
```

建议的验收命令：

```bash
# 方式 A：docker compose
docker compose up --build           # 然后访问 http://localhost:8000/docs

# 方式 B：本机
pip install -r requirements-dev.txt
pytest -q
python scripts/trial.py
uvicorn app.main:app --port 8000
```
