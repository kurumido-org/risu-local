"""HTTP 契約（risu.api）をサーバー未起動でも必ず実行する．テスト設計 T25 / T26 / T27 / T28 / T12．

TestClient でアプリを直接叩く（起動済みサーバー向けの tests/test_api.py は，サーバーが無ければ
同じ TestClient に切り替わる）．skip で成功に見せない．
"""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import risu.api  # noqa: E402
from risu.importers import demand_flow_unit, gmns_to_scenario  # noqa: E402
from risu.results import results_store  # noqa: E402

from fixtures import F1, IsolatedStore, changed_paths, f1  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(risu.api.app)


def _upload(client, name, data, tmax=800, **more):
    files = [("files", (name, data, "application/octet-stream"))]
    for n, d in more.items():
        files.append(("files", (n, d, "text/csv")))
    return client.post("/upload", files=files, data={"tmax": str(tmax)})


class TestCoreEndpoints:
    """T25: 正常 200，不正なシナリオ 422，未知結果 404．サーバー未起動の skip なし．"""

    def test_simulate_results_scenario_and_404(self, client):
        with IsolatedStore():
            r = client.post("/simulate", json=F1)
            assert r.status_code == 200, r.text
            sid = r.json()["id"]
            assert r.json()["stats"]["total_trips"] > 0
            env = client.get(f"/results/{sid}", headers={"Accept-Encoding": "identity"}).json()
            assert env["risu_schema_version"] == "1.0" and env["scenario"]["links"][0]["capacity"] == 0.5
            assert env["result"]["frame_format"] == "columnar_v3" and env["source"] == {"type": "manual"}
            light = client.get(f"/results/{sid}/scenario").json()
            assert "result" not in light and light["scenario"]["random_seed"] == 42
            assert client.get("/results/does_not_exist").status_code == 404
            assert client.get("/results/does_not_exist/scenario").status_code == 404
            rows = client.get("/results").json()["results"]
            assert any(x["sim_id"] == sid for x in rows)

    @pytest.mark.parametrize("bad, needle", [
        ({**F1, "links": [{"name": "AB", "start": "A", "end": "ZZ", "length": 3000}]}, "ZZ"),
        ({**F1, "deltan": 0}, "deltan"),
        ({**F1, "demands": [{"orig": "A", "dest": "C", "t_start": 600, "t_end": 600, "flow": 0.6}]}, "t_end"),
        ({**F1, "links": [{"name": "AB", "start": "A", "end": "B", "length": -5}]}, "length"),
        ({**F1, "nodes": F1["nodes"] + [{"name": "A", "x": 9, "y": 9}]}, "重複"),
    ])
    def test_invalid_scenarios_are_422(self, client, bad, needle):
        with IsolatedStore() as iso:
            r = client.post("/simulate", json=bad)
            assert r.status_code == 422, r.text
            assert needle in r.text
            assert iso.new_ids() == []

    def test_healthz_and_static(self, client):
        assert client.get("/healthz").json()["status"] == "ok"
        assert "RISU" in client.get("/").text
        assert client.get("/sample_risu.csv").status_code == 200


class TestUploadRobustness:
    """T26: BOM 付き CSV，空セル，正しい JSON envelope，壊れた JSON，文字コード違い．"""

    def test_bom_csv_with_empty_cells(self, client):
        csv = ("﻿type,name,x,y,start,end,length,free_flow_speed,number_of_lanes,orig,dest,t_start,t_end,flow\n"
               "node,A,0,0,,,,,,,,,,\nnode,B,3000,0,,,,,,,,,,\n"
               "link,r1,,,A,B,3000,,,,,,,\n"            # 速度・車線が空 → 既定値
               "demand,,,,,,,,,A,B,0,300,0.4\n")
        with IsolatedStore():
            r = _upload(client, "test.csv", csv.encode("utf-8"))
            assert r.status_code == 200, r.text
            sc = results_store[r.json()["id"]]["_scenario"]
            assert sc["links"][0]["free_flow_speed"] == 20 and sc["links"][0]["number_of_lanes"] == 1

    def test_json_envelope_roundtrip_keeps_params(self, client):
        """T12: 非既定の deltan / reaction_time / random_seed / tmax を全経路で保持する．"""
        sc = f1(tmax=2400, deltan=5, reaction_time=1.7, random_seed=13)
        with IsolatedStore():
            sid = client.post("/simulate", json=sc).json()["id"]
            env = client.get(f"/results/{sid}/scenario").json()
            r = _upload(client, "scn.json", json.dumps(env).encode("utf-8"), tmax=500)
            assert r.status_code == 200, r.text
            new = results_store[r.json()["id"]]
            assert new["_meta"]["source"]["imported_from_sim_id"] == sid
            assert changed_paths(results_store[sid]["_scenario"], new["_scenario"]) == {}
            for k, v in (("deltan", 5), ("reaction_time", 1.7), ("random_seed", 13), ("tmax", 2400)):
                assert new["_scenario"][k] == v
            keys = ("total_trips", "completed_trips", "average_travel_time_s")   # 壁時計の計測値は比べない
            assert {k: new["stats"][k] for k in keys} == {k: results_store[sid]["stats"][k] for k in keys}
            # 再実行（差分なし）でも保持
            import asyncio
            import types

            from risu.tools import handle_rerun_simulation
            content, rid, err = asyncio.run(handle_rerun_simulation(
                {"base_sim_id": sid, "modifications": [{"action": "set_tmax", "tmax": 3000}]},
                types.SimpleNamespace(messages=[])))
            assert not err and set(changed_paths(sc, results_store[rid]["_scenario"])) == {"tmax"}

    def test_broken_json_is_422_and_existing_results_survive(self, client):
        with IsolatedStore() as iso:
            sid = client.post("/simulate", json=F1).json()["id"]
            r = _upload(client, "broken.json", b'{"scenario": {"nodes": [')
            assert r.status_code == 422, r.text
            assert "解析できません" in r.json()["detail"] and "broken.json" in r.json()["detail"]
            r = _upload(client, "list.json", b"[1, 2, 3]")
            assert r.status_code == 422 and "オブジェクト" in r.json()["detail"]
            r = _upload(client, "sjis.csv", "type,name\nnode,駅前".encode("cp932"))
            assert r.status_code == 422 and "UTF-8" in r.json()["detail"]
            r = _upload(client, "mystery.csv", b"col_a,col_b\n1,2\n")
            assert r.status_code == 422 and "解釈できません" in r.json()["detail"]
            r = _upload(client, "none.txt", b"hello")
            assert r.status_code == 400
            assert client.get(f"/results/{sid}").status_code == 200
            assert iso.new_ids() == [sid]


NODE_CSV = "node_id,x_coord,y_coord,zone_id\n1,100000,200000,1\n2,101000,200000,2\n3,102000,200000,3\n"
LINK_CSV = "link_id,from_node_id,to_node_id,length,free_speed,lanes,capacity\n1,1,2,1,72,2,1800\n2,2,3,1,72,1,1800\n"
CONFIG_CSV = "long_length,speed\nkm,kmh\n"


class TestGmnsUnits:
    """T27 / §6.1 単位: 1 km → 1,000 m，72 km/時 → 20 m/秒，1,800 台/時/車線 × 2 車線 → 1.0 台/秒．"""

    def test_length_speed_and_capacity_conversion(self):
        sc = gmns_to_scenario(NODE_CSV, LINK_CSV, None, CONFIG_CSV, 1200)
        l1, l2 = sc["links"]
        assert l1["length"] == pytest.approx(1000.0) and l1["free_flow_speed"] == pytest.approx(20.0)
        assert l1["number_of_lanes"] == 2 and l1["capacity"] == pytest.approx(1.0)      # 車線係数は 1 回だけ
        assert l2["capacity"] == pytest.approx(0.5)
        assert [n["x"] for n in sc["nodes"]] == [100000.0, 101000.0, 102000.0]       # 直交座標はそのまま
        assert sc["notes"] == []

    def test_demand_units_explicit_or_notified(self):
        explicit_hour = "o_zone_id,d_zone_id,volume_per_hour\n1,3,1800\n"
        explicit_sec = "o_zone_id,d_zone_id,flow\n1,3,0.5\n"
        ambiguous_big = "o_zone_id,d_zone_id,volume\n1,3,1800\n"
        ambiguous_small = "o_zone_id,d_zone_id,volume\n1,3,5\n"
        for csv, flow, noted in ((explicit_hour, 0.5, False), (explicit_sec, 0.5, False),
                                 (ambiguous_big, 0.5, True), (ambiguous_small, 5.0, True)):
            sc = gmns_to_scenario(NODE_CSV, LINK_CSV, csv, CONFIG_CSV, 1200)
            assert sc["demands"][0]["flow"] == pytest.approx(flow), csv
            assert sc["demands"][0]["orig"] == "1" and sc["demands"][0]["dest"] == "3"
            assert bool(sc["notes"]) is noted, (csv, sc["notes"])
        assert demand_flow_unit("flow") == ("per_sec", None)
        assert demand_flow_unit("vph")[0] == "per_hour" and demand_flow_unit("trips_per_hr")[0] == "per_hour"
        unit, note = demand_flow_unit("volume")
        assert unit is None and "単位が明示されていない" in note

    def test_upload_reports_the_assumption(self, client):
        with IsolatedStore():
            r = client.post("/upload", data={"tmax": "600"}, files=[
                ("files", ("node.csv", NODE_CSV, "text/csv")), ("files", ("link.csv", LINK_CSV, "text/csv")),
                ("files", ("demand.csv", "o_zone_id,d_zone_id,volume\n1,3,1800\n", "text/csv")),
                ("files", ("config.csv", CONFIG_CSV, "text/csv"))])
            assert r.status_code == 200, r.text
            assert "単位が明示されていない" in r.json()["message"]
            sc = results_store[r.json()["id"]]["_scenario"]
            assert sc["demands"][0]["flow"] == pytest.approx(0.5) and sc["links"][0]["capacity"] == pytest.approx(1.0)


class TestNonFiniteInputs:
    """T28: 座標・速度・容量等の NaN / Infinity（JSON トークン・文字列）を UXsim 実行前に拒否する．"""

    def _with(self, path, value):
        sc = json.loads(json.dumps(F1))
        kind, idx, field = path
        sc[kind][idx][field] = value
        return sc

    @pytest.mark.parametrize("path, value", [
        (("nodes", 0, "x"), float("nan")), (("nodes", 1, "y"), float("inf")),
        (("links", 0, "length"), float("nan")), (("links", 0, "capacity"), "Infinity"),
        (("links", 0, "free_flow_speed"), "NaN"), (("demands", 0, "flow"), float("nan")),
        (("demands", 0, "t_end"), float("inf")),
    ])
    def test_simulate_rejects_non_finite(self, client, path, value):
        with IsolatedStore() as iso:
            # 標準 json.dumps は NaN / Infinity をトークンのまま出す（httpx の json= は拒否するので生で送る）
            r = client.post("/simulate", content=json.dumps(self._with(path, value), allow_nan=True),
                            headers={"Content-Type": "application/json"})
            assert r.status_code == 422, (path, value, r.text)
            assert iso.new_ids() == []

    def test_upload_and_tool_paths_reject_non_finite(self, client):
        import asyncio
        import types

        from risu.tools import ToolTurnState, collect_tool_results
        with IsolatedStore() as iso:
            bad = self._with(("links", 0, "length"), "NaN")
            r = _upload(client, "nan.json", json.dumps({"scenario": bad}).encode("utf-8"))
            assert r.status_code == 422, r.text
            tb = types.SimpleNamespace(name="run_simulation", input=self._with(("nodes", 0, "x"), float("nan")),
                                       id="t_nan", type="tool_use")
            results = asyncio.run(collect_tool_results([tb], ToolTurnState(None)))
            assert results[0].get("is_error") and "エラー" in results[0]["content"]
            assert iso.new_ids() == []
            # 比較: 有限値なら通る
            assert client.post("/simulate", json=F1).status_code == 200
