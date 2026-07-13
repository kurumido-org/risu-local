"""端点座標計算と角度ユーティリティ。"""

from __future__ import annotations

import math

from .models import TrafficSide


def compute_endpoint_coords(
    cx: float,
    cy: float,
    theta: float,
    R: float,
    D: float,
    traffic_side: TrafficSide = "left",
) -> tuple[tuple[float, float], tuple[float, float]]:
    """中心 (cx, cy) と方位角 theta から、進入端点 IN と退出端点 OUT の座標を返す。

    座標系: y軸上向き、x軸右向き（数学標準）。
    左側通行: 進行方向の左 = 90° CCW 回転。
    右側通行: 進行方向の右 = 90° CW 回転。
    """
    ux, uy = math.cos(theta), math.sin(theta)
    if traffic_side == "left":
        # 進入車: 進行方向 = -u, 左 = 90° CCW(-u) = (sin θ, -cos θ)
        in_off = (math.sin(theta), -math.cos(theta))
        # 退出車: 進行方向 = +u, 左 = 90° CCW(+u) = (-sin θ, cos θ)
        out_off = (-math.sin(theta), math.cos(theta))
    else:  # "right"
        in_off = (-math.sin(theta), math.cos(theta))
        out_off = (math.sin(theta), -math.cos(theta))

    in_xy = (cx + R * ux + D * in_off[0], cy + R * uy + D * in_off[1])
    out_xy = (cx + R * ux + D * out_off[0], cy + R * uy + D * out_off[1])
    return in_xy, out_xy


def normalize_angle(a: float) -> float:
    """角度を (-π, π] に正規化。"""
    while a > math.pi:
        a -= 2 * math.pi
    while a <= -math.pi:
        a += 2 * math.pi
    return a
