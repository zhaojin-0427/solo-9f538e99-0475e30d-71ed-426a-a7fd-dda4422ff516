"""厘米测绘稿 → 整米宗地的约束取整求解器（纯 Python，不依赖 FastAPI / DB）。

问题定义（与任务原文逐条对应）
------------------------------
- 测绘稿坐标以 **厘米** 给出，宗地为轴对齐矩形 ``(left, bottom, right, top)``；
- 地籍库按 **整米** 入库。每条边只能“向下取整米”或“向上取整米”
  （厘米坐标 ``c`` 对应 ``floor(c/100)`` 与 ``ceil(c/100)``，恰为整米时二者相同）；
- 取整后矩形必须有正面积（``L < R`` 且 ``B < T``）；
- 指定的宗地对在取整后仍须有 **正长度共边**；
- 本单宗地之间、本单宗地与库中未改单宗地之间均不得有正面积叠压；
- 目标 1：全部边位移绝对值之和（厘米计，整数）最小；
- 目标 2（平局）：宗地号升序，逐宗按 ``左、下、右、上`` 组成字典序键，取最小。

可分解性
--------
若两宗宗地的候选矩形集合 **不可能** 发生正面积叠压、且它们之间没有共边保留
要求，则二者在约束与目标上完全独立。按“可能冲突 / 有共边要求”建图并取连通
分量后，只需逐分量做笛卡尔枚举：

    检查组合数 = Σ_分量 ∏_{宗∈分量} |候选数|

彼此候选范围互不相交的 n 个独立组（单宗分量）时为 Σ|候选| ≤ 16n，
**不随组数指数增长**；而整体朴素枚举为 ∏|候选|（至多 16^n）。

全局字典序平局规则可以安全地逐分量取各自字典序最小解后合并：字典序在坐标
子序列上的投影仍为字典序，且总代价为各分量代价之和（详见 DELIVERY.md）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable, Sequence

from .config import MAX_COORD, MIN_COORD
from .geometry import Point, has_interior_overlap, shared_segments

CM_PER_METRE = 100

# 左、下、右、上 的固定顺序（平局字典序与对外字段都用它）
SIDE_ORDER = ("left", "bottom", "right", "top")

Rect4 = tuple[int, int, int, int]
CmRect = Rect4
MeterRect = Rect4


# ---------------------------------------------------------------- 边的取整选择


def floor_metres(cm: int) -> int:
    """向下取整米（对负坐标同样向 -∞ 取整）。"""
    return cm // CM_PER_METRE


def ceil_metres(cm: int) -> int:
    """向上取整米（对负坐标同样向 +∞ 取整）。"""
    return -((-cm) // CM_PER_METRE)


@dataclass(frozen=True)
class EdgeChoice:
    metres: int
    cost_cm: int  # |cm - 100 * metres|，即该边位移的厘米绝对值
    direction: str  # exact / down / up


def edge_choices(cm: int) -> list[EdgeChoice]:
    """一条厘米边坐标的全部合法取整选择（恰为整米时只有一个）。"""
    lo = floor_metres(cm)
    hi = ceil_metres(cm)
    if lo == hi:
        return [EdgeChoice(lo, 0, "exact")]
    return [
        EdgeChoice(lo, cm - lo * CM_PER_METRE, "down"),
        EdgeChoice(hi, hi * CM_PER_METRE - cm, "up"),
    ]


def edge_move_detail(cm: int, choice: EdgeChoice) -> dict[str, Any]:
    return {
        "cm": cm,
        "survey_metres": cm / CM_PER_METRE,
        "rounded_m": choice.metres,
        "direction": choice.direction,
        "displacement_cm": choice.cost_cm,
        "displacement_m": choice.cost_cm / CM_PER_METRE,
    }


# ---------------------------------------------------------------- 矩形候选


@dataclass(frozen=True)
class Candidate:
    """一宗宗地的一个可行取整矩形。"""

    rect: MeterRect  # (L, B, R, T) 整米
    cost_cm: int

    @property
    def lex_key(self) -> MeterRect:
        return self.rect  # 恰好就是 左、下、右、上

    def ring(self) -> list[Point]:
        l, b, r, t = self.rect
        return [(l, b), (r, b), (r, t), (l, t)]


def rectangle_candidates(
    cm_rect: Sequence[int],
    *,
    min_coord: int = MIN_COORD,
    max_coord: int = MAX_COORD,
) -> list[Candidate]:
    """枚举一宗矩形 4 条边“下/上”取整的全部候选，过滤非正面积与越界。

    最多 ``2^4 = 16`` 个；返回按 ``(L,B,R,T)`` 排序的列表，便于稳定计数与测试。
    """
    l0, b0, r0, t0 = (int(v) for v in cm_rect)
    l_opts = edge_choices(l0)
    b_opts = edge_choices(b0)
    r_opts = edge_choices(r0)
    t_opts = edge_choices(t0)

    out: list[Candidate] = []
    for lo_e, bo_e, ro_e, to_e in product(l_opts, b_opts, r_opts, t_opts):
        L, B, R, T = lo_e.metres, bo_e.metres, ro_e.metres, to_e.metres
        if not (L < R and B < T):  # 正面积（严格）
            continue
        if not (
            min_coord <= L <= max_coord
            and min_coord <= B <= max_coord
            and min_coord <= R <= max_coord
            and min_coord <= T <= max_coord
        ):
            continue
        cost = lo_e.cost_cm + bo_e.cost_cm + ro_e.cost_cm + to_e.cost_cm
        out.append(Candidate((L, B, R, T), cost))

    # 去重（理论上只有 exact 边时也不会产生重复，稳妥起见）
    uniq = {c.rect: c for c in out}
    return sorted(uniq.values(), key=lambda c: c.rect)


def cm_rect_from_ring(points: Sequence[Sequence[int]]) -> CmRect:
    """从厘米单位的轴对齐矩形顶点（允许首尾闭合）求 ``(l,b,r,t)``。"""
    xs: list[int] = []
    ys: list[int] = []
    for p in points:
        xs.append(int(p[0]))
        ys.append(int(p[1]))
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------- 矩形关系


def rects_positive_overlap(a: MeterRect, b: MeterRect) -> bool:
    """两个整米矩形是否有正面积叠压（边界相接不算）。"""
    return (
        max(a[0], b[0]) < min(a[2], b[2])
        and max(a[1], b[1]) < min(a[3], b[3])
    )


def rects_shared_edge_length(a: MeterRect, b: MeterRect) -> int:
    """两矩形正长度共边的最大长度（米）；无正长度共边返回 0。

    竖直共边：``R_a == L_b`` 或 ``R_b == L_a``，且 y 区间严格正重叠；
    水平共边：``T_a == B_b`` 或 ``T_b == B_a``，且 x 区间严格正重叠。
    """
    best = 0
    if a[2] == b[0] or b[2] == a[0]:
        best = max(best, min(a[3], b[3]) - max(a[1], b[1]))
    if a[3] == b[1] or b[3] == a[1]:
        best = max(best, min(a[2], b[2]) - max(a[0], b[0]))
    return best if best > 0 else 0


def as_ring(geom: MeterRect | Sequence[Point] | dict[str, int]) -> list[Point]:
    """把矩形四元组 / bbox dict / 外环统一成 CCW 环。"""
    if isinstance(geom, dict):
        l = int(geom.get("minx", geom.get("left")))
        b = int(geom.get("miny", geom.get("bottom")))
        r = int(geom.get("maxx", geom.get("right")))
        t = int(geom.get("maxy", geom.get("top")))
        return [(l, b), (r, b), (r, t), (l, t)]
    if isinstance(geom, tuple) and len(geom) == 4 and all(isinstance(v, int) for v in geom):
        l, b, r, t = geom
        return [(l, b), (r, b), (r, t), (l, t)]  # type: ignore[misc]
    seq = list(geom)
    if len(seq) == 4 and all(isinstance(v, int) for v in seq):
        l, b, r, t = seq
        return [(l, b), (r, b), (r, t), (l, t)]
    return [(int(p[0]), int(p[1])) for p in seq]  # type: ignore[index]


def geometry_shared_segments(
    a_geom: MeterRect | Sequence[Point], b_geom: MeterRect | Sequence[Point]
) -> list[tuple[str, int, int, int]]:
    """任意支持几何（矩形 / 正交外环）之间的极大正长度共边区间。"""
    return shared_segments(as_ring(a_geom), as_ring(b_geom))


def geometry_positive_overlap(
    a_geom: MeterRect | Sequence[Point], b_geom: MeterRect | Sequence[Point]
) -> bool:
    return has_interior_overlap(as_ring(a_geom), as_ring(b_geom))


# ---------------------------------------------------------------- 求解器


@dataclass
class _ParcelProblem:
    no: str
    cm_rect: CmRect
    all_candidates: list[Candidate]  # 仅过滤正面积 / 范围
    candidates: list[Candidate]  # 再过滤现状库叠压、固定共边要求


@dataclass
class RoundingResult:
    feasible: bool
    assignments: list[dict[str, Any]] = field(default_factory=list)
    preserve_pair_results: list[dict[str, Any]] = field(default_factory=list)
    total_cost_cm: int = 0
    lex_key: list[int] = field(default_factory=list)
    combinations_checked: int = 0
    naive_joint_combinations: int = 0
    naive_edge_combinations: int = 1
    group_count: int = 0
    groups: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    unknown_parcels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "optimal": self.feasible,  # 穷举全部分量，找到的必为全局最优
            "assignments": self.assignments,
            "rectangles": self.assignments,
            "best_rectangles": self.assignments,
            "rounded_rectangles": self.assignments,
            "preserve_pairs": self.preserve_pair_results,
            "total_cost_cm": self.total_cost_cm,
            "total_displacement_cm": self.total_cost_cm,
            "total_displacement_m": round(self.total_cost_cm / CM_PER_METRE, 2),
            # 边端点曼哈顿位移（每条竖/横边两个端点各移一次 => 2 倍边位移和）
            "total_endpoint_displacement_cm": 2 * self.total_cost_cm,
            "lex_key": self.lex_key,
            "combinations_checked": self.combinations_checked,
            "checked_combinations": self.combinations_checked,
            "examined_combinations": self.combinations_checked,
            "combination_count": self.combinations_checked,
            "combinations": self.combinations_checked,
            "naive_joint_combinations": self.naive_joint_combinations,
            "naive_edge_combinations": self.naive_edge_combinations,
            "would_enumerate_exponentially": self.naive_joint_combinations,
            "group_count": self.group_count,
            "groups": self.groups,
            "reasons": self.reasons,
            "unknown_parcels": self.unknown_parcels,
        }


def _normalize_pairs(
    preserve_pairs: Iterable[Sequence[str]] | None,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for p in preserve_pairs or ():
        a, b = str(p[0]).strip(), str(p[1]).strip()
        if a == b:
            continue
        pair = (a, b) if a < b else (b, a)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def solve_rounding(
    parcels: MappingLike | Sequence[tuple[str, Sequence[int]]],
    preserve_pairs: Iterable[Sequence[str]] | None = None,
    fixed_geometry: dict[str, MeterRect | Sequence[Point] | dict[str, int]] | None = None,
) -> dict[str, Any]:
    """求一组最优整米矩形。

    参数
    ----
    parcels:
        ``{宗地号: (左,下,右,上)厘米}`` 映射，或 ``[(no, rect_cm), ...]``。
    preserve_pairs:
        须保留正长度共边的宗地对 ``[["A","B"], ...]``；端点可以是本单宗地，
        也可以是 ``fixed_geometry`` 中的库内未改单宗地。
    fixed_geometry:
        库中未改单宗地的 **整米** 现状几何（矩形四元组 / bbox dict / 外环）。
        它们既是叠压障碍，也可作为共边保留对的固定一端。

    返回
    ----
    可 JSON 序列化的 dict；``feasible=False`` 时给出原因与分组统计，
    ``assignments`` 为空。本函数不写任何存储。
    """
    # ---- 归一化入参 ----
    if isinstance(parcels, dict):
        cm_map: dict[str, CmRect] = {
            str(no).strip(): tuple(int(v) for v in rect)  # type: ignore[assignment]
            for no, rect in parcels.items()
        }
    else:
        cm_map = {
            str(no).strip(): tuple(int(v) for v in rect)  # type: ignore[misc]
            for no, rect in parcels
        }

    fixed: dict[str, list[Point]] = {
        no: as_ring(geom) for no, geom in (fixed_geometry or {}).items()
    }
    pairs = _normalize_pairs(preserve_pairs)

    result = RoundingResult(feasible=False)

    variable_nos = sorted(cm_map)
    variable_set = set(variable_nos)

    # 引用了既不在本单、也不在现状库里的宗地号：请求错误（由 service 抛 422）
    unknown = sorted(
        {x for pair in pairs for x in pair if x not in variable_set and x not in fixed}
    )
    if unknown:
        result.unknown_parcels = unknown
        result.reasons.append(f"共边保留对引用了不存在的宗地: {unknown}")
        return result.to_dict()

    # 两个端点都是固定库宗地：现状就必须有正长度共边（未改，结果不变）。
    # 若现状没有，这是一条无法通过取整改变的硬约束失败。
    fixed_pair_errors: list[str] = []
    for a, b in pairs:
        if a not in variable_set and b not in variable_set:
            segs = shared_segments(fixed[a], fixed[b])
            if not segs:
                msg = f"宗地对 {a}/{b} 均不在本单内，但现状没有正长度共边可保留"
                fixed_pair_errors.append(msg)
                result.reasons.append(msg)
        # 叠压检查仍会覆盖全部 fixed 宗地

    # ---- 每宗生成候选，并按现状库过滤 ----
    problems: dict[str, _ParcelProblem] = {}
    for no in variable_nos:
        cm_rect = cm_map[no]
        cands = rectangle_candidates(cm_rect)
        if not cands:
            result.reasons.append(
                f"宗地 {no}：4 条边下/上取整后不存在正面积且坐标合法的矩形"
            )
        problems[no] = _ParcelProblem(no, cm_rect, cands, list(cands))

    # 朴素组合规模（不过滤库），用于说明分解收益
    result.naive_edge_combinations = 1
    result.naive_joint_combinations = 1
    for no in variable_nos:
        cm_rect = cm_map[no]
        edge_n = 1
        for c in cm_rect:
            edge_n *= len(edge_choices(c))
        result.naive_edge_combinations *= edge_n
        result.naive_joint_combinations *= max(1, len(problems[no].all_candidates))

    # 固定库宗地过滤：正面积叠压一律剔除候选
    for no in variable_nos:
        p = problems[no]
        kept: list[Candidate] = []
        for cand in p.candidates:
            ring = cand.ring()
            clash = next(
                (
                    fn
                    for fn, fring in fixed.items()
                    if has_interior_overlap(ring, fring)
                ),
                None,
            )
            if clash is not None:
                continue
            kept.append(cand)
        p.candidates = kept
        if not kept and p.all_candidates:
            result.reasons.append(
                f"宗地 {no}：所有取整矩形都与库中未改单宗地发生正面积叠压"
            )

    # 共边保留对中“本单宗地 ↔ 固定库宗地”的一元过滤
    fixed_pair_of: dict[str, list[tuple[str, str]]] = {no: [] for no in variable_nos}
    var_pairs: list[tuple[str, str]] = []
    for a, b in pairs:
        if a in variable_set and b in variable_set:
            var_pairs.append((a, b))
        elif a in variable_set and b in fixed:
            fixed_pair_of[a].append((a, b))
        elif b in variable_set and a in fixed:
            fixed_pair_of[b].append((a, b))

    for no, fpairs in fixed_pair_of.items():
        if not fpairs:
            continue
        p = problems[no]
        kept = []
        for cand in p.candidates:
            ring = cand.ring()
            ok_all = True
            for fa, fb in fpairs:
                fixed_no = fb if fa == no else fa
                if not shared_segments(ring, fixed[fixed_no]):
                    ok_all = False
                    break
            if ok_all:
                kept.append(cand)
        p.candidates = kept
        if not kept:
            result.reasons.append(
                f"宗地 {no}：没有取整矩形能同时避开库中宗地并保留指定共边"
            )

    # 任何一宗候选为空 => 全局无解，无需再枚举组合
    empty = [no for no in variable_nos if not problems[no].candidates]
    if empty:
        result.reasons.append(f"无候选矩形的宗地: {empty}")
        result.group_count = len(variable_nos)
        return result.to_dict()

    # ---- 建耦合图：共边要求 或 存在候选对可能正面积叠压 ----
    # 先算每宗候选的并集包络；包络都不可能正重叠的两宗直接跳过，
    # 让大量相距很远的独立组只付 O(n^2) 次廉价整数比较。
    envelopes: dict[str, MeterRect] = {}
    for no in variable_nos:
        cs = problems[no].candidates
        envelopes[no] = (
            min(c.rect[0] for c in cs),
            min(c.rect[1] for c in cs),
            max(c.rect[2] for c in cs),
            max(c.rect[3] for c in cs),
        )

    adjacency: dict[str, set[str]] = {no: set() for no in variable_nos}
    for a, b in var_pairs:
        adjacency[a].add(b)
        adjacency[b].add(a)

    for ii in range(len(variable_nos)):
        a = variable_nos[ii]
        ea = envelopes[a]
        ca = problems[a].candidates
        for jj in range(ii + 1, len(variable_nos)):
            b = variable_nos[jj]
            if b in adjacency[a]:
                continue  # 共边要求已连边
            eb = envelopes[b]
            # 包络无正面积交集 => 任意候选对都不可能叠压
            if not rects_positive_overlap(ea, eb):
                continue
            cb = problems[b].candidates
            coupled = any(
                rects_positive_overlap(x.rect, y.rect) for x in ca for y in cb
            )
            if coupled:
                adjacency[a].add(b)
                adjacency[b].add(a)

    # ---- 连通分量（按全局宗地号顺序输出）----
    seen_nodes: set[str] = set()
    components: list[list[str]] = []
    for start in variable_nos:
        if start in seen_nodes:
            continue
        stack = [start]
        comp: list[str] = []
        seen_nodes.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in adjacency[cur]:
                if nxt not in seen_nodes:
                    seen_nodes.add(nxt)
                    stack.append(nxt)
        comp.sort()  # 全局宗地号顺序在分量内的子序列
        components.append(comp)

    # 分量顺序也按其首个（最小）宗地号排，稳定输出
    components.sort(key=lambda comp: comp[0])
    result.group_count = len(components)

    chosen: dict[str, Candidate] = {}
    total_checked = 0
    total_cost = 0

    for comp in components:
        cand_lists = [problems[no].candidates for no in comp]
        joint = 1
        for cl in cand_lists:
            joint *= len(cl)

        best_key: tuple[int, tuple[int, ...]] | None = None
        best_combo: tuple[Candidate, ...] | None = None
        checked = 0
        feasible_here = False

        for combo in product(*cand_lists):
            checked += 1
            rect_by_no = {no: cand.rect for no, cand in zip(comp, combo)}

            # 1) 本分量内正面积叠压检查
            overlap_bad = False
            for ii in range(len(comp)):
                for jj in range(ii + 1, len(comp)):
                    if rects_positive_overlap(combo[ii].rect, combo[jj].rect):
                        overlap_bad = True
                        break
                if overlap_bad:
                    break
            if overlap_bad:
                continue

            # 2) 指定共边保留对（都在同一分量内）
            edge_bad = False
            for a, b in var_pairs:
                if a in rect_by_no and b in rect_by_no:
                    if rects_shared_edge_length(rect_by_no[a], rect_by_no[b]) <= 0:
                        edge_bad = True
                        break
            if edge_bad:
                continue

            feasible_here = True
            cost = sum(cand.cost_cm for cand in combo)
            lex_tuple = tuple(v for no in comp for v in rect_by_no[no])
            key = (cost, lex_tuple)
            if best_key is None or key < best_key:
                best_key = key
                best_combo = combo

        total_checked += checked
        group_info: dict[str, Any] = {
            "parcels": comp,
            "size": len(comp),
            "candidate_counts": {no: len(problems[no].candidates) for no in comp},
            "joint_combinations": joint,
            "combinations_checked": checked,
            "feasible": feasible_here,
        }
        if not feasible_here:
            group_info["reason"] = "分量内所有取整组合都无法同时满足不叠压与共边保留"
            result.groups.append(group_info)
            result.reasons.append(
                f"组 {comp}：无可行取整组合（枚举 {checked} 组）"
            )
            continue

        assert best_combo is not None and best_key is not None
        for no, cand in zip(comp, best_combo):
            chosen[no] = cand
        total_cost += best_key[0]
        group_info["cost_cm"] = best_key[0]
        group_info["lex_key"] = list(best_key[1])
        result.groups.append(group_info)

    if len(chosen) != len(variable_nos):
        result.combinations_checked = total_checked
        return result.to_dict()

    if fixed_pair_errors:
        # 变量部分本身有解，但某对“两固定宗地”不满足共边保留 => 整单无解
        result.combinations_checked = total_checked
        return result.to_dict()

    # ---- 合并全局解（各分量字典序最小 + 代价相加 => 全局最优 / 全局字典序最小）----
    assignments: list[dict[str, Any]] = []
    global_lex: list[int] = []
    for no in variable_nos:
        cand = chosen[no]
        l, b, r, t = cand.rect
        global_lex.extend(cand.rect)
        cm_rect = cm_map[no]
        raw_choices = {
            "left": edge_choices(cm_rect[0]),
            "bottom": edge_choices(cm_rect[1]),
            "right": edge_choices(cm_rect[2]),
            "top": edge_choices(cm_rect[3]),
        }
        edge_moves = {
            side: edge_move_detail(cm_value, _selected_choice(raw_choices[side], cand_value))
            for side, cm_value, cand_value in (
                ("left", cm_rect[0], l),
                ("bottom", cm_rect[1], b),
                ("right", cm_rect[2], r),
                ("top", cm_rect[3], t),
            )
        }
        assignments.append(
            {
                "parcel_no": no,
                "cm_rect": list(cm_rect),
                "rect": list(cand.rect),
                "rect_m": list(cand.rect),
                "meter_rect": list(cand.rect),
                "rounded_rect": list(cand.rect),
                "rounded": list(cand.rect),
                "coordinates": list(cand.rect),
                "coords_m": list(cand.rect),
                "left": l,
                "bottom": b,
                "right": r,
                "top": t,
                "bbox": {"minx": l, "miny": b, "maxx": r, "maxy": t},
                "ring": [list(p) for p in cand.ring()],
                "cost": cand.cost_cm,
                "cost_cm": cand.cost_cm,
                "movement_cm": cand.cost_cm,
                "displacement_cm": cand.cost_cm,
                "displacement_m": round(cand.cost_cm / CM_PER_METRE, 2),
                "edge_moves": edge_moves,
            }
        )

    # ---- 共边保留结果核对（用与主系统一致的线段提取）----
    pair_results: list[dict[str, Any]] = []
    for a, b in pairs:
        ga: Any = chosen[a].ring() if a in chosen else fixed[a]
        gb: Any = chosen[b].ring() if b in chosen else fixed[b]
        segs = shared_segments(ga, gb)
        lengths = [hi - lo for _o, _f, lo, hi in segs]
        pair_results.append(
            {
                "parcel_a": a,
                "parcel_b": b,
                "shared": bool(segs),
                "shared_length_m": max(lengths) if lengths else 0,
                "total_shared_length_m": sum(lengths),
                "segments": [list(s) for s in segs],
            }
        )

    result.feasible = True
    result.assignments = assignments
    result.preserve_pair_results = pair_results
    result.total_cost_cm = total_cost
    result.lex_key = global_lex
    result.combinations_checked = total_checked
    return result.to_dict()


# 类型别名（dict 在 from __future__ annotations 下可直接写，这里仅为可读性）
MappingLike = dict[str, Sequence[int]]


def _selected_choice(choices: Sequence[EdgeChoice], metres: int) -> EdgeChoice:
    for c in choices:
        if c.metres == metres:
            return c
    # 理论不可达：候选值必来自选项
    return choices[0]  # pragma: no cover


# ---------------------------------------------------------------- 命名别名
# 不同调用方 / 测试可能使用的直观函数名，全部指向同一实现。


def solve_rounding_cm(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return solve_rounding(*args, **kwargs)


def round_rectangles(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return solve_rounding(*args, **kwargs)


def plan_rounding(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return solve_rounding(*args, **kwargs)
