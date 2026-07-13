"""GMNS (General Modeling Network Specification) CSV 入出力。

参考: https://github.com/zephyr-data-specs/GMNS

ファイル構成:
    <dir>/
        node.csv
        link.csv
        demand.csv   (optional)

カラム定義は GMNS 標準を踏襲しつつ、本パッケージ固有の属性を保持するための拡張
カラムを追加する。GMNS 標準と互換性を保つため、未知のカラムは無視する。

node.csv:
    node_id, x_coord, y_coord, node_type,
    parent_intersection_id  (extension)

link.csv:
    link_id, from_node_id, to_node_id, length, lanes, free_speed,
    link_type, oneway,
    movement_type           (extension, internal_movement のみ)
    parent_intersection_id  (extension, internal_movement / internal_chain のみ)

demand.csv:
    orig_node_id, dest_node_id, t_start, t_end, flow
"""

from __future__ import annotations

import csv
import os

from .models import Demand, Link, Network, Node


# ──────────────────────────────────────────────
# 読み込み
# ──────────────────────────────────────────────
def _read_csv(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _to_float(v: str | None, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    return float(v)


def _to_int(v: str | None, default: int = 0) -> int:
    if v is None or v == "":
        return default
    return int(float(v))


def _to_bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "t")


def read_gmns(directory: str) -> Network:
    """指定ディレクトリから node.csv / link.csv / demand.csv を読み込む。"""
    node_rows = _read_csv(os.path.join(directory, "node.csv"))
    link_rows = _read_csv(os.path.join(directory, "link.csv"))
    demand_rows = _read_csv(os.path.join(directory, "demand.csv"))

    nodes: list[Node] = []
    for r in node_rows:
        nt = (r.get("node_type") or "ordinary").strip() or "ordinary"
        nodes.append(Node(
            id=r["node_id"],
            x=_to_float(r.get("x_coord")),
            y=_to_float(r.get("y_coord")),
            node_type=nt,  # type: ignore[arg-type]
            parent_intersection_id=(r.get("parent_intersection_id") or None) or None,
        ))

    links: list[Link] = []
    for r in link_rows:
        lt = (r.get("link_type") or "road").strip() or "road"
        mv = r.get("movement_type") or None
        if mv == "":
            mv = None
        links.append(Link(
            id=r["link_id"],
            from_node=r["from_node_id"],
            to_node=r["to_node_id"],
            length=_to_float(r.get("length")),
            free_flow_speed=_to_float(r.get("free_speed"), default=20.0),
            jam_density=_to_float(r.get("jam_density"), default=0.2),
            number_of_lanes=_to_int(r.get("lanes"), default=1),
            oneway=_to_bool(r.get("oneway")),
            link_type=lt,  # type: ignore[arg-type]
            parent_intersection_id=(r.get("parent_intersection_id") or None) or None,
            movement_type=mv,  # type: ignore[arg-type]
        ))

    demands: list[Demand] = []
    for r in demand_rows:
        demands.append(Demand(
            orig=r["orig_node_id"],
            dest=r["dest_node_id"],
            t_start=_to_float(r.get("t_start")),
            t_end=_to_float(r.get("t_end")),
            flow=_to_float(r.get("flow")),
        ))

    return Network(nodes=nodes, links=links, demands=demands)


# ──────────────────────────────────────────────
# 書き込み
# ──────────────────────────────────────────────
NODE_COLS = ["node_id", "x_coord", "y_coord", "node_type", "parent_intersection_id"]
LINK_COLS = [
    "link_id", "from_node_id", "to_node_id", "length", "lanes", "free_speed",
    "jam_density", "link_type", "oneway", "movement_type", "parent_intersection_id",
]
DEMAND_COLS = ["orig_node_id", "dest_node_id", "t_start", "t_end", "flow"]


def _write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in columns})


def write_gmns(net: Network, directory: str) -> None:
    """指定ディレクトリへ node.csv / link.csv / demand.csv を書き出す。"""
    os.makedirs(directory, exist_ok=True)

    node_rows = [
        {
            "node_id":                n.id,
            "x_coord":                n.x,
            "y_coord":                n.y,
            "node_type":              n.node_type,
            "parent_intersection_id": n.parent_intersection_id or "",
        }
        for n in net.nodes
    ]
    _write_csv(os.path.join(directory, "node.csv"), NODE_COLS, node_rows)

    link_rows = [
        {
            "link_id":                lk.id,
            "from_node_id":           lk.from_node,
            "to_node_id":             lk.to_node,
            "length":                 lk.length,
            "lanes":                  lk.number_of_lanes,
            "free_speed":             lk.free_flow_speed,
            "jam_density":            lk.jam_density,
            "link_type":              lk.link_type,
            "oneway":                 "true" if lk.oneway else "false",
            "movement_type":          lk.movement_type or "",
            "parent_intersection_id": lk.parent_intersection_id or "",
        }
        for lk in net.links
    ]
    _write_csv(os.path.join(directory, "link.csv"), LINK_COLS, link_rows)

    demand_rows = [
        {
            "orig_node_id": d.orig,
            "dest_node_id": d.dest,
            "t_start":      d.t_start,
            "t_end":        d.t_end,
            "flow":         d.flow,
        }
        for d in net.demands
    ]
    _write_csv(os.path.join(directory, "demand.csv"), DEMAND_COLS, demand_rows)
