"""
UXsim API + MCP Server
  - POST /simulate      : シミュレーション実行
  - GET  /results/{id}  : 結果取得
  - POST /chat          : LLM (Ollama) との対話 ＋ ツール呼び出し
  - GET  /mcp           : MCP エンドポイント (SSE)
"""

import asyncio
import csv
import io
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from pydantic import BaseModel
from starlette.requests import Request
from starlette.routing import Route

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
import os
from dotenv import load_dotenv
load_dotenv()

# LLM バックエンド: "mock" / "claude" / "ollama"
LLM_BACKEND = os.environ.get("LLM_BACKEND", "claude")

# Ollama 設定
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL    = "qwen2.5:3b"

# Claude 設定
CLAUDE_MODEL = "claude-sonnet-4-20250514"  # 高精度・ツール呼び出し安定
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ──────────────────────────────────────────────
# 再現性のためのバージョン情報 / DL JSON スキーマ
# ──────────────────────────────────────────────
RISU_SCHEMA_VERSION = "1.0"
RISU_VERSION = "0.1"
try:
    import uxsim as _uxsim_module
    UXSIM_VERSION = getattr(_uxsim_module, "__version__", "unknown")
except Exception:
    UXSIM_VERSION = "unknown"

# ──────────────────────────────────────────────
# グローバル状態（本番はRedis等に置き換える）
# ──────────────────────────────────────────────
results_store: dict[str, Any] = {}
executor = ThreadPoolExecutor(max_workers=4)

# UXsim 実行タイムアウト（秒）。環境変数で上書き可能。
UXSIM_TIMEOUT_SEC = int(os.getenv("RISU_UXSIM_TIMEOUT", "120"))


async def _run_uxsim_async(scenario) -> dict:
    """
    UXsim をタイムアウト付きで非同期実行するラッパー。
    エラーを HTTPException に分類してユーザー向けメッセージを返す。
    """
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, _run_uxsim, scenario),
            timeout=UXSIM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            408,
            detail=f"シミュレーションがタイムアウトしました（{UXSIM_TIMEOUT_SEC}s 超過）。ネットワーク規模や tmax を縮小してください。",
        )
    except HTTPException:
        raise
    except MemoryError:
        raise HTTPException(
            413,
            detail="メモリ不足でシミュレーションが中断されました。ネットワーク規模を縮小してください。",
        )
    except KeyError as e:
        # シナリオ内で存在しないノード/リンクを参照
        raise HTTPException(
            400,
            detail=f"シナリオ定義エラー: 参照先「{e.args[0] if e.args else '?'}」が見つかりません。",
        )
    except ValueError as e:
        # UXsim の不正入力 or _validate_scenario_size
        raise HTTPException(400, detail=f"シナリオが不正です: {e}")
    except Exception as e:
        # その他の UXsim 内部エラー
        print(f"[RISU uxsim error] {e.__class__.__name__}: {e}")
        print(_tb.format_exc())
        raise HTTPException(
            500,
            detail=f"シミュレーション計算エラー: {e.__class__.__name__}",
        )


def _last_user_message_text(body) -> str | None:
    """ChatRequest body から最後のユーザーメッセージを抽出（source 記録用）。"""
    try:
        for m in reversed(body.messages):
            if getattr(m, "role", None) == "user":
                c = getattr(m, "content", None)
                if isinstance(c, str):
                    return c[:2000]  # 念のため上限
                return str(c)[:2000] if c else None
    except Exception:
        return None
    return None


def _store_sim(sim_id: str, result: dict, source: dict | None = None) -> None:
    """シミュレーション結果を results_store に保存し、再現性メタを付与する。

    result は _run_uxsim の戻り値（"_scenario" を含む）。
    source は呼び出し経路の由来を表す任意の辞書（省略時は manual）。
    """
    src = source or {"type": "manual"}
    result["_meta"] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": src,
    }
    results_store[sim_id] = result


def _build_envelope(sim_id: str, *, include_result: bool = True) -> dict:
    """results_store の内部表現を DL/取得用の正規エンベロープに変換する。

    include_result=False の場合は再現に必要な scenario と meta のみを返す
    （Scenario DL ボタン用）。
    """
    raw = results_store[sim_id]
    meta = raw.get("_meta", {})
    envelope = {
        "risu_schema_version": RISU_SCHEMA_VERSION,
        "sim_id": sim_id,
        "created_at": meta.get("created_at"),
        "uxsim_version": UXSIM_VERSION,
        "risu_version": RISU_VERSION,
        "scenario": raw.get("_scenario", {}),
        "source": meta.get("source", {"type": "unknown"}),
    }
    if include_result:
        envelope["result"] = {
            "stats":        raw.get("stats", {}),
            "geojson":      raw.get("geojson"),
            "frames":       raw.get("frames"),
            "frame_times":  raw.get("frame_times"),
            "tmax":         raw.get("tmax"),
            "buildings":    raw.get("buildings"),
            # コンパクト列指向フレーム用メタ
            "link_names":   raw.get("link_names"),
            "frame_format": raw.get("frame_format"),
            # 信号現示メタデータ
            "signals":      raw.get("signals"),
        }
    return envelope

# ──────────────────────────────────────────────
# Pydantic スキーマ
# ──────────────────────────────────────────────
class NodeInput(BaseModel):
    name: str
    x: float
    y: float
    flow_capacity: float | None = None
    node_type: str | None = None  # "ordinary" | "centroid" など。None=ordinary 扱い
    signal: list[float] | None = None  # 信号現示の青時間リスト（秒）。例: [60,60] → 2現示各60秒

class LinkInput(BaseModel):
    name: str
    start: str
    end: str
    length: float
    free_flow_speed: float = 20.0
    jam_density: float = 0.2
    number_of_lanes: int = 1
    oneway: bool = False  # True = 一方通行、False = 双方向道路（自動ミラーリングの対象）
    signal_group: int | None = None  # この進入リンクが青になる信号現示番号（0始まり）

class DemandInput(BaseModel):
    orig: str
    dest: str
    t_start: float
    t_end: float
    flow: float

class SimulationInput(BaseModel):
    name: str = "sim"
    tmax: int = 3600
    deltan: int = 5
    nodes: list[NodeInput]
    links: list[LinkInput]
    demands: list[DemandInput]
    expand_intersections: bool = False

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatInput(BaseModel):
    messages: list[ChatMessage]

# ──────────────────────────────────────────────
# 交差点展開（intersection-expander パッケージへ委譲）
# ──────────────────────────────────────────────
# RISU_Work/intersection-expander の純粋実装に展開ロジックを委譲する。
# 設計と手計算は intersection-expander/DESIGN.md および
# intersection-expander/examples/01_4way_worked.md を参照。
from intersection_expander import (
    Demand as _IxDemand,
    ExpandConfig as _IxExpandConfig,
    Link as _IxLink,
    Network as _IxNetwork,
    Node as _IxNode,
    expand as _ix_expand,
)


def _scenario_to_ix_network(scenario: SimulationInput) -> _IxNetwork:
    """SimulationInput → intersection_expander.Network"""
    nodes = []
    for n in scenario.nodes:
        nt = (n.node_type or "ordinary").strip() or "ordinary"
        nodes.append(_IxNode(id=n.name, x=n.x, y=n.y, node_type=nt))  # type: ignore[arg-type]
    links = []
    for lk in scenario.links:
        links.append(_IxLink(
            id=lk.name,
            from_node=lk.start,
            to_node=lk.end,
            length=lk.length,
            free_flow_speed=lk.free_flow_speed,
            jam_density=lk.jam_density,
            number_of_lanes=lk.number_of_lanes,
            oneway=lk.oneway,
        ))
    demands = [
        _IxDemand(orig=d.orig, dest=d.dest, t_start=d.t_start, t_end=d.t_end, flow=d.flow)
        for d in scenario.demands
    ]
    return _IxNetwork(nodes=nodes, links=links, demands=demands)


def _ix_network_to_scenario(net: _IxNetwork, base: SimulationInput) -> SimulationInput:
    """intersection_expander.Network → SimulationInput

    base: 元の SimulationInput（name, tmax, deltan, NodeInput.flow_capacity の保持に使う）
    """
    flow_cap_by_name = {n.name: n.flow_capacity for n in base.nodes if n.flow_capacity is not None}
    signal_by_name = {n.name: n.signal for n in base.nodes if n.signal is not None}
    sig_group_by_name = {lk.name: lk.signal_group for lk in base.links if lk.signal_group is not None}

    def _resolve_signal(n) -> list | None:
        """展開されたノードに元交差点の signal を継承する。
        - n.id が元ノード名と一致: その signal
        - n は entry サブノード: parent_intersection_id の signal を継承
        （UXsim の信号制御は各 entry サブノードに設定することで、
          外部流入リンクの signal_group が正しく機能する）
        """
        sig = signal_by_name.get(n.id)
        if sig is not None:
            return sig
        # entry サブノードの場合、親交差点の信号を引き継ぐ
        if n.node_type == "entry" and n.parent_intersection_id:
            return signal_by_name.get(n.parent_intersection_id)
        return None

    new_nodes = [
        NodeInput(
            name=n.id,
            x=n.x,
            y=n.y,
            flow_capacity=flow_cap_by_name.get(n.id),
            node_type=n.node_type,
            signal=_resolve_signal(n),
        )
        for n in net.nodes
    ]
    new_links = [
        LinkInput(
            name=lk.id,
            start=lk.from_node,
            end=lk.to_node,
            length=lk.length,
            free_flow_speed=lk.free_flow_speed,
            jam_density=lk.jam_density,
            number_of_lanes=lk.number_of_lanes,
            oneway=lk.oneway,
            signal_group=sig_group_by_name.get(lk.id),
        )
        for lk in net.links
    ]
    new_demands = [
        DemandInput(orig=d.orig, dest=d.dest, t_start=d.t_start, t_end=d.t_end, flow=d.flow)
        for d in net.demands
    ]
    return SimulationInput(
        name=base.name,
        tmax=base.tmax,
        deltan=base.deltan,
        nodes=new_nodes,
        links=new_links,
        demands=new_demands,
        expand_intersections=False,
    )


def _expand_intersections(scenario: SimulationInput) -> SimulationInput:
    """SimulationInput を展開する。intersection-expander パッケージへの薄いラッパー。"""
    ix_net = _scenario_to_ix_network(scenario)
    expanded = _ix_expand(ix_net, _IxExpandConfig(traffic_side="left"))
    return _ix_network_to_scenario(expanded, base=scenario)


# ──────────────────────────────────────────────
# UXsim 実行（同期 → Executor で非同期化）
# ──────────────────────────────────────────────
def _run_uxsim(scenario: SimulationInput) -> dict:
    # リソース保護: ユーザー入力 / LLM 経由のいずれでも上限を適用
    try:
        _validate_scenario_size(scenario)
    except NameError:
        # _validate_scenario_size 定義前に呼ばれた場合（起動順序保険）は素通し
        pass
    # 信号メタデータを expand_intersections の前に元シナリオから抽出
    # （展開後はノード名が変わって signal 情報が失われるため）
    _orig_signal_nodes = [
        {"name": n.name, "x": float(n.x), "y": float(n.y),
         "signal": [float(p) for p in n.signal]}
        for n in scenario.nodes if n.signal and sum(n.signal) > 0
    ]
    _orig_signal_groups = {
        lk.name: int(lk.signal_group)
        for lk in scenario.links if lk.signal_group is not None
    }
    if scenario.expand_intersections:
        scenario = _expand_intersections(scenario)

    from uxsim import World

    _world_kwargs = dict(
        name=scenario.name,
        tmax=scenario.tmax,
        deltan=scenario.deltan,
        print_mode=0,
        save_mode=0,
        show_mode=0,
    )
    try:
        # uxsim >= 1.14 (beta) は C++ 高速化バックエンドをサポート
        W = World(**_world_kwargs, cpp=True)
    except TypeError:
        W = World(**_world_kwargs)

    node_map = {}
    for n in scenario.nodes:
        kwargs = {}
        if n.flow_capacity is not None:
            kwargs["flow_capacity"] = n.flow_capacity
        if n.signal is not None:
            kwargs["signal"] = n.signal
        node_map[n.name] = W.addNode(n.name, x=n.x, y=n.y, **kwargs)

    link_map = {}
    for lk in scenario.links:
        link_kwargs = {}
        if lk.signal_group is not None:
            link_kwargs["signal_group"] = lk.signal_group
        link_map[lk.name] = W.addLink(
            lk.name,
            start_node=node_map[lk.start],
            end_node=node_map[lk.end],
            length=lk.length,
            free_flow_speed=lk.free_flow_speed,
            jam_density=lk.jam_density,
            number_of_lanes=lk.number_of_lanes,
            **link_kwargs,
        )

    for d in scenario.demands:
        W.adddemand(
            orig=node_map[d.orig],
            dest=node_map[d.dest],
            t_start=d.t_start,
            t_end=d.t_end,
            flow=d.flow,
        )

    t0 = time.perf_counter()
    W.exec_simulation()
    elapsed = time.perf_counter() - t0

    W.analyzer.basic_analysis()

    # ---- リンク情報（GeoJSON） ----
    # 同一座標ペアのリンクを検出し、重複分にオフセットを付与して視覚的に区別
    coord_count = {}  # (sx,sy,ex,ey) -> count（同一方向）
    for lk in W.LINKS:
        key = (lk.start_node.x, lk.start_node.y, lk.end_node.x, lk.end_node.y)
        coord_count[key] = coord_count.get(key, 0) + 1

    coord_idx = {}  # 同一座標ペアの何番目か
    features = []
    for lk in W.LINKS:
        sx, sy = lk.start_node.x, lk.start_node.y
        ex, ey = lk.end_node.x, lk.end_node.y
        key = (sx, sy, ex, ey)
        idx = coord_idx.get(key, 0)
        coord_idx[key] = idx + 1
        total = coord_count[key]

        # 重複がある場合、垂直方向にオフセット
        if total > 1:
            dx, dy = ex - sx, ey - sy
            length = (dx**2 + dy**2) ** 0.5 or 1
            # 法線方向の単位ベクトル
            nx, ny = -dy / length, dx / length
            # オフセット量（リンク長の2%、中央揃え）
            offset = (idx - (total - 1) / 2) * max(length * 0.02, 5)
            sx += nx * offset
            sy += ny * offset
            ex += nx * offset
            ey += ny * offset

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [sx, sy],
                    [ex, ey],
                ],
            },
            "properties": {
                "name":            lk.name,
                "length":          lk.length,
                "free_flow_speed": lk.free_flow_speed,
                "number_of_lanes": lk.number_of_lanes,
            },
        })

    # ---- 個車軌跡データ ----
    # Vehicle.log_t/log_x/log_link/log_v/log_state からスナップショットを構築
    # 旧実装は O(T × V × L) の Python 線形探索でボトルネック化していたため、
    # 「車両ごとに 1 パス」で frames と link_timeline を同時集計するよう最適化。
    t_post0 = time.perf_counter()

    # リンク座標マップ（中心座標／法線オフセットは features 側で適用済みなので
    # ここでは元座標を使う）
    link_coord_map = {}
    link_ffs_map = {}
    link_idx_map = {}  # lk_name -> int index (for compact frame format)
    for i, lk in enumerate(W.LINKS):
        link_coord_map[lk.name] = (
            lk.start_node.x, lk.start_node.y,
            lk.end_node.x,   lk.end_node.y,
            lk.length,
        )
        link_ffs_map[lk.name] = lk.free_flow_speed
        link_idx_map[lk.name] = i

    # 車両ユニーク ID
    veh_id_map = {id(veh): i for i, veh in enumerate(W.VEHICLES.values())}

    # 1パス目: 全車両のログを舐めながら (t -> columns) と (t -> link -> speeds) を構築
    # コンパクト列指向フォーマット: 各 tk に対し {ids:[], xs:[], ys:[], vs:[], alphas:[], li:[]}
    # - ffs は出力しない（link index から client 側で解決）
    # - link は名前文字列でなく整数 index
    frames_by_tk = {}            # int_t_key(×10) -> column dict
    link_speeds_by_tk = {}       # int_t_key -> {link_name: [speeds]}

    for veh in W.VEHICLES.values():
        vid = veh_id_map[id(veh)]
        log_t     = veh.log_t
        log_x     = veh.log_x
        log_v     = veh.log_v
        log_link  = veh.log_link
        log_state = veh.log_state
        n = len(log_t)
        for i in range(n):
            # state == "run" のみ採用（旧実装と同条件）
            if str(log_state[i]) != "run":
                continue
            lk_obj = log_link[i]
            if lk_obj == -1 or not hasattr(lk_obj, "name"):
                continue
            lk_name = lk_obj.name
            lc = link_coord_map.get(lk_name)
            if lc is None:
                continue
            sx, sy, ex, ey, llen = lc

            t_val = float(log_t[i])
            tk = int(round(t_val * 10))  # 0.1 秒精度

            pos = float(log_x[i])
            alpha = min(max(pos / llen, 0.0), 1.0) if llen > 0 else 0.0
            vx = sx * (1.0 - alpha) + ex * alpha
            vy = sy * (1.0 - alpha) + ey * alpha
            spd = float(log_v[i])

            cols = frames_by_tk.get(tk)
            if cols is None:
                cols = {"ids": [], "xs": [], "ys": [], "vs": [], "alphas": [], "li": []}
                frames_by_tk[tk] = cols
                link_speeds_by_tk[tk] = {}
            cols["ids"].append(vid)
            cols["xs"].append(round(vx, 2))
            cols["ys"].append(round(vy, 2))
            cols["vs"].append(round(spd, 2))
            cols["alphas"].append(round(alpha, 4))
            cols["li"].append(link_idx_map[lk_name])
            ls = link_speeds_by_tk[tk]
            sl = ls.get(lk_name)
            if sl is None:
                ls[lk_name] = [spd]
            else:
                sl.append(spd)

    # 全 t キー（昇順）
    sorted_tks = sorted(frames_by_tk.keys())

    # 間引き: 最大 200 フレーム。len // 200 が 1 のときは間引かない。
    if len(sorted_tks) > 200:
        step = max(1, len(sorted_tks) // 200)
        sorted_tks = sorted_tks[::step]

    # 出力 frames（旧フォーマット維持: キーは "12.3" のような小数文字列）
    frames = {}
    for tk in sorted_tks:
        t_val = tk / 10.0
        # キーは旧実装互換: round(t_val,1) → str(...)。整数の場合 "12.0"
        frames[str(round(t_val, 1))] = frames_by_tk[tk]

    # link_timeline を間引き後の t セットだけ集計
    link_names = [lk.name for lk in W.LINKS]
    link_timeline = {ln: [] for ln in link_names}
    for tk in sorted_tks:
        t_val = round(tk / 10.0, 1)
        ls = link_speeds_by_tk.get(tk, {})
        for ln in link_names:
            speeds = ls.get(ln)
            if speeds:
                avg = sum(speeds) / len(speeds)
            else:
                avg = link_ffs_map[ln]
            link_timeline[ln].append({"t": t_val, "speed": round(avg, 2)})
    for f in features:
        f["properties"]["timeline"] = link_timeline[f["properties"]["name"]]

    post_elapsed = time.perf_counter() - t_post0

    # ---- 集計統計 ----
    # 座標ペアのユニーク数を確認（重複リンク検出）
    coord_pairs = set()
    for f in features:
        c = f["geometry"]["coordinates"]
        coord_pairs.add((c[0][0], c[0][1], c[1][0], c[1][1]))
    print(f"[RISU] Simulation done: {len(W.NODES)} nodes, {len(W.LINKS)} links, "
          f"{len(features)} GeoJSON features, {len(coord_pairs)} unique coord pairs "
          f"(exec={elapsed:.2f}s, post={post_elapsed:.2f}s)")
    stats = {
        "total_trips":           int(W.analyzer.trip_all),
        "completed_trips":       int(W.analyzer.trip_completed),
        "average_travel_time_s": round(float(W.analyzer.average_travel_time), 1)
            if W.analyzer.trip_completed > 0 else None,
        "simulation_time_s":     round(elapsed, 2),
        "post_processing_s":     round(post_elapsed, 2),
    }

    # 信号現示メタデータ（可視化用）
    # expand_intersections 前に抽出した元シナリオの信号情報を使う
    # （展開後のノード名は変わっており、signal は一部の内部ノードに移動済）
    # phase_log は UXsim の実シミュレーション結果（signal_log）を採用し、
    # クライアント表示と内部挙動のタイミングずれをなくす。
    signals = []
    try:
        # 展開後のリンク名集合（元の link 名が残っているかチェック）
        expanded_link_names = {lk.name for lk in scenario.links}
        # 元交差点 ID → entry サブノードの signal_log を 1 つ採用
        # (entry サブノードは全て同一サイクルなのでどれでも良い)
        log_by_orig_id = {}
        for w_node in W.NODES:
            sl = getattr(w_node, "signal_log", None)
            if sl is None or len(sl) == 0:
                continue
            # 元 ID 自身（展開なしの場合）
            if w_node.name in {n["name"] for n in _orig_signal_nodes}:
                log_by_orig_id[w_node.name] = list(sl)
                continue
            # entry サブノード（展開後）: parent_intersection_id 相当
            # ノード ID パターン: "{orig_id}__entry_{nb_id}"
            if "__entry_" in w_node.name:
                orig_id = w_node.name.split("__entry_")[0]
                if orig_id not in log_by_orig_id:
                    log_by_orig_id[orig_id] = list(sl)

        for orig_node in _orig_signal_nodes:
            groups = {
                lk_name: g for lk_name, g in _orig_signal_groups.items()
                if lk_name in expanded_link_names
            }
            phase_log = log_by_orig_id.get(orig_node["name"], [])
            signals.append({
                "node":     orig_node["name"],
                "x":        orig_node["x"],
                "y":        orig_node["y"],
                "phases":   orig_node["signal"],
                "groups":   groups,
                "phase_log": phase_log,    # UXsim 実 phase（秒単位）
            })
    except Exception as e:
        print(f"[RISU] signal metadata extraction failed: {e}")
        signals = []

    return {
        # 再現用: 実行に使った正規化済みシナリオ（SimulationInput）の完全コピー
        "_scenario": scenario.model_dump(),
        "geojson": {"type": "FeatureCollection", "features": features},
        "frames":  frames,
        "frame_times": sorted([float(k) for k in frames.keys()]),
        "stats":   stats,
        "tmax":    scenario.tmax,
        "signals": signals,
        # コンパクト列指向フォーマット用: link 名一覧（li インデックスで参照）
        "link_names": [lk.name for lk in W.LINKS],
        # スキーマバージョン（クライアントのフォーマット分岐用）
        "frame_format": "columnar_v2",
    }


def _apply_link_geometries(result: dict, link_geometries: dict):
    """GeoJSON features のリンク座標を OSMnx の道路形状（多点 LineString）で置き換える。"""
    if not link_geometries:
        return
    for f in result.get("geojson", {}).get("features", []):
        name = f["properties"]["name"]
        if name in link_geometries:
            f["geometry"]["coordinates"] = link_geometries[name]


# ──────────────────────────────────────────────
# MCP サーバー定義
# ──────────────────────────────────────────────
mcp_server = Server("uxsim-mcp")

@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_simulation",
            description=(
                "UXsim 交通流シミュレーションを実行します。"
                "ノード・リンク・需要を指定してください。"
                "結果は GeoJSON 形式と集計統計で返されます。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name":    {"type": "string",  "description": "シミュレーション名"},
                    "tmax":    {"type": "integer",  "description": "シミュレーション終了時刻（秒）"},
                    "deltan":  {"type": "integer",  "description": "車両集計単位（デフォルト5台）"},
                    "nodes":   {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "x":    {"type": "number"},
                                "y":    {"type": "number"},
                                "flow_capacity": {"type": "number"},
                                "signal": {"type": "array", "items": {"type": "number"}, "description": "信号現示の青時間リスト（秒）"},
                            },
                            "required": ["name", "x", "y"],
                        },
                    },
                    "links": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name":             {"type": "string"},
                                "start":            {"type": "string"},
                                "end":              {"type": "string"},
                                "length":           {"type": "number"},
                                "free_flow_speed":  {"type": "number"},
                                "jam_density":      {"type": "number"},
                                "number_of_lanes":  {"type": "integer"},
                                "oneway":           {"type": "boolean", "description": "true=一方通行"},
                                "signal_group":     {"type": "integer", "description": "信号現示番号（0始まり）"},
                            },
                            "required": ["name", "start", "end", "length"],
                        },
                    },
                    "demands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "orig":    {"type": "string"},
                                "dest":    {"type": "string"},
                                "t_start": {"type": "number"},
                                "t_end":   {"type": "number"},
                                "flow":    {"type": "number"},
                            },
                            "required": ["orig", "dest", "t_start", "t_end", "flow"],
                        },
                    },
                    "expand_intersections": {
                        "type": "boolean",
                        "description": "true にすると degree>=3 のノードを左折/直進/右折別の局所サブグラフへ自動展開する",
                    },
                },
                "required": ["nodes", "links", "demands"],
            },
        ),
        Tool(
            name="get_result",
            description="以前実行したシミュレーションの結果を ID で取得します。",
            inputSchema={
                "type": "object",
                "properties": {
                    "simulation_id": {"type": "string", "description": "run_simulation が返した ID"},
                },
                "required": ["simulation_id"],
            },
        ),
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "run_simulation":
        sim_input = SimulationInput(**arguments)
        loop = asyncio.get_event_loop()
        result = await _run_uxsim_async(sim_input)
        sim_id = str(uuid.uuid4())[:8]
        _store_sim(sim_id, result, {"type": "llm", "via": "mcp"})
        summary = result["stats"]
        return [TextContent(
            type="text",
            text=(
                f"シミュレーション完了。ID: {sim_id}\n"
                f"総トリップ数: {summary['total_trips']}\n"
                f"完了トリップ数: {summary['completed_trips']}\n"
                f"平均旅行時間: {summary['average_travel_time_s']} 秒\n"
                f"計算時間: {summary['simulation_time_s']} 秒\n"
                f"結果取得: GET /results/{sim_id}"
            ),
        )]

    elif name == "get_result":
        sim_id = arguments["simulation_id"]
        if sim_id not in results_store:
            return [TextContent(type="text", text=f"ID {sim_id} の結果が見つかりません。")]
        stats = results_store[sim_id]["stats"]
        return [TextContent(type="text", text=json.dumps(stats, ensure_ascii=False))]

    return [TextContent(type="text", text=f"未知のツール: {name}")]


# ──────────────────────────────────────────────
# FastAPI アプリ
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    executor.shutdown(wait=False)

app = FastAPI(title="RISU API", lifespan=lifespan)

# ──────────────────────────────────────────────
# グローバル例外ハンドラー
# ──────────────────────────────────────────────
import traceback as _tb
from fastapi.responses import JSONResponse as _JSONResp

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """
    HTTPException 以外の未捕捉例外を 500 に統一。
    詳細はサーバーログだけに残し、ユーザーには汎用メッセージ。
    """
    # HTTPException は FastAPI が処理するが、念のため
    if isinstance(exc, HTTPException):
        raise exc
    trace = _tb.format_exc()
    path = request.url.path if request else "?"
    print(f"[RISU unhandled] path={path} error={exc.__class__.__name__}: {exc}")
    print(trace)
    return _JSONResp(
        status_code=500,
        content={
            "detail": "予期しないエラーが発生しました。時間を空けて再度お試しください。",
            "error_id": id(exc) & 0xFFFFFF,  # 運用ログと突き合わせ可能な簡易 ID
        },
    )

_default_origins = "http://localhost:8001,http://127.0.0.1:8001"
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("RISU_ALLOWED_ORIGINS", _default_origins).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# アップロードサイズ上限（DoS 防止）
# ──────────────────────────────────────────────
MAX_UPLOAD_BYTES = int(os.getenv("RISU_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))  # 10MB
_UPLOAD_PATHS = ("/upload", "/gmns/import", "/import/osm")

@app.middleware("http")
async def limit_upload_mw(request, call_next):
    if request.method == "POST" and request.url.path in _UPLOAD_PATHS:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_BYTES:
            from fastapi.responses import JSONResponse
            mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            return JSONResponse(
                status_code=413,
                content={"detail": f"リクエストが大きすぎます（上限 {mb}MB）"},
            )
    return await call_next(request)

# ---- REST エンドポイント ----

# ──────────────────────────────────────────────
# シナリオサイズ上限（リソース保護）
# 環境変数で上書き可能。プラン制御を本番化する際はユーザーのプランを参照すること。
# ──────────────────────────────────────────────
MAX_NODES   = int(os.getenv("RISU_MAX_NODES",   "50000"))
MAX_LINKS   = int(os.getenv("RISU_MAX_LINKS",   "100000"))
MAX_DEMANDS = int(os.getenv("RISU_MAX_DEMANDS", "10000"))
MAX_TMAX    = int(os.getenv("RISU_MAX_TMAX",    "86400"))  # 24 時間

def _validate_scenario_size(scenario: SimulationInput) -> None:
    """シナリオのサイズ上限チェック。超過時は 413 を返す。"""
    n_nodes   = len(scenario.nodes)
    n_links   = len(scenario.links)
    n_demands = len(scenario.demands)
    if n_nodes > MAX_NODES:
        raise HTTPException(413, detail=f"ノード数が上限を超過: {n_nodes} > {MAX_NODES}")
    if n_links > MAX_LINKS:
        raise HTTPException(413, detail=f"リンク数が上限を超過: {n_links} > {MAX_LINKS}")
    if n_demands > MAX_DEMANDS:
        raise HTTPException(413, detail=f"需要データ数が上限を超過: {n_demands} > {MAX_DEMANDS}")
    if scenario.tmax and scenario.tmax > MAX_TMAX:
        raise HTTPException(413, detail=f"シミュレーション時間が上限を超過: {scenario.tmax}s > {MAX_TMAX}s")


@app.post("/simulate")
async def simulate(scenario: SimulationInput):
    """シミュレーションを実行して結果IDを返す"""
    _validate_scenario_size(scenario)
    result = await _run_uxsim_async(scenario)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {"type": "manual"})
    return {"id": sim_id, "stats": result["stats"]}

@app.get("/results/{sim_id}")
async def get_results(sim_id: str):
    """新スキーマ（risu_schema_version 1.0）の完全エンベロープを返す。"""
    if sim_id not in results_store:
        raise HTTPException(404, detail="Result not found")
    return _build_envelope(sim_id, include_result=True)


@app.get("/results/{sim_id}/scenario")
async def get_results_scenario(sim_id: str):
    """再現用の軽量エンベロープ（scenario + meta のみ、result なし）を返す。"""
    if sim_id not in results_store:
        raise HTTPException(404, detail="Result not found")
    return _build_envelope(sim_id, include_result=False)

# ---- Chat エンドポイント ----
# LLM_BACKEND 環境変数で切り替え: "mock" / "claude" / "ollama"

SYSTEM_PROMPT = """私はRISUです。交通流シミュレーター UXsim を対話的に操作するアシスタントです。
一人称は常に「RISU」を使います。

【最重要ルール】
ユーザーがシミュレーションの実行を求めている場合（ネットワーク作成、渋滞シミュレーション、交通シナリオなど）、
「実行します」「作成します」と言うだけでなく、必ずその場で run_simulation ツールを呼び出してください。
テキストだけで応答してツール呼び出しを省略することは絶対にしないでください。

【ツール呼び出しの指針】
- ノード座標の単位はメートル（例: x=0, y=0 ～ x=5000, y=5000）
- flow の単位は台/秒（例: 0.5 = 1秒に0.5台）
- 知らないネットワーク名を求められた場合でも、妥当な仮定でシナリオを構築してツールを呼び出すこと

【リンクの方向ルール（必須）】
- すべての link は有向リンク（start → end の一方向）として扱われる
- 双方向道路は必ず "2 本の並行有向リンク" として表現すること
- 推奨: A→B と B→A を両方明示的に作成する
- 省力法: 片方だけ書いて oneway: false（デフォルト）にしておけば、expand_intersections=true 時に
  サーバー側で逆向きリンクが自動生成される
- 一方通行を表現したい場合のみ oneway: true をセットして片方だけ作る

【交差点展開（仮想街路ネットワーク作成時）】
仮想的な街路ネットワーク（grid, 十字, T字, Y字, 多枝交差点など）を自分で構築する場合は、
交差点を単一ノードのまま記述し、run_simulation の `expand_intersections` を必ず true にすること。
サーバー側で各交差点を自動的に「進入端点・退出端点・movement(左折/直進/右折)内部リンク」
からなる局所サブグラフへ展開する。

LLM 側がやるべきこと:
- 交差点 1 つ = ノード 1 つ で記述する（端点や内部リンクは生成しない）
- 道路は交差点ノード間を結ぶ普通のリンクとして双方向に記述する
- demand の orig / dest は交差点ノード名をそのまま使ってよい（自動で spawn/sink に張り替わる）
- expand_intersections: true を指定する

LLM 側がやらなくていいこと:
- 端点座標の計算
- 左右オフセットや角度判定
- u_turn 除外
- 内部リンクの生成

OSM や CSV から取り込んだ実ネットワーク、もしくは単純な直線道路（degree<3）の場合は
expand_intersections は false（または省略）でよい。

■ movement 種別
- 4枝交差点では進入方向に対し: 対向側 = through / 左側 = left_turn / 右側 = right_turn / 同方向折返し = u_turn
- 原則として u_turn は生成しない（ユーザーが明示的に求めた場合のみ生成）
- 禁止 movement（中央分離帯, 右折禁止 等）に対応する内部リンクは生成しない（"欠落"で表現）
- リンク名やコメントで movement_type が分かるようにしておく

■ 外部リンクとの接続関係（必須）
  外部道路 ─→ 進入端点 ─→ 内部リンク ─→ 退出端点 ─→ 外部道路
- 外部道路リンクは 1本 を 2分割し、交差点手前で進入端点に終端、交差点直後で退出端点から始端する
- 進入リンク数と進入端点数、退出リンク数と退出端点数は一致すること

■ 適用対象外（展開しない）
- 立体交差・単なる形状点・movement 区別の不要な単純2リンク連結点
- ラウンドアバウト（環状リンク+流入流出として別途表現）

■ 最小実装要件（必ず満たす）
- 進入端点・退出端点の生成
- movement 単位の内部リンク生成
- 禁止 movement (デフォルトで u_turn) は未生成

【信号制御】
UXsim は交差点ノードに信号制御を設定できる。2 つのパラメータで記述する:

■ ノード側: signal パラメータ（各現示の青時間リスト）
  - signal: [60, 60]  → 2現示、各60秒青 → サイクル長120秒
  - signal: [30, 10, 50, 5]  → 4現示、サイクル長95秒
  - signal を省略 or null → 信号なし（常時通行可能）

■ リンク側: signal_group パラメータ（どの現示で青になるか）
  - signal_group: 0  → signal[0] の現示で通行可能
  - signal_group: 1  → signal[1] の現示で通行可能
  - signal_group を省略 → 信号に関係なく常時通行可能（退出リンクはこれ）

■ 使い方のルール
  - signal はノード（交差点）に設定する。signal_group は進入リンクに設定する
  - 退出リンク（交差点→外部）には signal_group を付けない（常時通行可能）
  - 同じ signal_group の進入リンクは同じ現示で同時に青になる
  - 典型例: 東西方向 signal_group=0、南北方向 signal_group=1

■ 4枝交差点の例（2現示、東西青/南北青）
  nodes:
    {"name": "I", "x": 0, "y": 0, "signal": [60, 60]}  ← サイクル120秒
  links (進入リンクのみ signal_group を設定):
    {"name": "EI", "start": "E", "end": "I", "signal_group": 0}  ← phase 0 で青
    {"name": "WI", "start": "W", "end": "I", "signal_group": 0}  ← phase 0 で青
    {"name": "SI", "start": "S", "end": "I", "signal_group": 1}  ← phase 1 で青
    {"name": "NI", "start": "N", "end": "I", "signal_group": 1}  ← phase 1 で青
    {"name": "IE", "start": "I", "end": "E"}  ← 退出: signal_group なし
    {"name": "IW", "start": "I", "end": "W"}  ← 退出: signal_group なし

■ 交差点展開 (expand_intersections=true) との併用
  - 展開前のノードに signal を、展開前の進入リンクに signal_group を設定する
  - 展開後、進入リンクは内部チェーンに分割されるが、signal_group は
    展開された entry ノードが属する交差点ノードに引き継がれる
  - ユーザーが signal_group を指定しない場合でも、expand_intersections 時に
    方向別のデフォルト signal_group を自動付与することも可能（将来拡張）

■ ユーザーが「信号をつけて」「信号制御して」と言った場合
  - まず交差点ノードに signal パラメータを追加する
  - 進入リンクに signal_group を割り当てる（対向方向は同じ group）
  - 青時間はユーザーの指示に従う。指示がなければ均等（例: [60, 60]）にする

【OSM（OpenStreetMap）連携】
- ユーザーが実在の地名・場所・駅名・ランドマーク等を言及した場合、import_osm_network ツールを使う
  例: 「東京駅周辺」「渋谷の道路」「大阪城公園あたり」「新宿駅」
- import_osm_network は地名を自動でジオコーディングし、道路ネットワークと建物データをダウンロードする
- distance_m パラメータで範囲を制御する（デフォルト500m）。ユーザーの要望に応じて調整する
  - 「広い範囲」→ 1000〜2000m、「狭い範囲」「駅前だけ」→ 200〜300m
- 取得後は自動的にダミー需要でシミュレーションが実行される
- ユーザーが範囲や需要を調整したい場合は対話的にヒアリングしてよい
- 結果は建物付きの地図として表示される

【グラフ機能】
- ユーザーがグラフ・チャート・分析・可視化を求めた場合、get_simulation_data ツールを呼んでデータを取得する
- データを受け取ったら、ユーザーの要望に合った Chart.js 設定を ```chart ... ``` コードブロックで出力する
- Chart.js設定は完全なJSON: {type, data: {labels, datasets}, options} 形式
- 例: ```chart\n{"type":"line","data":{"labels":[0,100,200],"datasets":[{"label":"速度","data":[20,15,10],"borderColor":"#0d9668"}]}}\n```
- どんな種類のグラフでも自由に作成できる（折れ線、棒、散布図、レーダー等）
- 複数のグラフを返す場合は複数の ```chart ブロックを使う
- データの加工・計算・フィルタリングは自由に行ってよい（平均、差分、比率、累積など）

【ファイル添付】
- ユーザーがCSV/JSONファイルを添付した場合、メッセージ内にパース結果が含まれる
- RISU CSV形式のパース結果にはnodes/links/demandsがJSON形式で含まれるので、そのまま run_simulation に渡すこと
- ユーザーのメッセージも確認し、パラメータ変更の要望があれば反映すること

【結果の説明】
結果を説明する際は、渋滞箇所・平均旅行時間・完了率などを分かりやすく日本語で解説してください。
常に「RISUが〜しました」のように一人称で話してください。"""

# ── モック用シナリオ定義 ──
MOCK_SCENARIOS = {
    "bottleneck": {
        "scenario": {
            "name": "bottleneck",
            "tmax": 2000,
            "deltan": 5,
            "nodes": [
                {"name": "start", "x": 0, "y": 0},
                {"name": "neck",  "x": 5000, "y": 0, "flow_capacity": 0.4},
                {"name": "goal",  "x": 7500, "y": 0},
            ],
            "links": [
                {"name": "road1", "start": "start", "end": "neck",  "length": 5000},
                {"name": "road2", "start": "neck",  "end": "goal",  "length": 2500, "free_flow_speed": 10},
            ],
            "demands": [
                {"orig": "start", "dest": "goal", "t_start": 0, "t_end": 600, "flow": 0.8},
            ],
        },
        "description": (
            "ボトルネック道路のシミュレーションを実行しました。\n\n"
            "【シナリオ】\n"
            "・start → neck → goal の3ノード直線道路\n"
            "・neck ノードの流出容量を 0.4 台/秒に制限（ボトルネック）\n"
            "・road2 の自由流速度を 10 m/s に低下\n"
            "・0〜600秒に 0.8 台/秒の需要を投入\n\n"
            "【結果の見方】\n"
            "・タイムスライダーを動かすと、時間経過に伴う速度変化が確認できます\n"
            "・緑=自由流（スムーズ）、赤=渋滞を示します\n"
            "・ボトルネック手前（road1）で渋滞が発生し、速度が低下している様子が観察できます"
        ),
    },
    "grid": {
        "scenario": {
            "name": "grid_3x3",
            "tmax": 3000,
            "deltan": 5,
            "nodes": [
                {"name": "n00", "x": 0,    "y": 0},
                {"name": "n10", "x": 2000, "y": 0},
                {"name": "n20", "x": 4000, "y": 0},
                {"name": "n01", "x": 0,    "y": 2000},
                {"name": "n11", "x": 2000, "y": 2000},
                {"name": "n21", "x": 4000, "y": 2000},
                {"name": "n02", "x": 0,    "y": 4000},
                {"name": "n12", "x": 2000, "y": 4000},
                {"name": "n22", "x": 4000, "y": 4000},
            ],
            "links": [
                # 水平方向（→）
                {"name": "h00",  "start": "n00", "end": "n10", "length": 2000},
                {"name": "h10",  "start": "n10", "end": "n20", "length": 2000},
                {"name": "h01",  "start": "n01", "end": "n11", "length": 2000},
                {"name": "h11",  "start": "n11", "end": "n21", "length": 2000},
                {"name": "h02",  "start": "n02", "end": "n12", "length": 2000},
                {"name": "h12",  "start": "n12", "end": "n22", "length": 2000},
                # 水平方向（←）
                {"name": "h00r", "start": "n10", "end": "n00", "length": 2000},
                {"name": "h10r", "start": "n20", "end": "n10", "length": 2000},
                {"name": "h01r", "start": "n11", "end": "n01", "length": 2000},
                {"name": "h11r", "start": "n21", "end": "n11", "length": 2000},
                {"name": "h02r", "start": "n12", "end": "n02", "length": 2000},
                {"name": "h12r", "start": "n22", "end": "n12", "length": 2000},
                # 垂直方向（↑）
                {"name": "v00",  "start": "n00", "end": "n01", "length": 2000},
                {"name": "v10",  "start": "n10", "end": "n11", "length": 2000},
                {"name": "v20",  "start": "n20", "end": "n21", "length": 2000},
                {"name": "v01",  "start": "n01", "end": "n02", "length": 2000},
                {"name": "v11",  "start": "n11", "end": "n12", "length": 2000},
                {"name": "v21",  "start": "n21", "end": "n22", "length": 2000},
                # 垂直方向（↓）
                {"name": "v00r", "start": "n01", "end": "n00", "length": 2000},
                {"name": "v10r", "start": "n11", "end": "n10", "length": 2000},
                {"name": "v20r", "start": "n21", "end": "n20", "length": 2000},
                {"name": "v01r", "start": "n02", "end": "n01", "length": 2000},
                {"name": "v11r", "start": "n12", "end": "n11", "length": 2000},
                {"name": "v21r", "start": "n22", "end": "n21", "length": 2000},
            ],
            "demands": [
                {"orig": "n00", "dest": "n22", "t_start": 0, "t_end": 800, "flow": 0.5},
                {"orig": "n02", "dest": "n20", "t_start": 0, "t_end": 800, "flow": 0.3},
                {"orig": "n20", "dest": "n02", "t_start": 200, "t_end": 600, "flow": 0.4},
            ],
        },
        "description": (
            "3×3 グリッドネットワークのシミュレーションを実行しました。\n\n"
            "【シナリオ】\n"
            "・9ノード（3×3格子）、24リンク（全道路双方向）のグリッド道路網\n"
            "・3つの OD 需要: 左下→右上、左上→右下、右下→左上\n"
            "・交差点（n11）付近で交通が集中\n\n"
            "【結果の見方】\n"
            "・双方向リンクは円弧で表示されます（進行方向の右側に膨らむ）\n"
            "・中央の交差点付近のリンクが赤くなり、混雑が確認できます\n"
            "・タイムスライダーで需要投入前後の変化を観察してください"
        ),
    },
    "default": {
        "scenario": {
            "name": "simple_road",
            "tmax": 1500,
            "deltan": 5,
            "nodes": [
                {"name": "A", "x": 0, "y": 0},
                {"name": "B", "x": 3000, "y": 1000},
                {"name": "C", "x": 6000, "y": 0},
            ],
            "links": [
                {"name": "AB", "start": "A", "end": "B", "length": 3200},
                {"name": "BC", "start": "B", "end": "C", "length": 3200},
            ],
            "demands": [
                {"orig": "A", "dest": "C", "t_start": 0, "t_end": 500, "flow": 0.6},
            ],
        },
        "description": (
            "シミュレーションを実行しました。\n\n"
            "【シナリオ】\n"
            "・A → B → C の3ノード道路\n"
            "・0〜500秒に 0.6 台/秒の需要\n\n"
            "【結果の見方】\n"
            "・タイムスライダーで速度の時間変化を確認できます\n"
            "・緑=スムーズ、赤=渋滞です"
        ),
    },
}

def _mock_llm_response(user_text: str) -> dict:
    """キーワードマッチでシナリオ選択 or テキスト応答を返す"""
    text = user_text.lower()

    # シミュレーション不要な質問
    greetings = ["hello", "こんにちは", "はじめ", "ありがとう", "thanks"]
    if any(g in text for g in greetings):
        return {"content": "こんにちは！交通流シミュレーター UXsim のアシスタントです。\n\nシミュレーションしたいシナリオを入力してください。例えば：\n・「ボトルネック道路を作って」\n・「3×3 グリッドネットワーク」\n・「渋滞をシミュレーションして」\n\nお気軽にどうぞ！", "scenario_key": None}

    info_keywords = ["教えて", "説明", "とは", "仕組み", "条件"]
    if any(k in text for k in info_keywords) and not any(k in text for k in ["作って", "シミュレ", "実行"]):
        return {"content": "交通流の渋滞は、道路の容量を超える交通需要が発生したときに起こります。\n\n主な要因：\n・ボトルネック（車線減少、合流部）\n・交通需要の集中（ラッシュアワー）\n・信号制御の不適切な設定\n\n実際にシミュレーションで確認してみましょう！\n「ボトルネック道路を作って」と入力してみてください。", "scenario_key": None}

    # シナリオ選択
    if any(k in text for k in ["ボトルネック", "bottleneck", "単純", "渋滞"]):
        return {"content": None, "scenario_key": "bottleneck"}
    if any(k in text for k in ["グリッド", "grid", "格子", "3×3", "3x3"]):
        return {"content": None, "scenario_key": "grid"}

    # デフォルト: シミュレーション実行
    return {"content": None, "scenario_key": "default"}


@app.post("/chat")
async def chat(body: ChatInput):
    """チャットエンドポイント（mock / claude / ollama）
    フロントエンドは常に JSON で送信。
    ファイル添付時はフロントでテキスト読み取りしてメッセージに含める。
    Claude バックエンド時は SSE ストリーミングで進捗を返す。
    """
    # 空メッセージ防止
    for i, m in enumerate(body.messages):
        if not m.content or not m.content.strip():
            body.messages[i] = ChatMessage(role=m.role, content="(空メッセージ)")

    last_msg = body.messages[-1].content if body.messages else ""

    if LLM_BACKEND == "mock":
        return await _chat_mock(last_msg)
    elif LLM_BACKEND == "claude":
        return await _chat_claude_stream(body)
    else:
        return await _chat_ollama(body)


async def _chat_mock(user_text: str):
    """モック LLM: キーワードマッチでシナリオを選択し、実際の UXsim を実行"""
    mock = _mock_llm_response(user_text)

    # テキスト応答のみ（シミュレーション不要）
    if mock["scenario_key"] is None:
        return {"role": "assistant", "content": mock["content"], "sim_id": None}

    # シミュレーション実行
    scenario_data = MOCK_SCENARIOS[mock["scenario_key"]]
    sim_input = SimulationInput(**scenario_data["scenario"])
    loop = asyncio.get_event_loop()
    result = await _run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {
        "type": "llm",
        "llm_backend": "mock",
        "scenario_key": mock["scenario_key"],
        "llm_user_message": user_text,
    })

    stats = result["stats"]
    description = scenario_data["description"]
    summary = (
        f"{description}\n\n"
        f"【統計情報】\n"
        f"・総トリップ数: {stats['total_trips']}\n"
        f"・完了トリップ数: {stats['completed_trips']}\n"
        f"・平均旅行時間: {stats['average_travel_time_s']} 秒\n"
        f"・計算時間: {stats['simulation_time_s']} 秒"
    )

    return {"role": "assistant", "content": summary, "sim_id": sim_id}


# ── シミュレーションデータ集計（LLM に渡す） ──
def _get_simulation_data(sim_id: str) -> dict | None:
    """シミュレーション結果から集計データを返す（LLMがチャート生成に使用）"""
    if sim_id not in results_store:
        return None
    data = results_store[sim_id]
    frames = data.get("frames", {})
    geojson = data.get("geojson", {})
    features = geojson.get("features", [])
    tmax = data.get("tmax", 3600)
    stats = data.get("stats", {})

    frame_times = sorted([float(k) for k in frames.keys()])
    if not frame_times:
        return None

    # 間引き（最大40点）
    step = max(1, len(frame_times) // 40)
    sampled = frame_times[::step]

    # ネットワーク全体の時系列
    time_labels = []
    net_avg_speed = []
    net_vehicle_count = []
    for t in sampled:
        t_key = str(t) if str(t) in frames else str(round(t, 1))
        vehs = frames.get(t_key, [])
        time_labels.append(round(t))
        net_vehicle_count.append(len(vehs))
        if vehs:
            net_avg_speed.append(round(sum(v["v"] for v in vehs) / len(vehs), 2))
        else:
            net_avg_speed.append(None)

    # リンク別速度
    link_names = [f["properties"]["name"] for f in features]
    link_speeds = {}
    for f in features:
        ln = f["properties"]["name"]
        tl = f["properties"].get("timeline", [])
        if not tl:
            continue
        speeds = []
        for t in sampled:
            lo, hi = 0, len(tl) - 1
            while lo < hi:
                m = (lo + hi + 1) >> 1
                if tl[m]["t"] <= t: lo = m
                else: hi = m - 1
            speeds.append(round(tl[lo]["speed"], 2))
        link_speeds[ln] = speeds

    # 速度分布（全期間）
    all_speeds = []
    for t in sampled:
        t_key = str(t) if str(t) in frames else str(round(t, 1))
        for v in frames.get(t_key, []):
            all_speeds.append(round(v["v"], 1))

    speed_hist = {"labels": [], "counts": []}
    if all_speeds:
        max_spd = max(all_speeds)
        bin_size = max(1, round(max_spd / 12))
        bins = list(range(0, int(max_spd) + bin_size + 1, bin_size))
        counts = [0] * (len(bins) - 1)
        for s in all_speeds:
            idx = min(int(s / bin_size), len(counts) - 1)
            counts[idx] += 1
        speed_hist["labels"] = [f"{bins[i]}-{bins[i+1]}" for i in range(len(counts))]
        speed_hist["counts"] = counts

    return {
        "sim_id": sim_id,
        "tmax": tmax,
        "stats": stats,
        "time_labels": time_labels,
        "network_avg_speed": net_avg_speed,
        "network_vehicle_count": net_vehicle_count,
        "link_names": link_names,
        "link_speeds": link_speeds,
        "speed_histogram": speed_hist,
    }


# ── Claude ツール定義 ──
CLAUDE_TOOLS = [
    {
        "name": "run_simulation",
        "description": (
            "UXsim 交通流シミュレーションを実行する。"
            "ノード（交差点）、リンク（道路）、需要（交通量）を指定する。"
            "座標の単位はメートル、flow の単位は台/秒。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":    {"type": "string", "description": "シミュレーション名"},
                "tmax":    {"type": "integer", "description": "シミュレーション終了時刻（秒）。デフォルト2000"},
                "deltan":  {"type": "integer", "description": "車両集計単位（デフォルト5）"},
                "nodes": {
                    "type": "array",
                    "description": "交差点リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "x":    {"type": "number", "description": "X座標（メートル）"},
                            "y":    {"type": "number", "description": "Y座標（メートル）"},
                            "flow_capacity": {"type": "number", "description": "ノード流出容量（台/秒）。省略で無制限"},
                            "signal": {
                                "type": "array",
                                "items": {"type": "number"},
                                "description": "信号現示の青時間リスト（秒）。例: [60,60]→2現示各60秒。省略=信号なし",
                            },
                        },
                        "required": ["name", "x", "y"],
                    },
                },
                "links": {
                    "type": "array",
                    "description": "道路リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":             {"type": "string"},
                            "start":            {"type": "string", "description": "始点ノード名"},
                            "end":              {"type": "string", "description": "終点ノード名"},
                            "length":           {"type": "number", "description": "道路長（メートル）"},
                            "free_flow_speed":  {"type": "number", "description": "自由流速度（m/s）。デフォルト20"},
                            "jam_density":      {"type": "number", "description": "渋滞密度（台/m）。デフォルト0.2"},
                            "number_of_lanes":  {"type": "integer", "description": "車線数。デフォルト1"},
                            "oneway":           {"type": "boolean", "description": "true=一方通行。false(デフォルト)の場合、expand_intersections時に逆向きリンクが自動生成される。"},
                            "signal_group":     {"type": "integer", "description": "この進入リンクが青になる信号現示番号（0始まり）。退出リンクには不要。省略=常時通行可能"},
                        },
                        "required": ["name", "start", "end", "length"],
                    },
                },
                "demands": {
                    "type": "array",
                    "description": "交通需要リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "orig":    {"type": "string", "description": "出発ノード名"},
                            "dest":    {"type": "string", "description": "到着ノード名"},
                            "t_start": {"type": "number", "description": "需要開始時刻（秒）"},
                            "t_end":   {"type": "number", "description": "需要終了時刻（秒）"},
                            "flow":    {"type": "number", "description": "交通量（台/秒）"},
                        },
                        "required": ["orig", "dest", "t_start", "t_end", "flow"],
                    },
                },
                "expand_intersections": {
                    "type": "boolean",
                    "description": (
                        "true にすると、サーバー側で degree>=3 の各ノードを "
                        "進入端点・退出端点・movement(左折/直進/右折)内部リンク "
                        "からなる局所サブグラフへ自動展開する。"
                        "仮想街路ネットワーク（grid, 十字, T字, 多枝交差点）を "
                        "自分で組み立てる場合は必ず true にすること。"
                        "OSM や CSV から取り込んだ実ネットワークでは false でよい。"
                    ),
                },
            },
            "required": ["nodes", "links", "demands"],
        },
    },
    {
        "name": "get_simulation_data",
        "description": (
            "シミュレーション結果の集計データを取得する。"
            "ユーザーがグラフ・チャート・分析を求めた場合に呼び出す。"
            "返されるデータ: time_labels, network_avg_speed, network_vehicle_count, "
            "link_speeds(リンク別), speed_histogram, stats。"
            "データを受け取ったら、Chart.js設定JSONを ```chart ... ``` コードブロックで返すこと。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sim_id": {"type": "string", "description": "シミュレーションID"},
            },
            "required": ["sim_id"],
        },
    },
    {
        "name": "import_osm_network",
        "description": (
            "OpenStreetMap から実在の道路ネットワークをダウンロードしてシミュレーションを実行する。"
            "地名・ランドマーク名を指定すると、自動でジオコーディングし、周辺の道路と建物を取得する。"
            "例: '東京駅', 'Shibuya Station', '大阪城公園', 'Times Square, New York' など。"
            "取得後、自動的にダミー需要を設定してシミュレーションを実行する。"
            "結果はブラウザ上に建物付きの地図として表示される。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "地名・ランドマーク名・住所（日本語・英語どちらも可）",
                },
                "distance_m": {
                    "type": "integer",
                    "description": "中心からの取得半径（メートル）。デフォルト500。大きいほど広い範囲だが処理に時間がかかる。100〜2000が推奨。",
                },
                "tmax": {
                    "type": "integer",
                    "description": "シミュレーション時間（秒）。デフォルト3600",
                },
            },
            "required": ["place"],
        },
    }
]


# ──────────────────────────────────────────────
# Prompt Caching ヘルパー
# ──────────────────────────────────────────────
# Anthropic の Prompt Caching は system プロンプト + tool 定義をキャッシュすることで
# 入力トークンの 90% OFF を実現する（5 分 TTL）。
# RISU は system が ~3,000 tokens、tools が ~2,000 tokens なので効果絶大。
# https://docs.anthropic.com/en/docs/prompt-caching
def _cached_system(text: str) -> list:
    """system プロンプトをキャッシュ有効形式で返す"""
    return [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]

def _cached_tools() -> list:
    """tool 定義の末尾に cache_control を付与（全 tool 定義を一括キャッシュ）"""
    if not CLAUDE_TOOLS:
        return []
    tools = [dict(t) for t in CLAUDE_TOOLS]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


# ──────────────────────────────────────────────
# トークン使用量ロガー
# ──────────────────────────────────────────────
def _log_usage(context: str, response) -> dict:
    """Claude レスポンスから usage を抽出してログ出力。将来 DB 保存フックに繋げられる"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    in_tok  = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create  = getattr(usage, "cache_creation_input_tokens", 0) or 0
    # Claude Sonnet 4 価格: $3 / $15 per MTok、キャッシュ読み: $0.30、キャッシュ書き: $3.75
    # JPY @ 150円/USD 概算
    cost_usd = (
        (in_tok - cache_read) * 3 / 1_000_000
        + cache_read * 0.30 / 1_000_000
        + cache_create * 3.75 / 1_000_000
        + out_tok * 15 / 1_000_000
    )
    cost_jpy = cost_usd * 150
    total_in = in_tok + cache_read
    cache_pct = (cache_read / max(1, total_in)) * 100
    try:
        msg = (
            f"[RISU usage] {context}: "
            f"in={in_tok}+cache_read={cache_read}+cache_create={cache_create}/out={out_tok} "
            f"(cache {cache_pct:.0f}%) ~JPY {cost_jpy:.2f}"
        )
        # Windows cp932 コンソール対策: エンコード不能文字は置換
        try:
            print(msg)
        except UnicodeEncodeError:
            import sys
            sys.stdout.buffer.write(msg.encode("utf-8", errors="replace") + b"\n")
            sys.stdout.flush()
    except Exception as e:
        print(f"[RISU usage] log failed: {e!r}")
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "cost_jpy": cost_jpy,
    }


# ──────────────────────────────────────────────
# tool_use ループ上限（暴走・コスト爆弾対策）
# ──────────────────────────────────────────────
MAX_TOOL_ROUNDS = int(os.getenv("RISU_MAX_TOOL_ROUNDS", "3"))


def _sse_event(data: dict) -> str:
    """SSE イベント文字列を生成"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _chat_claude_stream(body: ChatInput):
    """Claude API チャット — SSE ストリーミングで進捗を返す"""
    import anthropic
    import re

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    system = SYSTEM_PROMPT
    if results_store:
        last_sim_id = list(results_store.keys())[-1]
        link_names = [f["properties"]["name"] for f in results_store[last_sim_id].get("geojson", {}).get("features", [])]
        system += f"\n\n【現在のコンテキスト】\n直前のシミュレーションID: {last_sim_id}\nリンク名一覧: {', '.join(link_names[:20])}"

    chart_pattern = re.compile(r'```\s*chart\w*\s*\n?(.*?)\n?\s*```', re.DOTALL | re.IGNORECASE)

    async def event_generator():
        sim_id = None
        try:
            # ── 1回目: LLM 呼び出し（ストリーミング）──
            yield _sse_event({"type": "progress", "message": "Step 1/3: シナリオを設計中..."})

            first_streamed_text = False

            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=64000,
                system=_cached_system(system),
                messages=messages,
                tools=_cached_tools(),
            ) as first_stream:
                for event in first_stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            if not first_streamed_text:
                                yield _sse_event({"type": "stream_start"})
                                first_streamed_text = True
                            yield _sse_event({"type": "text_delta", "text": event.delta.text})

                response = first_stream.get_final_message()
                _log_usage("stream first-round", response)

            # テキストのみの応答（ツール呼び出しなし）
            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")

                # シミュレーション意図がありそうならリトライ（tool_choice で強制）
                last_user_msg = ""
                for m in reversed(messages):
                    if m["role"] == "user":
                        last_user_msg = m["content"] if isinstance(m["content"], str) else ""
                        break
                sim_keywords = ["シミュレーション", "シミュレート", "実行", "グリッド", "ネットワーク", "渋滞", "ボトルネック", "道路", "交通"]
                if any(k in last_user_msg for k in sim_keywords):
                    if first_streamed_text:
                        yield _sse_event({"type": "stream_end_partial"})
                    yield _sse_event({"type": "progress", "message": "Step 1/3: シナリオを再設計中..."})
                    response = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=32000,
                        system=_cached_system(system),
                        messages=messages + [
                            {"role": "assistant", "content": text},
                            {"role": "user", "content": "run_simulation ツールを使って今すぐシミュレーションを実行してください。"},
                        ],
                        tools=_cached_tools(),
                        tool_choice={"type": "tool", "name": "run_simulation"},
                    )
                    _log_usage("stream retry (tool_choice)", response)
                    if response.stop_reason != "tool_use":
                        yield _sse_event({"type": "done", "role": "assistant", "content": text, "sim_id": None})
                        return
                    # 下のツール実行に続行
                else:
                    yield _sse_event({"type": "done", "role": "assistant", "content": text, "sim_id": None})
                    return

            # ── ツール実行 ──
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = []

            for tool_block in tool_blocks:
                fn_name = tool_block.name
                fn_args = tool_block.input

                if fn_name == "run_simulation":
                    yield _sse_event({"type": "progress", "message": "Step 2/3: UXsim でシミュレーション実行中..."})
                    try:
                        sim_input = SimulationInput(**fn_args)
                        loop = asyncio.get_event_loop()
                        result = await _run_uxsim_async(sim_input)
                        sim_id = str(uuid.uuid4())[:8]
                        _store_sim(sim_id, result, {
                            "type": "llm",
                            "llm_backend": "claude",
                            "tool": "run_simulation",
                            "llm_user_message": _last_user_message_text(body),
                        })
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": json.dumps({**result["stats"], "sim_id": sim_id}, ensure_ascii=False),
                        })
                    except Exception as e:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": f"シミュレーション実行エラー: {str(e)}\n入力を修正して再度 run_simulation を呼んでください。",
                            "is_error": True,
                        })

                elif fn_name == "import_osm_network":
                    place = fn_args.get("place", "")
                    dist = fn_args.get("distance_m", 500)
                    osm_tmax = fn_args.get("tmax", 3600)
                    yield _sse_event({"type": "progress", "message": f"Step 2/3: OpenStreetMap から「{place}」周辺のデータを取得中..."})
                    try:
                        loop = asyncio.get_event_loop()
                        osm_result = await loop.run_in_executor(
                            executor, _run_osm_import, place, dist
                        )
                        scenario = dict(osm_result)
                        buildings = scenario.pop("buildings", [])
                        link_geometries = scenario.pop("link_geometries", {})
                        scenario.pop("center", None)
                        scenario.pop("distance_m", None)
                        summary = scenario.pop("summary", "")
                        scenario["tmax"] = osm_tmax

                        if not scenario["demands"] and len(scenario["nodes"]) >= 2:
                            scenario["demands"] = _generate_osm_demands(
                                scenario["nodes"], scenario["links"], osm_tmax
                            )

                        yield _sse_event({"type": "progress", "message": "Step 2/3: UXsim でシミュレーション実行中..."})
                        sim_input = SimulationInput(**scenario)
                        result = await _run_uxsim_async(sim_input)
                        result["buildings"] = buildings
                        _apply_link_geometries(result, link_geometries)
                        sim_id = str(uuid.uuid4())[:8]
                        _store_sim(sim_id, result, {
                            "type": "osm",
                            "via": "llm",
                            "llm_backend": "claude",
                            "place": place,
                            "distance_m": dist,
                            "llm_user_message": _last_user_message_text(body),
                        })

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": json.dumps({
                                **result["stats"],
                                "sim_id": sim_id,
                                "summary": summary,
                                "node_count": len(scenario["nodes"]),
                                "link_count": len(scenario["links"]),
                                "building_count": len(buildings),
                            }, ensure_ascii=False),
                        })
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": f"OSMインポートエラー: {str(e)}",
                            "is_error": True,
                        })

                elif fn_name == "get_simulation_data":
                    yield _sse_event({"type": "progress", "message": "データを集計中..."})
                    req_sim_id = fn_args.get("sim_id", "")
                    # sim_id が空または存在しない場合、直前のシミュレーションIDを使う
                    if (not req_sim_id or req_sim_id not in results_store) and results_store:
                        req_sim_id = list(results_store.keys())[-1]
                    sim_data = _get_simulation_data(req_sim_id)
                    if sim_data:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": json.dumps(sim_data, ensure_ascii=False),
                        })
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": "データが見つかりません。まず run_simulation でシミュレーションを実行してください。",
                        })

            # ── ツール結果を渡して最終回答をストリーミング生成 ──
            yield _sse_event({"type": "progress", "message": "Step 3/3: 結果を分析中..."})

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            # ストリーミングで最終回答を生成するヘルパー
            async def _stream_final_response(msgs):
                """messages を渡して streaming 呼び出し。テキストは text_delta で逐次送信し、
                ツール呼び出しがあれば蓄積して返す。最終テキストも返す。"""
                collected_text = []
                collected_tool_blocks = []

                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=8192,
                    system=_cached_system(system),
                    messages=msgs,
                    tools=_cached_tools(),
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_start":
                            if event.content_block.type == "tool_use":
                                collected_tool_blocks.append({
                                    "index": event.index,
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input_json": "",
                                })
                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                collected_text.append(event.delta.text)
                                yield {"event": "text_delta", "data": event.delta.text}
                            elif event.delta.type == "input_json_delta":
                                if collected_tool_blocks:
                                    collected_tool_blocks[-1]["input_json"] += event.delta.partial_json

                    # stream 終了後、最終 response を取得
                    final_response = stream.get_final_message()
                    _log_usage("stream post-tool", final_response)

                # ツールブロックを anthropic オブジェクトとして返す
                real_tool_blocks = [b for b in final_response.content if b.type == "tool_use"]
                full_text = "".join(collected_text)
                yield {"event": "stream_done", "text": full_text, "tool_blocks": real_tool_blocks, "response": final_response}

            # 1回目のストリーミング
            yield _sse_event({"type": "stream_start"})
            stream_result = None
            async for item in _stream_final_response(messages):
                if item["event"] == "text_delta":
                    yield _sse_event({"type": "text_delta", "text": item["data"]})
                elif item["event"] == "stream_done":
                    stream_result = item

            final_text = stream_result["text"]
            next_tool_blocks = stream_result["tool_blocks"]

            # さらにツール呼び出しがある場合はループで追加処理（最大3ラウンド）
            for _round in range(MAX_TOOL_ROUNDS):
                if not next_tool_blocks:
                    break
                yield _sse_event({"type": "stream_end_partial"})
                next_tool_results = []
                for tb in next_tool_blocks:
                    if tb.name == "get_simulation_data":
                        yield _sse_event({"type": "progress", "message": "チャートデータを取得中..."})
                        req_sim_id = tb.input.get("sim_id", "")
                        # sim_id が空または存在しない場合、直前のシミュレーションIDを使う
                        if (not req_sim_id or req_sim_id not in results_store) and results_store:
                            req_sim_id = list(results_store.keys())[-1]
                        sd = _get_simulation_data(req_sim_id)
                        next_tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.id,
                            "content": json.dumps(sd, ensure_ascii=False) if sd else "データが見つかりません。まず run_simulation でシミュレーションを実行してください。",
                        })
                    elif tb.name == "run_simulation":
                        yield _sse_event({"type": "progress", "message": "追加シミュレーションを実行中..."})
                        try:
                            si = SimulationInput(**tb.input)
                            loop = asyncio.get_event_loop()
                            r = await _run_uxsim_async(si)
                            new_id = str(uuid.uuid4())[:8]
                            _store_sim(new_id, r, {
                                "type": "llm",
                                "llm_backend": "claude",
                                "tool": "run_simulation",
                                "round": "follow_up",
                                "llm_user_message": _last_user_message_text(body),
                            })
                            sim_id = new_id
                            next_tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tb.id,
                                "content": json.dumps({**r["stats"], "sim_id": new_id}, ensure_ascii=False),
                            })
                        except Exception as e:
                            next_tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tb.id,
                                "content": f"シミュレーション実行エラー: {str(e)}",
                                "is_error": True,
                            })
                    elif tb.name == "import_osm_network":
                        _place = tb.input.get("place", "")
                        _dist = tb.input.get("distance_m", 500)
                        _tmax = tb.input.get("tmax", 3600)
                        yield _sse_event({"type": "progress", "message": f"OpenStreetMap から「{_place}」のデータを取得中..."})
                        try:
                            loop = asyncio.get_event_loop()
                            osm_r = await loop.run_in_executor(executor, _run_osm_import, _place, _dist)
                            _scenario = dict(osm_r)
                            _buildings = _scenario.pop("buildings", [])
                            _link_geoms = _scenario.pop("link_geometries", {})
                            _scenario.pop("center", None)
                            _scenario.pop("distance_m", None)
                            _summary = _scenario.pop("summary", "")
                            _scenario["tmax"] = _tmax
                            if not _scenario["demands"] and len(_scenario["nodes"]) >= 2:
                                _scenario["demands"] = _generate_osm_demands(
                                    _scenario["nodes"], _scenario["links"], _tmax
                                )
                            si = SimulationInput(**_scenario)
                            r = await _run_uxsim_async(si)
                            r["buildings"] = _buildings
                            _apply_link_geometries(r, _link_geoms)
                            new_id = str(uuid.uuid4())[:8]
                            _store_sim(new_id, r, {
                                "type": "osm",
                                "via": "llm",
                                "llm_backend": "claude",
                                "place": _place,
                                "distance_m": _dist,
                                "round": "follow_up",
                                "llm_user_message": _last_user_message_text(body),
                            })
                            sim_id = new_id
                            next_tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tb.id,
                                "content": json.dumps({
                                    **r["stats"], "sim_id": new_id,
                                    "summary": _summary,
                                    "node_count": len(_scenario["nodes"]),
                                    "link_count": len(_scenario["links"]),
                                    "building_count": len(_buildings),
                                }, ensure_ascii=False),
                            })
                        except Exception as e:
                            import traceback
                            traceback.print_exc()
                            next_tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tb.id,
                                "content": f"OSMインポートエラー: {str(e)}",
                                "is_error": True,
                            })
                    else:
                        next_tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.id,
                            "content": f"未知のツール: {tb.name}",
                        })

                messages.append({"role": "assistant", "content": stream_result["response"].content})
                messages.append({"role": "user", "content": next_tool_results})

                # 次ラウンドもストリーミング
                yield _sse_event({"type": "stream_start"})
                next_tool_blocks = []
                async for item in _stream_final_response(messages):
                    if item["event"] == "text_delta":
                        yield _sse_event({"type": "text_delta", "text": item["data"]})
                    elif item["event"] == "stream_done":
                        final_text = item["text"]
                        next_tool_blocks = item["tool_blocks"]
                        stream_result = item

            # チャート抽出
            charts = []
            for m in chart_pattern.finditer(final_text):
                try:
                    chart_json = json.loads(m.group(1))
                    charts.append(chart_json)
                except json.JSONDecodeError as e:
                    print(f"[RISU] chart JSON parse failed: {e}; raw={m.group(1)[:200]!r}")
            clean_text = chart_pattern.sub('', final_text).strip()
            if "```chart" in clean_text.lower() or "```\nchart" in clean_text.lower():
                # 抽出漏れの兆候。ログに残してデバッグ可能に
                idx = clean_text.lower().find("```")
                print(f"[RISU] WARN: chart fence still present after strip; near={clean_text[max(0,idx-20):idx+200]!r}")
            print(f"[RISU] chat done: charts={len(charts)}, clean_text_len={len(clean_text)}")

            # sim_id が未設定の場合、直近のシミュレーション結果をフォールバック
            if sim_id is None and results_store:
                sim_id = list(results_store.keys())[-1]

            resp = {"type": "done", "role": "assistant", "content": clean_text, "sim_id": sim_id}
            if charts:
                resp["charts"] = charts
            yield _sse_event(resp)

        except anthropic.AuthenticationError:
            print("[RISU] CRITICAL: ANTHROPIC_API_KEY invalid!")
            yield _sse_event({"type": "error", "message": "サーバー側で LLM に接続できません。運営にお問い合わせください。"})
        except anthropic.RateLimitError:
            yield _sse_event({"type": "error", "message": "LLM が混雑しています。しばらく待って再試行してください。"})
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            if status == 529:
                yield _sse_event({"type": "error", "message": "LLM が一時的に過負荷です。1〜2 分後に再試行してください。"})
            elif status and 500 <= status < 600:
                yield _sse_event({"type": "error", "message": "LLM サーバー側のエラーです。しばらく後で再試行してください。"})
            else:
                print(f"[RISU] Claude APIStatusError {status}: {e}")
                yield _sse_event({"type": "error", "message": f"LLM エラー（コード: {status}）"})
        except anthropic.APIConnectionError:
            yield _sse_event({"type": "error", "message": "LLM に接続できません。ネットワーク接続を確認してください。"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            # ユーザーには詳細を伏せる
            print(f"[RISU] chat error: {e.__class__.__name__}: {e}")
            yield _sse_event({"type": "error", "message": "処理中にエラーが発生しました。もう一度お試しください。"})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _chat_claude(body: ChatInput):
    """Claude API を使ったチャット（tool_use 対応）"""
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # メッセージ変換（Claude API 形式）
    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    # 直前のシミュレーションIDをシステムプロンプトに注入
    system = SYSTEM_PROMPT
    if results_store:
        last_sim_id = list(results_store.keys())[-1]
        link_names = [f["properties"]["name"] for f in results_store[last_sim_id].get("geojson", {}).get("features", [])]
        system += f"\n\n【現在のコンテキスト】\n直前のシミュレーションID: {last_sim_id}\nリンク名一覧: {', '.join(link_names[:20])}"

    try:
        # 1回目：ツール付きリクエスト
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=_cached_system(system),
            messages=messages,
            tools=_cached_tools(),
        )
        _log_usage("sync first-round", response)

        # テキストのみの応答（ツール呼び出しなし）
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text")

            # LLM が「実行します」と言ったのにツールを呼ばなかった場合、再試行
            sim_keywords = ["実行します", "作成します", "シミュレーション", "構築します"]
            if any(k in text for k in sim_keywords):
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "run_simulation ツールを呼び出して、今すぐシミュレーションを実行してください。テキストだけでなくツールを使ってください。"})
                retry = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=4096,
                    system=_cached_system(system),
                    messages=messages,
                    tools=_cached_tools(),
                )
                _log_usage("sync retry", retry)
                if retry.stop_reason == "tool_use":
                    response = retry
                    # 下のツール処理に続行
                else:
                    return {"role": "assistant", "content": text, "sim_id": None}
            else:
                return {"role": "assistant", "content": text, "sim_id": None}

        # ツール呼び出しがある場合（複数ツール呼び出しにも対応）
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        sim_id = None
        tool_results = []

        for tool_block in tool_blocks:
            fn_name = tool_block.name
            fn_args = tool_block.input

            if fn_name == "run_simulation":
                try:
                    sim_input = SimulationInput(**fn_args)
                    loop = asyncio.get_event_loop()
                    result = await _run_uxsim_async(sim_input)
                    sim_id = str(uuid.uuid4())[:8]
                    _store_sim(sim_id, result, {
                        "type": "llm",
                        "llm_backend": "claude",
                        "tool": "run_simulation",
                        "llm_user_message": _last_user_message_text(body),
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": json.dumps({**result["stats"], "sim_id": sim_id}, ensure_ascii=False),
                    })
                except Exception as e:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": f"シミュレーション実行エラー: {str(e)}\n入力を修正して再度 run_simulation を呼んでください。",
                        "is_error": True,
                    })

            elif fn_name == "import_osm_network":
                try:
                    place = fn_args.get("place", "")
                    dist = fn_args.get("distance_m", 500)
                    osm_tmax = fn_args.get("tmax", 3600)
                    loop = asyncio.get_event_loop()
                    osm_result = await loop.run_in_executor(
                        executor, _run_osm_import, place, dist
                    )
                    # シナリオ構築（ダミー需要追加）
                    scenario = dict(osm_result)
                    buildings = scenario.pop("buildings", [])
                    link_geometries = scenario.pop("link_geometries", {})
                    scenario.pop("center", None)
                    scenario.pop("distance_m", None)
                    summary = scenario.pop("summary", "")
                    scenario["tmax"] = osm_tmax

                    if not scenario["demands"] and len(scenario["nodes"]) >= 2:
                        scenario["demands"] = _generate_osm_demands(
                            scenario["nodes"], scenario["links"], osm_tmax
                        )

                    sim_input = SimulationInput(**scenario)
                    result = await _run_uxsim_async(sim_input)
                    # 建物・道路形状データを結果に追加
                    result["buildings"] = buildings
                    _apply_link_geometries(result, link_geometries)
                    sim_id = str(uuid.uuid4())[:8]
                    _store_sim(sim_id, result, {
                        "type": "osm",
                        "via": "llm",
                        "llm_backend": "claude",
                        "place": place,
                        "distance_m": dist,
                        "llm_user_message": _last_user_message_text(body),
                    })

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": json.dumps({
                            **result["stats"],
                            "sim_id": sim_id,
                            "summary": summary,
                            "node_count": len(scenario["nodes"]),
                            "link_count": len(scenario["links"]),
                            "building_count": len(buildings),
                        }, ensure_ascii=False),
                    })
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": f"OSMインポートエラー: {str(e)}",
                        "is_error": True,
                    })

            elif fn_name == "get_simulation_data":
                sim_data = _get_simulation_data(fn_args.get("sim_id", ""))
                if sim_data:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": json.dumps(sim_data, ensure_ascii=False),
                    })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": "データが見つかりません。シミュレーションIDを確認してください。",
                    })

        # ツール結果を渡して次の回答を生成（最大3ラウンド）
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        import re
        chart_pattern = re.compile(r'```\s*chart\w*\s*\n?(.*?)\n?\s*```', re.DOTALL | re.IGNORECASE)

        for _round in range(MAX_TOOL_ROUNDS):
            resp_next = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=8192,
                system=_cached_system(system),
                messages=messages,
                tools=_cached_tools(),
            )
            _log_usage(f"sync round {_round + 1}", resp_next)

            # テキスト部分を収集
            final_text = "".join(b.text for b in resp_next.content if b.type == "text")

            # さらにツール呼び出しがある場合は処理を続ける
            next_tool_blocks = [b for b in resp_next.content if b.type == "tool_use"]
            if not next_tool_blocks:
                break

            next_tool_results = []
            for tb in next_tool_blocks:
                if tb.name == "run_simulation":
                    try:
                        si = SimulationInput(**tb.input)
                        loop = asyncio.get_event_loop()
                        r = await _run_uxsim_async(si)
                        new_id = str(uuid.uuid4())[:8]
                        _store_sim(new_id, r, {
                            "type": "llm",
                            "llm_backend": "claude",
                            "tool": "run_simulation",
                            "round": "follow_up",
                            "llm_user_message": _last_user_message_text(body),
                        })
                        sim_id = new_id
                        next_tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.id,
                            "content": json.dumps({**r["stats"], "sim_id": new_id}, ensure_ascii=False),
                        })
                    except Exception as e:
                        next_tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.id,
                            "content": f"シミュレーション実行エラー: {str(e)}\n入力を修正して再度 run_simulation を呼んでください。",
                            "is_error": True,
                        })
                elif tb.name == "import_osm_network":
                    try:
                        _place = tb.input.get("place", "")
                        _dist = tb.input.get("distance_m", 500)
                        _tmax = tb.input.get("tmax", 3600)
                        loop = asyncio.get_event_loop()
                        osm_r = await loop.run_in_executor(
                            executor, _run_osm_import, _place, _dist
                        )
                        _scenario = dict(osm_r)
                        _buildings = _scenario.pop("buildings", [])
                        _link_geoms = _scenario.pop("link_geometries", {})
                        _scenario.pop("center", None)
                        _scenario.pop("distance_m", None)
                        _summary = _scenario.pop("summary", "")
                        _scenario["tmax"] = _tmax
                        if not _scenario["demands"] and len(_scenario["nodes"]) >= 2:
                            _scenario["demands"] = _generate_osm_demands(
                                _scenario["nodes"], _scenario["links"], _tmax
                            )
                        si = SimulationInput(**_scenario)
                        r = await _run_uxsim_async(si)
                        r["buildings"] = _buildings
                        _apply_link_geometries(r, _link_geoms)
                        new_id = str(uuid.uuid4())[:8]
                        _store_sim(new_id, r, {
                            "type": "osm",
                            "via": "llm",
                            "llm_backend": "claude",
                            "place": _place,
                            "distance_m": _dist,
                            "round": "follow_up",
                            "llm_user_message": _last_user_message_text(body),
                        })
                        sim_id = new_id
                        next_tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.id,
                            "content": json.dumps({
                                **r["stats"], "sim_id": new_id,
                                "summary": _summary,
                                "node_count": len(_scenario["nodes"]),
                                "link_count": len(_scenario["links"]),
                                "building_count": len(_buildings),
                            }, ensure_ascii=False),
                        })
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        next_tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.id,
                            "content": f"OSMインポートエラー: {str(e)}",
                            "is_error": True,
                        })
                elif tb.name == "get_simulation_data":
                    sd = _get_simulation_data(tb.input.get("sim_id", ""))
                    next_tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tb.id,
                        "content": json.dumps(sd, ensure_ascii=False) if sd else "データが見つかりません。",
                    })
                else:
                    next_tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tb.id,
                        "content": f"未知のツール: {tb.name}",
                    })

            messages.append({"role": "assistant", "content": resp_next.content})
            messages.append({"role": "user", "content": next_tool_results})

        # ```chart ... ``` ブロックからChart.js設定を抽出
        charts = []
        for m in chart_pattern.finditer(final_text):
            try:
                chart_json = json.loads(m.group(1))
                charts.append(chart_json)
            except json.JSONDecodeError:
                pass
        clean_text = chart_pattern.sub('', final_text).strip()

        resp = {"role": "assistant", "content": clean_text, "sim_id": sim_id}
        if charts:
            resp["charts"] = charts
        return resp

    except anthropic.AuthenticationError:
        raise HTTPException(401, detail="Anthropic API キーが無効です。ANTHROPIC_API_KEY を確認してください")
    except anthropic.RateLimitError:
        raise HTTPException(429, detail="Claude API のレートリミットに達しました。しばらく待ってください")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, detail=f"Claude エラー: {str(e)}")


async def _chat_ollama(body: ChatInput):
    """本番用: Ollama LLM と対話"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": m.role, "content": m.content} for m in body.messages]

    TOOLS_FOR_OLLAMA = [
        {
            "type": "function",
            "function": {
                "name": "run_simulation",
                "description": "UXsim 交通流シミュレーションを実行する",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":    {"type": "string"},
                        "tmax":    {"type": "integer"},
                        "deltan":  {"type": "integer"},
                        "nodes":   {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}, "flow_capacity": {"type": "number"}}, "required": ["name", "x", "y"]}},
                        "links":   {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "length": {"type": "number"}, "free_flow_speed": {"type": "number"}}, "required": ["name", "start", "end", "length"]}},
                        "demands": {"type": "array", "items": {"type": "object", "properties": {"orig": {"type": "string"}, "dest": {"type": "string"}, "t_start": {"type": "number"}, "t_end": {"type": "number"}, "flow": {"type": "number"}}, "required": ["orig", "dest", "t_start", "t_end", "flow"]}},
                    },
                    "required": ["nodes", "links", "demands"],
                },
            },
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "tools": TOOLS_FOR_OLLAMA, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
            assistant_msg = data["message"]

            if not assistant_msg.get("tool_calls"):
                return {"role": "assistant", "content": assistant_msg["content"], "sim_id": None}

            tool_call = assistant_msg["tool_calls"][0]
            fn_args = tool_call["function"]["arguments"]
            if isinstance(fn_args, str):
                fn_args = json.loads(fn_args)

            sim_input = SimulationInput(**fn_args)
            loop = asyncio.get_event_loop()
            result = await _run_uxsim_async(sim_input)
            sim_id = str(uuid.uuid4())[:8]
            _store_sim(sim_id, result, {
                "type": "llm",
                "llm_backend": "ollama",
                "tool": "run_simulation",
                "llm_user_message": _last_user_message_text(body),
            })

            messages.append({"role": "assistant", "content": "", "tool_calls": assistant_msg["tool_calls"]})
            messages.append({"role": "tool", "content": json.dumps(result["stats"], ensure_ascii=False)})

            resp2 = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            )
            resp2.raise_for_status()
            final_msg = resp2.json()["message"]["content"]

            return {"role": "assistant", "content": final_msg, "sim_id": sim_id}

    except httpx.TimeoutException:
        raise HTTPException(504, detail="Ollama の応答がタイムアウトしました")
    except httpx.ConnectError:
        raise HTTPException(502, detail="Ollama に接続できません。ollama serve が起動しているか確認してください")
    except Exception as e:
        raise HTTPException(500, detail=f"チャットエラー: {str(e)}")


# ──────────────────────────────────────────────
# CSV / GMNS / OSM インポート
# ──────────────────────────────────────────────

def _find_col(fields: list[str], candidates: list[str], default: str | None = None) -> str | None:
    """fields 内から candidates のいずれかに一致するカラム名を返す（大文字小文字無視）"""
    field_lower = {f.lower(): f for f in fields}
    for c in candidates:
        if c.lower() in field_lower:
            return field_lower[c.lower()]
    return default


def _get_val(row: dict, col: str | None, default=None):
    """行から指定カラムの値を取得（None/空文字はデフォルト値）"""
    if col is None:
        return default
    val = row.get(col, "")
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip()


def _get_float(row: dict, col: str | None, default: float = 0.0) -> float:
    val = _get_val(row, col, None)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_int(row: dict, col: str | None, default: int = 1) -> int:
    val = _get_val(row, col, None)
    if val is None:
        return default
    try:
        return int(float(val))
    except ValueError:
        return default


def _parse_csv_scenario(content: str) -> dict:
    """
    単一 CSV からシナリオを推定する。
    カラム名を柔軟にマッチング。対応フォーマット:
      1) RISU 独自形式: type 列で node/link/demand を区別
      2) ノード CSV: name/id + x/y 座標系カラム
      3) リンク CSV: start/from + end/to 系カラム
      4) 需要 CSV: orig/from + dest/to + volume/flow 系カラム
      5) GMNS 形式 (node_id, from_node_id 等)
    """
    reader = csv.DictReader(io.StringIO(content))
    raw_fields = list(reader.fieldnames or [])
    fields_lower = [f.strip().lower() for f in raw_fields]

    # ============ RISU 独自 CSV: type 列あり ============
    if "type" in fields_lower:
        nodes, links, demands = [], [], []
        col_type = _find_col(raw_fields, ["type"])
        col_name = _find_col(raw_fields, ["name", "id", "node_id", "link_id"])
        col_x = _find_col(raw_fields, ["x", "x_coord", "lon", "longitude"])
        col_y = _find_col(raw_fields, ["y", "y_coord", "lat", "latitude"])
        col_start = _find_col(raw_fields, ["start", "start_node", "from", "from_node", "from_node_id", "source"])
        col_end = _find_col(raw_fields, ["end", "end_node", "to", "to_node", "to_node_id", "target", "dest"])
        col_length = _find_col(raw_fields, ["length", "distance", "dist"])
        col_ffs = _find_col(raw_fields, ["free_flow_speed", "speed", "free_speed", "speed_limit", "ffs"])
        col_lanes = _find_col(raw_fields, ["number_of_lanes", "lanes", "num_lanes"])
        col_orig = _find_col(raw_fields, ["orig", "origin", "o_zone_id", "from", "source"])
        col_dest_d = _find_col(raw_fields, ["dest", "destination", "d_zone_id", "to", "target"])
        col_tstart = _find_col(raw_fields, ["t_start", "start_time", "time_start"])
        col_tend = _find_col(raw_fields, ["t_end", "end_time", "time_end"])
        col_flow = _find_col(raw_fields, ["flow", "volume", "demand", "rate"])

        for row in reader:
            t = _get_val(row, col_type, "").lower()
            if t == "node":
                nodes.append({
                    "name": _get_val(row, col_name, ""),
                    "x": _get_float(row, col_x, 0),
                    "y": _get_float(row, col_y, 0),
                })
            elif t == "link":
                links.append({
                    "name": _get_val(row, col_name, ""),
                    "start": _get_val(row, col_start, ""),
                    "end": _get_val(row, col_end, ""),
                    "length": _get_float(row, col_length, 1000),
                    "free_flow_speed": _get_float(row, col_ffs, 20),
                    "number_of_lanes": _get_int(row, col_lanes, 1),
                })
            elif t == "demand":
                demands.append({
                    "orig": _get_val(row, col_orig, ""),
                    "dest": _get_val(row, col_dest_d, ""),
                    "t_start": _get_float(row, col_tstart, 0),
                    "t_end": _get_float(row, col_tend, 3600),
                    "flow": _get_float(row, col_flow, 0.5),
                })
        return {"format": "risu_csv", "nodes": nodes, "links": links, "demands": demands}

    # ============ ノード CSV 判定 ============
    # name/id + x/y 系カラムがあるか
    NODE_NAME_COLS = ["name", "node_name", "node_id", "id", "node"]
    NODE_X_COLS = ["x", "x_coord", "lon", "longitude", "lng", "経度"]
    NODE_Y_COLS = ["y", "y_coord", "lat", "latitude", "緯度"]

    col_nname = _find_col(raw_fields, NODE_NAME_COLS)
    col_nx = _find_col(raw_fields, NODE_X_COLS)
    col_ny = _find_col(raw_fields, NODE_Y_COLS)

    if col_nx and col_ny:
        col_cap = _find_col(raw_fields, ["flow_capacity", "capacity", "cap"])
        nodes = []
        for row in reader:
            name = _get_val(row, col_nname, f"n{len(nodes)}")
            x = _get_float(row, col_nx, 0)
            y = _get_float(row, col_ny, 0)
            node = {"name": name, "x": x, "y": y}
            cap = _get_float(row, col_cap, -1)
            if cap > 0:
                node["flow_capacity"] = cap
            nodes.append(node)
        return {"format": "node_csv", "nodes": nodes}

    # ============ リンク CSV 判定 ============
    LINK_START_COLS = ["start", "start_node", "from", "from_node", "from_node_id",
                       "source", "origin", "始点", "start_id"]
    LINK_END_COLS = ["end", "end_node", "to", "to_node", "to_node_id",
                     "target", "dest", "destination", "終点", "end_id"]

    col_ls = _find_col(raw_fields, LINK_START_COLS)
    col_le = _find_col(raw_fields, LINK_END_COLS)

    if col_ls and col_le:
        col_lname = _find_col(raw_fields, ["name", "link_name", "link_id", "id", "link"])
        col_length = _find_col(raw_fields, ["length", "distance", "dist", "長さ"])
        col_ffs = _find_col(raw_fields, ["free_flow_speed", "speed", "free_speed",
                                          "speed_limit", "ffs", "制限速度", "速度"])
        col_lanes = _find_col(raw_fields, ["number_of_lanes", "lanes", "num_lanes", "車線数"])
        col_jd = _find_col(raw_fields, ["jam_density", "kjam"])

        links = []
        for row in reader:
            name = _get_val(row, col_lname, f"link{len(links)}")
            start = _get_val(row, col_ls, "")
            end = _get_val(row, col_le, "")
            length = _get_float(row, col_length, 1000)
            ffs = _get_float(row, col_ffs, 20)
            lanes = _get_int(row, col_lanes, 1)
            link = {
                "name": name, "start": start, "end": end,
                "length": length, "free_flow_speed": ffs,
                "number_of_lanes": lanes,
            }
            jd = _get_float(row, col_jd, -1)
            if jd > 0:
                link["jam_density"] = jd
            links.append(link)
        return {"format": "link_csv", "links": links}

    # ============ 需要 CSV 判定 ============
    DEMAND_ORIG_COLS = ["orig", "origin", "o_zone_id", "from", "source", "出発"]
    DEMAND_DEST_COLS = ["dest", "destination", "d_zone_id", "to", "target", "到着"]
    DEMAND_VOL_COLS = ["flow", "volume", "demand", "rate", "交通量"]

    col_do = _find_col(raw_fields, DEMAND_ORIG_COLS)
    col_dd = _find_col(raw_fields, DEMAND_DEST_COLS)
    col_dv = _find_col(raw_fields, DEMAND_VOL_COLS)

    if col_do and col_dd and col_dv:
        col_tstart = _find_col(raw_fields, ["t_start", "start_time", "time_start"])
        col_tend = _find_col(raw_fields, ["t_end", "end_time", "time_end"])
        demands = []
        for row in reader:
            vol = _get_float(row, col_dv, 0)
            if vol <= 0:
                continue
            demands.append({
                "orig": _get_val(row, col_do, ""),
                "dest": _get_val(row, col_dd, ""),
                "t_start": _get_float(row, col_tstart, 0),
                "t_end": _get_float(row, col_tend, 3600),
                "flow": vol,
            })
        return {"format": "demand_csv", "demands": demands}

    raise ValueError("CSV 形式を認識できません。ノード（name,x,y）、リンク（start,end,length）、または RISU CSV 形式を使用してください。")


def _gmns_to_scenario(
    nodes_csv: str | None,
    links_csv: str | None,
    demand_csv: str | None,
    config_csv: str | None = None,
    tmax: int = 3600,
) -> dict:
    """GMNS CSV ファイル群からシナリオ dict を組み立てる"""

    # 単位設定の読み取り
    length_unit = "meters"
    speed_unit = "kmh"
    if config_csv:
        reader = csv.DictReader(io.StringIO(config_csv))
        for row in reader:
            length_unit = row.get("long_length", "meters").strip().lower()
            speed_unit = row.get("speed", "kmh").strip().lower()
            break

    # 単位変換係数
    length_to_m = 1.0
    if length_unit in ("miles", "mile", "mi"):
        length_to_m = 1609.344
    elif length_unit in ("km", "kilometers"):
        length_to_m = 1000.0
    elif length_unit in ("feet", "ft"):
        length_to_m = 0.3048

    speed_to_ms = 1.0 / 3.6  # km/h → m/s default
    if speed_unit in ("mph",):
        speed_to_ms = 0.44704  # mph → m/s
    elif speed_unit in ("m/s", "ms"):
        speed_to_ms = 1.0

    # ノード
    nodes = []
    node_zone_map = {}  # node_id → zone_id
    if nodes_csv:
        parsed = _parse_csv_scenario(nodes_csv)
        coord_is_latlon = False
        # 座標が緯度経度か判定（-180~180 範囲なら）
        for n in parsed["nodes"]:
            if -180 <= n["x"] <= 180 and -90 <= n["y"] <= 90:
                coord_is_latlon = True
            break
        # 緯度経度→メートル変換
        if coord_is_latlon and parsed["nodes"]:
            ref_y = parsed["nodes"][0]["y"]
            deg_to_m = 111320.0
            cos_lat = math.cos(math.radians(ref_y))
            for n in parsed["nodes"]:
                n["x"] = n["x"] * deg_to_m * cos_lat
                n["y"] = n["y"] * deg_to_m
        nodes = parsed["nodes"]

        # zone マッピング（demand 用）
        reader2 = csv.DictReader(io.StringIO(nodes_csv))
        for row in reader2:
            nid = str(row.get("node_id", "").strip())
            zid = str(row.get("zone_id", "").strip())
            if zid and zid != "0" and zid != "":
                node_zone_map[zid] = nid

    # リンク
    links = []
    if links_csv:
        parsed = _parse_csv_scenario(links_csv)
        for lk in parsed["links"]:
            lk["length"] = lk["length"] * length_to_m
            raw_speed = lk["free_flow_speed"]
            lk["free_flow_speed"] = max(raw_speed * speed_to_ms, 1.0)
        links = parsed["links"]

    # 需要
    demands = []
    if demand_csv:
        parsed = _parse_csv_scenario(demand_csv)
        if parsed["format"] in ("gmns_demand", "demand_csv"):
            for d in parsed["demands"]:
                orig_zone = str(d["orig"])
                dest_zone = str(d["dest"])
                # zone_id → node_id マッピング
                orig_node = node_zone_map.get(orig_zone, orig_zone)
                dest_node = node_zone_map.get(dest_zone, dest_zone)
                # flow がすでに台/秒の場合はそのまま、volume が大きい場合は台/時→台/秒変換
                flow = d.get("flow", 0)
                if flow > 10:  # 10 台/秒超 → 台/時と推定
                    flow = flow / 3600.0
                if flow > 0:
                    demands.append({
                        "orig": orig_node,
                        "dest": dest_node,
                        "t_start": d.get("t_start", 0),
                        "t_end": d.get("t_end", min(tmax * 0.6, 3600)),
                        "flow": round(flow, 6),
                    })

    return {
        "name": "gmns_import",
        "tmax": tmax,
        "deltan": 5,
        "nodes": nodes,
        "links": links,
        "demands": demands,
    }


def _run_osm_import(place: str, distance_m: int = 1000) -> dict:
    """OSM からネットワーク＋建物を取得して UXsim シナリオに変換。
    OSMnx のグラフを直接活用し、道路形状・速度推定・車線数を取得する。
    """
    import osmnx as ox
    from pyproj import Transformer

    # ── ジオコーディング: 自然言語 → (lat, lon) ──
    center = ox.geocode(place)  # (lat, lon)
    center_lat, center_lon = center

    # ── 道路ネットワーク取得 ──
    # drive_service: 自動車通行可能な全道路（service道路・駐車場内道路等を含む）
    G = ox.graph_from_point(center, dist=distance_m, network_type="drive_service")
    G = ox.add_edge_speeds(G)       # highway 種別から速度推定 (speed_kph)

    # メートル座標に投影
    Gp = ox.project_graph(G)
    crs = Gp.graph.get("crs", None)

    # ── 投影座標系の Transformer（建物座標変換用）──
    # Gp.graph["crs"] は pyproj CRS (例: "EPSG:32654")
    transformer = None
    if crs:
        transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    # ── ノード抽出 ──
    scenario_nodes = []
    node_name_map = {}  # osm_node_id -> name
    for node_id, data in Gp.nodes(data=True):
        name = str(node_id)
        node_name_map[node_id] = name
        scenario_nodes.append({
            "name": name,
            "x": round(float(data["x"]), 2),
            "y": round(float(data["y"]), 2),
        })

    # ── リンク抽出（道路形状の中間点も含む） ──
    scenario_links = []
    link_geometries = {}  # link_name -> [[x,y], [x,y], ...]
    link_idx = 0
    for u, v, key, data in Gp.edges(keys=True, data=True):
        u_name = node_name_map.get(u)
        v_name = node_name_map.get(v)
        if u_name is None or v_name is None:
            continue

        link_name = f"link_{link_idx}"
        link_idx += 1

        length = float(data.get("length", 100))
        speed_kph = float(data.get("speed_kph", 30))
        speed_ms = round(speed_kph / 3.6, 2)

        # 車線数
        lanes_raw = data.get("lanes", 1)
        if isinstance(lanes_raw, list):
            lanes_raw = lanes_raw[0]
        try:
            lanes = max(1, int(lanes_raw))
        except (ValueError, TypeError):
            lanes = 1

        scenario_links.append({
            "name": link_name,
            "start": u_name,
            "end": v_name,
            "length": round(length, 2),
            "free_flow_speed": speed_ms,
            "number_of_lanes": lanes,
        })

        # 道路形状（GeoJSON 用の座標列）
        if "geometry" in data:
            coords = [[round(x, 2), round(y, 2)] for x, y in data["geometry"].coords]
        else:
            u_data = Gp.nodes[u]
            v_data = Gp.nodes[v]
            coords = [
                [round(u_data["x"], 2), round(u_data["y"], 2)],
                [round(v_data["x"], 2), round(v_data["y"], 2)],
            ]
        link_geometries[link_name] = coords

    # ── 建物フットプリント取得 ──
    buildings = []
    try:
        bldg_gdf = ox.features_from_point(center, tags={"building": True}, dist=distance_m)
        for _, row in bldg_gdf.iterrows():
            geom = row.geometry
            polys = []
            if geom.geom_type == "Polygon":
                polys = [geom]
            elif geom.geom_type == "MultiPolygon":
                polys = list(geom.geoms)
            for poly in polys:
                if transformer:
                    coords = []
                    for lon, lat in poly.exterior.coords:
                        mx, my = transformer.transform(lon, lat)
                        coords.append([round(mx, 2), round(my, 2)])
                    buildings.append(coords)
                else:
                    # フォールバック: 簡易変換
                    coords = [
                        [round(lon * 111000, 2), round(lat * 111000, 2)]
                        for lon, lat in poly.exterior.coords
                    ]
                    buildings.append(coords)
    except Exception:
        pass  # 建物データなしでも続行

    return {
        "name": f"osm_{place[:30]}",
        "tmax": 3600,
        "deltan": 5,
        "nodes": scenario_nodes,
        "links": scenario_links,
        "demands": [],
        "buildings": buildings,
        "link_geometries": link_geometries,
        "center": {"lat": center_lat, "lon": center_lon},
        "distance_m": distance_m,
        "summary": (
            f"OSM から「{place}」周辺（半径{distance_m}m）の道路ネットワークを取得しました。"
            f"{len(scenario_nodes)} ノード、{len(scenario_links)} リンク、{len(buildings)} 棟の建物。"
        ),
    }


def _generate_osm_demands(nodes: list[dict], links: list[dict], tmax: int = 3600) -> list[dict]:
    """OSM ネットワークの境界ノードから多方向の需要を生成し、全道路を利用させる。

    ネットワーク周縁（境界）のノードを特定し、それら全ペア間に需要を設定する。
    これにより交通がネットワーク全体に分散する。
    """
    if len(nodes) < 2:
        return []

    # ノード座標
    xs = [n["x"] for n in nodes]
    ys = [n["y"] for n in nodes]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)

    # 接続情報: 各ノードの次数（リンク端点として登場する回数）
    degree = {}
    for lk in links:
        degree[lk["start"]] = degree.get(lk["start"], 0) + 1
        degree[lk["end"]] = degree.get(lk["end"], 0) + 1

    # 境界ノード候補: 次数が少ない（行き止まり・端点）ノード、
    # またはネットワーク中心から遠いノード
    node_by_name = {n["name"]: n for n in nodes}
    max_dist = max(math.hypot(n["x"] - cx, n["y"] - cy) for n in nodes) or 1

    # スコア: 中心からの距離が大きい + 次数が小さい → 境界ノードらしい
    scored = []
    for n in nodes:
        d = math.hypot(n["x"] - cx, n["y"] - cy) / max_dist  # 0~1
        deg = degree.get(n["name"], 0)
        # 次数1（行き止まり）=高スコア、次数2=中、次数3以上=低
        deg_score = 1.0 if deg <= 1 else (0.6 if deg == 2 else 0.3)
        scored.append((d * 0.6 + deg_score * 0.4, n["name"]))

    scored.sort(reverse=True)

    # 上位ノードから境界ノードを選択（最大8個、最小4個）
    # 近すぎるノード同士は除外
    min_sep = max_dist * 0.3  # 中心からの最大距離の30%以上離れていること
    boundary_nodes = []
    for _, name in scored:
        n = node_by_name[name]
        too_close = False
        for bn in boundary_nodes:
            bn_node = node_by_name[bn]
            if math.hypot(n["x"] - bn_node["x"], n["y"] - bn_node["y"]) < min_sep:
                too_close = True
                break
        if not too_close:
            boundary_nodes.append(name)
            if len(boundary_nodes) >= 8:
                break

    # 最低4ノード確保できない場合はしきい値を下げて再試行
    if len(boundary_nodes) < 4:
        boundary_nodes = [name for _, name in scored[:min(8, len(scored))]]

    # 全ペア間に需要を生成
    demands = []
    n_boundary = len(boundary_nodes)
    # ペア数に応じてフロー量を調整（多すぎると渋滞しすぎる）
    n_pairs = n_boundary * (n_boundary - 1)
    flow_per_pair = max(0.05, min(0.3, 2.0 / max(n_pairs, 1)))

    for i in range(n_boundary):
        for j in range(n_boundary):
            if i == j:
                continue
            demands.append({
                "orig": boundary_nodes[i],
                "dest": boundary_nodes[j],
                "t_start": 0,
                "t_end": tmax * 0.5,
                "flow": round(flow_per_pair, 3),
            })

    return demands


# ---- GMNS GitHub データセット取得 ----

GMNS_REPO = "HanZhengIntelliTransport/GMNS_Plus_Dataset"
GMNS_API  = f"https://api.github.com/repos/{GMNS_REPO}/contents"
GMNS_RAW  = f"https://raw.githubusercontent.com/{GMNS_REPO}/main"


@app.get("/gmns/datasets")
async def list_gmns_datasets():
    """GMNS リポジトリのデータセット一覧を取得"""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(GMNS_API)
        if r.status_code != 200:
            raise HTTPException(502, detail="GitHub API に接続できません")
        items = r.json()
        datasets = [
            {"name": item["name"], "path": item["path"]}
            for item in items
            if item["type"] == "dir" and not item["name"].startswith(".")
               and item["name"] not in ("Documents", "GMNS_Tools", "Incomplete_Networks")
        ]
    return {"datasets": datasets}


@app.post("/gmns/import")
async def import_gmns(dataset: str = Form(...), tmax: int = Form(3600)):
    """GMNS データセットを GitHub からダウンロードしてシミュレーション実行"""
    async with httpx.AsyncClient(timeout=30) as client:
        # データセット内のファイル一覧を取得
        r = await client.get(f"{GMNS_API}/{dataset}")
        if r.status_code != 200:
            raise HTTPException(404, detail=f"データセット '{dataset}' が見つかりません")

        files = {item["name"].lower(): item["name"] for item in r.json() if item["type"] == "file"}

        # 必要な CSV をダウンロード
        async def fetch_csv(filename):
            actual = files.get(filename)
            if not actual:
                return None
            resp = await client.get(f"{GMNS_RAW}/{dataset}/{actual}")
            return resp.text if resp.status_code == 200 else None

        nodes_csv  = await fetch_csv("node.csv")
        links_csv  = await fetch_csv("link.csv")
        demand_csv = await fetch_csv("demand.csv")
        config_csv = await fetch_csv("config.csv")

    if not nodes_csv or not links_csv:
        raise HTTPException(400, detail=f"データセット '{dataset}' に node.csv / link.csv がありません")

    try:
        scenario = _gmns_to_scenario(nodes_csv, links_csv, demand_csv, config_csv, tmax)
    except Exception as e:
        raise HTTPException(422, detail=f"GMNS パースエラー: {str(e)}")

    if not scenario["links"]:
        raise HTTPException(422, detail="リンクが0件です")

    # 需要がなければダミー生成
    if not scenario["demands"] and len(scenario["nodes"]) >= 2:
        scenario["demands"] = [{
            "orig": scenario["nodes"][0]["name"],
            "dest": scenario["nodes"][-1]["name"],
            "t_start": 0,
            "t_end": 1800,
            "flow": 0.3,
        }]

    scenario["name"] = dataset

    sim_input = SimulationInput(**scenario)
    loop = asyncio.get_event_loop()
    result = await _run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {"type": "gmns", "dataset_id": dataset})

    return {
        "id": sim_id,
        "stats": result["stats"],
        "message": f"GMNS '{dataset}' をインポート（{len(scenario['nodes'])} ノード, {len(scenario['links'])} リンク）",
    }


# ---- ファイルアップロードエンドポイント ----

@app.post("/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    tmax: int = Form(3600),
):
    """CSV / JSON ファイルをアップロードしてシミュレーションを実行"""
    # ファイルの内容を読み取り
    file_contents = {}
    for f in files:
        raw = await f.read()
        content = raw.decode("utf-8-sig")  # BOM 対応
        name = f.filename.lower() if f.filename else ""
        file_contents[name] = content

    # ---- JSON ファイルの場合 ----
    json_files = [n for n in file_contents if n.endswith(".json")]
    if json_files:
        payload = json.loads(file_contents[json_files[0]])
        # 新スキーマ (risu_schema_version 1.0) に対応
        # 形式 1: { "risu_schema_version": "1.0", "scenario": {...}, ... }
        # 形式 2: 旧 RISU JSON: トップレベルに nodes/links/demands
        imported_from = None
        if isinstance(payload, dict) and (
            "risu_schema_version" in payload or "scenario" in payload
        ):
            scenario_dict = payload.get("scenario", payload)
            imported_from = payload.get("sim_id")
        else:
            scenario_dict = payload
        sim_input = SimulationInput(**scenario_dict)
        loop = asyncio.get_event_loop()
        result = await _run_uxsim_async(sim_input)
        sim_id = str(uuid.uuid4())[:8]
        source = {"type": "json", "filename": json_files[0]}
        if imported_from:
            source["imported_from_sim_id"] = imported_from
        _store_sim(sim_id, result, source)
        return {
            "id": sim_id,
            "stats": result["stats"],
            "message": f"JSON ファイルからシミュレーション実行完了"
                       + (f"（再現: {imported_from}）" if imported_from else ""),
        }

    # ---- CSV ファイルの場合 ----
    csv_files = {n: c for n, c in file_contents.items() if n.endswith(".csv")}

    if not csv_files:
        raise HTTPException(400, detail="JSON または CSV ファイルをアップロードしてください")

    # 単一 CSV の場合（RISU 独自形式を試行）
    if len(csv_files) == 1:
        name, content = next(iter(csv_files.items()))
        parsed = _parse_csv_scenario(content)

        if parsed["format"] == "risu_csv":
            scenario = {
                "name": "csv_import",
                "tmax": tmax,
                "deltan": 5,
                "nodes": parsed["nodes"],
                "links": parsed["links"],
                "demands": parsed["demands"],
            }
            sim_input = SimulationInput(**scenario)
            loop = asyncio.get_event_loop()
            result = await _run_uxsim_async(sim_input)
            sim_id = str(uuid.uuid4())[:8]
            _store_sim(sim_id, result, {
                "type": "csv",
                "format": "risu_csv",
                "filename": name,
            })
            return {
                "id": sim_id,
                "stats": result["stats"],
                "message": f"RISU CSV からシミュレーション実行完了",
            }

        # 単一の GMNS node/link ファイルの場合、シミュレーションはせずネットワークだけ返す
        return {
            "id": None,
            "parsed": parsed,
            "message": f"GMNS {parsed['format']} を読み込みました。node.csv + link.csv + demand.csv を一緒にアップロードするとシミュレーションを実行します。",
        }

    # 複数 CSV → GMNS セットとして処理
    nodes_csv = None
    links_csv = None
    demand_csv = None
    config_csv = None
    for name, content in csv_files.items():
        if "node" in name:
            nodes_csv = content
        elif "link" in name:
            links_csv = content
        elif "demand" in name:
            demand_csv = content
        elif "config" in name:
            config_csv = content

    if not nodes_csv and not links_csv:
        raise HTTPException(400, detail="node.csv または link.csv が見つかりません")

    scenario = _gmns_to_scenario(nodes_csv, links_csv, demand_csv, config_csv, tmax)

    if not scenario["links"]:
        raise HTTPException(400, detail="link.csv が見つからないかリンクが0件です")

    # 需要がない場合はダミー需要を生成（可視化だけ可能に）
    if not scenario["demands"] and len(scenario["nodes"]) >= 2:
        scenario["demands"] = [{
            "orig": scenario["nodes"][0]["name"],
            "dest": scenario["nodes"][-1]["name"],
            "t_start": 0,
            "t_end": 1800,
            "flow": 0.3,
        }]

    sim_input = SimulationInput(**scenario)
    loop = asyncio.get_event_loop()
    result = await _run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {
        "type": "gmns",
        "format": "files",
        "filenames": list(csv_files.keys()),
    })
    return {
        "id": sim_id,
        "stats": result["stats"],
        "message": f"GMNS データからシミュレーション実行完了（{len(scenario['nodes'])} ノード, {len(scenario['links'])} リンク）",
    }


@app.post("/import/osm")
async def import_osm(place: str = Form(...), tmax: int = Form(3600),
                     distance_m: int = Form(500)):
    """OpenStreetMap から道路ネットワークを取得"""
    # 入力バリデーション
    place = place.strip()
    if not place or len(place) > 200:
        raise HTTPException(400, detail="地名が不正です（空または長すぎます）")
    if any(c in place for c in "\x00\n\r\t\u2028\u2029"):
        raise HTTPException(400, detail="地名に制御文字が含まれています")
    if distance_m < 50 or distance_m > 5000:
        raise HTTPException(400, detail="半径は 50m〜5000m の範囲で指定してください")
    if tmax < 60 or tmax > MAX_TMAX:
        raise HTTPException(400, detail=f"tmax は 60〜{MAX_TMAX} 秒の範囲で指定してください")

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(executor, _run_osm_import, place, distance_m)
    except Exception as e:
        raise HTTPException(500, detail=f"OSM インポートエラー: {str(e)}")

    # 需要なしでも可視化用にダミー需要を追加してシミュレーション実行
    scenario = dict(result)
    buildings = scenario.pop("buildings", [])
    link_geometries = scenario.pop("link_geometries", {})
    scenario.pop("center", None)
    scenario.pop("distance_m", None)
    scenario.pop("summary", None)
    scenario["tmax"] = tmax

    if not scenario["demands"] and len(scenario["nodes"]) >= 2:
        scenario["demands"] = _generate_osm_demands(
            scenario["nodes"], scenario["links"], tmax
        )

    sim_input = SimulationInput(**scenario)
    sim_result = await _run_uxsim_async(sim_input)
    sim_result["buildings"] = buildings
    _apply_link_geometries(sim_result, link_geometries)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, sim_result, {
        "type": "osm",
        "place": place,
        "distance_m": distance_m,
    })

    return {
        "id": sim_id,
        "stats": sim_result["stats"],
        "message": result.get("summary", ""),
        "scenario": scenario,
    }


# ---- MCP SSE エンドポイント ----

sse_transport = SseServerTransport("/mcp/messages/")

@app.get("/mcp")
async def mcp_sse(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1], mcp_server.create_initialization_options()
        )

@app.post("/mcp/messages/")
async def mcp_messages(request: Request):
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# ---- ヘルスチェック ----
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---- 静的ファイル（UI）----
import os
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=True)
