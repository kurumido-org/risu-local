"""データモデル定義。

入力ネットワーク・出力ネットワークともに本ファイルの dataclass を使用する。
仕様書 (intersection_expansion_spec.md) §9 のデータ構造要件に準拠。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── 型定義 ──
NodeType = Literal[
    "ordinary",     # 普通の道路ノード（接続点・交差点候補）
    "centroid",     # virtual O/D ノード（demand の orig/dest 専用、入力時のみ使用）
    "entry",        # 展開で生成された進入端点（兼 right-turn 分岐）
    "left_split",   # 進入チェーンの内側ノード（left-turn 分岐 / through 起点）
    "left_merge",   # 退出チェーンの内側ノード（left-turn / through 合流点）
    "exit",         # 展開で生成された退出端点（兼 right-turn 合流）
]

LinkType = Literal[
    "road",                # 通常の道路リンク
    "internal_movement",   # 交差点内部の movement リンク
    "internal_chain",      # 進入/退出チェーンの直列リンク（entry→left_split, left_merge→exit）
    "connector",           # centroid と道路ネットワークを繋ぐリンク
]

MovementType = Literal["through", "left_turn", "right_turn", "u_turn"]

TrafficSide = Literal["left", "right"]


# ── データ ──
@dataclass
class Node:
    id: str
    x: float
    y: float
    node_type: NodeType = "ordinary"
    parent_intersection_id: str | None = None  # entry/exit のとき元交差点 ID を保持


@dataclass
class Link:
    id: str
    from_node: str
    to_node: str
    length: float
    free_flow_speed: float = 20.0
    jam_density: float = 0.2
    number_of_lanes: int = 1
    oneway: bool = False
    link_type: LinkType = "road"
    parent_intersection_id: str | None = None     # internal_movement のとき必須
    movement_type: MovementType | None = None     # internal_movement のとき必須
    in_link_id: str | None = None                 # internal_movement のとき: 進入元の外部リンク ID
    out_link_id: str | None = None                # internal_movement のとき: 退出先の外部リンク ID


@dataclass
class Demand:
    orig: str
    dest: str
    t_start: float
    t_end: float
    flow: float


@dataclass
class Network:
    nodes: list[Node] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    demands: list[Demand] = field(default_factory=list)

    # ── 便利アクセサ ──
    def node_by_id(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def link_by_id(self, link_id: str) -> Link:
        for lk in self.links:
            if lk.id == link_id:
                return lk
        raise KeyError(link_id)
