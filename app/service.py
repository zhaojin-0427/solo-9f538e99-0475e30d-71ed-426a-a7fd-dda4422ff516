"""核心业务服务：整单改界、共边对账、核验签收、制图供数。

事务边界
--------
``apply_order`` 在 **一个** ``BEGIN IMMEDIATE`` 事务内完成：
候选外环回读 → 整单图形冲突校验 → 写宗地外环 → 重算 / 对账共边版本与
签收状态 → 写单记录。任何异常都整体 ROLLBACK，做到“写入失败照旧”。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import utcnow_iso
from .errors import (
    InvalidRoundingRequestError,
    NoFeasibleRoundingError,
    NotFoundError,
    OrderNoExistsError,
    ParcelConflictError,
    StaleSignoffError,
)
from .geometry import (
    BBox,
    Seg,
    bbox,
    has_interior_overlap,
    plan_ring_reads,
    shared_segments,
)
from .rounding import edge_choices, solve_rounding
from .schemas import OrderIn, RoundingOrderIn

# ------------------------------------------------------------ 序列化

_EDGE_SELECT = """
SELECT e.*,
       (SELECT MAX(s.version) FROM signoffs s WHERE s.edge_id = e.edge_id)
         AS last_signoff_version
FROM shared_edges e
"""


def _edge_geometry(orient: str, fixed: int, lo: int, hi: int) -> dict[str, Any]:
    if orient == "h":
        coords = [[lo, fixed], [hi, fixed]]
    else:
        coords = [[fixed, lo], [fixed, hi]]
    return {"type": "LineString", "coordinates": coords}


def edge_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    orient = row["orient"]
    version = row["version"]
    state = row["state"]
    last_ver = row["last_signoff_version"]
    current_sid = row["current_signoff_id"]

    if state == "removed":
        sign_status = "removed"
    elif state == "signed" and current_sid is not None:
        sign_status = "signed"
    elif last_ver is not None and last_ver < version:
        sign_status = "stale"  # 旧版本签收已过期，必须重验
    else:
        sign_status = "unsigned"

    return {
        "edge_id": row["edge_id"],
        "parcel_a": row["parcel_a"],
        "parcel_b": row["parcel_b"],
        "orientation": "horizontal" if orient == "h" else "vertical",
        "orient_code": orient,
        "fixed_coord": row["fixed"],
        "lo": row["lo"],
        "hi": row["hi"],
        "length": row["hi"] - row["lo"],
        "geometry": _edge_geometry(orient, row["fixed"], row["lo"], row["hi"]),
        "version": version,
        "state": state,
        "sign_status": sign_status,
        "signed_by": row["signed_by"],
        "signed_at": row["signed_at"],
        "current_signoff_id": current_sid,
        "last_signoff_version": last_ver,
        "updated_order_id": row["updated_order_id"],
    }


def _feature(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    e = edge_to_dict(row) if isinstance(row, sqlite3.Row) else row
    return {
        "type": "Feature",
        "geometry": e["geometry"],
        "properties": {
            "edge_id": e["edge_id"],
            "parcel_a": e["parcel_a"],
            "parcel_b": e["parcel_b"],
            "version": e["version"],
            "signoff_id": e["current_signoff_id"],
            "signed_by": e["signed_by"],
            "signed_at": e["signed_at"],
        },
    }


def parcel_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "parcel_no": row["parcel_no"],
        "ring": json.loads(row["ring_json"]),
        "bbox": {
            "minx": row["minx"],
            "miny": row["miny"],
            "maxx": row["maxx"],
            "maxy": row["maxy"],
        },
        "area2": row["area2"],
        "order_id": row["order_id"],
        "updated_at": row["updated_at"],
    }


# ------------------------------------------------------------ 共边版本对账

def _seg_key(seg: Seg) -> Seg:
    return seg


def _endpoints(seg: Seg) -> tuple[tuple[int, int], tuple[int, int]]:
    o, f, lo, hi = seg
    if o == "h":
        return (lo, f), (hi, f)
    return (f, lo), (f, hi)


def _manhattan(p: tuple[int, int], q: tuple[int, int]) -> int:
    return abs(p[0] - q[0]) + abs(p[1] - q[1])


def _matchable(old: Seg, new: Seg) -> bool:
    """是否把旧线与新线视为同一条共边的“范围变动”。

    条件：同向，且沿延伸方向的区间有严格正长度重叠（整体平移 / 伸缩 /
    等长挪段都满足）。对不上的旧线按“消失”处理，新线按新增处理。
    """
    if old[0] != new[0]:
        return False
    return min(old[3], new[3]) > max(old[2], new[2])


def _move_cost(old: Seg, new: Seg) -> int:
    a1, a2 = _endpoints(old)
    b1, b2 = _endpoints(new)
    return _manhattan(a1, b1) + _manhattan(a2, b2)


def reconcile_pair(
    conn: sqlite3.Connection,
    pa: str,
    pb: str,
    new_segs: list[Seg],
    order_id: int,
    now: str,
) -> dict[str, list[dict[str, Any]]]:
    """把一对宗地的当前共边对账到 ``new_segs``，维护版本与签收状态。

    - 几何范围完全一致：原行不动 → 版本、签收原样保留；
    - 匹配上但范围有变：同一 edge_id 升 version，签收作废（sign_status=stale）；
    - 旧线匹配不上：置 removed，停止供数；
    - 新线匹配不上：插入 version=1 的待核验线。
    """
    rows = conn.execute(
        _EDGE_SELECT + " WHERE e.parcel_a = ? AND e.parcel_b = ? AND e.state != 'removed'",
        (pa, pb),
    ).fetchall()
    current: dict[Seg, sqlite3.Row] = {
        (r["orient"], r["fixed"], r["lo"], r["hi"]): r for r in rows
    }
    new_keys = list(new_segs)
    new_key_set = set(new_keys)

    result: dict[str, list[dict[str, Any]]] = {
        "unchanged": [],
        "updated": [],
        "created": [],
        "removed": [],
    }

    # 1) 完全一致：保留签收，不动行
    exact_rows: dict[Seg, sqlite3.Row] = {}
    for key in new_keys:
        if key in current:
            row = current[key]
            exact_rows[key] = row
            result["unchanged"].append(
                {"edge_id": row["edge_id"], "geometry": list(key), "version": row["version"]}
            )

    old_left = [r for key, r in current.items() if key not in new_key_set]
    new_left = [k for k in new_keys if k not in exact_rows]

    # 2) 旧线 ↔ 新线 按端点位移代价做全局贪心匹配
    candidates: list[tuple[int, int, Seg]] = []
    for r in old_left:
        old_key: Seg = (r["orient"], r["fixed"], r["lo"], r["hi"])
        for new_key in new_left:
            if _matchable(old_key, new_key):
                candidates.append((_move_cost(old_key, new_key), r["edge_id"], new_key))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    paired_edge_ids: set[int] = set()
    paired_new: set[Seg] = set()
    pair_plan: list[tuple[sqlite3.Row, Seg]] = []
    row_by_id = {r["edge_id"]: r for r in old_left}
    for _cost, edge_id, new_key in candidates:
        if edge_id in paired_edge_ids or new_key in paired_new:
            continue
        paired_edge_ids.add(edge_id)
        paired_new.add(new_key)
        pair_plan.append((row_by_id[edge_id], new_key))

    for row, new_key in pair_plan:
        new_version = row["version"] + 1
        o, f, lo, hi = new_key
        conn.execute(
            """UPDATE shared_edges
               SET orient=?, fixed=?, lo=?, hi=?, version=?, state='unsigned',
                   signed_by=NULL, signed_at=NULL, current_signoff_id=NULL,
                   updated_order_id=?, updated_at=?
               WHERE edge_id=?""",
            (o, f, lo, hi, new_version, order_id, now, row["edge_id"]),
        )
        result["updated"].append(
            {
                "edge_id": row["edge_id"],
                "old_geometry": [row["orient"], row["fixed"], row["lo"], row["hi"]],
                "new_geometry": list(new_key),
                "old_version": row["version"],
                "version": new_version,
                "sign_required": True,
            }
        )

    # 3) 未匹配的旧线：消失，停止供数（保留历史行）
    for row in old_left:
        if row["edge_id"] in paired_edge_ids:
            continue
        conn.execute(
            """UPDATE shared_edges
               SET state='removed', signed_by=NULL, signed_at=NULL,
                   current_signoff_id=NULL, updated_order_id=?, updated_at=?
               WHERE edge_id=?""",
            (order_id, now, row["edge_id"]),
        )
        result["removed"].append(
            {
                "edge_id": row["edge_id"],
                "geometry": [row["orient"], row["fixed"], row["lo"], row["hi"]],
                "version": row["version"],
            }
        )

    # 4) 未匹配的新线：新增，版本 1，待核验
    for key in new_left:
        if key in paired_new:
            continue
        o, f, lo, hi = key
        cur = conn.execute(
            """INSERT INTO shared_edges
               (parcel_a, parcel_b, orient, fixed, lo, hi, version, state,
                created_order_id, updated_order_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, 'unsigned', ?, ?, ?)""",
            (pa, pb, o, f, lo, hi, order_id, order_id, now),
        )
        result["created"].append(
            {"edge_id": cur.lastrowid, "geometry": list(key), "version": 1}
        )

    return result


# ------------------------------------------------------------ 整单应用

def apply_order(conn: sqlite3.Connection, order: OrderIn) -> dict[str, Any]:
    """整单校验并原子替换 / 落库。冲突抛 :class:`ParcelConflictError`。"""
    now = utcnow_iso()

    # 先做不依赖库的环校验（错误直接 422，尚未开启事务）
    normalized, new_bboxes = validate_order_geometry(order)

    conn.execute("BEGIN IMMEDIATE")
    try:
        summary = persist_validated_order(conn, order, normalized, new_bboxes, now)
        conn.commit()
        return summary
    except BaseException:
        conn.rollback()
        raise


def validate_order_geometry(
    order: OrderIn,
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, BBox]]:
    normalized: dict[str, list[tuple[int, int]]] = {}
    new_bboxes: dict[str, BBox] = {}
    for item in order.parcels:
        ring = normalize_ring_safe(item.ring, item.parcel_no)
        normalized[item.parcel_no] = ring
        new_bboxes[item.parcel_no] = bbox(ring)
    return normalized, new_bboxes


def persist_validated_order(
    conn: sqlite3.Connection,
    order: OrderIn,
    normalized: dict[str, list[tuple[int, int]]],
    new_bboxes: dict[str, BBox],
    now: str,
) -> dict[str, Any]:
    """在 **已开启的事务** 内完成整单落库，不负责 commit / rollback。

    厘米取整单在同一个 ``BEGIN IMMEDIATE`` 事务内“按入库时现行图求解 +
    走完整整单改界规则”，因此也复用本函数。
    """
    order_nos = set(normalized)

    # 单号处理（写锁内取序号，避免并发重号）
    seq_row = conn.execute("SELECT COALESCE(MAX(order_id), 0) + 1 AS seq FROM orders").fetchone()
    seq = seq_row["seq"]
    order_no = order.order_no or f"ORD-{seq:04d}"
    if conn.execute("SELECT 1 FROM orders WHERE order_no = ?", (order_no,)).fetchone():
        raise OrderNoExistsError(f"单号 {order_no} 已存在", order_no=order_no)

    # 全量外接框扫描（不读外环），取本单各宗地的旧外接框
    bbox_rows = conn.execute(
        "SELECT parcel_no, minx, miny, maxx, maxy FROM parcels"
    ).fetchall()
    all_bboxes: list[tuple[str, BBox]] = []
    old_bboxes: dict[str, BBox] = {}
    for r in bbox_rows:
        box: BBox = (r["minx"], r["miny"], r["maxx"], r["maxy"])
        all_bboxes.append((r["parcel_no"], box))
        if r["parcel_no"] in order_nos:
            old_bboxes[r["parcel_no"]] = box

    query_boxes: list[BBox] = list(new_bboxes.values()) + list(old_bboxes.values())
    selected = plan_ring_reads(all_bboxes, order_nos, query_boxes)

    # 只回读候选宗地的外环 JSON
    existing_nos = [n for n in selected if n not in normalized]
    db_rings: dict[str, list[tuple[int, int]]] = {}
    rings_read_from_db: list[str] = []
    if existing_nos:
        marks = ",".join("?" for _ in existing_nos)
        ring_rows = conn.execute(
            f"SELECT parcel_no, ring_json FROM parcels WHERE parcel_no IN ({marks})",
            existing_nos,
        ).fetchall()
        for r in ring_rows:
            db_rings[r["parcel_no"]] = [tuple(p) for p in json.loads(r["ring_json"])]  # type: ignore[arg-type]
            rings_read_from_db.append(r["parcel_no"])

    post_rings: dict[str, list[tuple[int, int]]] = {}
    for no in selected:
        if no in normalized:
            post_rings[no] = normalized[no]
        elif no in db_rings:
            post_rings[no] = db_rings[no]

    # 以“整单替换后的图形”为准做冲突校验；只查至少一侧在本单内的对
    conflicts: list[dict[str, Any]] = []
    affected_pairs: set[tuple[str, str]] = set()
    checked: set[tuple[str, str]] = set()
    for no in sorted(order_nos):
        for other in sorted(selected):
            if other == no:
                continue
            pair = (no, other) if no < other else (other, no)
            if pair in checked:
                continue
            checked.add(pair)
            affected_pairs.add(pair)
            if has_interior_overlap(post_rings[no], post_rings[other]):
                conflicts.append(
                    {
                        "conflict_id": f"C{len(conflicts) + 1}",
                        "parcels": list(pair),
                        "reason": "interior_overlap",
                        "message": "两宗地内部相交（正面积重叠）",
                    }
                )

    if conflicts:
        raise ParcelConflictError(conflicts)

    # 落单头
    cur = conn.execute(
        "INSERT INTO orders (order_no, item_count, result_json, created_at) VALUES (?, ?, ?, ?)",
        (order_no, len(order.parcels), "{}", now),
    )
    order_id = cur.lastrowid

    # upsert 宗地外环
    for no, ring in normalized.items():
        minx, miny, maxx, maxy = new_bboxes[no]
        from .geometry import signed_area2

        conn.execute(
            """INSERT INTO parcels
               (parcel_no, ring_json, minx, miny, maxx, maxy, area2, order_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(parcel_no) DO UPDATE SET
                 ring_json=excluded.ring_json, minx=excluded.minx, miny=excluded.miny,
                 maxx=excluded.maxx, maxy=excluded.maxy, area2=excluded.area2,
                 order_id=excluded.order_id, updated_at=excluded.updated_at""",
            (
                no,
                json.dumps([list(p) for p in ring], ensure_ascii=False),
                minx,
                miny,
                maxx,
                maxy,
                abs(signed_area2(ring)),
                order_id,
                now,
            ),
        )

    # 重算受影响宗地对的共边并对账版本 / 签收
    edge_summary = {"unchanged": [], "updated": [], "created": [], "removed": []}
    for pa, pb in sorted(affected_pairs):
        segs = shared_segments(post_rings[pa], post_rings[pb])
        part = reconcile_pair(conn, pa, pb, segs, order_id, now)
        for k in edge_summary:
            edge_summary[k].extend(part[k])

    summary: dict[str, Any] = {
        "order_no": order_no,
        "order_id": order_id,
        "status": "applied",
        "applied_parcels": sorted(order_nos),
        "readback": {
            "candidate_parcels": sorted(selected),
            "candidate_count": len(selected),
            "rings_read_from_db": sorted(rings_read_from_db),
            "ring_read_count": len(rings_read_from_db),
            "note": "仅回读外接框与本单任一新/旧外接框相交的宗地外环",
        },
        "shared_edges": edge_summary,
        "created_at": now,
    }
    conn.execute(
        "UPDATE orders SET result_json = ? WHERE order_id = ?",
        (json.dumps(summary, ensure_ascii=False), order_id),
    )
    # 注意：不在此处 commit，事务边界由 apply_order / 取整服务控制
    return summary


def normalize_ring_safe(raw: Any, parcel_no: str) -> list[tuple[int, int]]:
    # 间接一层便于测试 monkeypatch 注入故障
    from .geometry import normalize_ring

    return normalize_ring(raw, parcel_no)


# ------------------------------------------------------------ 厘米取整单


def _candidate_envelope_m(cm_rect: tuple[int, int, int, int]) -> BBox:
    """一条边只能取 floor/ceil，候选外接框的并集包络（整米，宁大勿小）。"""
    l, b, r, t = cm_rect
    l_opts = [e.metres for e in edge_choices(l)]
    b_opts = [e.metres for e in edge_choices(b)]
    r_opts = [e.metres for e in edge_choices(r)]
    t_opts = [e.metres for e in edge_choices(t)]
    return min(l_opts), min(b_opts), max(r_opts), max(t_opts)


def load_fixed_geometry_for_rounding(
    conn: sqlite3.Connection,
    solver_parcels: dict[str, tuple[int, int, int, int]],
    pairs: list[tuple[str, str]],
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, Any]]:
    """读取求解需要的“库中未改单宗地”整米外环。

    先用外接框四列做候选筛选（与整单改界同一套回读规划），只回读候选外环；
    共边保留对引用的库内宗地无论位置都强制回读。
    """
    order_nos = set(solver_parcels)
    envelopes = {no: _candidate_envelope_m(rect) for no, rect in solver_parcels.items()}

    rows = conn.execute(
        "SELECT parcel_no, minx, miny, maxx, maxy FROM parcels"
    ).fetchall()
    all_bboxes: list[tuple[str, BBox]] = []
    for r in rows:
        if r["parcel_no"] in order_nos:
            continue  # 本单宗地将被整单替换，旧图形不是障碍
        all_bboxes.append(
            (r["parcel_no"], (r["minx"], r["miny"], r["maxx"], r["maxy"]))
        )

    selected = plan_ring_reads(
        all_bboxes, order_parcel_nos=set(), query_boxes=list(envelopes.values())
    )
    # plan_ring_reads 会并入 order_parcel_nos（这里传空集）；显式再保一次底
    selected = {no for no in selected if no not in order_nos}

    pair_db_nos = {
        x
        for pair in pairs
        for x in pair
        if x not in order_nos
    }
    selected |= pair_db_nos

    fixed: dict[str, list[tuple[int, int]]] = {}
    read_nos = sorted(selected)
    if read_nos:
        marks = ",".join("?" for _ in read_nos)
        ring_rows = conn.execute(
            f"SELECT parcel_no, ring_json FROM parcels WHERE parcel_no IN ({marks})",
            read_nos,
        ).fetchall()
        found: set[str] = set()
        for r in ring_rows:
            fixed[r["parcel_no"]] = [tuple(p) for p in json.loads(r["ring_json"])]  # type: ignore[arg-type]
            found.add(r["parcel_no"])
        missing = sorted(pair_db_nos - found)
        if missing:
            raise NotFoundError(
                f"共边保留对引用的库内宗地不存在: {missing}",
                unknown_parcels=missing,
            )

    plan_info = {
        "db_parcel_count": len(all_bboxes),
        "rings_read_from_db": read_nos,
        "ring_read_count": len(read_nos),
        "envelopes_metres": {no: list(box) for no, box in sorted(envelopes.items())},
    }
    return fixed, plan_info


def _solve_body_against_conn(
    conn: sqlite3.Connection, body: RoundingOrderIn
) -> tuple[dict[str, Any], dict[str, list[tuple[int, int]]], dict[str, Any]]:
    solver_parcels = body.solver_parcels()
    pairs = body.solver_pairs()
    fixed, read_info = load_fixed_geometry_for_rounding(conn, solver_parcels, pairs)
    solution = solve_rounding(
        solver_parcels,
        preserve_pairs=pairs,
        fixed_geometry=fixed,
    )
    return solution, fixed, read_info


def plan_rounding_order(conn: sqlite3.Connection, body: RoundingOrderIn) -> dict[str, Any]:
    """只试算不落库（dry-run）。读取现行宗地图，但不开启写事务。"""
    solution, _fixed, read_info = _solve_body_against_conn(conn, body)
    assignments = solution.get("assignments", [])
    return {
        "status": "planned",
        "dry_run": True,
        "applied": False,
        "unit_in": "cm",
        "unit_out": "m",
        "rectangles": assignments,
        "rounded_rectangles": assignments,
        "results": assignments,
        "db_readback": read_info,
        **solution,
    }


def submit_rounding_order(
    conn: sqlite3.Connection, body: RoundingOrderIn
) -> dict[str, Any]:
    """一个 ``BEGIN IMMEDIATE`` 事务内：读现行图 → 最优取整 → 整单改界落库。

    无解：回滚（事实上未写入）并抛 :class:`NoFeasibleRoundingError`；
    写入失败：同一事务回滚，原宗地图 / 共边 / 供数照旧。
    """
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        solution, _fixed, read_info = _solve_body_against_conn(conn, body)

        if solution.get("unknown_parcels"):
            raise InvalidRoundingRequestError(
                "共边保留对引用了既不在本单、也不在库中的宗地",
                unknown_parcels=solution["unknown_parcels"],
            )
        if not solution.get("feasible"):
            raise NoFeasibleRoundingError(
                "无可行的整米取整组合：无法同时满足正面积、保留正长度共边以及"
                "不与本单 / 库中宗地正面积叠压；原宗地图与供数未改变",
                solver=solution,
                reasons=solution.get("reasons", []),
                combinations_checked=solution.get("combinations_checked", 0),
                group_count=solution.get("group_count", 0),
            )

        # 用求出的整米矩形构造标准整单，复用整单改界的全部规则
        meter_parcels = [
            {"parcel_no": item["parcel_no"], "ring": item["ring"]}
            for item in solution["assignments"]
        ]
        order_in = OrderIn.model_validate(
            {
                "order_no": body.order_no,
                "parcels": meter_parcels,
            }
        )
        normalized, new_bboxes = validate_order_geometry(order_in)
        summary = persist_validated_order(
            conn, order_in, normalized, new_bboxes, now
        )

        rounding_report = {
            "unit_in": "cm",
            "unit_out": "m",
            "total_cost_cm": solution["total_cost_cm"],
            "total_displacement_cm": solution["total_displacement_cm"],
            "total_displacement_m": solution["total_displacement_m"],
            "lex_key": solution["lex_key"],
            "combinations_checked": solution["combinations_checked"],
            "naive_joint_combinations": solution["naive_joint_combinations"],
            "group_count": solution["group_count"],
            "groups": solution["groups"],
            "preserve_pairs": solution["preserve_pairs"],
            "db_readback": read_info,
        }
        summary["rounding"] = rounding_report
        # 让整单记录里也带上取整报告
        conn.execute(
            "UPDATE orders SET result_json = ? WHERE order_id = ?",
            (json.dumps(summary, ensure_ascii=False), summary["order_id"]),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    rectangles = solution["assignments"]
    return {
        "status": "applied",
        "applied": True,
        "dry_run": False,
        "unit_in": "cm",
        "unit_out": "m",
        "order_no": summary["order_no"],
        "order_id": summary["order_id"],
        "parcel_count": len(rectangles),
        # 多种直观字段名，方便不同调用方
        "rectangles": rectangles,
        "rounded_rectangles": rectangles,
        "best_rectangles": rectangles,
        "results": rectangles,
        "rounded_parcels": rectangles,
        "solution": solution,
        "parcels": [
            {
                "parcel_no": item["parcel_no"],
                "rect": item["rect"],
                "ring": item["ring"],
                "bbox": item["bbox"],
                "cost_cm": item["cost_cm"],
            }
            for item in rectangles
        ],
        "preserve_pairs": solution["preserve_pairs"],
        "cost": solution["total_cost_cm"],
        "objective_value_cm": solution["total_cost_cm"],
        "total_cost_cm": solution["total_cost_cm"],
        "total_displacement_cm": solution["total_displacement_cm"],
        "total_displacement_m": solution["total_displacement_m"],
        "lex_key": solution["lex_key"],
        "combinations_checked": solution["combinations_checked"],
        "naive_joint_combinations": solution["naive_joint_combinations"],
        "group_count": solution["group_count"],
        "groups": solution["groups"],
        "solver_stats": {
            "combinations_checked": solution["combinations_checked"],
            "naive_joint_combinations": solution["naive_joint_combinations"],
            "group_count": solution["group_count"],
            "groups": solution["groups"],
        },
        "db_readback": read_info,
        # 直接展开整单改界结果，老调用方关心的 shared_edges / readback 仍在顶层
        "shared_edges": summary["shared_edges"],
        "readback": summary["readback"],
        "applied_parcels": summary["applied_parcels"],
        "order_result": summary,
        "created_at": summary["created_at"],
    }


# ------------------------------------------------------------ 查询

def normalize_pair(parcel_a: str, parcel_b: str) -> tuple[str, str]:
    a = (parcel_a or "").strip()
    b = (parcel_b or "").strip()
    if not a or not b:
        from .errors import AppError

        raise AppError("必须提供两个宗地号 parcel_a 与 parcel_b")
    if a == b:
        from .errors import AppError

        raise AppError("parcel_a 与 parcel_b 必须是不同宗地")
    return (a, b) if a < b else (b, a)


def pair_edges(
    conn: sqlite3.Connection, parcel_a: str, parcel_b: str, include_removed: bool = True
) -> dict[str, Any]:
    a, b = normalize_pair(parcel_a, parcel_b)
    sql = _EDGE_SELECT + " WHERE e.parcel_a = ? AND e.parcel_b = ?"
    params: list[Any] = [a, b]
    if not include_removed:
        sql += " AND e.state != 'removed'"
    sql += " ORDER BY e.orient, e.fixed, e.lo, e.edge_id"
    rows = conn.execute(sql, params).fetchall()
    edges = [edge_to_dict(r) for r in rows]
    current = [e for e in edges if e["state"] != "removed"]
    return {
        "parcel_a": a,
        "parcel_b": b,
        "edge_count": len(current),
        "edges": edges,
    }


def parcel_edges(conn: sqlite3.Connection, parcel_no: str) -> dict[str, Any]:
    parcel_no = parcel_no.strip()
    if not conn.execute("SELECT 1 FROM parcels WHERE parcel_no=?", (parcel_no,)).fetchone():
        raise NotFoundError(f"宗地 {parcel_no} 不存在", parcel_no=parcel_no)
    rows = conn.execute(
        _EDGE_SELECT
        + """ WHERE (e.parcel_a = ? OR e.parcel_b = ?) AND e.state != 'removed'
              ORDER BY e.parcel_a, e.parcel_b, e.orient, e.fixed, e.lo""",
        (parcel_no, parcel_no),
    ).fetchall()
    return {"parcel_no": parcel_no, "edge_count": len(rows), "edges": [edge_to_dict(r) for r in rows]}


def get_parcel(conn: sqlite3.Connection, parcel_no: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM parcels WHERE parcel_no=?", (parcel_no,)).fetchone()
    if row is None:
        raise NotFoundError(f"宗地 {parcel_no} 不存在", parcel_no=parcel_no)
    return parcel_to_dict(row)


def list_parcels(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT parcel_no, minx, miny, maxx, maxy, area2, order_id, updated_at FROM parcels ORDER BY parcel_no"
    ).fetchall()
    return {
        "count": len(rows),
        "parcels": [
            {
                "parcel_no": r["parcel_no"],
                "bbox": {"minx": r["minx"], "miny": r["miny"], "maxx": r["maxx"], "maxy": r["maxy"]},
                "area2": r["area2"],
                "order_id": r["order_id"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ],
    }


def get_order(conn: sqlite3.Connection, order_no: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    if row is None:
        raise NotFoundError(f"单 {order_no} 不存在", order_no=order_no)
    result = json.loads(row["result_json"])
    return {
        "order_id": row["order_id"],
        "order_no": row["order_no"],
        "item_count": row["item_count"],
        "created_at": row["created_at"],
        "result": result,
    }


def list_orders(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT order_id, order_no, item_count, created_at FROM orders ORDER BY order_id DESC LIMIT 100"
    ).fetchall()
    return {"count": len(rows), "orders": [dict(r) for r in rows]}


# ------------------------------------------------------------ 签收

def _sign_row_locked(
    conn: sqlite3.Connection, row: sqlite3.Row, verifier: str, seen_version: int, now: str
) -> dict[str, Any]:
    """在已开启的事务内对单行做版本校验并写入签收。调用方负责提交/回滚。"""
    if row["state"] == "removed":
        from .errors import EdgeRemovedError

        raise EdgeRemovedError(
            f"共边 {row['edge_id']} 已消失，停止供数，不能签收",
            edge=edge_to_dict(row),
        )

    if seen_version != row["version"]:
        raise StaleSignoffError(
            f"签收被拒绝：核验员所见版本 v{seen_version} 已过期，"
            f"当前版本为 v{row['version']}，请按最新图形重新核验后签收",
            edge_id=row["edge_id"],
            seen_version=seen_version,
            current_version=row["version"],
            current_edge=edge_to_dict(row),
        )

    cur = conn.execute(
        "INSERT INTO signoffs (edge_id, version, verifier, signed_at) VALUES (?, ?, ?, ?)",
        (row["edge_id"], row["version"], verifier, now),
    )
    signoff_id = cur.lastrowid
    conn.execute(
        """UPDATE shared_edges
           SET state='signed', signed_by=?, signed_at=?, current_signoff_id=?
           WHERE edge_id=?""",
        (verifier, now, signoff_id, row["edge_id"]),
    )
    fresh = conn.execute(_EDGE_SELECT + " WHERE e.edge_id = ?", (row["edge_id"],)).fetchone()
    body = edge_to_dict(fresh)
    body["signoff_id"] = signoff_id
    return body


def sign_edge(
    conn: sqlite3.Connection, edge_id: int, verifier: str, seen_version: int
) -> dict[str, Any]:
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(_EDGE_SELECT + " WHERE e.edge_id = ?", (edge_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"共边 {edge_id} 不存在", edge_id=edge_id)
        body = _sign_row_locked(conn, row, verifier, seen_version, now)
        conn.commit()
        return body
    except BaseException:
        conn.rollback()
        raise


def sign_pair(
    conn: sqlite3.Connection,
    parcel_a: str,
    parcel_b: str,
    verifier: str,
    items: list[Any],
) -> dict[str, Any]:
    """按宗地对批量签收；同一事务内任一版本过期则整批回滚拒绝。"""
    pa, pb = normalize_pair(parcel_a, parcel_b)
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    signed: list[dict[str, Any]] = []
    try:
        for item in items:
            edge_id = item["edge_id"] if isinstance(item, dict) else item.edge_id
            seen_version = (
                item["seen_version"] if isinstance(item, dict) else item.seen_version
            )
            row = conn.execute(
                _EDGE_SELECT
                + " WHERE e.edge_id = ? AND e.parcel_a = ? AND e.parcel_b = ?",
                (edge_id, pa, pb),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"宗地对 {pa}/{pb} 下不存在共边 {edge_id}",
                    edge_id=edge_id,
                    parcel_a=pa,
                    parcel_b=pb,
                )
            signed.append(_sign_row_locked(conn, row, verifier, seen_version, now))
        conn.commit()
        return {"parcel_a": pa, "parcel_b": pb, "signed_count": len(signed), "edges": signed}
    except BaseException:
        conn.rollback()
        raise


# ------------------------------------------------------------ 制图取数

def mapping_feed(conn: sqlite3.Connection) -> dict[str, Any]:
    """只给“当前版本且当前签收有效”的线；过期 / 消失线一律不出现。"""
    rows = conn.execute(
        _EDGE_SELECT
        + """
        JOIN signoffs s ON s.signoff_id = e.current_signoff_id
        WHERE e.state = 'signed'
          AND s.edge_id = e.edge_id
          AND s.version = e.version
        ORDER BY e.parcel_a, e.parcel_b, e.edge_id
        """
    ).fetchall()
    features = [_feature(r) for r in rows]
    return {
        "type": "FeatureCollection",
        "survey_area": "default",
        "feature_count": len(features),
        "features": features,
    }


def delivery_by_signoff(conn: sqlite3.Connection, signoff_id: int) -> dict[str, Any]:
    """凭签收凭证取数；凭证对应版本不是当前有效签收时拒绝（过期签收）。"""
    srow = conn.execute("SELECT * FROM signoffs WHERE signoff_id=?", (signoff_id,)).fetchone()
    if srow is None:
        raise NotFoundError(f"签收凭证 {signoff_id} 不存在", signoff_id=signoff_id)
    erow = conn.execute(
        _EDGE_SELECT + " WHERE e.edge_id=?", (srow["edge_id"],)
    ).fetchone()
    if erow is None:
        raise NotFoundError("签收凭证对应的共边不存在", signoff_id=signoff_id)

    edge = edge_to_dict(erow)
    valid = (
        erow["state"] == "signed"
        and erow["current_signoff_id"] == signoff_id
        and srow["version"] == erow["version"]
    )
    if not valid:
        raise StaleSignoffError(
            "签收已过期或共边已变动 / 消失，拒绝按该凭证供数，请重新核验签收",
            signoff_id=signoff_id,
            signed_version=srow["version"],
            current_version=erow["version"],
            current_state=erow["state"],
            current_edge=edge,
        )

    return {
        "signoff_id": signoff_id,
        "valid": True,
        "signed_by": srow["verifier"],
        "signed_at": srow["signed_at"],
        "feature": _feature(erow),
    }
