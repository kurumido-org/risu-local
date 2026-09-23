"""HTTP 層（risu.api）: 起動中サーバーへのエンドポイント確認と待ち受けの既定値．"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))



# ============================================================
# 7. API エンドポイントテスト（サーバー起動が必要）
# ============================================================

class TestAPIEndpoints:
    """
    サーバーが起動している場合のみ実行．
    pytest tests/ -v -k "api" で選択実行可．
    """

    API = "http://localhost:8001"

    @pytest.fixture(scope="class")
    def client(self):
        import httpx
        try:
            r = httpx.get(f"{self.API}/docs", timeout=3)
            if r.status_code != 200:
                pytest.skip("サーバー未起動")
        except Exception:
            pytest.skip("サーバー未起動")
        return httpx.Client(base_url=self.API, timeout=60)

    @pytest.fixture(scope="class")
    def sim_id(self, client):
        """テスト用シミュレーションを実行"""
        resp = client.post("/simulate", json={
            "nodes": [
                {"name": "A", "x": 0, "y": 0},
                {"name": "B", "x": 5000, "y": 0},
            ],
            "links": [
                {"name": "r1", "start": "A", "end": "B", "length": 5000},
            ],
            "demands": [
                {"orig": "A", "dest": "B", "t_start": 0, "t_end": 300, "flow": 0.5},
            ],
            "tmax": 800,
        })
        assert resp.status_code == 200
        return resp.json()["id"]

    def test_simulate_returns_id_and_stats(self, client):
        resp = client.post("/simulate", json={
            "nodes": [{"name": "X", "x": 0, "y": 0}, {"name": "Y", "x": 1000, "y": 0}],
            "links": [{"name": "xy", "start": "X", "end": "Y", "length": 1000}],
            "demands": [{"orig": "X", "dest": "Y", "t_start": 0, "t_end": 100, "flow": 0.3}],
            "tmax": 500,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "stats" in data

    def test_results_endpoint(self, client, sim_id):
        resp = client.get(f"/results/{sim_id}")
        assert resp.status_code == 200
        data = resp.json()
        # 新スキーマ (risu_schema_version 1.0): result が入れ子
        assert data.get("risu_schema_version") == "1.0"
        assert "scenario" in data
        assert "source" in data
        assert "result" in data
        r = data["result"]
        assert "geojson" in r
        assert "frames" in r
        assert "frame_times" in r
        assert "stats" in r
        assert "tmax" in r

    def test_results_scenario_endpoint(self, client, sim_id):
        """軽量シナリオ DL エンドポイントは result を含まない"""
        resp = client.get(f"/results/{sim_id}/scenario")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("risu_schema_version") == "1.0"
        assert "scenario" in data
        scn = data["scenario"]
        assert "nodes" in scn and "links" in scn and "demands" in scn
        assert "result" not in data  # 結果データは含まない

    def test_roundtrip_scenario_dl(self, client, sim_id):
        """シナリオ DL → /upload で再現できることを確認"""
        # 1. シナリオ取得
        resp = client.get(f"/results/{sim_id}/scenario")
        scenario_dl = resp.json()
        # 2. オリジナルの統計
        orig = client.get(f"/results/{sim_id}").json()["result"]["stats"]
        # 3. 復元アップロード
        import json as _json
        resp = client.post(
            "/upload",
            files={"files": ("scn.json", _json.dumps(scenario_dl), "application/json")},
            data={"tmax": "500"},
        )
        assert resp.status_code == 200
        new_stats = resp.json()["stats"]
        # 4. 主要統計の一致
        for k in ("total_trips", "completed_trips", "average_travel_time_s"):
            assert orig.get(k) == new_stats.get(k), f"{k} differs: {orig.get(k)} vs {new_stats.get(k)}"

    def test_results_404(self, client):
        resp = client.get("/results/nonexistent_12345")
        assert resp.status_code == 404

    def test_upload_risu_csv(self, client):
        """RISU CSV のアップロード"""
        csv_content = (
            "type,name,x,y,start,end,length,free_flow_speed,number_of_lanes,orig,dest,t_start,t_end,flow\n"
            "node,A,0,0,,,,,,,,,,\n"
            "node,B,3000,0,,,,,,,,,,\n"
            "link,r1,,,A,B,3000,20,1,,,,,\n"
            "demand,,,,,,,,,A,B,0,300,0.4\n"
        )
        resp = client.post("/upload", data={"tmax": "800"}, files={
            "files": ("test.csv", csv_content, "text/csv"),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "stats" in data

    def test_docs_endpoint(self, client):
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_static_index(self, client):
        """index.html が配信される"""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "RISU" in resp.text

    def test_static_sample_csv(self, client):
        """サンプル CSV がダウンロードできる"""
        resp = client.get("/sample_risu.csv")
        assert resp.status_code == 200
        assert "type,name" in resp.text


# ============================================================
# 待ち受け設定（認証が無いので既定は localhost に限定する）
# ============================================================

class TestBindDefaults:
    """RISU は認証を持たないため，既定で外部に開いてはいけない．

    0.0.0.0 で待ち受けると，同一 LAN の誰でもシミュレーション実行・結果閲覧・
    /chat 経由の LLM 呼び出し（= サーバー所有者の API キーでの課金）ができてしまう．
    「うっかり公開」を防ぐため，既定値をここで固定する．
    """

    def test_default_host_is_loopback_only(self):
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        assert risu.api.RISU_HOST == "127.0.0.1", (
            f"既定の待ち受けが {risu.api.RISU_HOST} になっている．"
            "認証が無いので既定は 127.0.0.1 でなければならない"
        )

    def test_default_reload_is_off(self):
        """オートリロードは開発用．既定で有効だとプロセスが 2 つ起動する．"""
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        assert risu.api.RISU_RELOAD is False

    def test_host_and_port_are_overridable(self, monkeypatch):
        """別マシンから使いたい人は環境変数で明示的に開ける．"""
        import importlib

        import risu.api as _s   # 待ち受け設定は risu.api が env から読む（server.py は再公開のみ）
        monkeypatch.setenv("RISU_HOST", "0.0.0.0")
        monkeypatch.setenv("RISU_PORT", "9000")
        monkeypatch.setenv("RISU_RELOAD", "1")
        try:
            reloaded = importlib.reload(_s)
            assert reloaded.RISU_HOST == "0.0.0.0"
            assert reloaded.RISU_PORT == 9000
            assert reloaded.RISU_RELOAD is True
        finally:
            monkeypatch.delenv("RISU_HOST", raising=False)
            monkeypatch.delenv("RISU_PORT", raising=False)
            monkeypatch.delenv("RISU_RELOAD", raising=False)
            importlib.reload(_s)   # 他のテストに影響しないよう戻す

    def test_cors_default_is_localhost_only(self):
        """CORS も既定は localhost のみ（ブラウザ経由の横取りを防ぐ）．"""
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        assert all("localhost" in o or "127.0.0.1" in o for o in risu.api.ALLOWED_ORIGINS), \
            f"CORS の既定に外部オリジンが含まれている: {risu.api.ALLOWED_ORIGINS}"
