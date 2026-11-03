"""招待制ネットワークサーバーのタイル経路（risu.network_tiles）と，OSM 取得失敗の区別．

ローカルの HTTP サーバーを偽の risu-network にして，結合・道路種別の絞り込み・鍵の宛先・
障害時のエラーの区別・キャッシュを確かめる（実ネットワークに出ない）．

[修正履歴] run_osm_import は取得時の例外をすべて「該当する道路が見つかりませんでした．
半径を大きくしてください」に変換しており，サーバー障害でも LLM が範囲を広げて再試行していた．
取得元の障害は OsmUnavailableError（/import/osm では HTTP 503）で返す．
"""

import bz2
import json
import math
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import quoteattr

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

KEY = "sk-test-0123456789"
Z = 13


def _tile_bounds(x, y, z):
    n = 1 << z

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return (x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y))   # w, s, e, n


def _make_tiles(root):
    """タイル境界の経度 b をまたぐ道路を 2 タイルに分けて書く（way は node ごと両方に入る）．"""
    from risu.network_tiles import _tile_x, _tile_y
    x, y = _tile_x(140.88, Z), _tile_y(38.26, Z)
    _w, s, e, n = _tile_bounds(x, y, Z)
    b, lat = e, (s + n) / 2                     # (x, y) と (x+1, y) の境界上
    nodes = {1: (lat, b - 0.002), 2: (lat, b - 0.0005), 3: (lat, b + 0.0005), 4: (lat, b + 0.002),
             5: (lat + 0.002, b - 0.0005), 6: (lat - 0.001, b + 0.0005), 7: (lat + 0.001, b + 0.0005),
             8: (lat + 0.02, b - 0.003), 9: (lat + 0.021, b - 0.003)}
    ways = {100: ([1, 2, 3, 4], {"highway": "primary", "name": "大通り & <東>"}),
            101: ([2, 5], {"highway": "residential"}),
            102: ([3, 6], {"highway": "footway"}),
            103: ([3, 7], {"highway": "service", "service": "driveway"}),
            104: ([8, 9], {"highway": "primary"})}      # 範囲の外（2 km 先）

    def tile_of(nid):
        return (x if nodes[nid][1] < b else x + 1, y)
    content: dict = {}
    for wid, (refs, _tags) in ways.items():
        for t in {tile_of(r) for r in refs}:
            content.setdefault(t, set()).add(wid)
    for (tx, ty), wids in content.items():
        nids = sorted({r for wid in wids for r in ways[wid][0]})
        xml = ["<?xml version='1.0' encoding='UTF-8'?>", '<osm version="0.6" generator="risu-network">']
        xml += [f' <node id="{i}" lat="{nodes[i][0]:.7f}" lon="{nodes[i][1]:.7f}"/>' for i in nids]
        for wid in sorted(wids):
            refs, tags = ways[wid]
            xml.append(f' <way id="{wid}">' + "".join(f'<nd ref="{r}"/>' for r in refs)
                       + "".join(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items())
                       + "</way>")
        xml.append("</osm>")
        p = root / "tiles" / str(Z) / str(tx) / f"{ty}.osm.bz2"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bz2.compress("\n".join(xml).encode("utf-8")))
    return (lat, b), sorted(content)


@pytest.fixture
def server(tmp_path, monkeypatch):
    pytest.importorskip("osmnx")
    import osmnx

    import risu.network_tiles as nt
    center, tiles = _make_tiles(tmp_path / "srv")
    # 配信国は 2 つ．japan は日本のおおよその範囲（タイルがあるのは仙台の 2 個だけ），
    # sendai-test は japan の内側の狭い範囲（中心点が両方に入るときは狭い方を使う）
    w, s_, e, n = _tile_bounds(tiles[0][0], tiles[0][1], Z)
    state = {"date": "2026-11-01", "requests": [], "status": None,
             "regions": [{"id": "japan", "bounds": [122.0, 20.0, 154.0, 46.0]},
                         {"id": "sendai-test", "bounds": [w - 0.05, s_ - 0.05, e + 0.1, n + 0.05]}]}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Authorization")))
            if state["status"]:
                self.send_response(state["status"])
                self.end_headers()
                return
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                self.send_response(401)
                self.end_headers()
                return
            region_ids = [r["id"] for r in state["regions"]]
            parts = self.path.split("/")              # ["", "v1", region, ...]
            if self.path == "/v1/regions.json":
                body = json.dumps({"regions": state["regions"]}).encode()
            elif len(parts) >= 4 and parts[2] in region_ids and parts[3] == "manifest.json":
                # japan には仙台のタイルを載せない（中心点が狭い sendai-test に入るときだけ取れる）
                body = json.dumps({"date": state["date"], "zoom": Z,
                                   "tiles": tiles if parts[2] == "sendai-test" else []}).encode()
            else:
                prefix = "/v1/sendai-test/tiles/"
                p = tmp_path / "srv" / "tiles" / self.path[len(prefix):]
                if not self.path.startswith(prefix) or not p.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                body = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(nt, "NETWORK_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setattr(nt, "NETWORK_API_KEY", KEY)
    monkeypatch.setattr(nt, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(nt, "_json_mem", {})
    monkeypatch.setattr(nt, "_TIMEOUT_S", 5.0)
    monkeypatch.setattr(osmnx, "geocode", lambda place: center)
    state.update(cache=tmp_path / "cache", httpd=httpd, nt=nt, center=center)
    yield state
    httpd.shutdown()
    httpd.server_close()


def _names(sc):
    return sorted(n["name"] for n in sc["nodes"])


class TestNetworkTiles:

    def test_tiles_are_merged_and_filtered_like_overpass(self, server):
        from risu.importers import run_osm_import
        # drive: 歩道（102）と敷地内通路（103）は除き，タイル境界をまたぐ 100 は 1 本につながる
        drive = run_osm_import("仙台駅", 300, "drive")
        assert _names(drive) == ["1", "2", "4", "5"]
        # 取得元は risu-network と書く（OpenStreetMap に問い合わせたとは言わない．出典とデータの日付は添える）
        assert drive["summary"].startswith("risu-network（OpenStreetMap 由来，2026-11-01 時点のデータ）から")
        assert (drive["provider"], drive["data_date"]) == ("risu-network", "2026-11-01")
        assert len(drive["links"]) == 6                  # 1-2, 2-4, 2-5 の双方向
        # all: 敷地内通路（driveway）も入るので 3 が交差点として残る
        assert _names(run_osm_import("仙台駅", 300, "all")) == ["1", "2", "3", "4", "5", "7"]
        # major: 幹線 100 だけ．途中の 2・3 は単純化で消える
        assert _names(run_osm_import("仙台駅", 300, "major")) == ["1", "4"]
        tile_reqs = [p for p, _ in server["requests"] if "/tiles/" in p]
        assert len(tile_reqs) == 2                       # 2 回目以降はキャッシュから
        assert all(p.startswith("/v1/sendai-test/tiles/13/") for p in tile_reqs)   # 狭い方の配信国
        assert all(a == f"Bearer {KEY}" for _, a in server["requests"])

    def test_no_tile_in_range_says_no_roads(self, server, monkeypatch):
        import osmnx

        from risu.importers import run_osm_import
        monkeypatch.setattr(osmnx, "geocode", lambda place: (26.0, 128.0))     # japan の範囲内の海上
        with pytest.raises(ValueError, match="見つかりませんでした"):
            run_osm_import("太平洋", 300, "drive")

    def test_outside_every_region_is_out_of_coverage(self, server, monkeypatch):
        """配信国の外は「半径を大きく」ではなく「配信範囲外」．タイルは取りに行かない．"""
        import osmnx

        from risu.importers import run_osm_import
        from risu.network_tiles import OutOfCoverageError
        monkeypatch.setattr(osmnx, "geocode", lambda place: (48.8566, 2.3522))  # パリ
        with pytest.raises(OutOfCoverageError) as ei:
            run_osm_import("Paris", 300, "drive")
        msg = str(ei.value)
        assert "配信範囲外" in msg and "japan" in msg and "半径" not in msg
        assert not any("/tiles/" in p or "manifest" in p for p, _ in server["requests"])

    def test_rejected_key_is_unavailable_not_no_roads(self, server, caplog):
        import logging

        from risu.importers import OsmUnavailableError, run_osm_import
        server["status"] = 401
        with caplog.at_level(logging.INFO, logger="risu"), pytest.raises(OsmUnavailableError) as ei:
            run_osm_import("仙台駅", 300, "drive")
        msg = str(ei.value)
        assert "API キー" in msg and "見つかりませんでした" not in msg and "範囲や道路種別は変えずに" in msg
        assert KEY not in msg and KEY not in caplog.text

    def test_server_error_is_unavailable(self, server):
        from risu.importers import OsmUnavailableError, run_osm_import
        server["status"] = 502
        with pytest.raises(OsmUnavailableError, match="HTTP 502"):
            run_osm_import("仙台駅", 300, "drive")

    def test_server_down_uses_cache_or_reports_unavailable(self, server):
        from risu.importers import OsmUnavailableError, run_osm_import
        nt = server["nt"]
        run_osm_import("仙台駅", 300, "drive")           # キャッシュを作る
        server["httpd"].shutdown()
        server["httpd"].server_close()
        nt._json_mem.clear()
        # 前回の manifest とタイルがあれば，サーバーが落ちていても同じ範囲は取り込める
        assert len(run_osm_import("仙台駅", 300, "drive")["nodes"]) == 4
        # キャッシュが無ければ「道路が無い」ではなく「取得できない」
        shutil.rmtree(server["cache"])
        nt._json_mem.clear()
        with pytest.raises(OsmUnavailableError, match="接続できませんでした"):
            run_osm_import("仙台駅", 300, "drive")

    def test_new_data_date_replaces_old_cache(self, server):
        from risu.importers import run_osm_import
        nt = server["nt"]
        run_osm_import("仙台駅", 300, "drive")
        root = server["cache"] / "risu-network" / "sendai-test"
        assert (root / "2026-11-01").is_dir()
        server["date"] = "2026-12-01"
        nt._json_mem.clear()
        run_osm_import("仙台駅", 300, "drive")
        dirs = sorted(p.name for p in root.iterdir() if p.is_dir())
        assert dirs == ["2026-12-01"]

    def test_corrupt_cached_tile_is_dropped(self, server):
        from risu.importers import OsmUnavailableError, run_osm_import
        run_osm_import("仙台駅", 300, "drive")
        tiles = list((server["cache"] / "risu-network").rglob("*.osm.bz2"))
        for m in (server["cache"] / "risu-network").rglob("*.marshal"):
            m.unlink()                                   # 解析済みの控えが無い状態で XML が壊れている
        tiles[0].write_bytes(b"not bz2")
        with pytest.raises(OsmUnavailableError, match="壊れていました"):
            run_osm_import("仙台駅", 300, "drive")
        assert not tiles[0].exists()                     # 次回は取り直す
        assert len(run_osm_import("仙台駅", 300, "drive")["nodes"]) == 4

    def test_parsed_tiles_are_reused(self, server, monkeypatch):
        """2 回目以降はタイルの XML を読まず，解析済みの控え（marshal）を使う．壊れた控えは読み直す．"""
        from risu.importers import run_osm_import
        nt = server["nt"]
        first = run_osm_import("仙台駅", 300, "all")
        parsed = list((server["cache"] / "risu-network").rglob("*.marshal"))
        assert len(parsed) == 2

        def no_xml(path):
            raise AssertionError(f"XML を読み直した: {path}")
        with monkeypatch.context() as mp:
            mp.setattr(nt, "_parse_tile_xml", no_xml)
            assert run_osm_import("仙台駅", 300, "all")["nodes"] == first["nodes"]
        parsed[0].write_bytes(b"garbage")
        assert run_osm_import("仙台駅", 300, "all")["nodes"] == first["nodes"]
        assert parsed[0].read_bytes() != b"garbage"     # 作り直した

    def test_direct_graph_equals_xml_route(self, server, monkeypatch):
        """osmnx の _create_graph に直接渡したグラフは graph_from_xml 経由と属性まで同じ．
        osmnx を上げて非公開関数が変わったらここが落ちる（使えなければ XML 経由に戻る）．"""
        import builtins

        import osmnx

        from risu.importers import _NETWORK_FILTERS
        nt = server["nt"]
        center = osmnx.geocode("x")

        def sig(G):
            return (sorted((n, sorted((k, str(v)) for k, v in d.items())) for n, d in G.nodes(data=True)),
                    sorted((u, v, k, sorted((a, str(b)) for a, b in d.items()))
                           for u, v, k, d in G.edges(keys=True, data=True)))
        direct = nt.graph_from_tiles(osmnx, center, 300, _NETWORK_FILTERS["all"])
        real_import = builtins.__import__

        def no_private(name, *a, **k):
            if name == "osmnx.graph" and a and a[2] and "_create_graph" in a[2]:
                raise ImportError("simulated")
            return real_import(name, *a, **k)
        calls = []
        real_from_xml = osmnx.graph_from_xml
        with monkeypatch.context() as mp:
            mp.setattr(builtins, "__import__", no_private)
            mp.setattr(osmnx, "graph_from_xml", lambda *a, **k: calls.append(1) or real_from_xml(*a, **k))
            via_xml = nt.graph_from_tiles(osmnx, center, 300, _NETWORK_FILTERS["all"])
        assert calls == [1]                              # XML 経由に戻った
        assert len(direct) == 6 and sig(direct) == sig(via_xml)

    def test_auth_header_only_for_the_network_server(self):
        import requests

        from risu.network_tiles import _NetworkServerAuth
        auth = _NetworkServerAuth("https://net.example.jp", KEY)

        def sent(u):
            return requests.Request("GET", u, auth=auth).prepare().headers.get("Authorization")
        assert sent("https://net.example.jp/v1/regions.json") == f"Bearer {KEY}"
        assert sent("https://NET.example.jp/v1/japan/tiles/13/1/2.osm.bz2") == f"Bearer {KEY}"
        for other in ("https://nominatim.openstreetmap.org/search?q=x",
                      "https://overpass-api.de/api/interpreter",
                      "http://net.example.jp/v1/regions.json",           # 平文には送らない
                      "https://net.example.jp:8443/v1/regions.json",
                      "https://net.example.jp.evil.test/v1/regions.json"):
            assert sent(other) is None, other
        assert KEY not in repr(auth)

    def test_overpass_filter_semantics(self):
        from risu.network_tiles import parse_overpass_filter, way_matches
        t = parse_overpass_filter('["highway"]["area"!~"yes"]["highway"!~"footway|service"]')
        assert way_matches({"highway": "primary"}, t)
        assert way_matches({"highway": "primary", "area": "no"}, t)
        assert not way_matches({"highway": "primary", "area": "yes"}, t)
        assert not way_matches({"highway": "footway"}, t)
        assert not way_matches({"name": "x"}, t)
        t = parse_overpass_filter('["highway"~"primary|trunk"]')
        assert way_matches({"highway": "primary_link"}, t)    # Overpass と同じく部分一致
        assert not way_matches({"highway": "Primary"}, t)     # 大文字小文字は区別する
        with pytest.raises(ValueError):
            parse_overpass_filter('["highway"~"a"] junk')

    def test_network_filters_match_osmnx(self):
        """drive / all のタグ条件は osmnx の network_type と同じ（osmnx を上げたら確認）．"""
        pytest.importorskip("osmnx")
        from osmnx import _overpass

        from risu.importers import _NETWORK_FILTERS, _OSM_NETWORK_TYPE
        for rt, network_type in _OSM_NETWORK_TYPE.items():
            assert _NETWORK_FILTERS[rt] == _overpass._get_network_filter(network_type), rt

    def test_choose_region_and_bad_regions(self):
        from risu.network_tiles import OutOfCoverageError, _validate_regions, choose_region
        regions = _validate_regions({"regions": [
            {"id": "japan", "bounds": [122, 20, 154, 46]},
            {"id": "tokyo", "bounds": [139, 35, 140.5, 36]}]})
        assert choose_region(regions, 38.26, 140.88) == "japan"
        assert choose_region(regions, 35.68, 139.76) == "tokyo"         # 重なれば狭い方
        with pytest.raises(OutOfCoverageError):
            choose_region(regions, 25.03, 121.56)                       # 台北
        for bad in ({"regions": [{"id": "../x", "bounds": [0, 0, 1, 1]}]},       # パスに使う
                    {"regions": [{"id": "Japan", "bounds": [0, 0, 1, 1]}]},
                    {"regions": [{"id": "japan", "bounds": [1, 0, 0, 1]}]},
                    {"regions": "japan"}):
            with pytest.raises(ValueError):
                _validate_regions(bad)

    def test_tiles_for_bounds_covers_the_box(self):
        from risu.network_tiles import tiles_for_bounds
        ts = tiles_for_bounds(140.87, 38.25, 140.90, 38.27, Z)
        for x, y in ts:
            w, s, e, n = _tile_bounds(x, y, Z)
            assert w < 140.90 and e > 140.87 and s < 38.27 and n > 38.25
        xs, ys = {t[0] for t in ts}, {t[1] for t in ts}
        assert len(ts) == len(xs) * len(ys) and ys == set(range(min(ys), max(ys) + 1))


def _box(lat0, lon0, lat1, lon1):
    from shapely.geometry import box
    return box(lon0, lat0, lon1, lat1)


def _fake_boundary(monkeypatch, geom):
    """ox.geocode_to_gdf を，与えた形 1 つを返すものに差し替える（Nominatim に出ない）．"""
    import geopandas as gpd
    import osmnx
    monkeypatch.setattr(osmnx, "geocode_to_gdf",
                        lambda place: gpd.GeoDataFrame({"geometry": [geom]}, crs="EPSG:4326"))


class TestBoundaryImport:
    """area="boundary": 地名の境界（区・市の形）の内側で取り込む．

    [修正履歴] 「千代田区全域」が代表点 ±2,500 m の正方形で取り込まれ，区の南端が欠け，
    北・西は隣の区まで入っていた．"""

    def test_tiles_are_cut_by_the_boundary(self, server, monkeypatch):
        from risu.importers import run_osm_import
        lat, b = server["center"]
        # 境界は node 1（b - 0.002）を含まない．radius では入る
        _fake_boundary(monkeypatch, _box(lat - 0.0025, b - 0.001, lat + 0.0025, b + 0.0025))
        sc = run_osm_import("千代田区, 東京都", 0, "drive", "boundary")
        assert _names(sc) == ["2", "4", "5"]
        assert sc["area"] == "boundary" and sc["distance_m"] is None
        assert 0.1 < sc["area_km2"] < 0.3
        assert "区域内（約 0.2 km²，一般車道）" in sc["summary"]
        assert "1" in _names(run_osm_import("千代田区, 東京都", 300, "drive"))   # radius は従来どおり

    def test_too_large_or_not_an_area_is_rejected(self, server, monkeypatch):
        from risu.importers import run_osm_import
        lat, b = server["center"]
        _fake_boundary(monkeypatch, _box(lat - 0.1, b - 0.1, lat + 0.1, b + 0.1))     # 約 390 km²
        with pytest.raises(ValueError, match="上限 100 km²"):
            run_osm_import("東京都", 0, "drive", "boundary")
        _fake_boundary(monkeypatch, _box(lat, b, lat + 0.0001, b + 0.0001))          # 建物の大きさ
        with pytest.raises(ValueError, match="radius"):
            run_osm_import("東京駅", 0, "drive", "boundary")

        import osmnx

        def no_polygon(place):
            raise TypeError("Nominatim did not geocode query to a geometry of type (Multi)Polygon.")
        monkeypatch.setattr(osmnx, "geocode_to_gdf", no_polygon)
        with pytest.raises(ValueError, match="境界（区域の形）が OpenStreetMap にありませんでした"):
            run_osm_import("どこかの交差点", 0, "drive", "boundary")
        with pytest.raises(ValueError, match="area は"):
            run_osm_import("千代田区", 0, "drive", "ward")
        assert not [p for p, _ in server["requests"] if "/tiles/" in p]   # 断るときはタイルを取りに行かない

    def test_overpass_route_uses_graph_from_polygon(self, monkeypatch):
        pytest.importorskip("osmnx")
        import osmnx

        import risu.network_tiles as nt
        from risu.importers import _fetch_osm_graph
        monkeypatch.setattr(nt, "NETWORK_URL", "")
        got = {}
        monkeypatch.setattr(osmnx, "graph_from_polygon", lambda poly, **kw: got.update(poly=poly, **kw) or "G")
        poly = _box(35.67, 139.73, 35.71, 139.78)
        assert _fetch_osm_graph(osmnx, (35.69, 139.75), 0, "arterial", poly) == "G"
        assert got["poly"] is poly and "secondary" in got["custom_filter"]
        assert _fetch_osm_graph(osmnx, (35.69, 139.75), 0, "drive", poly) == "G"
        assert got["network_type"] == "drive"

    def test_api_and_tool_pass_the_area(self, monkeypatch):
        import asyncio

        from fastapi.testclient import TestClient
        from fixtures import F5, IsolatedStore

        import risu.api as api
        import risu.tools as tools
        seen = []

        def fake(place, distance_m=1000, road_types="drive", area="radius"):
            seen.append(area)
            return {"name": "osm", "tmax": 3600, "deltan": 5, "nodes": F5["nodes"], "links": F5["links"],
                    "demands": [], "link_geometries": {}, "center": {"lat": 0, "lon": 0},
                    "distance_m": None, "area": area, "area_km2": 11.4,
                    "provider": "risu-network", "data_date": "2026-11-01", "summary": "ok"}
        monkeypatch.setattr(api, "run_osm_import", fake)
        monkeypatch.setattr(tools, "run_osm_import", fake)
        block = type("B", (), {"type": "tool_use", "id": "t1", "name": "import_osm_network",
                               "input": {"place": "千代田区, 東京都", "area": "boundary", "road_types": "arterial"}})()

        async def run():
            return await tools.collect_tool_results([block], tools.ToolTurnState(body=None, via="mcp"))
        with IsolatedStore():
            client = TestClient(api.app)
            assert client.post("/import/osm", data={"place": "千代田区", "area": "ward"}).status_code == 400
            r = client.post("/import/osm", data={"place": "千代田区", "area": "boundary"})
            assert r.status_code == 200 and seen == ["boundary"]
            src = api.results_store[r.json()["id"]]["_meta"]["source"]
            assert src["area"] == "boundary" and src["area_km2"] == 11.4 and "distance_m" not in src
            assert (src["provider"], src["data_date"]) == ("risu-network", "2026-11-01")

            res = asyncio.run(run())
            assert seen == ["boundary", "boundary"]
            assert len(res) == 1 and not res[0].get("is_error") and "sim_id" in str(res[0]["content"])


class TestOverpassUnavailable:
    """RISU_NETWORK_URL が無いときの公開 Overpass 経路でも，通信障害は「道路が無い」にしない．"""

    def test_connection_error_is_unavailable(self, monkeypatch):
        pytest.importorskip("osmnx")
        import types

        import requests

        import risu.network_tiles as nt
        from risu.importers import OsmUnavailableError, run_osm_import
        monkeypatch.setattr(nt, "NETWORK_URL", "")

        def boom(*a, **k):
            raise requests.exceptions.ConnectionError()
        fake = types.SimpleNamespace(settings=types.SimpleNamespace(cache_folder="./cache"),
                                     geocode=lambda p: (38.26, 140.88), graph_from_point=boom)
        monkeypatch.setitem(sys.modules, "osmnx", fake)
        with pytest.raises(OsmUnavailableError) as ei:
            run_osm_import("仙台駅", 500, "drive")
        assert "見つかりませんでした" not in str(ei.value)

    def test_api_returns_503(self, monkeypatch):
        from fastapi.testclient import TestClient

        import risu.api as api
        from risu.importers import OsmUnavailableError

        def down(*a, **k):
            raise OsmUnavailableError("ネットワークサーバーに接続できませんでした")
        monkeypatch.setattr(api, "run_osm_import", down)
        r = TestClient(api.app).post("/import/osm", data={"place": "仙台駅"})
        assert r.status_code == 503 and "接続できませんでした" in r.json()["detail"]
