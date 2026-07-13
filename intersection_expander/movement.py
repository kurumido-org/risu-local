"""movement (左折/直進/右折/U-turn) 分類。"""

from __future__ import annotations

import math

from .geometry import normalize_angle
from .models import MovementType


def classify_movement(theta_in: float, theta_out: float) -> MovementType:
    """進入アーム角と退出アーム角から movement 種別を分類する。

    theta_in:  交差点中心から進入元の隣接ノードへの方位角
    theta_out: 交差点中心から退出先の隣接ノードへの方位角

    旋回角は通行方向に依存しない（左折は常に CCW = 正）。
    """
    turn = normalize_angle(theta_out - theta_in - math.pi)
    if abs(turn) > 3 * math.pi / 4:
        return "u_turn"
    if abs(turn) < math.pi / 4:
        return "through"
    return "left_turn" if turn > 0 else "right_turn"
