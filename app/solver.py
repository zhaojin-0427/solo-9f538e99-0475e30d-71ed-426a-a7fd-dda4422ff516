"""求解器命名别名模块。

核心实现位于 :mod:`app.rounding`。部分调用方 / 验收脚本可能按
``app.solver`` 导入，这里原样再导出常用符号，并提供几个直观函数名。
"""
from __future__ import annotations

from .rounding import (  # noqa: F401
    CM_PER_METRE,
    Candidate,
    ceil_metres,
    edge_choices,
    floor_metres,
    geometry_positive_overlap,
    geometry_shared_segments,
    plan_rounding,
    rectangle_candidates,
    rects_positive_overlap,
    rects_shared_edge_length,
    round_rectangles,
    solve_rounding,
    solve_rounding_cm,
)


def solve(*args, **kwargs):  # noqa: D401
    """``app.solver.solve`` 简写，等价于 :func:`app.rounding.solve_rounding`。"""
    return solve_rounding(*args, **kwargs)


def find_optimal_rectangles(*args, **kwargs):
    """命名别名：求位移和最小的一组整米矩形。"""
    return solve_rounding(*args, **kwargs)
