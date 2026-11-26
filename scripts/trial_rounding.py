#!/usr/bin/env python3
"""厘米取整单交付前试跑。

点名核对两件事：
  1) 保留共边：两条边各自最近取整会错开，求解器为保留正长度共边选择全局最优；
  2) 绕开已入库宗地：库中已有未改单宗地，取整矩形必须绕开、不得正面积叠压。

另演示：
  - 平局字典序（余数 50 时全部向下）；
  - 大量互不相交独立组时，检查组合数线性而非指数；
  - 无解整单不动库、不停供；
  - 成功后整米坐标入库并沿用整单改界的共边版本规则。

用法：
    PYTHONPATH=/tmp/pylibs python3 scripts/trial_rounding.py
"""
from __future__ import annotations

import json
import os
import sys

DB_FILE = "/tmp/cadastre_rounding_trial.db"
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB_FILE + suffix)
    except FileNotFoundError:
        pass
os.environ["CADASTRE_DB_PATH"] = DB_FILE
os.environ["SURVEY_AREA_NAME"] = "取整试跑测区"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.rounding import solve_rounding  # noqa: E402


def show(title, value):
    print(f"\n### {title}")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def must(cond, msg):
    if not cond:
        print(f"\n断言失败: {msg}")
        sys.exit(1)
    print(f"  ✓ {msg}")


def main():
    with TestClient(app) as c:
        print("=" * 74)
        print("厘米测绘稿 → 整米最优取整 试跑；POST /docs 中的 /api/rounding/orders")
        print("=" * 74)
        show("健康检查", c.get("/health").json())

        # ------------------------------------------------------------ 1) 保留共边
        body = {
            "order_no": "RND-EDGE",
            "parcels": [
                # A 右缘 1005cm：下到 10m 位移 5cm，上到 11m 位移 95cm
                {"parcel_no": "A", "rect_cm": [0, 0, 1005, 1000]},
                # B 左缘 1099cm：下到 10m 位移 99cm，上到 11m 位移 1cm
                {"parcel_no": "B", "rect_cm": [1099, 0, 2000, 1000]},
            ],
            "preserve_pairs": [["A", "B"]],
        }
        r = c.post("/api/rounding/orders", json=body)
        show("保留共边取整单 RND-EDGE", r.json())
        must(r.status_code == 200, "保留共边取整单成功")
        data = r.json()
        rects = {x["parcel_no"]: x["rect"] for x in data["rectangles"]}
        # 各自最近会选 A.R=10、B.L=11 而错开；为共边只能同取 10 或同取 11：
        #   同 10 代价 5+99=104cm；同 11 代价 95+1=96cm => 选 x=11
        must(rects == {"A": [0, 0, 11, 10], "B": [11, 0, 20, 10]},
             "为保留共边选择总位移最小的 x=11 方案（96cm）")
        must(data["total_cost_cm"] == 96, "总边位移 96cm")
        seg = data["preserve_pairs"][0]["segments"]
        must(seg == [["v", 11, 0, 10]], f"保留正长度共边 {seg}")

        edge = c.get("/api/shared-edges?parcel_a=A&parcel_b=B").json()
        coords = edge["edges"][0]["geometry"]["coordinates"]
        must(coords == [[11, 0], [11, 10]], f"整米共边已入库 {coords}")
        a_ring = c.get("/api/parcels/A").json()["ring"]
        must(all(isinstance(v, int) for p in a_ring for v in p), "库内坐标全部为整数米")

        # ------------------------------------------------------------ 2) 绕开已入库宗地
        # 放在 x≈510m 的远处，避免与上面 A/B 场景相互干扰。
        seed = c.post(
            "/api/orders",
            json={
                "order_no": "SEED-OBSTACLE",
                "parcels": [
                    {"parcel_no": "O", "ring": [[510, 0], [520, 0], [520, 10], [510, 10]]}
                ]
            },
        )
        show("预置障碍宗地 SEED-OBSTACLE", seed.json())
        must(seed.status_code == 200, "已入库障碍宗地 O=[510,0]-[520,10]")

        avoid_body = {
            "order_no": "RND-AVOID",
            "parcels": [
                # 左缘 50990：510m(10cm) 比 509m(90cm) 近；右缘 51090：511m(10cm) 比 510m(90cm) 近。
                # 最近组合 [510,0,511,10] 等都会正面积压入 O；唯一可行是左、右缘都向下取，
                # 得到 [509,0,510,10]，与 O 共边而不叠压（总位移 90+90=180cm）。
                {"parcel_no": "P", "rect_cm": [50990, 0, 51090, 1000]}
            ]
        }
        r = c.post("/api/rounding/orders", json=avoid_body)
        show("绕开已入库宗地 RND-AVOID", r.json())
        must(r.status_code == 200, "绕障取整单成功")
        prect = r.json()["rectangles"][0]["rect"]
        must(prect == [509, 0, 510, 10], f"P 取整为 {prect}，绕开 O 且无正面积叠压")
        o_ring = c.get("/api/parcels/O").json()["ring"]
        must(o_ring == [[510, 0], [520, 0], [520, 10], [510, 10]], "未改单宗地 O 图形不变")

        # 库内核验 P/O 无正面积叠压，且有一条合法共边
        po = c.get("/api/shared-edges?parcel_a=P&parcel_b=O").json()
        must(po["edge_count"] == 1, "P 与 O 之间是合法共边而非叠压")

        # ------------------------------------------------------------ 3) 平局字典序
        tie = solve_rounding({"T": (1050, 2050, 3050, 4050)})
        must(tie["assignments"][0]["rect"] == [10, 20, 30, 40],
             "四条边余数均 50 时代价平局，字典序取全部向下 [10,20,30,40]")

        # ------------------------------------------------------------ 4) 分组线性组合数
        n = 60
        parcels = {
            f"G{k:03d}": (k * 100_000, 0, k * 100_000 + 1050, 1050)
            for k in range(n)
        }
        stat = solve_rounding(parcels)
        show("60 个互不相交独立组的求解统计", {
            "group_count": stat["group_count"],
            "combinations_checked": stat["combinations_checked"],
            "naive_joint_combinations": stat["naive_joint_combinations"],
        })
        must(stat["group_count"] == n, f"识别出 {n} 个独立组")
        must(stat["combinations_checked"] <= 16 * n,
             f"实际检查 {stat['combinations_checked']} 组 ≤ 16×{n}，线性增长")
        must(stat["naive_joint_combinations"] > 16 ** 10,
             "朴素整体枚举是天文数字，反衬分解收益")

        # ------------------------------------------------------------ 5) 无解不动库 / 不停供
        base = c.post("/api/orders", json={
            "order_no": "BASE-SIGN",
            "parcels": [
                {"parcel_no": "C", "ring": [[0, 50], [10, 50], [10, 60], [0, 60]]},
                {"parcel_no": "D", "ring": [[10, 50], [20, 50], [20, 60], [10, 60]]},
            ],
        })
        must(base.status_code == 200, "再建 C/D 共边")
        eid = c.get("/api/shared-edges?parcel_a=C&parcel_b=D").json()["edges"][0]["edge_id"]
        c.post(f"/api/shared-edges/{eid}/sign", json={"verifier": "试跑员", "seen_version": 1})
        must(c.get("/api/mapping/shared-boundaries").json()["feature_count"] >= 1,
             "C/D 共边已签收并供数")

        bad = c.post("/api/rounding/orders", json={
            "order_no": "RND-NO-WAY",
            # 唯一整米矩形 [0,50,10,60] 与 C 完全叠压
            "parcels": [{"parcel_no": "X", "rect_cm": [0, 5000, 1000, 6000]}],
        })
        show("无解单响应（409）", bad.json())
        must(bad.status_code == 409 and bad.json()["error"] == "no_feasible_rounding",
             "无解返回 409 no_feasible_rounding")
        must(c.get("/api/parcels/X").status_code == 404, "无解单宗地 X 未入库")
        must(c.get("/api/orders/RND-NO-WAY").status_code == 404, "无解单不产生整单记录")
        feed = c.get("/api/mapping/shared-boundaries").json()["feature_count"]
        must(feed >= 1, f"供数照旧（当前 {feed} 条已签收线）")

        print("\n" + "=" * 74)
        print("全部取整试跑断言通过：共边已保留、已入库宗地已绕开、无解照旧、组合数线性。")
        print("=" * 74)


if __name__ == "__main__":
    main()
