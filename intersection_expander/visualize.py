"""matplotlib による可視化。"""

from __future__ import annotations

import math

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

from .models import Network


NODE_STYLE = {
    "ordinary":   dict(marker="o", color="black",   s=60, zorder=5),
    "centroid":   dict(marker="s", color="gray",    s=80, zorder=5),
    "entry":      dict(marker="^", color="#1f77b4", s=50, zorder=5),
    "left_split": dict(marker="D", color="#17becf", s=35, zorder=5),
    "left_merge": dict(marker="D", color="#ff7f0e", s=35, zorder=5),
    "exit":       dict(marker="v", color="#d62728", s=50, zorder=5),
}

MOVEMENT_COLOR = {
    "through":    "#666666",
    "left_turn":  "#1f77b4",
    "right_turn": "#d62728",
    "u_turn":     "#ff7f0e",
    None:         "#000000",
}

CHAIN_COLOR = "#7f7f7f"


def plot_network(
    net: Network,
    ax=None,
    title: str = "",
    show_node_labels: bool = True,
    arrow_size: float = 5,
):
    if ax is None:
        fig, ax = plt.subplots(figsize=(8, 8))
    else:
        fig = ax.figure

    nodes_by_id = {n.id: n for n in net.nodes}

    # リンクを矢印で描画
    for lk in net.links:
        if lk.from_node not in nodes_by_id or lk.to_node not in nodes_by_id:
            continue
        a = nodes_by_id[lk.from_node]
        b = nodes_by_id[lk.to_node]
        if lk.link_type == "internal_movement":
            color = MOVEMENT_COLOR.get(lk.movement_type, "#000")
            lw = 1.6
            alpha = 0.95
        elif lk.link_type == "internal_chain":
            color = CHAIN_COLOR
            lw = 2.2
            alpha = 0.9
        elif lk.link_type == "road":
            color = "#222222"
            lw = 1.2
            alpha = 0.7
        else:
            color = "#888888"
            lw = 1.0
            alpha = 0.5
        arrow = FancyArrowPatch(
            (a.x, a.y), (b.x, b.y),
            arrowstyle="->",
            color=color, linewidth=lw, alpha=alpha,
            mutation_scale=arrow_size, shrinkA=3, shrinkB=3, zorder=3,
        )
        ax.add_patch(arrow)

    # ノードを描画
    by_type: dict[str, list] = {}
    for n in net.nodes:
        by_type.setdefault(n.node_type, []).append(n)
    for nt, nlist in by_type.items():
        style = NODE_STYLE.get(nt, dict(marker="o", color="black", s=40))
        ax.scatter([n.x for n in nlist], [n.y for n in nlist], label=nt, **style)
        if show_node_labels:
            for n in nlist:
                ax.annotate(
                    n.id, (n.x, n.y),
                    fontsize=6, xytext=(4, 4),
                    textcoords="offset points", color="#444",
                )

    ax.set_aspect("equal", adjustable="box")
    ax.set_title(title)
    ax.grid(True, linestyle=":", alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    return fig, ax


def plot_before_after(
    net_before: Network,
    net_after: Network,
    title: str,
    save_path: str,
    zoom_centers: list[tuple[float, float]] | None = None,
    zoom_radius: float = 90.0,
):
    """3 パネル: BEFORE / AFTER 全体 / AFTER 拡大（zoom_centers 周り）"""
    n_zoom = len(zoom_centers) if zoom_centers else 1
    n_cols = 2 + n_zoom
    fig, axes = plt.subplots(1, n_cols, figsize=(7 * n_cols, 7))
    if n_cols == 1:
        axes = [axes]

    plot_network(net_before, ax=axes[0], title=f"{title}\nBEFORE", show_node_labels=True)
    plot_network(net_after, ax=axes[1], title="AFTER (full)", show_node_labels=False)

    # 拡大ビュー
    if zoom_centers is None:
        # AFTER ネットワークの entry/exit ノードの中心を計算
        ents = [n for n in net_after.nodes if n.node_type in ("entry", "exit")]
        if ents:
            cx = sum(n.x for n in ents) / len(ents)
            cy = sum(n.y for n in ents) / len(ents)
            zoom_centers = [(cx, cy)]
        else:
            zoom_centers = []

    for i, (zx, zy) in enumerate(zoom_centers):
        ax = axes[2 + i]
        plot_network(net_after, ax=ax, title=f"AFTER (zoom @ {zx:.0f},{zy:.0f})", show_node_labels=True, arrow_size=7)
        ax.set_xlim(zx - zoom_radius, zx + zoom_radius)
        ax.set_ylim(zy - zoom_radius, zy + zoom_radius)

    fig.tight_layout()
    fig.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return save_path
