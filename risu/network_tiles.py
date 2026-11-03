"""招待制のネットワークサーバー（risu-network）が配る z13 タイルから道路グラフを作る．

公開 Overpass は混雑で遅い・失敗するため，国ごとの道路を z13（約 4 km 四方）の
OSM XML（.osm.bz2）に切ったものを配信してもらい，ここで結合して osmnx のグラフにする．

- サーバー: ``RISU_NETWORK_URL``（例 ``https://<host>``）．配信 URL には国（region）が入る．
  ``GET /v1/regions.json`` → ``{"regions": [{"id": "japan", "bounds": [west, south, east, north]}, ...]}``
  ``GET /v1/{region}/manifest.json`` → ``{"date": "2026-11-01", "zoom": 13, "tiles": [[x, y], ...]}``
  ``GET /v1/{region}/tiles/{z}/{x}/{y}.osm.bz2`` → タイル本体（manifest に無いタイルは道路が無い）
  中心点を含む region を使う（複数あれば範囲の狭い方）．どれにも入らなければ配信範囲外．
- 鍵: ``RISU_NETWORK_API_KEY``．``Authorization: Bearer`` で **NETWORK_URL と同じ origin にだけ**付ける
  （``_NetworkServerAuth``）．risu-local 自身の利用者認証ではない（CLAUDE.md §3.8）．
  ログ・例外の文面・結果のメタには出さない．
- タイルの規則: way は自分の node が 1 つでも入るタイルすべてに，node ごと丸ごと入っている
  （osmium の complete_ways と同じ）．範囲を覆うタイルを結合して way / node の重複を除けば，
  Overpass で同じ範囲を問い合わせたのと同じ集合になる．
- キャッシュ: ``<RISU_OSM_CACHE_DIR>/risu-network/<region>/<date>/<z>/<x>/<y>.osm.bz2``．
  region のデータの日付が変わったら，その region の古い日付のディレクトリは消す．
- 速さのため，タイルは初回に XML を読んだら全道路を marshal で隣に保存し（``<y>.py312.marshal``），
  2 回目からそれを読む（XML の解析 1 秒前後が 0.1 秒未満になる）．pickle は読込でコードを実行し得るので使わない．
  グラフは osmnx の ``_create_graph`` に Overpass の応答と同じ形で直接渡す（XML の書き出し・読み直しを省く）．
  非公開関数なので，使えなければ XML 経由（``graph_from_xml``）に戻る．
- グラフ化は ``osmnx.graph_from_bbox`` と同じ手順（500 m 外側まで取って切り詰め → 最大連結成分 →
  単純化 → 元の範囲で切り詰め）を公開 API で行う．道路種別の絞り込みは Overpass のフィルタ文字列
  （``OSM_ROAD_PRESETS`` / osmnx の network_type）をここで解釈して当てる．
"""

from __future__ import annotations

import bz2
import json
import marshal
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

from .runtime import log

__all__ = [
    "NETWORK_API_KEY",
    "NETWORK_URL",
    "OsmUnavailableError",
    "OutOfCoverageError",
    "choose_region",
    "graph_from_tiles",
    "graph_from_tiles_polygon",
    "parse_overpass_filter",
    "tiles_for_bounds",
    "way_matches",
]

NETWORK_URL = os.getenv("RISU_NETWORK_URL", "").strip().rstrip("/")
NETWORK_API_KEY = os.getenv("RISU_NETWORK_API_KEY", "").strip()
CACHE_ROOT = os.getenv("RISU_OSM_CACHE_DIR") or "./cache"

_TIMEOUT_S = 30.0
_FETCH_WORKERS = 8
_MANIFEST_TTL_S = 6 * 3600.0
_BUFFER_M = 500.0                 # osmnx.graph_from_polygon と同じ外側の余裕
_MAX_TILES = 400                  # 半径 5 km でも 30 個程度．これを超えるのは入力の誤り
_DATE_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")      # URL とキャッシュのディレクトリ名になる


class OsmUnavailableError(RuntimeError):
    """道路データの取得元（ネットワークサーバー / Overpass）が応答しない・要求を拒否した．
    「その範囲に道路が無い」とは区別する（範囲を広げて再試行しても解決せず，余計に重くなるため）．"""


class OutOfCoverageError(ValueError):
    """中心点がネットワークサーバーの配信国（region）のどれにも入らない．
    範囲や道路種別を変えても解決しないので，「道路が無い」の文面（半径を広げる案内）にしない．"""


# ── 鍵の宛先 ─────────────────────────────────────────────

def _origin(url: str) -> tuple[str, str, int | None]:
    u = urlsplit(url)
    return (u.scheme.lower(), (u.hostname or "").lower(), u.port)


class _NetworkServerAuth:
    """requests の auth．NETWORK_URL と同じ origin（scheme・ホスト・ポート）宛てにだけ鍵を付ける．
    リダイレクト先が別ホストなら requests が Authorization を外す（rebuild_auth）．"""

    def __init__(self, url: str, key: str):
        self._origin = _origin(url)
        self._key = key

    def __call__(self, r):
        if _origin(r.url) == self._origin:
            r.headers["Authorization"] = f"Bearer {self._key}"
        return r

    def __repr__(self):     # 鍵をログや例外の表示に出さない
        return "<_NetworkServerAuth>"


# ── タイル座標 ───────────────────────────────────────────

def _tile_x(lon: float, z: int) -> int:
    n = 1 << z
    return min(n - 1, max(0, int((lon + 180.0) / 360.0 * n)))


def _tile_y(lat: float, z: int) -> int:
    n = 1 << z
    lat = max(-85.05112878, min(85.05112878, lat))
    r = math.radians(lat)
    return min(n - 1, max(0, int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)))


def tiles_for_bounds(west: float, south: float, east: float, north: float, z: int) -> list[tuple[int, int]]:
    """経緯度の範囲を覆う z のタイル (x, y) の一覧．"""
    x0, x1 = _tile_x(west, z), _tile_x(east, z)
    y0, y1 = _tile_y(north, z), _tile_y(south, z)      # y は北が小さい
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


# ── 道路種別のフィルタ（Overpass QL のタグ条件を解釈する） ────────

_FILTER_TERM = re.compile(r'\["([^"]+)"(?:(!?~)"([^"]*)")?\]')


def parse_overpass_filter(s: str) -> list[tuple[str, str, re.Pattern | None]]:
    """``["highway"]["area"!~"yes"]["highway"~"a|b"]`` を (key, op, regex) の並びにする．
    op は ``has``（キーがある）/ ``~``（キーがあり正規表現に合う）/ ``!~``（キーが無いか合わない）．
    Overpass と同じく部分一致・大文字小文字を区別する．解釈できない文字列は ValueError．"""
    terms, pos = [], 0
    for m in _FILTER_TERM.finditer(s):
        if m.start() != pos:
            break
        key, op, rx = m.group(1), m.group(2), m.group(3)
        terms.append((key, op or "has", re.compile(rx) if op else None))
        pos = m.end()
    if pos != len(s) or not terms:
        raise ValueError(f"道路種別のフィルタを解釈できません: {s!r}")
    return terms


def way_matches(tags: dict[str, str], terms) -> bool:
    for key, op, rx in terms:
        v = tags.get(key)
        if op == "has":
            if v is None:
                return False
        elif op == "~":
            if v is None or not rx.search(v):
                return False
        elif v is not None and rx.search(v):     # "!~"
            return False
    return True


# ── 取得 ────────────────────────────────────────────────

_json_lock = threading.Lock()
_json_mem: dict[str, tuple[float, Any]] = {}      # 配信パス -> (取得時刻, 検証済みの中身)


def _cache_root() -> Path:
    return Path(CACHE_ROOT) / "risu-network"


def _session():
    import requests
    s = requests.Session()
    if NETWORK_API_KEY:
        s.auth = _NetworkServerAuth(NETWORK_URL, NETWORK_API_KEY)
    return s


def _get(sess, path: str) -> bytes | None:
    """NETWORK_URL + path を取得する．404 は None．それ以外の失敗は OsmUnavailableError．"""
    import requests
    url = NETWORK_URL + path
    try:
        r = sess.get(url, timeout=_TIMEOUT_S)
    except requests.RequestException as e:
        raise OsmUnavailableError(
            f"risu-network（RISU_NETWORK_URL）に接続できませんでした（{e.__class__.__name__}）"
        ) from None
    if r.status_code in (401, 403):
        log.error("network server rejected the request: %s %s", r.status_code, path)
        raise OsmUnavailableError(
            "risu-network が API キーを受け付けませんでした"
            + ("（RISU_NETWORK_API_KEY が無効か失効しています）" if NETWORK_API_KEY
               else "（RISU_NETWORK_API_KEY を設定してください）")
        )
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise OsmUnavailableError(f"risu-network がエラーを返しました（HTTP {r.status_code}）")
    return r.content


def _validate_regions(m) -> list[dict]:
    if not isinstance(m, dict) or not isinstance(m.get("regions"), list):
        raise ValueError("regions is not a list")
    out = []
    for r in m["regions"]:
        rid, b = r.get("id"), r.get("bounds")
        if not isinstance(rid, str) or not _REGION_RE.match(rid):
            raise ValueError(f"bad region id: {rid!r}")
        if not isinstance(b, list) or len(b) != 4:
            raise ValueError(f"bad bounds for {rid}")
        w, s_, e, n = (float(v) for v in b)
        if not (w < e and s_ < n):
            raise ValueError(f"bad bounds for {rid}")
        out.append({"id": rid, "bounds": (w, s_, e, n)})
    return out


def _validate_manifest(m) -> dict:
    if not isinstance(m, dict):
        raise ValueError("manifest is not an object")
    date, zoom, tiles = m.get("date"), m.get("zoom"), m.get("tiles")
    if not isinstance(date, str) or not _DATE_RE.match(date):
        raise ValueError(f"bad date: {date!r}")
    if not isinstance(zoom, int) or not 0 <= zoom <= 20:
        raise ValueError(f"bad zoom: {zoom!r}")
    if not isinstance(tiles, list):
        raise ValueError("tiles is not a list")
    return {"date": date, "zoom": zoom, "tiles": {(int(x), int(y)) for x, y in tiles}}


def _prune_old_dates(region: str, keep: str) -> None:
    root = _cache_root() / region
    if not root.is_dir():
        return
    for p in root.iterdir():
        if p.is_dir() and p.name != keep and _DATE_RE.match(p.name):
            shutil.rmtree(p, ignore_errors=True)
            log.info("network tiles: removed cache of old %s data %s", region, p.name)


def _load_json(sess, path: str, disk: Path, validate) -> tuple[Any, bool]:
    """配信パスの JSON を取得して検証する（メモリに数時間保持し，ディスクにも控える）．
    サーバーに繋がらなくても前回の控えがあればそれで続ける（キャッシュ済みのタイルで足りる範囲なら
    取込できる）．返り値は (中身, 新しく取得したか)．"""
    with _json_lock:
        now = time.monotonic()
        hit = _json_mem.get(path)
        if hit and now - hit[0] < _MANIFEST_TTL_S:
            return hit[1], False
        try:
            raw = _get(sess, path)
            if raw is None:
                raise OsmUnavailableError(f"risu-network に {path} がありません（RISU_NETWORK_URL を確認）")
        except OsmUnavailableError as e:
            if disk.is_file():
                log.warning("network tiles: %s; using the cached %s", e, disk.name)
                v = validate(json.loads(disk.read_text(encoding="utf-8")))
                _json_mem[path] = (now, v)
                return v, False
            raise
        try:
            v = validate(json.loads(raw))
        except (ValueError, TypeError, AttributeError) as e:
            raise OsmUnavailableError(f"risu-network の {path} が読めません（{e}）") from None
        disk.parent.mkdir(parents=True, exist_ok=True)
        tmp = disk.with_name(f".{disk.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, disk)
        _json_mem[path] = (now, v)
        return v, True


def choose_region(regions: list[dict], lat: float, lon: float) -> str:
    """中心点を含む region の id（複数なら範囲の狭い方）．無ければ OutOfCoverageError．"""
    hits = [r for r in regions
            if r["bounds"][0] <= lon <= r["bounds"][2] and r["bounds"][1] <= lat <= r["bounds"][3]]
    if not hits:
        names = "，".join(r["id"] for r in regions) or "なし"
        raise OutOfCoverageError(
            f"この地点（緯度 {lat:.4f}，経度 {lon:.4f}）は risu-network の配信範囲外です"
            f"（配信中: {names}）．範囲や道路種別を変えても取得できません．"
            "RISU_NETWORK_URL を外すと公開 Overpass から取得します"
        )

    def area(r):
        w, s_, e, n = r["bounds"]
        return (e - w) * (n - s_)
    return min(hits, key=area)["id"]


def _load_region_manifest(sess, lat: float, lon: float) -> tuple[str, dict]:
    regions, _ = _load_json(sess, "/v1/regions.json", _cache_root() / "regions.json", _validate_regions)
    region = choose_region(regions, lat, lon)
    m, fresh = _load_json(sess, f"/v1/{region}/manifest.json",
                          _cache_root() / region / "manifest.json", _validate_manifest)
    if fresh:
        _prune_old_dates(region, m["date"])
    return region, m


def _tile_path(region: str, date: str, z: int, x: int, y: int) -> Path:
    return _cache_root() / region / date / str(z) / str(x) / f"{y}.osm.bz2"


def _fetch_tile(sess, region: str, date: str, z: int, x: int, y: int) -> Path | None:
    p = _tile_path(region, date, z, x, y)
    if p.is_file():
        return p
    raw = _get(sess, f"/v1/{region}/tiles/{z}/{x}/{y}.osm.bz2")
    if raw is None:      # manifest にあるのに無い．道路が無い扱いにして続ける
        log.warning("network tiles: %s %d/%d/%d listed in the manifest but not found", region, z, x, y)
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{y}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, p)
    return p


# ── 結合 ────────────────────────────────────────────────

_PARSED_SUFFIX = f".py{sys.version_info[0]}{sys.version_info[1]}.marshal"   # marshal は Python の版ごとに形式が違う
_PARSED_VERSION = 1


def _parse_tile_xml(path: Path) -> tuple[dict, list]:
    """タイルの XML を全道路そのままで読む．nodes: {id: (lat, lon, tags or None)}，ways: [(id, refs, tags)]．"""
    nodes: dict = {}
    ways: list = []
    with bz2.open(path, "rb") as f:
        for _, el in ET.iterparse(f, events=("end",)):
            tag = el.tag
            if tag == "node":
                tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
                nodes[int(el.get("id"))] = (float(el.get("lat")), float(el.get("lon")), tags or None)
                el.clear()
            elif tag == "way":
                ways.append((int(el.get("id")), [int(nd.get("ref")) for nd in el.iter("nd")],
                             {t.get("k"): t.get("v") for t in el.iter("tag")}))
                el.clear()
    return nodes, ways


def _load_tile(path: Path) -> tuple[dict, list]:
    """タイル 1 個の中身．解析済みの控え（marshal）があればそれを読み，無ければ XML を読んで控えを作る．"""
    parsed = path.with_name(path.name[: -len(".osm.bz2")] + _PARSED_SUFFIX)
    if parsed.is_file():
        try:
            ver, nodes, ways = marshal.loads(parsed.read_bytes())
            if ver == _PARSED_VERSION:
                return nodes, ways
        except (EOFError, ValueError, TypeError):
            pass
        parsed.unlink(missing_ok=True)
    try:
        nodes, ways = _parse_tile_xml(path)
    except (OSError, EOFError, ET.ParseError, ValueError, TypeError) as e:
        path.unlink(missing_ok=True)          # 壊れたキャッシュは次回取り直す
        raise OsmUnavailableError(f"risu-network のタイルが壊れていました（{path.name}: "
                                  f"{e.__class__.__name__}）．もう一度実行してください") from None
    try:
        tmp = parsed.with_name(f".{parsed.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(marshal.dumps((_PARSED_VERSION, nodes, ways)))
        os.replace(tmp, parsed)
    except OSError as e:                      # 控えが書けなくても取込は続ける
        log.warning("network tiles: could not cache parsed tile %s: %s", parsed.name, e)
    return nodes, ways


def _merge_tiles(paths, terms) -> tuple[dict, dict]:
    """タイルを結合する（ID で重複除去．way は道路種別のフィルタに合うものだけ）．"""
    nodes: dict = {}
    ways: dict = {}
    for p in paths:
        if p is None:
            continue
        tn, tw = _load_tile(p)
        for nid, v in tn.items():
            nodes.setdefault(nid, v)
        for wid, refs, tags in tw:
            if wid not in ways and way_matches(tags, terms):
                ways[wid] = (refs, tags)
    return nodes, ways


def _in_bounds(nodes: dict, refs, bounds) -> bool:
    west, south, east, north = bounds
    for r in refs:
        n = nodes.get(r)
        if n is not None and south <= n[0] <= north and west <= n[1] <= east:
            return True
    return False


def _build_graph(ox, nodes: dict, ways: dict):
    """結合したデータから単純化前のグラフを作る（graph_from_xml(simplify=False, retain_all=True) と同じもの）．"""
    try:
        from osmnx.graph import _create_graph
    except ImportError:
        _create_graph = None
    if _create_graph is not None:
        used: set[int] = set()
        for refs, _ in ways.values():
            used.update(r for r in refs if r in nodes)
        elements: list[dict] = []
        for nid in used:
            lat, lon, tags = nodes[nid]
            el = {"type": "node", "id": nid, "lat": lat, "lon": lon}
            if tags:
                el["tags"] = tags
            elements.append(el)
        for wid, (refs, tags) in ways.items():
            elements.append({"type": "way", "id": wid, "nodes": [r for r in refs if r in nodes], "tags": tags})
        try:
            return _create_graph([{"elements": elements}], False)
        except TypeError as e:               # osmnx の非公開関数の形が変わった
            log.warning("network tiles: osmnx _create_graph unusable (%s); falling back to XML", e)
    with tempfile.TemporaryDirectory(prefix="risu-tiles-") as d:
        xml_path = Path(d) / "area.osm"
        _write_osm_xml(xml_path, nodes, ways)
        return ox.graph_from_xml(xml_path, bidirectional=False, simplify=False, retain_all=True)


def _write_osm_xml(path: Path, nodes: dict, ways: dict) -> None:
    used: set[int] = set()
    for refs, _ in ways.values():
        used.update(r for r in refs if r in nodes)
    with open(path, "w", encoding="utf-8") as f:
        f.write("<?xml version='1.0' encoding='UTF-8'?>\n<osm version=\"0.6\" generator=\"risu-local\">\n")
        for nid in sorted(used):
            lat, lon, tags = nodes[nid]
            if tags:
                f.write(f' <node id="{nid}" lat="{lat:.7f}" lon="{lon:.7f}">')
                for k, v in tags.items():
                    f.write(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>")
                f.write("</node>\n")
            else:
                f.write(f' <node id="{nid}" lat="{lat:.7f}" lon="{lon:.7f}"/>\n')
        for wid in sorted(ways):
            refs, tags = ways[wid]
            f.write(f' <way id="{wid}">')
            for r in refs:
                if r in nodes:
                    f.write(f'<nd ref="{r}"/>')
            for k, v in tags.items():
                f.write(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>")
            f.write("</way>\n")
        f.write("</osm>\n")


def graph_from_tiles(ox, center: tuple[float, float], distance_m: float, road_filter: str):
    """center（lat, lon）から ±distance_m の範囲の道路グラフ（未投影）を作る．
    ``ox.graph_from_point(center, dist=distance_m, custom_filter=road_filter)`` の置き換え．

    取得元の障害は OsmUnavailableError，配信国の外は OutOfCoverageError，
    範囲に該当道路が無いときは ValueError．"""
    bbox = ox.utils_geo.bbox_from_point(center, distance_m)        # (left, bottom, right, top)
    return graph_from_tiles_polygon(ox, ox.utils_geo.bbox_to_poly(bbox), road_filter,
                                    point=(center[0], center[1]))


def graph_from_tiles_polygon(ox, polygon, road_filter: str, point: tuple[float, float] | None = None):
    """polygon（緯度経度の shapely (Multi)Polygon）の内側の道路グラフ（未投影）を作る．
    ``ox.graph_from_polygon(polygon, custom_filter=road_filter)`` の置き換え（行政区域の形での取込）．
    配信国は point（lat, lon．省略時は polygon の内点）で選ぶ．例外は graph_from_tiles と同じ．"""
    import networkx as nx

    if not NETWORK_URL:
        raise RuntimeError("RISU_NETWORK_URL is not set")
    terms = parse_overpass_filter(road_filter)
    t0 = time.perf_counter()
    if point is None:
        rp = polygon.representative_point()
        point = (rp.y, rp.x)

    poly_proj, crs_utm = ox.projection.project_geometry(polygon)
    poly_buff, _ = ox.projection.project_geometry(poly_proj.buffer(_BUFFER_M), crs=crs_utm, to_latlong=True)
    west, south, east, north = poly_buff.bounds
    bounds = (west, south, east, north)

    sess = _session()
    try:
        region, m = _load_region_manifest(sess, point[0], point[1])
        z = m["zoom"]
        wanted = tiles_for_bounds(west, south, east, north, z)
        if len(wanted) > _MAX_TILES:
            raise ValueError(f"範囲が広すぎます（タイル {len(wanted)} 個）．範囲を小さくしてください")
        present = [t for t in wanted if t in m["tiles"]]
        if not present:
            raise ValueError("この範囲には道路のタイルがありません（海上など）")
        with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(present))) as pool:
            paths = list(pool.map(lambda t: _fetch_tile(sess, region, m["date"], z, t[0], t[1]), present))
    finally:
        sess.close()
    t_fetch = time.perf_counter() - t0

    nodes, ways = _merge_tiles(paths, terms)
    # node が 1 つでも範囲（外側の余裕込み）に入る way だけ残す（Overpass の bbox 問い合わせと同じ）
    ways = {w: v for w, v in ways.items() if _in_bounds(nodes, v[0], bounds)}
    if not ways:
        raise ValueError("該当する道路がありません")

    G_buff = _build_graph(ox, nodes, ways)

    # 以下は osmnx.graph_from_polygon と同じ手順
    G_buff = ox.truncate.truncate_graph_polygon(G_buff, poly_buff, truncate_by_edge=False)
    G_buff = ox.truncate.largest_component(G_buff, strongly=False)
    G_buff = ox.simplification.simplify_graph(G_buff)
    G = ox.truncate.truncate_graph_polygon(G_buff, polygon, truncate_by_edge=False)
    G = ox.truncate.largest_component(G, strongly=False)
    spn = ox.stats.count_streets_per_node(G_buff, nodes=G.nodes)
    nx.set_node_attributes(G, values=spn, name="street_count")

    G.graph["risu_network"] = {"region": region, "date": m["date"]}   # 取得元の表記（run_osm_import）
    log.info("network tiles: %s, %d tiles (data %s) fetched in %.1f s, graph %d nodes / %d edges in %.1f s",
             region, len(present), m["date"], t_fetch, len(G), len(G.edges), time.perf_counter() - t0)
    return G
