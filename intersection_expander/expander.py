"""コア変換アルゴリズム。

各交差点アームを 4 ノードのチェーンに展開する:

  進入側: [外部道路] → entry → left_split → (through/left_turn 内部リンク) → ...
                       ↓
                       right_turn 内部リンク

  退出側: ... (through/left_turn 内部リンク) → left_merge → exit → [外部道路]
                                                            ↑
                                                            right_turn 内部リンク

つまり進入時にはまず right-turn が分岐し（entry ノード），その後 left-turn が分岐する
（left_split ノード）。退出時には left-turn が先に合流し（left_merge），その後
right-turn が合流して交差点を抜ける（exit）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

from .geometry import compute_endpoint_coords
from .models import Demand, Link, Network, Node, TrafficSide
from .movement import classify_movement


@dataclass
class ExpandConfig:
    traffic_side: TrafficSide = "left"
    R_outer: float = 45.0    # entry/exit の中心からの距離（外側）。auto_scale=True の場合は上限値
    R_inner: float = 27.0    # left_split/left_merge の中心からの距離（内側）。auto_scale 時は R_outer と同比率でスケーリング
    D: float = 8.0           # 進入と退出を左右に分離する距離。auto_scale 時は R_outer と同比率でスケーリング
    allow_u_turn: bool = False
    auto_mirror: bool = True
    auto_scale: bool = True   # True: R_outer/R_inner/D をネットワーク規模に自動調整する
    auto_scale_fraction: float = 0.35  # 交差点接続リンクの最短長に対する R_outer の最大比率
    detect_method: Literal["node_type", "auto_degree", "hybrid"] = "hybrid"
    intersection_min_degree: int = 3
    internal_ffs: float = 10.0
    min_internal_length: float = 1.0


def auto_mirror(net: Network) -> Network:
    """oneway=False のリンクで逆向きが無いものを自動生成する。"""
    existing = {(lk.from_node, lk.to_node) for lk in net.links}
    new_links = list(net.links)
    for lk in net.links:
        if lk.oneway:
            continue
        if (lk.to_node, lk.from_node) in existing:
            continue
        new_links.append(Link(
            id=lk.id + "_rev",
            from_node=lk.to_node,
            to_node=lk.from_node,
            length=lk.length,
            free_flow_speed=lk.free_flow_speed,
            jam_density=lk.jam_density,
            number_of_lanes=lk.number_of_lanes,
            oneway=False,
            link_type=lk.link_type,
        ))
        existing.add((lk.to_node, lk.from_node))
    return Network(nodes=list(net.nodes), links=new_links, demands=list(net.demands))


def _build_neighbor_sets(net: Network) -> dict[str, set[str]]:
    nb: dict[str, set[str]] = {n.id: set() for n in net.nodes}
    for lk in net.links:
        if lk.from_node == lk.to_node:
            continue
        if lk.from_node in nb and lk.to_node in nb:
            nb[lk.from_node].add(lk.to_node)
            nb[lk.to_node].add(lk.from_node)
    return nb


def _detect_intersections(net: Network, cfg: ExpandConfig) -> set[str]:
    nb = _build_neighbor_sets(net)
    result: set[str] = set()
    for n in net.nodes:
        if n.node_type == "centroid":
            continue
        if cfg.detect_method in ("auto_degree", "hybrid"):
            if len(nb[n.id]) >= cfg.intersection_min_degree:
                result.add(n.id)
    return result


def _compute_chain_coords(
    cx: float,
    cy: float,
    theta: float,
    R_outer: float,
    R_inner: float,
    D: float,
    traffic_side: TrafficSide,
) -> dict[str, tuple[float, float]]:
    """1 アームについて 4 つのチェーンノードの座標を計算する。

    返り値: dict with keys "entry", "left_split", "left_merge", "exit"
    """
    ux, uy = math.cos(theta), math.sin(theta)
    if traffic_side == "left":
        in_off = (math.sin(theta), -math.cos(theta))
        out_off = (-math.sin(theta), math.cos(theta))
    else:
        in_off = (-math.sin(theta), math.cos(theta))
        out_off = (math.sin(theta), -math.cos(theta))

    return {
        "entry":      (cx + R_outer * ux + D * in_off[0],  cy + R_outer * uy + D * in_off[1]),
        "left_split": (cx + R_inner * ux + D * in_off[0],  cy + R_inner * uy + D * in_off[1]),
        "left_merge": (cx + R_inner * ux + D * out_off[0], cy + R_inner * uy + D * out_off[1]),
        "exit":       (cx + R_outer * ux + D * out_off[0], cy + R_outer * uy + D * out_off[1]),
    }


def _compute_auto_scale(
    net: Network,
    intersections: set[str],
    cfg: ExpandConfig,
) -> tuple[float, float, float]:
    """ネットワーク規模に基づいて R_outer / R_inner / D を自動計算する。

    交差点に接続するリンクのうち最短のものを基準にし、両端が交差点の場合でも
    端点が重ならないようにスケーリングする。

    返り値: (R_outer, R_inner, D)
    """
    # 交差点に接続するリンクの最短長を求める
    min_link_len = float("inf")
    for lk in net.links:
        if lk.from_node in intersections or lk.to_node in intersections:
            min_link_len = min(min_link_len, lk.length)

    if min_link_len == float("inf") or min_link_len <= 0:
        return cfg.R_outer, cfg.R_inner, cfg.D

    # R_outer の上限 = 最短リンク長 × fraction
    # 両端が交差点の場合 2*R_outer < link_length が必要なので
    # fraction=0.35 なら 2*0.35=0.70 < 1.0 で安全
    max_R_outer = min_link_len * cfg.auto_scale_fraction
    R_outer = min(cfg.R_outer, max_R_outer)

    # R_inner と D はデフォルト値との比率を維持
    ratio_inner = cfg.R_inner / cfg.R_outer   # デフォルト: 27/45 = 0.6
    ratio_D = cfg.D / cfg.R_outer             # デフォルト: 8/45 ≈ 0.178
    R_inner = R_outer * ratio_inner
    D = R_outer * ratio_D

    # 最低限の可視性を確保（R_outer < 1m だと小さすぎる）
    R_outer = max(R_outer, 1.0)
    R_inner = max(R_inner, 0.5)
    D = max(D, 0.2)

    return R_outer, R_inner, D


def expand(net: Network, cfg: ExpandConfig | None = None) -> Network:
    """ネットワークを展開する。"""
    if cfg is None:
        cfg = ExpandConfig()

    if cfg.auto_mirror:
        net = auto_mirror(net)

    intersections = _detect_intersections(net, cfg)
    if not intersections:
        return net

    # demand バリデーション
    for d in net.demands:
        if d.orig in intersections:
            raise ValueError(
                f"demand orig={d.orig} は交差点ノードです。centroid を使ってください。"
            )
        if d.dest in intersections:
            raise ValueError(
                f"demand dest={d.dest} は交差点ノードです。centroid を使ってください。"
            )

    # 自動スケーリング: ネットワーク規模に合わせて R_outer / R_inner / D を調整
    if cfg.auto_scale:
        R_outer, R_inner, D = _compute_auto_scale(net, intersections, cfg)
    else:
        R_outer, R_inner, D = cfg.R_outer, cfg.R_inner, cfg.D

    nb = _build_neighbor_sets(net)
    nodes_by_id = {n.id: n for n in net.nodes}

    new_nodes: list[Node] = [n for n in net.nodes if n.id not in intersections]
    new_links: list[Link] = []

    # 各交差点・各アームのチェーンノード ID をブックキープ
    entry_id_of:      dict[tuple[str, str], str] = {}
    left_split_id_of: dict[tuple[str, str], str] = {}
    left_merge_id_of: dict[tuple[str, str], str] = {}
    exit_id_of:       dict[tuple[str, str], str] = {}
    pos_of: dict[str, tuple[float, float]] = {}

    for inter_id in intersections:
        inter = nodes_by_id[inter_id]
        cx, cy = inter.x, inter.y

        arm_thetas: dict[str, float] = {}
        for nb_id in nb[inter_id]:
            nb_node = nodes_by_id[nb_id]
            arm_thetas[nb_id] = math.atan2(nb_node.y - cy, nb_node.x - cx)

        # 4 ノードチェーンを各アームに生成
        for nb_id, theta in arm_thetas.items():
            coords = _compute_chain_coords(
                cx, cy, theta, R_outer, R_inner, D, cfg.traffic_side
            )
            ids = {
                "entry":      f"{inter_id}__entry_{nb_id}",
                "left_split": f"{inter_id}__lsplit_{nb_id}",
                "left_merge": f"{inter_id}__lmerge_{nb_id}",
                "exit":       f"{inter_id}__exit_{nb_id}",
            }
            for role, nid in ids.items():
                x, y = coords[role]
                new_nodes.append(Node(
                    id=nid, x=x, y=y,
                    node_type=role,  # type: ignore[arg-type]
                    parent_intersection_id=inter_id,
                ))
                pos_of[nid] = (x, y)

            entry_id_of[(inter_id, nb_id)]      = ids["entry"]
            left_split_id_of[(inter_id, nb_id)] = ids["left_split"]
            left_merge_id_of[(inter_id, nb_id)] = ids["left_merge"]
            exit_id_of[(inter_id, nb_id)]       = ids["exit"]

            # チェーンリンク 1: entry → left_split （進入チェーン内側方向）
            ex, ey = coords["entry"]
            sx, sy = coords["left_split"]
            new_links.append(Link(
                id=f"{inter_id}__chain_in_{nb_id}",
                from_node=ids["entry"],
                to_node=ids["left_split"],
                length=max(math.hypot(ex - sx, ey - sy), cfg.min_internal_length),
                free_flow_speed=cfg.internal_ffs,
                link_type="internal_chain",
                parent_intersection_id=inter_id,
            ))
            # チェーンリンク 2: left_merge → exit （退出チェーン外側方向）
            mx, my = coords["left_merge"]
            xx, xy = coords["exit"]
            new_links.append(Link(
                id=f"{inter_id}__chain_out_{nb_id}",
                from_node=ids["left_merge"],
                to_node=ids["exit"],
                length=max(math.hypot(mx - xx, my - xy), cfg.min_internal_length),
                free_flow_speed=cfg.internal_ffs,
                link_type="internal_chain",
                parent_intersection_id=inter_id,
            ))

        # 内部 movement リンク
        for nb_a, theta_a in arm_thetas.items():
            for nb_b, theta_b in arm_thetas.items():
                if nb_a == nb_b:
                    continue
                m_type = classify_movement(theta_a, theta_b)
                if m_type == "u_turn" and not cfg.allow_u_turn:
                    continue

                if m_type == "right_turn":
                    src_id = entry_id_of[(inter_id, nb_a)]
                    dst_id = exit_id_of[(inter_id, nb_b)]
                else:
                    # through, left_turn, (u_turn if enabled)
                    src_id = left_split_id_of[(inter_id, nb_a)]
                    dst_id = left_merge_id_of[(inter_id, nb_b)]

                sx, sy = pos_of[src_id]
                dx, dy = pos_of[dst_id]
                length = max(math.hypot(dx - sx, dy - sy), cfg.min_internal_length)
                new_links.append(Link(
                    id=f"{inter_id}__mov_{nb_a}_to_{nb_b}_{m_type}",
                    from_node=src_id,
                    to_node=dst_id,
                    length=length,
                    free_flow_speed=cfg.internal_ffs,
                    link_type="internal_movement",
                    movement_type=m_type,
                    parent_intersection_id=inter_id,
                ))

    # 外部リンクの張り替え
    for lk in net.links:
        s_int = lk.from_node in intersections
        e_int = lk.to_node in intersections
        if not s_int and not e_int:
            new_links.append(lk)
            continue
        new_from = lk.from_node
        new_to = lk.to_node
        if s_int:
            new_from = exit_id_of.get((lk.from_node, lk.to_node), lk.from_node)
        if e_int:
            new_to = entry_id_of.get((lk.to_node, lk.from_node), lk.to_node)
        new_len = lk.length
        if s_int:
            new_len -= R_outer
        if e_int:
            new_len -= R_outer
        if new_len < 5.0:
            new_len = max(lk.length * 0.1, 5.0)
        new_links.append(replace(lk, from_node=new_from, to_node=new_to, length=new_len))

    return Network(nodes=new_nodes, links=new_links, demands=list(net.demands))
