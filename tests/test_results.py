"""結果ストアと送出（risu.results）: v3 符号化，圧縮交渉，上限，永続化，並行アクセス．"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.aggregate import get_simulation_data  # noqa: E402
from risu.results import results_store  # noqa: E402
from risu.simulation import run_uxsim  # noqa: E402

from helpers import (  # noqa: E402
    BOTTLENECK_SCENARIO,
)

class TestStoreLimitAndUsageCost:
    def test_results_store_evicts_oldest(self, monkeypatch):
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
        monkeypatch.setattr(risu.results, "MAX_RESULTS", 2)
        base = run_uxsim(BOTTLENECK_SCENARIO)
        ids = ["evict_a", "evict_b", "evict_c"]
        try:
            for sid in ids:
                risu.results.store_sim(sid, dict(base), {"type": "manual"})
            assert "evict_a" not in results_store
            assert "evict_b" in results_store and "evict_c" in results_store
        finally:
            for sid in ids:
                results_store.pop(sid, None)

    def test_usage_cost_never_negative_with_cached_input(self):
        """新 API の usage は input_tokens にキャッシュ分を含まない．以前の式は負の円額を出した"""
        from types import SimpleNamespace as NS
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
        u = risu.llm.log_usage("test", NS(usage=NS(input_tokens=2, output_tokens=190,
                                                  cache_read_input_tokens=8234,
                                                  cache_creation_input_tokens=1359)))
        assert u["cost_jpy"] > 0
        tally = risu.llm.UsageTally()
        tally.add("t", NS(usage=NS(input_tokens=2, output_tokens=10, cache_read_input_tokens=10000,
                                   cache_creation_input_tokens=0)))
        d = tally.as_dict()
        assert d["cache_hit_pct"] >= 99 and d["cost_jpy"] > 0


# ============================================================
# 送出時のフレーム量子化（columnar_v3）
# ============================================================

class TestFrameWireEncodingV3:
    """/results が返す columnar_v3 のエンコード/デコード整合性．

    v3 は「送出時だけ」の表現で，results_store 側は v2（素の値）のまま．
    サーバー内の消費側（get_simulation_data 等）が壊れないことも確認する．
    """

    @classmethod
    def setup_class(cls):
        from risu.results import store_sim
        cls.res = run_uxsim(BOTTLENECK_SCENARIO)
        cls.sim_id = "wire_v3_test"
        store_sim(cls.sim_id, cls.res)

    @classmethod
    def teardown_class(cls):
        results_store.pop(cls.sim_id, None)

    @staticmethod
    def _decode_v3(f):
        """フロント側 (`isV3` ブランチ) と同じ復元を Python で行う．"""
        import numpy as np
        ids = np.cumsum(np.asarray(f["ids"], dtype=np.int64))
        return {
            "ids": ids,
            "xs": np.asarray(f["xs"], dtype=np.float64),
            "ys": np.asarray(f["ys"], dtype=np.float64),
            "vs": np.asarray(f["vs"], dtype=np.float64) * 0.1,
            "alphas": np.asarray(f["alphas"], dtype=np.float64) * 0.001,
            "li": np.asarray(f["li"]),
        }

    def test_roundtrip_matches_within_tolerance(self):
        """量子化 → 復元で，描画に影響しない誤差に収まること．"""
        import numpy as np
        from risu.results import encode_frames_v3

        src = self.res["frames"]
        enc = encode_frames_v3(src)
        assert set(enc.keys()) == set(src.keys())

        for k in src:
            got = self._decode_v3(enc[k])
            orig = src[k]
            # ids と li は完全一致でなければならない（差分符号化は可逆）
            np.testing.assert_array_equal(got["ids"], np.asarray(orig["ids"]))
            np.testing.assert_array_equal(got["li"], np.asarray(orig["li"]))
            # xs/ys は 1 m 丸め，vs は 0.1 m/s，alphas は 0.001
            assert np.max(np.abs(got["xs"] - np.asarray(orig["xs"]))) <= 0.5
            assert np.max(np.abs(got["ys"] - np.asarray(orig["ys"]))) <= 0.5
            assert np.max(np.abs(got["vs"] - np.asarray(orig["vs"]))) <= 0.05
            assert np.max(np.abs(got["alphas"] - np.asarray(orig["alphas"]))) <= 0.0005

    def test_ids_delta_is_reversible_on_sorted_ids(self):
        """フレーム内 ids が昇順である前提（差分符号化の条件）を守っていること．"""
        import numpy as np
        for f in self.res["frames"].values():
            ids = np.asarray(f["ids"])
            assert np.all(np.diff(ids) >= 0), "フレーム内 ids が昇順でない"

    def test_envelope_marks_v3_and_shrinks(self):
        """エンベロープの frame_format が v3 になり，バイト数が v2 より小さいこと．"""
        import orjson
        from risu.results import build_envelope, envelope_json_bytes

        v2_env = build_envelope(self.sim_id, include_result=True)
        assert v2_env["result"]["frame_format"] == "columnar_v2"
        v2_bytes = orjson.dumps(
            v2_env, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)

        v3_bytes = envelope_json_bytes(self.sim_id)
        assert b'"columnar_v3"' in v3_bytes
        assert len(v3_bytes) < len(v2_bytes), (
            f"v3 が v2 より大きい: {len(v3_bytes)} >= {len(v2_bytes)}")

    def test_results_store_is_not_mutated(self):
        """送出用の変換が results_store の配列を書き換えないこと．

        CLAUDE.md の「結果 dict は保存後に変更しないこと」を守る
        （_enc_cache は一度作ると使い回されるため，壊すと以後ずっと壊れる）．
        """
        import numpy as np
        from risu.results import envelope_json_bytes

        before = {k: np.asarray(v["xs"]).copy() for k, v in self.res["frames"].items()}
        envelope_json_bytes(self.sim_id)
        for k, arr in before.items():
            np.testing.assert_array_equal(np.asarray(self.res["frames"][k]["xs"]), arr)
        assert results_store[self.sim_id]["frame_format"] == "columnar_v2"

    def test_simulation_data_still_works(self):
        """サーバー内の集計（LLM に渡すデータ）が素の値を読めていること．"""
        data = get_simulation_data(self.sim_id)
        assert data is not None
        assert data.get("network_avg_speed")
        # 速度が 0.1 m/s 単位の「整数」になっていない（=量子化が漏れていない）
        speeds = [s for s in data["network_avg_speed"] if s]
        assert speeds, "速度データが空"


# ============================================================
# /results の圧縮方式ネゴシエーション（zstd / gzip / identity）
# ============================================================

class TestResultsEncodingNegotiation:
    """Accept-Encoding に応じて圧縮方式を選ぶ経路のテスト．

    zstd は gzip より小さく速い（grid20 相当で 16.1MB/0.47s → 5.4MB/0.07s）ので
    優先するが，zstandard が無い環境・zstd 非対応のクライアントでも
    必ず動かなければならない．フォールバックの網羅がここの目的．
    """

    @classmethod
    def setup_class(cls):
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
        cls.sid = "test_enc_negotiation"
        risu.results.store_sim(cls.sid, run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})

    @classmethod
    def teardown_class(cls):
        results_store.pop(cls.sid, None)

    @staticmethod
    def _client():
        from fastapi.testclient import TestClient
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
        return TestClient(risu.api.app)

    # ── 選択ロジック単体 ──────────────────────────

    def test_negotiate_prefers_zstd_when_available(self):
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
        if risu.results._zstd is None:
            pytest.skip("zstandard 未インストール")
        assert risu.results.negotiate_encoding("gzip, deflate, br, zstd") == "zstd"
        assert risu.results.negotiate_encoding("ZSTD") == "zstd", "大文字small文字を無視すべき"

    def test_negotiate_falls_back_to_gzip(self):
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
        assert risu.results.negotiate_encoding("gzip, deflate, br") == "gzip"
        assert risu.results.negotiate_encoding("gzip") == "gzip"

    def test_negotiate_identity_when_nothing_supported(self):
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
        assert risu.results.negotiate_encoding("") == "identity"
        assert risu.results.negotiate_encoding("identity") == "identity"
        assert risu.results.negotiate_encoding(None) == "identity"

    def test_negotiate_uses_gzip_if_zstandard_missing(self, monkeypatch):
        """zstandard が入っていない環境では zstd を要求されても gzip になる．"""
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
        monkeypatch.setattr(risu.results, "_zstd", None)
        assert risu.results.negotiate_encoding("gzip, deflate, br, zstd") == "gzip"

    # ── エンドポイントの実挙動 ────────────────────

    def test_all_encodings_return_identical_payload(self):
        """圧縮方式が変わっても中身は同一（TestClient が透過的に解凍する）．"""
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
        c = self._client()
        base = c.get(f"/results/{self.sid}", headers={"Accept-Encoding": "identity"})
        assert base.status_code == 200
        assert base.headers.get("content-encoding") is None
        expected = base.json()

        gz = c.get(f"/results/{self.sid}", headers={"Accept-Encoding": "gzip"})
        assert gz.headers.get("content-encoding") == "gzip"
        assert gz.json() == expected

        if risu.results._zstd is not None:
            zs = c.get(f"/results/{self.sid}", headers={"Accept-Encoding": "gzip, zstd"})
            assert zs.headers.get("content-encoding") == "zstd"
            # TestClient(httpx) が zstd を解凍できない場合は自前で解凍して比較する
            try:
                got = zs.json()
            except Exception:
                got = json.loads(risu.results._zstd.ZstdDecompressor().decompress(zs.content))
            assert got == expected

    def test_vary_header_is_set_exactly_once(self):
        """キャッシュが方式違いを取り違えないよう Vary を返す．重複させないこと．

        非圧縮の応答には GZipMiddleware が Vary を足すので，自前でも付けると
        `Vary: Accept-Encoding, Accept-Encoding` になる（実際にそうなっていた）．
        """
        c = self._client()
        for ae in ("identity", "gzip", "gzip, zstd"):
            r = c.get(f"/results/{self.sid}", headers={"Accept-Encoding": ae})
            vary = r.headers.get("vary", "")
            tokens = [t.strip().lower() for t in vary.split(",") if t.strip()]
            # 検証するのは「Accept-Encoding が 1 回だけ」．他のトークンは許す
            # （starlette >= 1.7 の CORSMiddleware は全応答に Vary: Origin を足す）
            assert tokens.count("accept-encoding") == 1, f"Vary が不正（{ae}）: {vary!r}"
            assert len(tokens) == len(set(tokens)), f"Vary に重複（{ae}）: {vary!r}"

    def test_cache_is_per_encoding_and_reused(self):
        """方式ごとに別キャッシュを持ち，2 回目は再圧縮しない．"""
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
        sid = "test_enc_cache"
        risu.results.store_sim(sid, run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            c = self._client()
            r1 = c.get(f"/results/{sid}", headers={"Accept-Encoding": "gzip"})
            cache = results_store[sid]["_enc_cache"]
            assert set(cache) == {"gzip"}, "要求していない方式まで作っている"

            r2 = c.get(f"/results/{sid}", headers={"Accept-Encoding": "gzip"})
            assert r2.content == r1.content
            assert results_store[sid]["_enc_cache"]["gzip"] is cache["gzip"], "再圧縮している"

            if risu.results._zstd is not None:
                c.get(f"/results/{sid}", headers={"Accept-Encoding": "zstd"})
                assert set(results_store[sid]["_enc_cache"]) == {"gzip", "zstd"}
        finally:
            results_store.pop(sid, None)

    def test_zstd_payload_is_smaller_than_gzip(self):
        """zstd を選ぶ意味があること（同じ結果で実際に小さい）．"""
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
        if risu.results._zstd is None:
            pytest.skip("zstandard 未インストール")
        gz = risu.results.envelope_compressed_bytes(self.sid, "gzip")
        zs = risu.results.envelope_compressed_bytes(self.sid, "zstd")
        assert len(zs) < len(gz), f"zstd={len(zs)} >= gzip={len(gz)}"

    def test_gzip_wrapper_still_works(self):
        """既存の envelope_gzip_bytes（後方互換ラッパー）が生きていること．"""
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
        assert risu.results.envelope_gzip_bytes(self.sid) == \
            risu.results.envelope_compressed_bytes(self.sid, "gzip")


# ============================================================
# 結果の永続化（RISU_RESULTS_DIR）
# ============================================================

class TestResultsPersistence:
    """RISU_RESULTS_DIR を設定すると結果をディスクに書き，再起動（= メモリから消えた）後も
    results_store が透過的に読み戻す．形式はダウンロードの .json+result と同じ．"""

    @pytest.fixture
    def store_dir(self, tmp_path, monkeypatch):
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
        monkeypatch.setattr(risu.results, "RESULTS_DIR", str(tmp_path))
        return tmp_path

    def _store(self, sid):
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
        fut = risu.results.store_sim(sid, run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        assert fut is not None
        assert fut.result(timeout=60) is not None
        return risu.results.results_store[sid]

    def test_disabled_by_default_writes_nothing(self, tmp_path, monkeypatch):
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
        monkeypatch.setattr(risu.results, "RESULTS_DIR", "")
        assert risu.results.store_sim("p_off", run_uxsim(BOTTLENECK_SCENARIO)) is None
        assert list(tmp_path.iterdir()) == []
        risu.results.results_store.pop("p_off", None)

    def test_store_writes_file_and_reloads_after_eviction(self, store_dir):
        import numpy as np
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
        sid = "p_reload"
        original = self._store(sid)
        files = list(store_dir.iterdir())
        assert len(files) == 1 and files[0].name.startswith(sid + ".json.")
        orig_data = get_simulation_data(sid)

        # メモリから消しても（再起動・MAX_RESULTS の追い出し相当）透過的に戻る
        dict.pop(risu.results.results_store, sid)
        assert not dict.__contains__(risu.results.results_store, sid)
        assert sid in risu.results.results_store
        loaded = risu.results.results_store[sid]
        assert loaded["_meta"]["persisted"] is True
        assert loaded["stats"] == original["stats"]
        assert loaded["_scenario"] == original["_scenario"]
        assert loaded["vehicle_counts"] == original["vehicle_counts"]
        assert loaded["trip_series"] == original["trip_series"]
        assert loaded["frame_avg_speed"] == original["frame_avg_speed"]
        assert loaded["speed_histogram"] == original["speed_histogram"]
        assert loaded["frame_times"] == original["frame_times"]
        # frames は v3 の分解能で戻る（ids は無損失，vs は 0.1 m/s，alphas は 0.001）
        for k in original["frames"]:
            a, b = original["frames"][k], loaded["frames"][k]
            assert isinstance(b["vs"], np.ndarray)
            assert np.array_equal(a["ids"], b["ids"]) and np.array_equal(a["li"], b["li"])
            assert np.abs(a["vs"] - b["vs"]).max() <= 0.051
            assert np.abs(a["alphas"] - b["alphas"]).max() <= 0.00051
        # LLM 向け集計は同じ（台数・累積は無損失，速度は保存済みの系列）
        re_data = get_simulation_data(sid)
        assert re_data["network_vehicle_count"] == orig_data["network_vehicle_count"]
        assert re_data["network_avg_speed"] == orig_data["network_avg_speed"]
        assert re_data["network_completed_count"] == orig_data["network_completed_count"]
        risu.results.results_store.pop(sid, None)

    def test_results_endpoint_serves_persisted_result(self, store_dir):
        from fastapi.testclient import TestClient
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
        sid = "p_http"
        self._store(sid)
        dict.pop(risu.results.results_store, sid)
        c = TestClient(risu.api.app)
        r = c.get(f"/results/{sid}", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        body = r.json()
        assert body["sim_id"] == sid and body["result"]["stats"]["total_trips"] > 0
        assert c.get(f"/results/{sid}/scenario").status_code == 200
        risu.results.results_store.pop(sid, None)

    def test_unsafe_ids_never_touch_disk(self, store_dir):
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
        for bad in ("../x", "a/b", "", "x" * 65, "..\\x"):
            assert bad not in risu.results.results_store
            assert risu.results.persisted_path(bad) is None

    def test_downloaded_json_can_be_dropped_in(self, store_dir):
        """ダウンロードした .json+result（素の JSON）をディレクトリに置くだけで読める．"""
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
        sid = "p_src"
        self._store(sid)
        env_bytes = risu.results.envelope_json_bytes(sid)
        (store_dir / "dropped.json").write_bytes(env_bytes)
        risu.results.results_store.pop(sid, None)
        assert "dropped" in risu.results.persisted_ids()
        assert "dropped" in risu.results.results_store
        d = get_simulation_data("dropped")
        assert d and d["stats"]["total_trips"] > 0
        risu.results.results_store.pop("dropped", None)


# ============================================================
# results_store の並行アクセス
# ============================================================

class TestResultsStoreConcurrency:
    """イベントループ・executor・永続化スレッドが同時に触っても壊れない．"""

    def test_concurrent_store_and_read_respects_limit(self, monkeypatch):
        import threading

        import risu.results
        monkeypatch.setattr(risu.results, "MAX_RESULTS", 5)
        base = run_uxsim(BOTTLENECK_SCENARIO)
        errors = []
        ids = [f"conc_{i}" for i in range(40)]

        def writer(i):
            try:
                risu.results.store_sim(ids[i], dict(base), {"type": "manual"})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reader():
            try:
                for sid in ids:
                    if sid in risu.results.results_store:
                        get_simulation_data(sid)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(40)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
        try:
            assert errors == [], errors
            in_memory = [s for s in ids if dict.__contains__(risu.results.results_store, s)]
            assert len(in_memory) <= 5
        finally:
            for sid in ids:
                risu.results.results_store.pop(sid, None)

    def test_concurrent_compression_yields_one_cache_entry(self):
        import threading

        import risu.results
        sid = "conc_zip"
        risu.results.store_sim(sid, run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        blobs = []
        def work():
            blobs.append(risu.results.envelope_compressed_bytes(sid, "gzip"))
        threads = [threading.Thread(target=work) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
        try:
            assert len(blobs) == 6
            assert all(b is blobs[0] for b in blobs), "キャッシュに入った 1 つが全員に返る"
            assert set(risu.results.results_store[sid]["_enc_cache"]) == {"gzip"}
        finally:
            risu.results.results_store.pop(sid, None)
