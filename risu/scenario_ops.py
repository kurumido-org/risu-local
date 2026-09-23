"""シナリオの生成と差分適用: rerun_simulation の modification，grid テンプレート，OD 需要の自動生成．
"""

from __future__ import annotations

import copy as _copy
import math

from .simulation import MAX_NODES

# ──────────────────────────────────────────────
# シナリオパッチエンジン（rerun_simulation 用）
# 大規模ネットワークを LLM に往復させず，保存済みシナリオへの
# 「小さな差分命令」だけで修正・再実行できるようにする．
# ──────────────────────────────────────────────


def _mod_match_indices(items: list[dict], mod: dict, kind: str) -> list[int]:
    """modification の対象指定（names / name_contains / all）から index 群を返す"""
    if mod.get("all"):
        return list(range(len(items)))
    if "names" in mod:
        wanted = list(mod["names"]) if isinstance(mod["names"], list) else [mod["names"]]
        wanted_set = set(map(str, wanted))
        idxs = [i for i, it in enumerate(items) if str(it.get("name")) in wanted_set]
        found = {str(items[i]["name"]) for i in idxs}
        missing = sorted(wanted_set - found)
        if missing:
            raise ValueError(f"{kind} が見つかりません: {missing}")
        return idxs
    if "name_contains" in mod:
        sub = str(mod["name_contains"])
        idxs = [i for i, it in enumerate(items) if sub in str(it.get("name", ""))]
        if not idxs:
            raise ValueError(f"名前に「{sub}」を含む {kind} がありません")
        return idxs
    raise ValueError(f"{kind} の対象指定が必要です（names / name_contains / all のいずれか）")


_LINK_SET_FIELDS = {"capacity", "free_flow_speed", "number_of_lanes",
                    "jam_density", "signal_group", "length"}
_NODE_SET_FIELDS = {"signal", "flow_capacity", "x", "y"}
_SCENARIO_PARAM_FIELDS = {"tmax", "deltan", "reaction_time", "random_seed"}
_DEMAND_SET_FIELDS = {"flow", "t_start", "t_end"}


def _apply_modifications(scenario: dict, mods: list[dict]) -> tuple[dict, list[str]]:
    """保存済みシナリオ dict に modification 命令列を適用する．

    戻り値: (新しいシナリオ dict, 適用ログ)．不正な命令は ValueError．
    """
    sc = _copy.deepcopy(scenario)
    sc.setdefault("nodes", []); sc.setdefault("links", []); sc.setdefault("demands", [])
    applied: list[str] = []

    for mi, mod in enumerate(mods):
        if not isinstance(mod, dict) or "action" not in mod:
            raise ValueError(f"modifications[{mi}]: action が必要です")
        action = mod["action"]

        if action == "update_links":
            idxs = _mod_match_indices(sc["links"], mod, "リンク")
            sets = mod.get("set") or {}
            bad = set(sets) - _LINK_SET_FIELDS
            if bad:
                raise ValueError(f"update_links の set に未対応のフィールド: {sorted(bad)}（対応: {sorted(_LINK_SET_FIELDS)}）")
            if not sets:
                raise ValueError("update_links には set が必要です")
            for i in idxs:
                sc["links"][i].update(sets)
            applied.append(f"update_links: {len(idxs)} 本に {sorted(sets)} を設定")

        elif action == "update_nodes":
            idxs = _mod_match_indices(sc["nodes"], mod, "ノード")
            sets = mod.get("set") or {}
            bad = set(sets) - _NODE_SET_FIELDS
            if bad:
                raise ValueError(f"update_nodes の set に未対応のフィールド: {sorted(bad)}（対応: {sorted(_NODE_SET_FIELDS)}）")
            if not sets:
                raise ValueError("update_nodes には set が必要です")
            for i in idxs:
                sc["nodes"][i].update(sets)
            applied.append(f"update_nodes: {len(idxs)} 個に {sorted(sets)} を設定")

        elif action == "update_demands":
            orig, dest = mod.get("orig"), mod.get("dest")
            idxs = [i for i, d in enumerate(sc["demands"])
                    if (orig is None or d.get("orig") == orig)
                    and (dest is None or d.get("dest") == dest)]
            if not idxs:
                raise ValueError(f"該当する需要がありません（orig={orig}, dest={dest}）")
            sets = mod.get("set") or {}
            bad = set(sets) - _DEMAND_SET_FIELDS
            if bad:
                raise ValueError(f"update_demands の set に未対応のフィールド: {sorted(bad)}")
            scale = mod.get("scale_flow")
            for i in idxs:
                sc["demands"][i].update(sets)
                if scale is not None:
                    sc["demands"][i]["flow"] = round(float(sc["demands"][i]["flow"]) * float(scale), 4)
            desc = []
            if sets: desc.append(f"{sorted(sets)} を設定")
            if scale is not None: desc.append(f"flow を {scale} 倍")
            applied.append(f"update_demands: {len(idxs)} 件に " + "，".join(desc))

        elif action == "add_node":
            node = mod.get("node")
            if not isinstance(node, dict) or "name" not in node:
                raise ValueError("add_node には node（name, x, y ...）が必要です")
            if any(n.get("name") == node["name"] for n in sc["nodes"]):
                raise ValueError(f"ノード名が重複: {node['name']}")
            sc["nodes"].append(node)
            applied.append(f"add_node: {node['name']}")

        elif action == "add_link":
            link = mod.get("link")
            if not isinstance(link, dict) or "name" not in link:
                raise ValueError("add_link には link（name, start, end, length ...）が必要です")
            if any(l.get("name") == link["name"] for l in sc["links"]):
                raise ValueError(f"リンク名が重複: {link['name']}")
            sc["links"].append(link)
            applied.append(f"add_link: {link['name']}")

        elif action == "add_demand":
            demand = mod.get("demand")
            if not isinstance(demand, dict):
                raise ValueError("add_demand には demand（orig, dest, t_start, t_end, flow）が必要です")
            sc["demands"].append(demand)
            applied.append(f"add_demand: {demand.get('orig')}→{demand.get('dest')}")

        elif action == "remove_links":
            idxs = set(_mod_match_indices(sc["links"], mod, "リンク"))
            sc["links"] = [l for i, l in enumerate(sc["links"]) if i not in idxs]
            applied.append(f"remove_links: {len(idxs)} 本を削除")

        elif action == "remove_nodes":
            idxs = set(_mod_match_indices(sc["nodes"], mod, "ノード"))
            names = {sc["nodes"][i]["name"] for i in idxs}
            sc["nodes"] = [n for i, n in enumerate(sc["nodes"]) if i not in idxs]
            n_links_before = len(sc["links"]); n_dem_before = len(sc["demands"])
            sc["links"] = [l for l in sc["links"] if l.get("start") not in names and l.get("end") not in names]
            sc["demands"] = [d for d in sc["demands"] if d.get("orig") not in names and d.get("dest") not in names]
            applied.append(
                f"remove_nodes: {len(idxs)} 個を削除（接続リンク {n_links_before - len(sc['links'])} 本，"
                f"需要 {n_dem_before - len(sc['demands'])} 件も削除）")

        elif action == "remove_demands":
            orig, dest = mod.get("orig"), mod.get("dest")
            if orig is None and dest is None and not mod.get("all"):
                raise ValueError("remove_demands には orig / dest / all のいずれかが必要です")
            before = len(sc["demands"])
            sc["demands"] = [d for d in sc["demands"]
                             if not ((orig is None or d.get("orig") == orig)
                                     and (dest is None or d.get("dest") == dest))] \
                if not mod.get("all") else []
            applied.append(f"remove_demands: {before - len(sc['demands'])} 件を削除")

        elif action == "set_tmax":
            sc["tmax"] = int(mod.get("tmax", sc.get("tmax", 3600)))
            applied.append(f"set_tmax: {sc['tmax']}s")

        elif action == "set_params":
            # シナリオ全体のパラメータ（tmax / deltan / reaction_time / random_seed）．
            # 値の妥当性は後段の SimulationInput で検証される．
            sets = {k: mod[k] for k in _SCENARIO_PARAM_FIELDS if k in mod}
            bad = set(mod) - _SCENARIO_PARAM_FIELDS - {"action"}
            if bad:
                raise ValueError(f"set_params に未対応のフィールド: {sorted(bad)}"
                                 f"（対応: {sorted(_SCENARIO_PARAM_FIELDS)}）")
            if not sets:
                raise ValueError(f"set_params には {sorted(_SCENARIO_PARAM_FIELDS)} のいずれかが必要です")
            for k, v in sets.items():
                if v is None:
                    sc.pop(k, None)   # None = 既定に戻す（reaction_time / random_seed）
                else:
                    sc[k] = v
            applied.append("set_params: " + ", ".join(f"{k}={v}" for k, v in sets.items()))

        elif action == "generate_demands":
            # サーバー側で OD 需要を自動生成する．LLM がノード名を列挙する
            # 必要がないため，大規模ネットワークでも「ランダムに OD を生成」の
            # ような指示に対応できる．
            strategy = mod.get("strategy", "random")
            if len(sc["nodes"]) < 2:
                raise ValueError("generate_demands にはノードが 2 つ以上必要です")
            if mod.get("clear_existing"):
                n_cleared = len(sc["demands"])
                sc["demands"] = []
            else:
                n_cleared = None
            try:
                new_demands = _generate_demands_spec(sc["nodes"], sc["links"], mod,
                                                     int(sc.get("tmax", 3600)))
            except ValueError as e:
                raise ValueError(f"generate_demands の {e}") from e
            sc["demands"].extend(new_demands)
            msg = f"generate_demands({strategy}): {len(new_demands)} 件を生成"
            if n_cleared is not None:
                msg += f"（既存 {n_cleared} 件はクリア）"
            applied.append(msg)

        elif action == "shift_demands":
            # 需要パターンの時間シフト（時差出勤・ピークカット）．
            # 時間帯 [t_from, t_to) に入る需要行の flow を fraction だけ減らし，
            # 同じ OD で t_start/t_end を shift_s ずらした行を追加する．
            # 例: 7-9 時の 30% を 1 時間前倒し → {"t_from":3600,"t_to":10800,"fraction":0.3,"shift_s":-3600}
            t_from = float(mod.get("t_from", 0)); t_to = float(mod.get("t_to", sc.get("tmax", 3600)))
            frac = float(mod.get("fraction", 0.3)); shift_s = float(mod.get("shift_s", -3600))
            if not (0 < frac <= 1):
                raise ValueError("shift_demands の fraction は 0 より大きく 1 以下")
            orig, dest = mod.get("orig"), mod.get("dest")
            moved = 0.0; n_rows = 0; new_rows = []
            for d in sc["demands"]:
                if orig is not None and d.get("orig") != orig: continue
                if dest is not None and d.get("dest") != dest: continue
                if float(d["t_start"]) < t_from or float(d["t_end"]) > t_to: continue
                part = round(float(d["flow"]) * frac, 4)
                if part <= 0: continue
                d["flow"] = round(float(d["flow"]) - part, 4)
                ns, ne = float(d["t_start"]) + shift_s, float(d["t_end"]) + shift_s
                if ns < 0:
                    raise ValueError(f"shift_demands: シフト後の開始時刻が負になります（{ns}s）")
                new_rows.append({"orig": d["orig"], "dest": d["dest"], "t_start": ns, "t_end": ne, "flow": part})
                moved += part * (float(d["t_end"]) - float(d["t_start"])); n_rows += 1
            if n_rows == 0:
                raise ValueError(f"shift_demands: 時間帯 [{t_from}, {t_to}) に該当する需要がありません")
            sc["demands"].extend(new_rows)
            applied.append(f"shift_demands: {n_rows} 行の {frac:.0%}（約 {moved:,.0f} 台）を {shift_s:+.0f}s 移動")

        else:
            raise ValueError(
                f"未対応の action: {action}（対応: update_links / update_nodes / update_demands / "
                "add_node / add_link / add_demand / remove_links / remove_nodes / remove_demands / "
                "generate_demands / shift_demands / set_tmax）")

    return sc, applied


# ──────────────────────────────────────────────
# run_simulation の入力展開（grid テンプレート / 需要自動生成）
#
# 「10×10 のグリッド」を LLM がノード 100 個・リンク 360 本を列挙して生成すると
# 出力トークン 2 万以上・数十秒かかる．テンプレートを渡すとサーバー側で展開する．
# ──────────────────────────────────────────────
def _expand_grid(spec: dict) -> tuple[list[dict], list[dict], dict]:
    nx = max(2, int(spec.get("nx", 3)))
    ny = max(2, int(spec.get("ny", nx)))
    spacing = float(spec.get("spacing", 500))
    prefix = str(spec.get("name_prefix", "n"))
    bidir = bool(spec.get("bidirectional", True))
    if nx * ny > MAX_NODES:
        raise ValueError(f"grid が大きすぎます: {nx}x{ny} > {MAX_NODES} ノード")
    link_attrs = {k: spec[k] for k in ("free_flow_speed", "jam_density", "number_of_lanes", "capacity")
                  if spec.get(k) is not None}
    nodes = [{"name": f"{prefix}{i}_{j}", "x": i * spacing, "y": j * spacing}
             for i in range(nx) for j in range(ny)]
    links = []
    def add(a, b):
        links.append({"name": f"{a}-{b}", "start": a, "end": b, "length": spacing, **link_attrs})
    for i in range(nx):
        for j in range(ny):
            a = f"{prefix}{i}_{j}"
            if i + 1 < nx:
                b = f"{prefix}{i+1}_{j}"; add(a, b)
                if bidir: add(b, a)
            if j + 1 < ny:
                b = f"{prefix}{i}_{j+1}"; add(a, b)
                if bidir: add(b, a)
    info = {"type": "grid", "nx": nx, "ny": ny, "spacing": spacing,
            "node_naming": f"{prefix}{{i}}_{{j}} (i=0..{nx-1}, j=0..{ny-1}, x=i*{spacing:g}, y=j*{spacing:g})",
            "link_naming": f"{prefix}0_0-{prefix}1_0 のように 始点名-終点名"}
    return nodes, links, info


def _generate_demands_spec(nodes: list[dict], links: list[dict], spec: dict, tmax: int) -> list[dict]:
    """OD 需要をサーバー側で生成する（rerun の generate_demands と run_simulation の auto_demands 共用）"""
    import random as _random
    strategy = spec.get("strategy", "random")
    if len(nodes) < 2:
        raise ValueError("需要の自動生成にはノードが 2 つ以上必要です")
    t_start = float(spec.get("t_start", 0))
    t_end = float(spec.get("t_end", tmax * 0.5))
    if strategy == "boundary":
        # ネットワーク周縁ノード全ペア（OSM インポートと同じロジック）
        new_demands = _generate_osm_demands(nodes, links, tmax)
        if spec.get("flow_per_pair") is not None:
            for d in new_demands:
                d["flow"] = float(spec["flow_per_pair"])
        for d in new_demands:
            d["t_start"] = t_start
            d["t_end"] = t_end
    elif strategy == "random":
        n_pairs = max(1, int(spec.get("n_pairs", 10)))
        if spec.get("flow_per_pair") is not None:
            flow = float(spec["flow_per_pair"])
        elif spec.get("flow_total") is not None:
            flow = round(float(spec["flow_total"]) / n_pairs, 4)
        else:
            flow = 0.2
        rng = _random.Random(spec.get("seed"))
        names = [n["name"] for n in nodes]
        new_demands = []
        for _ in range(n_pairs):
            orig, dest = rng.sample(names, 2)
            new_demands.append({"orig": orig, "dest": dest,
                                "t_start": t_start, "t_end": t_end, "flow": flow})
    else:
        raise ValueError(f"strategy は random / boundary（指定: {strategy}）")
    return new_demands


def _expand_run_simulation_args(fn_args: dict) -> tuple[dict, dict]:
    """run_simulation の tool 入力を SimulationInput 用 dict に展開する．

    戻り値: (scenario dict, 展開情報 dict — tool_result に含めて LLM に命名規則を伝える)
    """
    args = dict(fn_args)
    info = {}
    grid = args.pop("grid", None)
    auto = args.pop("auto_demands", None)
    nodes = list(args.get("nodes") or [])
    links = list(args.get("links") or [])
    if grid:
        g_nodes, g_links, g_info = _expand_grid(grid)
        nodes = g_nodes + nodes
        links = g_links + links
        info["grid"] = g_info
    if not nodes or not links:
        raise ValueError("nodes / links を指定するか，grid テンプレートを使ってください")
    demands = list(args.get("demands") or [])
    if auto:
        tmax = int(args.get("tmax") or 3600)
        gen = _generate_demands_spec(nodes, links, auto, tmax)
        demands = demands + gen
        info["auto_demands"] = {"strategy": auto.get("strategy", "random"), "generated": len(gen)}
    if not demands:
        raise ValueError("demands を指定するか auto_demands で自動生成してください")
    args["nodes"], args["links"], args["demands"] = nodes, links, demands
    return args, info


def _generate_osm_demands(nodes: list[dict], links: list[dict], tmax: int = 3600) -> list[dict]:
    """OSM ネットワークの境界ノードから多方向の需要を生成し，全道路を利用させる．

    ネットワーク周縁（境界）のノードを特定し，それら全ペア間に需要を設定する．
    これにより交通がネットワーク全体に分散する．
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

    # 境界ノード候補: 次数が少ない（行き止まり・端点）ノード，
    # またはネットワーク中心から遠いノード
    node_by_name = {n["name"]: n for n in nodes}
    max_dist = max(math.hypot(n["x"] - cx, n["y"] - cy) for n in nodes) or 1

    # スコア: 中心からの距離が大きい + 次数が小さい → 境界ノードらしい
    scored = []
    for n in nodes:
        d = math.hypot(n["x"] - cx, n["y"] - cy) / max_dist  # 0~1
        deg = degree.get(n["name"], 0)
        # 次数1（行き止まり）=高スコア，次数2=中，次数3以上=低
        deg_score = 1.0 if deg <= 1 else (0.6 if deg == 2 else 0.3)
        scored.append((d * 0.6 + deg_score * 0.4, n["name"]))

    scored.sort(reverse=True)

    # 上位ノードから境界ノードを選択（最大8個，最小4個）
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
