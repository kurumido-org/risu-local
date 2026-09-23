"""LLM ツールの実行（dispatch_tool_blocks）と各ツールのハンドラ．チャット・MCP の全経路で共通（CLAUDE.md §3.2）．
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import HTTPException

from .aggregate import get_simulation_data
from .importers import run_osm_import
from .results import list_results, results_store, store_sim
from .runtime import executor, log
from .scenario_ops import apply_modifications, expand_run_simulation_args, generate_osm_demands, osm_demand_summary
from .schema import ChatInput, SimulationInput
from .simulation import apply_link_geometries, run_uxsim_async, validate_scenario_size

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "ToolTurnState",
    "collect_tool_results",
    "conversation_context_block",
    "conversation_sim_id",
    "dispatch_tool_blocks",
    "handle_get_network_info",
    "handle_rerun_simulation",
    "last_user_message_text",
]


def last_user_message_text(body) -> str | None:
    """ChatRequest body から最後のユーザーメッセージを抽出（source 記録用）．"""
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


def handle_get_network_info(fn_args: dict) -> tuple[str, bool]:
    """get_network_info ツールの共通ハンドラ．(content, is_error) を返す．

    LLM がネットワークの中身（ノード名・リンク名・構造）を必要な分だけ
    照会するためのツール．常に上限つきで返し，コンテキストを溢れさせない．
    """
    sim_id = str(fn_args.get("sim_id", "")).strip()
    if sim_id not in results_store:
        known = list(results_store.keys())[-5:]
        return (f"sim_id '{sim_id}' が見つかりません．有効な sim_id: {known}", True)
    sc = results_store[sim_id].get("_scenario") or {}
    nodes = sc.get("nodes") or []
    links = sc.get("links") or []
    demands = sc.get("demands") or []

    include = str(fn_args.get("include", "summary")).lower()
    limit = max(1, min(int(fn_args.get("limit", 50) or 50), 200))
    offset = max(0, int(fn_args.get("offset", 0) or 0))
    name_contains = fn_args.get("name_contains")

    # 次数（ノードの接続本数）
    degree: dict[str, int] = {}
    for lk in links:
        degree[lk["start"]] = degree.get(lk["start"], 0) + 1
        degree[lk["end"]] = degree.get(lk["end"], 0) + 1

    out: dict = {"sim_id": sim_id,
                 "total": {"nodes": len(nodes), "links": len(links), "demands": len(demands)},
                 "tmax": sc.get("tmax")}

    if include == "summary":
        xs = [n["x"] for n in nodes]; ys = [n["y"] for n in nodes]
        lengths = [lk["length"] for lk in links]
        top_deg = sorted(nodes, key=lambda n: -degree.get(n["name"], 0))[:10]
        signal_nodes = [n["name"] for n in nodes if n.get("signal")]
        out["bbox"] = ({"x_min": min(xs), "x_max": max(xs),
                        "y_min": min(ys), "y_max": max(ys)} if xs else None)
        out["link_length"] = ({"min": round(min(lengths), 1), "max": round(max(lengths), 1),
                               "avg": round(sum(lengths) / len(lengths), 1)} if lengths else None)
        out["top_degree_nodes"] = [
            {"name": n["name"], "degree": degree.get(n["name"], 0)} for n in top_deg]
        out["signal_nodes"] = signal_nodes[:20]
        out["sample_node_names"] = [n["name"] for n in nodes[:20]]
        out["sample_link_names"] = [lk["name"] for lk in links[:20]]
        out["hint"] = ("詳細は include='nodes'/'links'/'demands'（limit/offset/name_contains 指定可）．"
                       "ランダム OD は rerun_simulation の generate_demands アクションが使える．")
    elif include == "nodes":
        items = nodes
        if name_contains:
            items = [n for n in items if str(name_contains) in str(n["name"])]
        out["matched"] = len(items)
        out["nodes"] = [
            {"name": n["name"], "x": n["x"], "y": n["y"],
             "degree": degree.get(n["name"], 0),
             **({"signal": n["signal"]} if n.get("signal") else {}),
             **({"flow_capacity": n["flow_capacity"]} if n.get("flow_capacity") is not None else {})}
            for n in items[offset:offset + limit]
        ]
        if len(items) > offset + limit:
            out["note"] = f"{offset + limit} 件目まで表示（全 {len(items)} 件）．offset で続きを取得"
    elif include == "links":
        items = links
        if name_contains:
            items = [l for l in items if str(name_contains) in str(l["name"])]
        out["matched"] = len(items)
        out["links"] = [
            {k: v for k, v in {
                "name": l["name"], "start": l["start"], "end": l["end"],
                "length": l["length"], "free_flow_speed": l.get("free_flow_speed"),
                "number_of_lanes": l.get("number_of_lanes"),
                "capacity": l.get("capacity"), "signal_group": l.get("signal_group"),
            }.items() if v is not None}
            for l in items[offset:offset + limit]
        ]
        if len(items) > offset + limit:
            out["note"] = f"{offset + limit} 件目まで表示（全 {len(items)} 件）．offset で続きを取得"
    elif include == "demands":
        out["matched"] = len(demands)
        out["demands"] = demands[offset:offset + limit]
        if len(demands) > offset + limit:
            out["note"] = f"{offset + limit} 件目まで表示（全 {len(demands)} 件）"
    else:
        return (f"include は summary / nodes / links / demands のいずれか（指定: {include}）", True)

    return (json.dumps(out, ensure_ascii=False), False)


async def handle_rerun_simulation(fn_args: dict, body, *, via: str = "chat"
                                   ) -> tuple[str, str | None, bool]:
    """rerun_simulation ツールの共通ハンドラ（stream / sync 両系統から使用）．

    戻り値: (tool_result content, 新 sim_id または None, is_error)
    """
    base_id = str(fn_args.get("base_sim_id", "")).strip()
    if base_id not in results_store:
        known = list(results_store.keys())[-5:]
        return (f"base_sim_id '{base_id}' が見つかりません．有効な sim_id: {known}", None, True)
    base = results_store[base_id]
    base_scenario = base.get("_scenario")
    if not base_scenario:
        return (f"sim_id '{base_id}' にはシナリオが保存されていません．", None, True)

    try:
        mods = fn_args.get("modifications") or []
        scenario, applied = apply_modifications(base_scenario, mods)
        if fn_args.get("tmax"):
            scenario["tmax"] = int(fn_args["tmax"])
            applied.append(f"tmax={scenario['tmax']}s")
        if fn_args.get("name"):
            scenario["name"] = str(fn_args["name"])
        si = SimulationInput(**scenario)
        validate_scenario_size(si)
        result = await run_uxsim_async(si)

        # OSM 由来の道路形状（曲線座標）を名前一致で引き継ぐ
        base_geom = {}
        for f in (base.get("geojson") or {}).get("features", []):
            coords = f.get("geometry", {}).get("coordinates")
            if coords and len(coords) > 2:
                base_geom[f["properties"]["name"]] = coords
        if base_geom:
            apply_link_geometries(result, base_geom)

        new_id = str(uuid.uuid4())[:8]
        store_sim(new_id, result, {
            "type": "llm",
            "via": via,
            "llm_backend": "claude",
            "tool": "rerun_simulation",
            "base_sim_id": base_id,
            "llm_user_message": last_user_message_text(body),
        })
        content = json.dumps({
            **result["stats"],
            "sim_id": new_id,
            "base_sim_id": base_id,
            "applied": applied,
            "network": {"nodes": len(scenario["nodes"]), "links": len(scenario["links"]),
                        "demands": len(scenario["demands"])},
        }, ensure_ascii=False)
        return (content, new_id, False)
    except HTTPException as e:
        return (f"再実行エラー: {e.detail}", None, True)
    except (ValueError, TypeError) as e:
        return (f"modifications が不正です: {e}\n修正して再度 rerun_simulation を呼んでください．", None, True)
    except Exception as e:
        return (f"再実行エラー: {e.__class__.__name__}: {e}", None, True)


async def _handle_run_simulation(fn_args: dict, body, *, source_round: str | None = None,
                                 via: str = "chat") -> tuple[str, str | None, bool]:
    """run_simulation ツールの共通ハンドラ（stream / sync 両系統から使用）．

    戻り値: (tool_result content, 新 sim_id または None, is_error)
    """
    try:
        scenario, info = expand_run_simulation_args(fn_args)
        sim_input = SimulationInput(**scenario)
        result = await run_uxsim_async(sim_input)
        sim_id = str(uuid.uuid4())[:8]
        src = {
            "type": "llm",
            "via": via,
            "llm_backend": "claude",
            "tool": "run_simulation",
            "llm_user_message": last_user_message_text(body),
        }
        if source_round:
            src["round"] = source_round
        store_sim(sim_id, result, src)
        payload = {**result["stats"], "sim_id": sim_id,
                   "network": {"nodes": len(scenario["nodes"]), "links": len(scenario["links"]),
                               "demands": len(scenario["demands"])}}
        payload.update(info)
        return (json.dumps(payload, ensure_ascii=False), sim_id, False)
    except HTTPException as e:
        return (f"シミュレーション実行エラー: {e.detail}\n入力を修正して再度 run_simulation を呼んでください．",
                None, True)
    except Exception as e:
        return (f"シミュレーション実行エラー: {str(e)}\n入力を修正して再度 run_simulation を呼んでください．",
                None, True)


def conversation_sim_id(body: ChatInput) -> str | None:
    """会話に紐づく有効なシミュレーションIDを返す（なければ None）"""
    sim_id = (getattr(body, "last_sim_id", None) or "").strip()
    return sim_id if sim_id and sim_id in results_store else None


def conversation_context_block(body: ChatInput) -> str:
    """システムプロンプト末尾に付けるコンテキスト注入文字列を生成．

    会話に紐づく sim（フロントが /chat で送る last_sim_id）だけを対象にする．
    results_store のグローバル最新を使うと，別会話や CSV アップロードで作られた
    無関係なシナリオを LLM が rerun_simulation で流用してしまうため．
    """
    sim_id = conversation_sim_id(body)
    if not sim_id:
        return ""
    link_names = [f["properties"]["name"] for f in results_store[sim_id].get("geojson", {}).get("features", [])]
    _sc = results_store[sim_id].get("_scenario") or {}
    _sizes = (f"{len(_sc.get('nodes') or [])} ノード / {len(_sc.get('links') or [])} リンク / "
              f"{len(_sc.get('demands') or [])} 需要, tmax={_sc.get('tmax', '?')}s")
    _more = f"（他 {len(link_names) - 20} 本）" if len(link_names) > 20 else ""
    # 需要の出所（OSM 取込の自動生成など）．仮定の需要を実測と取り違えて説明しないための情報
    _src = (results_store[sim_id].get("_meta") or {}).get("source") or {}
    _demand = _src.get("demand") if isinstance(_src.get("demand"), dict) else None
    _demand_line = f"\n需要の出所: {_demand['note']}" if _demand and _demand.get("note") else ""
    return f"""

【現在のコンテキスト】
この会話のシミュレーションID: {sim_id}
ネットワーク規模: {_sizes}{_demand_line}
リンク名の例: {', '.join(link_names[:20])}{_more}
この結果への修正・再実行・比較は rerun_simulation(base_sim_id="{sim_id}") を使うこと．
これ以外の依頼（新しいネットワークの設計）は run_simulation でゼロから作ること．"""


# ──────────────────────────────────────────────
# ツール実行の共通ディスパッチ
# ──────────────────────────────────────────────
class ToolTurnState:
    """1 ターン分のツール実行で持ち回る状態．

    sim_id            このターンで最後に作られたシミュレーション ID
    last_data_sim_id  get_simulation_data が最後に集計した sim_id（$data 解決の既定）
    sim_data_cache    このターンで取得した集計データ（チャートの $data 参照解決用）
    """

    __slots__ = ("body", "sim_id", "last_data_sim_id", "sim_data_cache", "via")

    def __init__(self, body: ChatInput | None, via: str = "chat"):
        self.body = body            # MCP 経由では None（会話コンテキストが無い）
        self.via = via              # 保存メタの source.via（"chat" / "mcp"）
        self.sim_id: str | None = None
        self.last_data_sim_id: str | None = None
        self.sim_data_cache: dict[str, dict] = {}


def _tool_result(tool_use_id: str, content: str, is_err: bool = False) -> dict:
    tr = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_err:
        tr["is_error"] = True
    return tr


async def dispatch_tool_blocks(tool_blocks, state: ToolTurnState, *, follow_up: bool = False):
    """tool_use ブロック群を実行する非同期ジェネレータ．

    ストリーミング経路（chat_claude_stream）と同期経路（_chat_claude）で
    **同じコードを通す**ためにここへ集約している．以前は初回ラウンドと追加ラウンド ×
    2 経路の計 4 箇所に同じ dispatch があり，片方だけ直すと挙動がずれる状態だった．

    yield するもの:
        ("progress", "メッセージ")   進捗．SSE 経路だけが転送し，同期経路は捨てる
        ("results", [tool_result])   最後に 1 回だけ．API へ返す tool_result のリスト

    follow_up=True は 2 ラウンド目以降．進捗の文言と，保存メタの "round" が変わる．

    tool_use には**必ず対応する tool_result を返すこと**（Anthropic API の要求）．
    未知のツール名でも結果を積む．
    """
    results = []

    for tb in tool_blocks:
        name = tb.name
        args = tb.input

        if name == "run_simulation":
            yield ("progress", "追加シミュレーションを実行中..." if follow_up
                   else "Step 2/3: UXsim でシミュレーション実行中...")
            content, new_sim_id, is_err = await _handle_run_simulation(
                args, state.body, source_round="follow_up" if follow_up else None, via=state.via)
            if new_sim_id:
                state.sim_id = new_sim_id
            results.append(_tool_result(tb.id, content, is_err))

        elif name == "rerun_simulation":
            yield ("progress", "修正を適用して再実行中..." if follow_up
                   else "Step 2/3: 修正を適用して再実行中...")
            content, new_sim_id, is_err = await handle_rerun_simulation(args, state.body, via=state.via)
            if new_sim_id:
                state.sim_id = new_sim_id
            results.append(_tool_result(tb.id, content, is_err))

        elif name == "get_network_info":
            if not follow_up:
                yield ("progress", "ネットワーク情報を照会中...")
            content, is_err = handle_get_network_info(args)
            results.append(_tool_result(tb.id, content, is_err))

        elif name == "import_osm_network":
            place = args.get("place", "")
            dist = args.get("distance_m", 500)
            osm_tmax = args.get("tmax", 3600)
            road_types = args.get("road_types", "drive")
            yield ("progress",
                   f"OpenStreetMap から「{place}」のデータを取得中..." if follow_up
                   else f"Step 2/3: OpenStreetMap から「{place}」周辺のデータを取得中...")
            try:
                loop = asyncio.get_event_loop()
                osm_result = await loop.run_in_executor(
                    executor, run_osm_import, place, dist, road_types
                )
                scenario = dict(osm_result)
                link_geometries = scenario.pop("link_geometries", {})
                scenario.pop("center", None)
                scenario.pop("distance_m", None)
                summary = scenario.pop("summary", "")
                scenario["tmax"] = osm_tmax

                if not scenario["demands"] and len(scenario["nodes"]) >= 2:
                    scenario["demands"] = generate_osm_demands(
                        scenario["nodes"], scenario["links"], osm_tmax
                    )
                    demand_info = osm_demand_summary(scenario["demands"], osm_tmax)
                else:
                    demand_info = {"method": "provided", "note": "取込データに含まれていた需要をそのまま使用"}

                if not follow_up:
                    yield ("progress", "Step 2/3: UXsim でシミュレーション実行中...")
                result = await run_uxsim_async(SimulationInput(**scenario))
                apply_link_geometries(result, link_geometries)
                new_id = str(uuid.uuid4())[:8]
                meta = {
                    "type": "osm",
                    "via": state.via,
                    "llm_backend": "claude",
                    "place": place,
                    "distance_m": dist,
                    "demand": demand_info,     # 需要の出所（仮定）．画面の前提表示と LLM のコンテキストに使う
                    "llm_user_message": last_user_message_text(state.body),
                }
                if follow_up:
                    meta["round"] = "follow_up"
                store_sim(new_id, result, meta)
                state.sim_id = new_id

                results.append(_tool_result(tb.id, json.dumps({
                    **result["stats"],
                    "sim_id": new_id,
                    "summary": summary,
                    "node_count": len(scenario["nodes"]),
                    "link_count": len(scenario["links"]),
                    "demand_assumption": demand_info["note"],
                }, ensure_ascii=False)))
            except Exception as e:
                log.exception("unexpected error")
                results.append(_tool_result(tb.id, f"OSMインポートエラー: {e!s}", True))

        elif name == "list_simulations":
            rows = list_results(min(int(args.get("limit") or 20), 100))
            results.append(_tool_result(tb.id, json.dumps(rows, ensure_ascii=False)))

        elif name == "get_simulation_data":
            yield ("progress", "チャートデータを取得中..." if follow_up else "データを集計中...")
            # sim_id が空または存在しない場合，このターンで実行した sim →
            # 会話に紐づく sim の順でフォールバック（グローバル最新は使わない）
            req_sim_id = args.get("sim_id", "")
            if not req_sim_id or req_sim_id not in results_store:
                req_sim_id = state.sim_id or conversation_sim_id(state.body) or ""
            sd = get_simulation_data(req_sim_id,
                                      points=args.get("points") or 30,
                                      max_links=args.get("max_links", 20))
            if sd:
                state.sim_data_cache[req_sim_id] = sd
                state.last_data_sim_id = req_sim_id
                results.append(_tool_result(tb.id, json.dumps(sd, ensure_ascii=False)))
            else:
                results.append(_tool_result(
                    tb.id,
                    "データが見つかりません．まず run_simulation でシミュレーションを実行してください．"))

        else:
            results.append(_tool_result(tb.id, f"未知のツール: {name}"))

    yield ("results", results)


async def collect_tool_results(tool_blocks, state: ToolTurnState, *, follow_up: bool = False):
    """dispatch_tool_blocks の進捗を捨てて tool_result だけ取る（同期経路用）．"""
    results = []
    async for kind, payload in dispatch_tool_blocks(tool_blocks, state, follow_up=follow_up):
        if kind == "results":
            results = payload
    return results
