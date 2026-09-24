"""外部形式の取込: RISU CSV / GMNS / OpenStreetMap（osmnx）．
"""

from __future__ import annotations

import csv
import io
import math
import os

from .runtime import log

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "GMNS_API",
    "GMNS_RAW",
    "MIN_LINK_LENGTH_M",
    "OSM_ROAD_PRESETS",
    "clamp_link_lengths",
    "gmns_to_scenario",
    "parse_csv_scenario",
    "run_osm_import",
]

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


# 取込データに含まれる長さ 0（または負）のリンクを補正する下限（m）．
# OSM では同一座標のノード間の辺，GMNS / CSV では欠損や 0 が入ることがある．
# SimulationInput は length > 0 を要求するので，取込側でここに切り上げて通す
# （落とすとネットワークが分断されるので長さだけ補正し，補正したリンク名を返す）．
MIN_LINK_LENGTH_M = 1.0


def clamp_link_lengths(links: list[dict], min_length: float = MIN_LINK_LENGTH_M) -> list[str]:
    """length が min_length 未満（欠損・0・負）のリンクを min_length にする．戻り値は補正したリンク名．"""
    fixed = []
    for lk in links:
        try:
            length = float(lk.get("length", 0) or 0)
        except (TypeError, ValueError):
            length = 0.0
        if not length > 0 or length < min_length:
            lk["length"] = min_length
            fixed.append(str(lk.get("name", "?")))
    if fixed:
        log.warning(f"length が {min_length} m 未満のリンク {len(fixed)} 本を {min_length} m に補正: "
                    f"{fixed[:5]}{' …' if len(fixed) > 5 else ''}")
    return fixed


def parse_csv_scenario(content: str) -> dict:
    """
    単一 CSV からシナリオを推定する．
    カラム名を柔軟にマッチング．対応フォーマット:
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
        col_lcap = _find_col(raw_fields, ["capacity", "cap"])  # リンク容量（台/s）
        col_orig = _find_col(raw_fields, ["orig", "origin", "o_zone_id", "from", "source"])
        col_dest_d = _find_col(raw_fields, ["dest", "destination", "d_zone_id", "to", "target"])
        col_tstart = _find_col(raw_fields, ["t_start", "start_time", "time_start"])
        col_tend = _find_col(raw_fields, ["t_end", "end_time", "time_end"])
        col_flow = _find_col(raw_fields, ["flow", "volume", "demand", "rate"])

        for row in reader:
            t = (_get_val(row, col_type, "") or "").lower()
            if t == "node":
                nodes.append({
                    "name": _get_val(row, col_name, ""),
                    "x": _get_float(row, col_x, 0),
                    "y": _get_float(row, col_y, 0),
                })
            elif t == "link":
                link = {
                    "name": _get_val(row, col_name, ""),
                    "start": _get_val(row, col_start, ""),
                    "end": _get_val(row, col_end, ""),
                    "length": _get_float(row, col_length, 1000),
                    "free_flow_speed": _get_float(row, col_ffs, 20),
                    "number_of_lanes": _get_int(row, col_lanes, 1),
                }
                lcap = _get_float(row, col_lcap, -1)
                if lcap > 0:
                    link["capacity"] = lcap
                links.append(link)
            elif t == "demand":
                demands.append({
                    "orig": _get_val(row, col_orig, ""),
                    "dest": _get_val(row, col_dest_d, ""),
                    "t_start": _get_float(row, col_tstart, 0),
                    "t_end": _get_float(row, col_tend, 3600),
                    "flow": _get_float(row, col_flow, 0.5),
                })
        clamp_link_lengths(links)
        return {"format": "risu_csv", "nodes": nodes, "links": links, "demands": demands}

    # ============ ノード CSV 判定 ============
    # name/id + x/y 系カラムがあるか
    # 識別子カラムの優先順位: ID 系 > name 系．
    # GMNS 等では node_id が主キーで name は表示ラベル（重複可）のため，
    # name を優先すると「ノード名が重複」エラーになる．
    # （リンクの from_node_id / to_node_id も node_id を参照するので整合する）
    NODE_NAME_COLS = ["node_id", "id", "name", "node_name", "node"]
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
        # 識別子は ID 系を優先（link_id が主キー，name はラベルの可能性がある）
        col_lname = _find_col(raw_fields, ["link_id", "id", "name", "link_name", "link"])
        col_length = _find_col(raw_fields, ["length", "distance", "dist", "長さ"])
        col_ffs = _find_col(raw_fields, ["free_flow_speed", "speed", "free_speed",
                                          "speed_limit", "ffs", "制限速度", "速度"])
        col_lanes = _find_col(raw_fields, ["number_of_lanes", "lanes", "num_lanes", "車線数"])
        col_jd = _find_col(raw_fields, ["jam_density", "kjam"])
        col_lcap = _find_col(raw_fields, ["capacity", "cap", "容量"])

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
            lcap = _get_float(row, col_lcap, -1)
            if lcap > 0:
                link["capacity"] = lcap
            links.append(link)
        clamp_link_lengths(links)
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

    raise ValueError("CSV 形式を認識できません．ノード（name,x,y），リンク（start,end,length），または RISU CSV 形式を使用してください．")


def gmns_to_scenario(
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
        parsed = parse_csv_scenario(nodes_csv)
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
        parsed = parse_csv_scenario(links_csv)
        for lk in parsed["links"]:
            lk["length"] = lk["length"] * length_to_m
            raw_speed = lk["free_flow_speed"]
            lk["free_flow_speed"] = max(raw_speed * speed_to_ms, 1.0)
            # GMNS の capacity は台/時/車線 → 台/s（リンク全体）に変換
            if "capacity" in lk:
                lanes = max(1, int(lk.get("number_of_lanes", 1) or 1))
                lk["capacity"] = round(lk["capacity"] * lanes / 3600.0, 4)
        links = parsed["links"]

    # 需要
    demands = []
    if demand_csv:
        parsed = parse_csv_scenario(demand_csv)
        if parsed["format"] in ("gmns_demand", "demand_csv"):
            for d in parsed["demands"]:
                orig_zone = str(d["orig"])
                dest_zone = str(d["dest"])
                # zone_id → node_id マッピング
                orig_node = node_zone_map.get(orig_zone, orig_zone)
                dest_node = node_zone_map.get(dest_zone, dest_zone)
                # flow がすでに台/秒の場合はそのまま，volume が大きい場合は台/時→台/秒変換
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

    clamp_link_lengths(links)   # 単位換算後に（0 や欠損を最小長へ）
    return {
        "name": "gmns_import",
        "tmax": tmax,
        "deltan": 5,
        "nodes": nodes,
        "links": links,
        "demands": demands,
    }


# OSM 道路種別プリセット
# custom_filter は Overpass QL の highway タグフィルタ．
# None のプリセットは network_type で取得する．
OSM_ROAD_PRESETS = {
    # 高速道路・国道級のみ（広域・大半径向け．ノード数が大幅に減り高速）
    "major": '["highway"~"motorway|trunk|primary|motorway_link|trunk_link|primary_link"]',
    # 幹線道路まで（major + 2次・3次幹線．都市スケールの標準）
    "arterial": '["highway"~"motorway|trunk|primary|secondary|tertiary'
                '|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"]',
    # 一般車道（住宅街の道路含む．サービス道路・駐車場内通路は除外）
    "drive": None,
    # 全車道（サービス道路・駐車場内通路含む．最も細かいが最も重い）
    "all": None,
}
_OSM_NETWORK_TYPE = {"drive": "drive", "all": "drive_service"}


def run_osm_import(place: str, distance_m: int = 1000, road_types: str = "drive") -> dict:
    """OSM から道路ネットワークを取得して UXsim シナリオに変換．
    OSMnx のグラフを直接活用し，道路形状・速度推定・車線数を取得する．

    road_types: "major" | "arterial" | "drive" | "all"（OSM_ROAD_PRESETS 参照）
    """
    import osmnx as ox

    # OSM キャッシュ先の上書き（Docker 等で永続ボリュームに向ける用）
    _cache_dir = os.getenv("RISU_OSM_CACHE_DIR")
    if _cache_dir:
        ox.settings.cache_folder = _cache_dir

    road_types = (road_types or "drive").strip().lower()
    if road_types not in OSM_ROAD_PRESETS:
        raise ValueError(
            f"road_types は {', '.join(OSM_ROAD_PRESETS)} のいずれかを指定してください: {road_types}"
        )

    # ── ジオコーディング: 自然言語 → (lat, lon) ──
    center = ox.geocode(place)  # (lat, lon)
    center_lat, center_lon = center

    # ── 道路ネットワーク取得 ──
    custom_filter = OSM_ROAD_PRESETS[road_types]
    try:
        if custom_filter is not None:
            G = ox.graph_from_point(center, dist=distance_m, custom_filter=custom_filter)
        else:
            G = ox.graph_from_point(
                center, dist=distance_m, network_type=_OSM_NETWORK_TYPE[road_types]
            )
    except Exception as e:
        # 対象道路が範囲内に存在しない場合（郊外で major 指定など）
        raise ValueError(
            f"「{place}」周辺（半径{distance_m}m）で road_types='{road_types}' に該当する"
            f"道路が見つかりませんでした．road_types を 'arterial' や 'drive' に広げるか，"
            f"半径を大きくしてください．（{e.__class__.__name__}）"
        ) from e
    G = ox.add_edge_speeds(G)       # highway 種別から速度推定 (speed_kph)

    # メートル座標に投影
    Gp = ox.project_graph(G)

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
    for u, v, _key, data in Gp.edges(keys=True, data=True):
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

    _road_labels = {
        "major": "主要道路（高速・国道級）",
        "arterial": "幹線道路",
        "drive": "一般車道",
        "all": "全車道",
    }
    fixed = clamp_link_lengths(scenario_links)   # 同一座標のノード間などで length 0 の辺が来る
    return {
        "name": f"osm_{place[:30]}",
        "tmax": 3600,
        "deltan": 5,
        "nodes": scenario_nodes,
        "links": scenario_links,
        "demands": [],
        "link_geometries": link_geometries,
        "center": {"lat": center_lat, "lon": center_lon},
        "distance_m": distance_m,
        "summary": (
            f"OSM から「{place}」周辺（半径{distance_m}m，{_road_labels[road_types]}）の"
            f"道路ネットワークを取得しました．"
            f"{len(scenario_nodes)} ノード，{len(scenario_links)} リンク．"
            + (f"長さ 0 のリンク {len(fixed)} 本を {MIN_LINK_LENGTH_M:g} m に補正しました．" if fixed else "")
        ),
    }


# ---- GMNS GitHub データセット取得 ----

GMNS_REPO = "HanZhengIntelliTransport/GMNS_Plus_Dataset"
GMNS_API  = f"https://api.github.com/repos/{GMNS_REPO}/contents"
GMNS_RAW  = f"https://raw.githubusercontent.com/{GMNS_REPO}/main"
