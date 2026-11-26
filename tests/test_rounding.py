"""厘米测绘稿 → 整米最优取整单的算法与端到端测试。

重点覆盖任务点名的性质：
- 每条边只能下/上取整米；正面积；
- 指定宗地对保留正长度共边；
- 本单之间、与库中未改单宗地之间无正面积叠压；
- 位移和最小 + 宗地号/左下入上 字典序平局；
- 互不相交的独立组只做分量枚举，组合检查数线性增长；
- 无解 / 写失败时原宗地图与供数照旧。
"""
from __future__ import annotations

import itertools
import json
import random

import pytest

from app.db import connect
from app.schemas import OrderIn
from app.rounding import (
    edge_choices,
    geometry_shared_segments,
    rectangle_candidates,
    rects_positive_overlap,
    rects_shared_edge_length,
    solve_rounding,
)


def rect_cm(x0, y0, x1, y1):
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------- 边与候选


def test_edge_floor_ceil_including_negative_coords():
    assert [(e.metres, e.direction) for e in edge_choices(105)] == [
        (1, "down"),
        (2, "up"),
    ]
    assert [(e.metres, e.cost_cm) for e in edge_choices(1000)] == [(10, 0)]
    # -1.05m：向下 = -2m（位移 95cm），向上 = -1m（位移 5cm）
    assert [(e.metres, e.cost_cm, e.direction) for e in edge_choices(-105)] == [
        (-2, 95, "down"),
        (-1, 5, "up"),
    ]


def test_candidates_at_most_16_and_zero_area_filtered():
    cands = rectangle_candidates((99, 99, 101, 101))
    # L∈{0,1}, B∈{0,1}, R∈{1,2}, T∈{1,2}；L<R、B<T
    assert len(cands) <= 16
    for c in cands:
        l, b, r, t = c.rect
        assert l < r and b < t
    # 向外取整 [0,0,2,2] 一定在候选里
    assert (0, 0, 2, 2) in {c.rect for c in cands}


# ---------------------------------------------------------------- 最优 / 平局


def test_tie_break_all_half_residue_prefers_all_lower_coords():
    # 四条边余数都是 50：下/上代价完全相同 => 字典序最小 = 全部向下
    result = solve_rounding({"A": (1050, 2050, 3050, 4050)})
    assert result["feasible"]
    a = result["assignments"][0]
    assert a["rect"] == [10, 20, 30, 40]
    assert result["lex_key"] == [10, 20, 30, 40]
    assert result["total_cost_cm"] == 200  # 4 边 × 50cm


def test_lex_tie_break_uses_sorted_parcel_numbers():
    # Z 与 A 同时存在；构造总代价平局，验证全局键按宗地号排序
    parcels = {
        "Z": (1050, 1050, 2050, 2050),
        "A": (3050, 3050, 4050, 4050),
    }
    result = solve_rounding(parcels)
    assert result["feasible"]
    # 全局 lex_key 必须以 A 的四坐标开头，而不是提交/字典插入顺序
    assert result["lex_key"][:4] == [30, 30, 40, 40]
    assert [a["parcel_no"] for a in result["assignments"]] == ["A", "Z"]
    assert result["lex_key"] == [30, 30, 40, 40, 10, 10, 20, 20]


def test_shared_edge_requirement_forces_more_expensive_choices():
    # A 右缘 1005：10(5cm)/11(95cm)；B 左缘 1099：10(99cm)/11(1cm)
    # 各自最近会得到 A.R=10、B.L=11（错开）；共边要求迫使二者取齐：
    #   x=10 代价 5+99=104；x=11 代价 95+1=96 => 选 x=11
    result = solve_rounding(
        {"A": (0, 0, 1005, 1000), "B": (1099, 0, 2000, 1000)},
        preserve_pairs=[("A", "B")],
    )
    rects = {a["parcel_no"]: a["rect"] for a in result["assignments"]}
    assert rects["A"] == [0, 0, 11, 10]
    assert rects["B"] == [11, 0, 20, 10]
    assert result["total_cost_cm"] == 96
    pair = result["preserve_pairs"][0]
    assert pair["shared"] and pair["shared_length_m"] == 10
    assert pair["segments"] == [["v", 11, 0, 10]]


def test_point_contact_does_not_count_as_preserved_shared_edge():
    # 对角相碰布局：A 恰为 [0,0,10,10]；B 的左下坐标余数 99，可取 (10或11, 10或11)。
    # 取 (10,10) 时仅在 (10,10) 点接触；取 11 则留出间隙。
    # 竖边取齐时 y 重叠长度 0，横边取齐时 x 重叠长度 0 => 无正长度共边，无解。
    result = solve_rounding(
        {
            "A": (0, 0, 1000, 1000),
            "B": (1099, 1099, 2000, 2000),
        },
        preserve_pairs=[("A", "B")],
    )
    assert result["feasible"] is False

    # 对照组：A 右缘 1005 可上取到 11、B 左缘 1005 可下取到 10、
    # B 下边恰为 10m => 唯一可行是横共边 y=10、x[10,11]，长 1m。
    ok = solve_rounding(
        {
            "A": (0, 0, 1005, 1000),
            "B": (1005, 1000, 2000, 2000),
        },
        preserve_pairs=[("A", "B")],
    )
    assert ok["feasible"] is True
    assert ok["preserve_pairs"][0]["shared_length_m"] == 1
    assert ok["preserve_pairs"][0]["segments"] == [["h", 10, 10, 11]]


def test_overlap_between_order_parcels_is_rejected():
    # 两宗完全相同的厘米矩形且坐标恰为整米 => 唯一候选完全叠压 => 无解
    result = solve_rounding(
        {"A": (0, 0, 1000, 1000), "B": (0, 0, 1000, 1000)}
    )
    assert result["feasible"] is False


# ---------------------------------------------------------------- 分组 / 组合数


def test_independent_groups_check_linear_combinations():
    n = 40
    # 每宗相隔 1000m，候选包络绝不可能相交，也没有跨组共边要求
    parcels = {
        f"P{k:03d}": (k * 100_000, 0, k * 100_000 + 1050, 1050)
        for k in range(n)
    }
    result = solve_rounding(parcels)
    assert result["feasible"]
    assert result["group_count"] == n
    # 每个单宗组检查的组合数就是其候选数（<=16），总和线性
    assert result["combinations_checked"] == sum(
        g["combinations_checked"] for g in result["groups"]
    )
    assert result["combinations_checked"] <= 16 * n
    # 朴素整体笛卡尔积是指数级的（这里是 2 个余数50的边? 实际每宗4候选）
    assert result["naive_joint_combinations"] > 16 ** 5
    assert result["combinations_checked"] < 16 * n + 1


def test_preserve_pair_merges_groups_but_others_stay_independent():
    parcels = {
        "A": (0, 0, 1005, 1000),
        "B": (1005, 0, 2005, 1000),
        "FAR": (900_000, 900_000, 901_050, 901_050),
    }
    result = solve_rounding(parcels, preserve_pairs=[("A", "B")])
    assert result["feasible"]
    assert result["group_count"] == 2
    sizes = sorted(g["size"] for g in result["groups"])
    assert sizes == [1, 2]


# ---------------------------------------------------------------- 随机差分：
# 分解求解器 vs 不做分解的全笛卡尔积暴力枚举


def _bruteforce_solve(cm, pairs, fixed):
    nos = sorted(cm)
    cands = {no: rectangle_candidates(cm[no]) for no in nos}
    best = None
    for combo in itertools.product(*[cands[n] for n in nos]):
        m = {n: c.rect for n, c in zip(nos, combo)}
        ok = True
        for n in nos:
            for fk, fr in fixed.items():
                # 随机对照里 fixed 都是矩形四元组，直接做区间判定
                if rects_positive_overlap(m[n], fr):
                    ok = False
        for i in range(len(nos)):
            for j in range(i + 1, len(nos)):
                if rects_positive_overlap(m[nos[i]], m[nos[j]]):
                    ok = False
        for a, b in pairs:
            if a in m and b in m:
                if rects_shared_edge_length(m[a], m[b]) <= 0:
                    ok = False
            elif a in m and b in fixed:
                if geometry_shared_segments(m[a], fixed[b]) == []:
                    ok = False
            elif b in m and a in fixed:
                if geometry_shared_segments(m[b], fixed[a]) == []:
                    ok = False
        if not ok:
            continue
        cost = sum(c.cost_cm for c in combo)
        lex = tuple(v for n in nos for v in m[n])
        key = (cost, lex)
        if best is None or key < best[0]:
            best = (key, dict(m))
    return best


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_solver_matches_full_bruteforce_random(seed):
    rng = random.Random(seed)
    for _ in range(30):
        n = rng.randint(1, 4)
        cm = {}
        base = rng.randint(-3, 3) * 1000
        for k in range(n):
            x = base + k * rng.randint(150, 420)
            y = rng.randint(-2, 2) * 300
            w = rng.randint(1, 4) * 100 + rng.randint(1, 99)
            h = rng.randint(1, 4) * 100 + rng.randint(1, 99)
            cm[f"P{k}"] = (x + rng.randint(0, 99), y + rng.randint(0, 99), 0, 0)
            l, b, _, _ = cm[f"P{k}"]
            cm[f"P{k}"] = (l, b, l + w, b + h)

        pairs = []
        if n >= 2:
            for _ in range(rng.randint(0, 2)):
                i, j = rng.sample(range(n), 2)
                pr = tuple(sorted((f"P{i}", f"P{j}")))
                if pr not in pairs:
                    pairs.append(pr)

        fixed = {}
        for f in range(rng.randint(0, 2)):
            fx = base + rng.randint(-2, n * 300)
            fy = rng.randint(-3, 3) * 200
            fixed[f"F{f}"] = (
                fx,
                fy,
                fx + rng.randint(1, 3) * 100,
                fy + rng.randint(1, 3) * 100,
            )
        if fixed and rng.random() < 0.3:
            pairs.append((rng.choice(list(cm)), rng.choice(list(fixed))))

        got = solve_rounding(cm, pairs, fixed_geometry=fixed)
        exp = _bruteforce_solve(cm, pairs, fixed)
        if exp is None:
            assert got["feasible"] is False, (cm, pairs, fixed)
        else:
            assert got["feasible"] is True, (cm, pairs, fixed, got["reasons"])
            got_rects = {a["parcel_no"]: tuple(a["rect"]) for a in got["assignments"]}
            assert got_rects == exp[1]
            assert got["total_cost_cm"] == exp[0][0]
            assert tuple(got["lex_key"]) == exp[0][1]


# ---------------------------------------------------------------- API 端到端


def _cm_body(**over):
    body = {
        "order_no": "RND-1",
        "parcels": [
            {"parcel_no": "A", "rect_cm": [0, 0, 1005, 1000]},
            {"parcel_no": "B", "rect_cm": [1099, 0, 2000, 1000]},
        ],
        "preserve_pairs": [["A", "B"]],
    }
    body.update(over)
    return body


def test_api_rounding_preserves_edge_and_returns_metres(client):
    r = client.post("/api/rounding/orders", json=_cm_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["unit_out"] == "m"
    rects = {x["parcel_no"]: x["rect"] for x in data["rectangles"]}
    assert rects == {"A": [0, 0, 11, 10], "B": [11, 0, 20, 10]}
    # 入库的是整米环
    a = client.get("/api/parcels/A").json()
    assert all(isinstance(v, int) for p in a["ring"] for v in p)
    assert a["ring"] == [[0, 0], [11, 0], [11, 10], [0, 10]]
    # 指定共边确实存在且正长度
    edge = client.get("/api/shared-edges?parcel_a=A&parcel_b=B").json()
    assert edge["edge_count"] == 1
    assert edge["edges"][0]["geometry"]["coordinates"] == [[11, 0], [11, 10]]
    # 整单改界结果也在响应里
    assert "shared_edges" in data and "readback" in data


def test_api_rounding_endpoint_aliases_and_lenient_fields(client):
    # 别名路径 + rect 字段名 + 对象式宗地对
    body = {
        "order_no": "ALIAS-1",
        "parcels": [
            {"parcel_no": "A", "rect": [0, 0, 1000, 1000]},
            {"no": "B", "bbox": [1000, 0, 2000, 1000]},
        ],
        "shared_edge_pairs": [{"parcel_a": "A", "parcel_b": "B"}],
    }
    r = client.post("/api/orders/round-cm", json=body)
    assert r.status_code == 200, r.text
    assert {x["parcel_no"] for x in r.json()["rectangles"]} == {"A", "B"}

    # 发到老的 /api/orders，带 unit=cm 标记也能识别
    body2 = _cm_body(order_no="VIA-ORDERS")
    body2["unit"] = "cm"
    r2 = client.post("/api/orders", json=body2)
    assert r2.status_code == 200, r2.text
    assert r2.json()["order_no"] == "VIA-ORDERS"


def test_api_dry_run_does_not_write(client):
    r = client.post("/api/rounding/solve", json=_cm_body(order_no="DRY"))
    assert r.status_code == 200, r.text
    assert r.json()["feasible"] is True
    assert r.json()["status"] == "planned"
    assert client.get("/api/parcels/A").status_code == 404
    assert client.get("/api/orders/DRY").status_code == 404


def test_api_rounding_avoids_existing_db_parcel(client):
    # 库里先有 O=[10,0]-[20,10]
    seed = client.post(
        "/api/orders",
        json={
            "order_no": "SEED-O",
            "parcels": [{"parcel_no": "O", "ring": [[10, 0], [20, 0], [20, 10], [10, 10]]}],
        },
    )
    assert seed.status_code == 200, seed.text

    # A 厘米稿 [990,0,1090,1000]：各自最近取整会压入 O；
    # 唯一可行是右缘向下到 10，得到 [9,0,10,10]，与 O 共边而不叠压。
    body = {
        "order_no": "AVOID",
        "parcels": [{"parcel_no": "A", "rect_cm": [990, 0, 1090, 1000]}],
    }
    r = client.post("/api/rounding/orders", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["rectangles"][0]["rect"] == [9, 0, 10, 10]
    assert client.get("/api/parcels/O").json()["ring"] == [
        [10, 0], [20, 0], [20, 10], [10, 10]
    ]


def test_api_no_solution_keeps_map_and_feed_unchanged(client):
    # 先有 A/B 共边并签收，供数 1 条
    seed = client.post(
        "/api/orders",
        json={
            "order_no": "BASE",
            "parcels": [
                {"parcel_no": "A", "ring": [[0, 0], [10, 0], [10, 10], [0, 10]]},
                {"parcel_no": "B", "ring": [[10, 0], [20, 0], [20, 10], [10, 10]]},
            ],
        },
    )
    assert seed.status_code == 200
    eid = client.get("/api/shared-edges?parcel_a=A&parcel_b=B").json()["edges"][0]["edge_id"]
    sign = client.post(f"/api/shared-edges/{eid}/sign", json={"verifier": "v", "seen_version": 1})
    sid = sign.json()["current_signoff_id"]
    assert client.get("/api/mapping/shared-boundaries").json()["feature_count"] == 1

    # X 的唯一整米矩形与 A 完全叠压 => 无解
    bad = client.post(
        "/api/rounding/orders",
        json={"order_no": "NOPE", "parcels": [{"parcel_no": "X", "rect_cm": [0, 0, 1000, 1000]}]},
    )
    assert bad.status_code == 409
    assert bad.json()["error"] == "no_feasible_rounding"
    assert bad.json()["solver"]["feasible"] is False

    # 原宗地图、坏单、供数、凭证全部照旧
    assert client.get("/api/parcels/X").status_code == 404
    assert client.get("/api/orders/NOPE").status_code == 404
    a = client.get("/api/parcels/A").json()["ring"]
    assert a == [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert client.get("/api/mapping/shared-boundaries").json()["feature_count"] == 1
    assert client.get(f"/api/mapping/delivery/{sid}").status_code == 200


def test_api_rounding_move_reconciles_signoff_version(client):
    # 已签收的 A/B 共边 v1；取整单把共边移到 x=11 => 同 edge_id 升 v2/stale
    seed = client.post(
        "/api/orders",
        json={
            "order_no": "BASE2",
            "parcels": [
                {"parcel_no": "A", "ring": [[0, 0], [10, 0], [10, 10], [0, 10]]},
                {"parcel_no": "B", "ring": [[10, 0], [20, 0], [20, 10], [10, 10]]},
            ],
        },
    )
    assert seed.status_code == 200
    edge = client.get("/api/shared-edges?parcel_a=A&parcel_b=B").json()["edges"][0]
    eid = edge["edge_id"]
    client.post(f"/api/shared-edges/{eid}/sign", json={"verifier": "v", "seen_version": 1})

    r = client.post("/api/rounding/orders", json=_cm_body(order_no="MOVE-CM"))
    assert r.status_code == 200, r.text
    updated = r.json()["shared_edges"]["updated"]
    assert len(updated) == 1 and updated[0]["edge_id"] == eid
    assert updated[0]["version"] == 2

    edge2 = client.get("/api/shared-edges?parcel_a=A&parcel_b=B").json()["edges"][0]
    assert edge2["version"] == 2 and edge2["sign_status"] == "stale"
    assert client.get("/api/mapping/shared-boundaries").json()["feature_count"] == 0


def test_rounding_write_failure_rolls_back(db, monkeypatch):
    from app.schemas import RoundingOrderIn
    from app import service

    service.apply_order(
        db,
        OrderIn(
            order_no="BASE",
            parcels=[
                {"parcel_no": "A", "ring": [[0, 0], [10, 0], [10, 10], [0, 10]]},
                {"parcel_no": "B", "ring": [[10, 0], [20, 0], [20, 10], [10, 10]]},
            ],
        ),
    )

    # 两宗的整单落库后必然重算 A/B 共边；在该阶段注入写盘故障
    def boom(*a, **k):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(service, "shared_segments", boom)

    body = RoundingOrderIn.model_validate(
        {
            "order_no": "CRASH-CM",
            "parcels": [
                {"parcel_no": "A", "rect_cm": [0, 0, 1200, 1000]},
                {"parcel_no": "B", "rect_cm": [1200, 0, 2000, 1000]},
            ],
            "preserve_pairs": [["A", "B"]],
        }
    )
    with pytest.raises(RuntimeError):
        service.submit_rounding_order(db, body)

    check = connect()
    try:
        a_ring = json.loads(
            check.execute("SELECT ring_json FROM parcels WHERE parcel_no='A'").fetchone()[0]
        )
        b_ring = json.loads(
            check.execute("SELECT ring_json FROM parcels WHERE parcel_no='B'").fetchone()[0]
        )
        assert [list(p) for p in a_ring] == [[0, 0], [10, 0], [10, 10], [0, 10]]
        assert [list(p) for p in b_ring] == [[10, 0], [20, 0], [20, 10], [10, 10]]
        assert check.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 1
        assert check.execute("SELECT COUNT(*) c FROM orders WHERE order_no='CRASH-CM'").fetchone()["c"] == 0
    finally:
        check.close()


def test_docs_page_lists_rounding_endpoint(client):
    spec = client.get("/openapi.json").json()
    assert "/api/rounding/orders" in spec["paths"]
    html = client.get("/docs").text
    assert "swagger" in html.lower()


def test_chain_of_pairs_all_preserved():
    # A-B-C 链式共边，三条竖线都在 x=10/20 附近，取整后必须仍两两正长度共边
    result = solve_rounding(
        {
            "A": (0, 0, 1005, 1000),
            "B": (995, 0, 2005, 1000),
            "C": (1995, 0, 3000, 1000),
        },
        preserve_pairs=[("A", "B"), ("B", "C")],
    )
    assert result["feasible"], result["reasons"]
    segs = {
        (p["parcel_a"], p["parcel_b"]): p["shared_length_m"]
        for p in result["preserve_pairs"]
    }
    assert segs[("A", "B")] > 0
    assert segs[("B", "C")] > 0


def test_lenient_dict_and_ring_shapes_accepted(client):
    # parcels 用 宗地号->矩形 的映射；坐标用厘米角点 ring；对用对象
    payload = {
        "order_no": "LENIENT",
        "unit": "cm",
        "parcels": {
            "A": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
            "B": [1000, 0, 2000, 1000],
        },
        "pairs": [{"a": "A", "b": "B"}],
    }
    r = client.post("/api/rounding/orders", json=payload)
    assert r.status_code == 200, r.text
    nos = {x["parcel_no"] for x in r.json()["rectangles"]}
    assert nos == {"A", "B"}


def test_cm_out_of_range_rejected_422(client):
    r = client.post(
        "/api/rounding/orders",
        json={"parcels": [{"parcel_no": "FAR", "rect_cm": [0, 0, 10**12, 1000]}]},
    )
    assert r.status_code == 422


def test_body_dry_run_flag_does_not_write(client):
    r = client.post(
        "/api/rounding/orders",
        json=_cm_body(order_no="DRYFLAG", dry_run=True),
    )
    assert r.status_code == 200
    assert r.json()["feasible"] is True and r.json()["dry_run"] is True
    assert client.get("/api/orders/DRYFLAG").status_code == 404


def test_preserve_pair_with_existing_db_parcel(client):
    # 库内 Q=[10,0,20,10]；新地 N 在其左侧，须与 Q 保留 x=10、y[2,8] 共边
    seed = client.post(
        "/api/orders",
        json={"order_no": "Q", "parcels": [
            {"parcel_no": "Q", "ring": [[10, 0], [20, 0], [20, 10], [10, 10]]},
            # 放一块不相邻的 D，验证不会被误连
            {"parcel_no": "D", "ring": [[100, 100], [110, 100], [110, 110], [100, 110]]},
        ]},
    )
    assert seed.status_code == 200, seed.text

    r = client.post("/api/rounding/orders", json={
        "order_no": "WITH-Q",
        "parcels": [{"parcel_no": "N", "rect_cm": [0, 200, 995, 800]}],
        "preserve_pairs": [["N", "Q"]],
    })
    assert r.status_code == 200, r.text
    assert r.json()["rectangles"][0]["rect"] == [0, 2, 10, 8]
    pair = r.json()["preserve_pairs"][0]
    assert pair["shared"] and pair["segments"] == [["v", 10, 2, 8]]
    # 共边已按整单规则入库
    edge = client.get("/api/shared-edges?parcel_a=N&parcel_b=Q").json()
    assert edge["edge_count"] == 1


def test_two_fixed_db_parcels_without_edge_makes_no_solution(client):
    # 两块互不共边的库内地块，却要求“保留共边” => 无法靠取整改变，409 且不写单
    seed = client.post(
        "/api/orders",
        json={"order_no": "TWOFIX", "parcels": [
            {"parcel_no": "Q1", "ring": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            {"parcel_no": "Q2", "ring": [[50, 50], [51, 50], [51, 51], [50, 51]]},
        ]},
    )
    assert seed.status_code == 200
    r = client.post("/api/rounding/orders", json={
        "order_no": "BADFIX",
        "parcels": [{"parcel_no": "N", "rect_cm": [200, 200, 300, 300]}],
        "preserve_pairs": [["Q1", "Q2"]],
    })
    assert r.status_code == 409
    assert r.json()["error"] == "no_feasible_rounding"
    assert client.get("/api/orders/BADFIX").status_code == 404
