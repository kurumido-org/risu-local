"""不変条件チェッカー。

DESIGN.md §7 / examples/01_4way_worked.md Step 10 に列挙された不変条件を
検査する。違反があった場合は ValidationError を投げる。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .models import Network


class ValidationError(AssertionError):
    """不変条件違反。"""


@dataclass
class ValidationReport:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_failed(self) -> None:
        if self.errors:
            msg = "Validation failed:\n  " + "\n  ".join(self.errors)
            if self.warnings:
                msg += "\nWarnings:\n  " + "\n  ".join(self.warnings)
            raise ValidationError(msg)


def validate(net: Network, allow_u_turn: bool = False) -> ValidationReport:
    """ネットワークの不変条件を検査する。"""
    errors: list[str] = []
    warnings: list[str] = []

    nodes_by_id = {n.id: n for n in net.nodes}

    # 重複ノード ID
    if len({n.id for n in net.nodes}) != len(net.nodes):
        errors.append("ノード ID に重複がある")

    # 重複リンク ID
    if len({l.id for l in net.links}) != len(net.links):
        errors.append("リンク ID に重複がある")

    # リンクが参照するノード ID の存在確認
    for lk in net.links:
        if lk.from_node not in nodes_by_id:
            errors.append(f"リンク {lk.id} の from_node={lk.from_node} が存在しない")
        if lk.to_node not in nodes_by_id:
            errors.append(f"リンク {lk.id} の to_node={lk.to_node} が存在しない")

    # 度数集計
    in_deg: dict[str, int] = defaultdict(int)
    out_deg: dict[str, int] = defaultdict(int)
    in_links_by_node: dict[str, list] = defaultdict(list)
    out_links_by_node: dict[str, list] = defaultdict(list)
    for lk in net.links:
        out_deg[lk.from_node] += 1
        in_deg[lk.to_node] += 1
        out_links_by_node[lk.from_node].append(lk)
        in_links_by_node[lk.to_node].append(lk)

    # ── 各 node_type ごとの不変条件 ──
    for n in net.nodes:
        nt = n.node_type

        if nt == "entry":
            # I1: in-degree に external road リンクが 1 本（あるいは connector）
            road_in = [l for l in in_links_by_node[n.id] if l.link_type in ("road", "connector")]
            if len(road_in) != 1:
                errors.append(
                    f"entry {n.id}: external link in-degree = {len(road_in)} (expected 1)"
                )
            # I3: chain_in リンク 1 本が出ていること
            chain_out = [l for l in out_links_by_node[n.id] if l.link_type == "internal_chain"]
            if len(chain_out) != 1:
                errors.append(
                    f"entry {n.id}: internal_chain out-degree = {len(chain_out)} (expected 1)"
                )

        elif nt == "left_split":
            # I5: chain_in 1 本が in
            chain_in = [l for l in in_links_by_node[n.id] if l.link_type == "internal_chain"]
            if len(chain_in) != 1:
                errors.append(
                    f"left_split {n.id}: internal_chain in-degree = {len(chain_in)} (expected 1)"
                )
            # I6: through/left_turn が出る
            mov_out = [
                l for l in out_links_by_node[n.id]
                if l.link_type == "internal_movement" and l.movement_type in ("through", "left_turn")
            ]
            if len(mov_out) < 1:
                warnings.append(
                    f"left_split {n.id}: through/left_turn out-degree = 0"
                )

        elif nt == "left_merge":
            # I7: chain_out 1 本
            chain_out = [l for l in out_links_by_node[n.id] if l.link_type == "internal_chain"]
            if len(chain_out) != 1:
                errors.append(
                    f"left_merge {n.id}: internal_chain out-degree = {len(chain_out)} (expected 1)"
                )
            # I8: through/left_turn が入る
            mov_in = [
                l for l in in_links_by_node[n.id]
                if l.link_type == "internal_movement" and l.movement_type in ("through", "left_turn")
            ]
            if len(mov_in) < 1:
                warnings.append(
                    f"left_merge {n.id}: through/left_turn in-degree = 0"
                )

        elif nt == "exit":
            # I2: external road リンク 1 本が out
            road_out = [l for l in out_links_by_node[n.id] if l.link_type in ("road", "connector")]
            if len(road_out) != 1:
                errors.append(
                    f"exit {n.id}: external link out-degree = {len(road_out)} (expected 1)"
                )
            # I4: chain_out 1 本が in
            chain_in = [l for l in in_links_by_node[n.id] if l.link_type == "internal_chain"]
            if len(chain_in) != 1:
                errors.append(
                    f"exit {n.id}: internal_chain in-degree = {len(chain_in)} (expected 1)"
                )

        elif nt == "centroid":
            pass  # centroid 自体の制約はチェックしない（demand の方でチェック）

    # ── internal_movement リンクの一貫性 ──
    for lk in net.links:
        if lk.link_type != "internal_movement":
            continue
        # I12: movement_type と parent_intersection_id を持つ
        if lk.movement_type is None:
            errors.append(f"internal_movement {lk.id}: movement_type が None")
        if lk.parent_intersection_id is None:
            errors.append(f"internal_movement {lk.id}: parent_intersection_id が None")
        # I9: u_turn 禁止
        if lk.movement_type == "u_turn" and not allow_u_turn:
            errors.append(f"internal_movement {lk.id}: u_turn だが allow_u_turn=False")
        # right_turn は entry → exit
        if lk.movement_type == "right_turn":
            f = nodes_by_id.get(lk.from_node)
            t = nodes_by_id.get(lk.to_node)
            if f is None or f.node_type != "entry":
                errors.append(f"right_turn {lk.id}: from_node が entry でない ({f.node_type if f else 'None'})")
            if t is None or t.node_type != "exit":
                errors.append(f"right_turn {lk.id}: to_node が exit でない ({t.node_type if t else 'None'})")
        # through, left_turn は left_split → left_merge
        if lk.movement_type in ("through", "left_turn"):
            f = nodes_by_id.get(lk.from_node)
            t = nodes_by_id.get(lk.to_node)
            if f is None or f.node_type != "left_split":
                errors.append(
                    f"{lk.movement_type} {lk.id}: from_node が left_split でない "
                    f"({f.node_type if f else 'None'})"
                )
            if t is None or t.node_type != "left_merge":
                errors.append(
                    f"{lk.movement_type} {lk.id}: to_node が left_merge でない "
                    f"({t.node_type if t else 'None'})"
                )

    # ── internal_chain リンクの一貫性 ──
    for lk in net.links:
        if lk.link_type != "internal_chain":
            continue
        # I13: parent_intersection_id を持つ
        if lk.parent_intersection_id is None:
            errors.append(f"internal_chain {lk.id}: parent_intersection_id が None")
        f = nodes_by_id.get(lk.from_node)
        t = nodes_by_id.get(lk.to_node)
        valid_pairs = {("entry", "left_split"), ("left_merge", "exit")}
        if f is None or t is None:
            continue
        if (f.node_type, t.node_type) not in valid_pairs:
            errors.append(
                f"internal_chain {lk.id}: 不正な接続 {f.node_type} → {t.node_type}"
            )

    # ── demand の検証 ──
    for d in net.demands:
        f = nodes_by_id.get(d.orig)
        t = nodes_by_id.get(d.dest)
        if f is None:
            errors.append(f"demand orig={d.orig} が存在しない")
            continue
        if t is None:
            errors.append(f"demand dest={d.dest} が存在しない")
            continue
        # I10: orig/dest は centroid または ordinary（leaf 想定）であるべき。
        # entry/exit/left_split/left_merge には出現してはならない
        forbidden = {"entry", "exit", "left_split", "left_merge"}
        if f.node_type in forbidden:
            errors.append(
                f"demand orig={d.orig} は {f.node_type} ノード（centroid を使うべき）"
            )
        if t.node_type in forbidden:
            errors.append(
                f"demand dest={d.dest} は {t.node_type} ノード（centroid を使うべき）"
            )

    return ValidationReport(errors=errors, warnings=warnings)
