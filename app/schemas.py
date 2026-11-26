"""请求 / 响应模型（同时用于 /docs 展示）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


class ParcelRingIn(BaseModel):
    parcel_no: str = Field(..., min_length=1, max_length=64, description="宗地号")
    # 严格整数：1.0、true 之类不会被接受
    ring: list[tuple[StrictInt, StrictInt]] | None = Field(
        default=None, min_length=4, description="地块外环顶点，按顺序首尾不必重复；无洞"
    )
    # 领域约束为“地块无洞”：允许显式传空数组，传非空内环一律拒绝
    holes: list[list[tuple[StrictInt, StrictInt]]] = Field(
        default_factory=list, description="必须为空：本地块模型只有外环、无洞"
    )
    # 仅用于 /api/orders 自动识别厘米取整单；老的 ring 提交不受影响
    rect_cm: list[StrictInt] | None = Field(default=None, exclude=True)

    @field_validator("parcel_no")
    @classmethod
    def _strip_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("宗地号不能为空白")
        return v

    @field_validator("holes")
    @classmethod
    def _no_holes(cls, v: list) -> list:
        if v:
            raise ValueError("地块无洞：不接受任何内环 holes")
        return v


_ROUNDING_UNIT_VALUES = {"cm", "centimeter", "centimeters", "厘米", "公分"}
_PAIR_KEYS = (
    "preserve_pairs",
    "shared_edge_pairs",
    "preserve_shared_edge_pairs",
    "required_shared_edge_pairs",
    "shared_boundary_pairs",
    "boundary_pairs",
    "adjacent_pairs",
    "shared_pairs",
    "edge_pairs",
    "keep_pairs",
    "keep_shared_edges",
    "required_shared_edges",
    "require_shared_edges",
    "pairs",
)
_RECT_KEYS = (
    "rect_cm",
    "cm_rect",
    "rectangle_cm",
    "bbox_cm",
    "coordinates_cm",
)


def looks_like_rounding_order(raw: Any) -> bool:
    """识别发往 ``POST /api/orders`` 的厘米取整单（老的整米环单不会误判）。"""
    if not isinstance(raw, dict):
        return False
    unit = _first_key(raw, ("unit", "coord_unit", "coordinate_unit", "units", "unit_in"))
    if isinstance(unit, str) and unit.strip().lower() in _ROUNDING_UNIT_VALUES:
        return True
    if any(k in raw and raw[k] for k in _PAIR_KEYS):
        return True
    parcels = raw.get("parcels")
    if isinstance(parcels, list) and parcels:
        for p in parcels:
            if isinstance(p, dict) and "ring" not in p and any(
                k in p for k in _RECT_KEYS + ("rect", "bbox")
            ):
                return True
    return False


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_no: str | None = Field(
        default=None, max_length=64, description="单号；不传则服务端生成 ORD-xxxx"
    )
    parcels: list[ParcelRingIn] = Field(..., min_length=1, description="本单整单替换的宗地外环")
    # 识别为厘米取整单时，归一化后的 RoundingOrderIn 原始字典放在这里，
    # 由路由转交给 rounding 服务；普通整米环单该字段为 None。
    rounding_data: dict[str, Any] | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _detect_rounding(cls, data: Any) -> Any:
        if isinstance(data, dict) and looks_like_rounding_order(data):
            normalized = coerce_rounding_payload(data)
            data = dict(data)
            data["rounding_data"] = normalized
            # 取整单的宗地没有 ring；ParcelRingIn.ring 已放宽为可选，
            # 但仍需让 parcels 通过基本结构校验：用 rect_cm 占位即可。
            data["parcels"] = [
                {"parcel_no": p["parcel_no"], "rect_cm": p["rect_cm"]}
                for p in normalized["parcels"]
            ]
        return data

    @field_validator("order_no")
    @classmethod
    def _strip_order_no(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("parcels")
    @classmethod
    def _unique_parcel_numbers(cls, v: list[ParcelRingIn]) -> list[ParcelRingIn]:
        nos = [p.parcel_no for p in v]
        dup = sorted({n for n in nos if nos.count(n) > 1})
        if dup:
            raise ValueError(f"同一单内宗地号重复: {dup}")
        return v

    def as_rounding_order(self) -> "RoundingOrderIn | None":
        if self.rounding_data is None:
            return None
        return RoundingOrderIn.model_validate(self.rounding_data)


class DirectRingIn(BaseModel):
    """单宗地录入 / 更正的便捷入口（内部仍按一宗地的整单处理）。"""

    order_no: str | None = Field(default=None, max_length=64)
    ring: list[tuple[StrictInt, StrictInt]] = Field(..., min_length=4)


class SignIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    verifier: str = Field(..., min_length=1, max_length=64, description="核验员标识 / 姓名")
    # 标准字段名 seen_version；同时兼容调用方直接传 version
    seen_version: int = Field(
        ...,
        ge=1,
        description="核验员在查询结果中看到的共边版本（也可用字段名 version）",
        validation_alias=AliasChoices("seen_version", "version"),
    )

    @field_validator("verifier")
    @classmethod
    def _strip_verifier(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("verifier 不能为空白")
        return v


class PairSignItemIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    edge_id: int = Field(..., description="宗地对查询结果中的 edge_id")
    seen_version: int = Field(
        ...,
        ge=1,
        validation_alias=AliasChoices("seen_version", "version"),
        description="所见版本（也可用字段名 version）",
    )


class PairSignIn(BaseModel):
    verifier: str = Field(..., min_length=1, max_length=64)
    items: list[PairSignItemIn] = Field(..., min_length=1, description="本批签收的线及各自所见版本")

    @field_validator("verifier")
    @classmethod
    def _strip_verifier(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("verifier 不能为空白")
        return v


Orientation = Literal["horizontal", "vertical"]
EdgeState = Literal["signed", "unsigned", "removed"]
SignStatus = Literal["signed", "unsigned", "stale", "removed"]


# ============================================================
# 厘米测绘稿 → 整米取整单
# ============================================================

def _first_key(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return None


def _as_int4(value: Any) -> list[int] | None:
    """把 4 个标量元素的数组 / 常见 bbox dict 统一成 [l, b, r, t]。

    元素不是标量（例如 4 个坐标点）时返回 None，交给环/点列逻辑处理。
    """
    if isinstance(value, dict):
        get = lambda *ks: _first_key(value, ks)  # noqa: E731
        l = get("left", "l", "minx", "x0", "xmin", "west")
        b = get("bottom", "b", "miny", "y0", "ymin", "south")
        r = get("right", "r", "maxx", "x1", "xmax", "east")
        t = get("top", "t", "maxy", "y1", "ymax", "north")
        if None not in (l, b, r, t):
            return [l, b, r, t]
        return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        if all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return list(value)
    return None


def _bbox_from_points(points: Any) -> list[int] | None:
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        return None
    xs: list[int] = []
    ys: list[int] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return None
        x, y = p
        if isinstance(x, bool) or isinstance(y, bool):
            return None
        if not isinstance(x, int) or not isinstance(y, int):
            return None
        xs.append(x)
        ys.append(y)
    return [min(xs), min(ys), max(xs), max(ys)]


def _coerce_cm_parcel(item: Any) -> dict[str, Any]:
    """把各种合理的宗地厘米矩形写法归一成 ``{"parcel_no", "rect_cm"}``。"""
    if isinstance(item, (list, tuple)) and len(item) == 2:
        no, rect = item
        four = _as_int4(rect)
        if four is None:
            four = _bbox_from_points(rect)
        if four is None:
            raise ValueError(f"宗地 {no} 的矩形无法识别: {rect!r}")
        return {"parcel_no": str(no), "rect_cm": four}
    if not isinstance(item, dict):
        raise ValueError("rounding parcel 必须是对象或 [宗地号, 矩形] 二元组")

    no = _first_key(
        item,
        (
            "parcel_no",
            "parcelNo",
            "parcel_id",
            "parcelId",
            "parcel",
            "pid",
            "no",
            "id",
            "code",
            "number",
            "name",
        ),
    )
    if no is None or str(no).strip() == "":
        raise ValueError("厘米取整单的每一宗都必须给出 parcel_no")

    rect = _first_key(
        item,
        (
            "rect_cm",
            "cm_rect",
            "rectangle_cm",
            "rectangle",
            "rect",
            "bbox_cm",
            "bbox",
            "bounds_cm",
            "cm_bounds",
            "bounds",
            "coordinates_cm",
            "coordinates",
            "cm_bbox",
            "xyxy",
        ),
    )
    four = _as_int4(rect) if rect is not None else None
    if four is None and rect is not None:
        # rect/coordinates 也可能直接给 4 个角点
        four = _bbox_from_points(rect)
    if four is None:
        # 也许直接给了 ring（厘米点列）
        ring = _first_key(
            item, ("ring_cm", "ring", "polygon_cm", "polygon", "points", "coordinates")
        )
        four = _bbox_from_points(ring) if ring is not None else None
    if four is None:
        # GeoJSON geometry：{"type":"Polygon","coordinates":[[[x,y],...]]}
        geom = item.get("geometry") if isinstance(item, dict) else None
        if isinstance(geom, dict):
            coords = geom.get("coordinates")
            if geom.get("type") == "Polygon" and coords:
                four = _bbox_from_points(coords[0])
            elif geom.get("type") == "LineString":
                four = _bbox_from_points(coords)
    if four is None:
        # 或散字段 left/bottom/right/top
        four = _as_int4(item)
    if four is None:
        raise ValueError(f"宗地 {no} 必须给出 4 个整数厘米坐标的 rect_cm=[左,下,右,上]")
    return {"parcel_no": str(no).strip(), "rect_cm": four}


def _coerce_pair(item: Any) -> list[str]:
    if isinstance(item, str):
        for sep in ("|", ",", "/"):
            if sep in item:
                parts = [p.strip() for p in item.split(sep) if p.strip()]
                if len(parts) == 2:
                    return parts
        raise ValueError(f"无法解析的宗地对: {item!r}")
    if isinstance(item, dict):
        a = _first_key(
            item,
            ("parcel_a", "parcelA", "parcel1", "p1", "a", "first", "left", "0"),
        )
        b = _first_key(
            item,
            ("parcel_b", "parcelB", "parcel2", "p2", "b", "second", "right", "1"),
        )
        if a is None or b is None:
            raise ValueError(f"无法解析的宗地对对象: {item}")
        return [str(a), str(b)]
    if isinstance(item, (list, tuple)) and len(item) == 2:
        return [str(item[0]), str(item[1])]
    raise ValueError(f"宗地对必须是 [a, b] 或 {{parcel_a, parcel_b}}: {item!r}")


def coerce_rounding_payload(raw: Any) -> dict[str, Any]:
    """把宽松的原始 JSON 归一成 :class:`RoundingOrderIn` 能校验的标准结构。

    同时兼容“宗地号数组 + 矩形数组”的并行写法。
    """
    if not isinstance(raw, dict):
        raise ValueError("请求体必须是 JSON 对象")

    order_no = _first_key(raw, ("order_no", "orderNo", "order_id", "orderId", "no"))
    dry_run = bool(
        _first_key(raw, ("dry_run", "dryRun", "preview", "solve_only", "check_only"))
        or False
    )

    raw_parcels = _first_key(raw, ("parcels", "items", "records", "rounding_parcels"))
    if isinstance(raw_parcels, dict):
        # 兼容 {"A": [l,b,r,t], "B": {...}} 的映射写法
        expanded: list[Any] = []
        for no, v in raw_parcels.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("parcel_no", no)
                expanded.append(item)
            else:
                expanded.append([no, v])
        raw_parcels = expanded
    if raw_parcels is None:
        nos = _first_key(raw, ("parcel_nos", "parcelNos", "numbers", "nos"))
        rects = _first_key(
            raw, ("rectangles", "rects", "cm_rects", "rectangles_cm", "bboxes")
        )
        if isinstance(nos, list) and isinstance(rects, list) and len(nos) == len(rects):
            raw_parcels = [
                {"parcel_no": no, "rect_cm": rect} for no, rect in zip(nos, rects)
            ]
    if not isinstance(raw_parcels, list) or not raw_parcels:
        raise ValueError("必须通过 parcels 给出至少一宗厘米矩形宗地")

    parcels = [_coerce_cm_parcel(p) for p in raw_parcels]

    raw_pairs = _first_key(
        raw,
        (
            "preserve_pairs",
            "shared_edge_pairs",
            "preserve_shared_edge_pairs",
            "required_shared_edge_pairs",
            "shared_boundary_pairs",
            "boundary_pairs",
            "adjacent_pairs",
            "shared_pairs",
            "edge_pairs",
            "keep_pairs",
            "keep_shared_edges",
            "required_shared_edges",
            "require_shared_edges",
            "pairs",
        ),
    )
    pairs = [_coerce_pair(p) for p in raw_pairs] if isinstance(raw_pairs, list) else []

    return {
        "order_no": str(order_no).strip() if order_no else None,
        "dry_run": dry_run,
        "parcels": parcels,
        "preserve_pairs": pairs,
    }


class RoundingParcelIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    parcel_no: str = Field(..., min_length=1, max_length=64, description="宗地号")
    rect_cm: list[StrictInt] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="整数厘米轴对齐矩形 [左, 下, 右, 上]；每条边入库时只能向下/向上取整米",
        validation_alias=AliasChoices(
            "rect_cm", "cm_rect", "rect", "bbox", "bounds", "coordinates"
        ),
    )

    @field_validator("parcel_no")
    @classmethod
    def _strip_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("宗地号不能为空白")
        return v

    @field_validator("rect_cm")
    @classmethod
    def _four_int_values(cls, v: list[StrictInt]) -> list[StrictInt]:
        if len(v) != 4:
            raise ValueError("rect_cm 必须恰好是 [左, 下, 右, 上] 4 个整数")
        # 与整米 ±100 万米范围对应：厘米稿不得超过 ±1 亿厘米
        cm_bound = 100 * 1_000_000
        for x in v:
            if not (-cm_bound <= x <= cm_bound):
                raise ValueError(f"厘米坐标 {x} 超出 ±{cm_bound} 厘米范围")
        # 注意：不在这里要求 cm 的 left<right/bottom<top——
        # 原文只要求“取整后”正面积；退化厘米稿可能向外取整出正面积。
        return v


class RoundingOrderIn(BaseModel):
    """厘米测绘稿整单取整入库请求。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    order_no: str | None = Field(default=None, max_length=64, description="整单号；不传自动生成")
    dry_run: bool = Field(default=False, description="仅试算最优组合，不写库")
    parcels: list[RoundingParcelIn] = Field(..., min_length=1)
    preserve_pairs: list[list[str]] = Field(
        default_factory=list, description="取整后仍须保持正长度共边的宗地对"
    )

    @model_validator(mode="before")
    @classmethod
    def _lenient_shape(cls, data: Any) -> Any:
        # 已经是标准结构（例如代码内直接构造）时原样返回
        if isinstance(data, dict) and "parcels" in data and isinstance(data["parcels"], list):
            std_keys = {"rect_cm"}
            first = data["parcels"][0] if data["parcels"] else None
            if isinstance(first, RoundingParcelIn):
                return data
            if isinstance(first, dict) and (
                std_keys & set(first.keys())
                or "rect_cm" in first
            ):
                # 仍跑一遍宽松归一也无妨，能识别更多别名
                try:
                    return coerce_rounding_payload(data)
                except ValueError:
                    return data
        try:
            return coerce_rounding_payload(data)
        except ValueError:
            # 交给 pydantic 产出标准 422
            return data

    @field_validator("order_no")
    @classmethod
    def _strip_order_no(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("parcels")
    @classmethod
    def _unique_parcel_numbers(
        cls, v: list[RoundingParcelIn]
    ) -> list[RoundingParcelIn]:
        nos = [p.parcel_no for p in v]
        dup = sorted({n for n in nos if nos.count(n) > 1})
        if dup:
            raise ValueError(f"同一取整单内宗地号重复: {dup}")
        return v

    def solver_parcels(self) -> dict[str, tuple[int, int, int, int]]:
        return {p.parcel_no: tuple(p.rect_cm) for p in self.parcels}  # type: ignore[misc]

    def solver_pairs(self) -> list[tuple[str, str]]:
        return [(p[0], p[1]) for p in self.preserve_pairs if len(p) == 2]
