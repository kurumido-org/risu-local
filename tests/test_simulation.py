"""UXsim の実行と後処理（risu.simulation）: 出力の形，双方向リンク，容量，信号メタ，フレームキー，後処理パイプライン，素の UXsim との一致．"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.aggregate import _get_simulation_data  # noqa: E402
from risu.results import results_store  # noqa: E402
from risu.schema import SimulationInput  # noqa: E402
from risu.simulation import _run_uxsim  # noqa: E402

from helpers import (  # noqa: E402
    BOTTLENECK_SCENARIO, GRID_BIDIRECTIONAL_SCENARIO,
)

# ============================================================
# 1. UXsim 実行結果の構造テスト
# ============================================================

class TestUXsimOutput:
    """_run_uxsim() の出力データ構造が正しいことを検証"""

    @pytest.fixture(scope="class")
    def result(self):
        return _run_uxsim(BOTTLENECK_SCENARIO)

    def test_top_level_keys(self, result):
        """必須キーが全て存在する"""
        assert "geojson" in result
        assert "frames" in result
        assert "frame_times" in result
        assert "stats" in result
        assert "tmax" in result

    def test_geojson_structure(self, result):
        """GeoJSON が FeatureCollection 形式"""
        geo = result["geojson"]
        assert geo["type"] == "FeatureCollection"
        assert len(geo["features"]) > 0

    def test_link_properties(self, result):
        """各リンクに必須プロパティが存在する"""
        for f in result["geojson"]["features"]:
            props = f["properties"]
            assert "name" in props
            assert "length" in props
            assert "free_flow_speed" in props
            assert "number_of_lanes" in props
            # タイムラインが存在する (リンクレベル描画用)
            assert "timeline" in props
            assert isinstance(props["timeline"], list)

    def test_link_timeline_format(self, result):
        """
        タイムラインの各エントリに t と speed が含まれる．
        [修正履歴] タイムラインを一度削除してしまい LINK モードが壊れた．
        """
        for f in result["geojson"]["features"]:
            tl = f["properties"]["timeline"]
            if len(tl) > 0:
                assert "t" in tl[0]
                assert "speed" in tl[0]

    def test_link_coordinates(self, result):
        """リンクが2点の LineString である"""
        for f in result["geojson"]["features"]:
            assert f["geometry"]["type"] == "LineString"
            coords = f["geometry"]["coordinates"]
            assert len(coords) == 2
            assert len(coords[0]) == 2  # [x, y]

    def test_frames_exist(self, result):
        """フレームデータが空でない"""
        assert len(result["frames"]) > 0
        assert len(result["frame_times"]) > 0

    def test_frame_times_sorted(self, result):
        """frame_times がソート済み"""
        ft = result["frame_times"]
        assert ft == sorted(ft)

    def test_frame_key_consistency(self, result):
        """
        frame_times の各値を str() したものが frames のキーに存在する．
        [修正履歴] Python は "25.0" をキーにするが，
        JS の String(25.0) は "25" になりマッチしなかった．
        → フロントエンドで parseFloat 正規化で対処．
        このテストはサーバー側のキー形式を記録する．
        """
        frames = result["frames"]
        for t in result["frame_times"]:
            key = str(round(t, 1))
            assert key in frames, f"frame_times の {t} に対応するキー '{key}' が frames にない"

    def test_vehicle_data_fields(self, result):
        """
        フレームがコンパクト列指向フォーマット（columnar_v2）である．
        各フレームは {ids, xs, ys, vs, alphas, li} の同じ長さの列を持つ．
        [修正履歴] 旧形式は車両ごとの dict のリスト．ペイロード削減のため
        列指向に移行した（li はリンク index，名前は link_names で解決）．
        """
        assert result.get("frame_format") == "columnar_v2"
        assert "link_names" in result
        found_vehicle = False
        for key, cols in result["frames"].items():
            for col in ("ids", "xs", "ys", "vs", "alphas", "li"):
                assert col in cols, f"列 {col} が必要"
            n = len(cols["ids"])
            assert all(len(cols[c]) == n for c in ("xs", "ys", "vs", "alphas", "li")), \
                "全列の長さが一致すること"
            if n > 0:
                found_vehicle = True
                assert all(0 <= a <= 1 for a in cols["alphas"]), "alpha が範囲外"
                n_links = len(result["link_names"])
                assert all(0 <= li < n_links for li in cols["li"]), "li が範囲外"
        assert found_vehicle, "走行中の車両が1台も見つからない"

    def test_vehicle_speed_reasonable(self, result):
        """車両速度が非負で，リンク自由流速度の2倍以内"""
        ffs_by_name = {
            f["properties"]["name"]: f["properties"]["free_flow_speed"]
            for f in result["geojson"]["features"]
        }
        link_names = result["link_names"]
        for key, cols in result["frames"].items():
            for spd, li in zip(cols["vs"], cols["li"]):
                assert spd >= 0, f"速度が負: {spd}"
                ffs = ffs_by_name[link_names[li]]
                assert spd <= ffs * 2, f"速度が異常に高い: {spd} > {ffs*2}"

    def test_stats_fields(self, result):
        """統計情報の必須フィールド"""
        s = result["stats"]
        assert "total_trips" in s
        assert "completed_trips" in s
        assert "average_travel_time_s" in s
        assert "simulation_time_s" in s
        assert s["total_trips"] >= s["completed_trips"]


# ============================================================
# 2. 双方向道路テスト
# ============================================================

class TestBidirectionalLinks:
    """
    [修正履歴] 双方向リンクが存在しないとフロントエンドで
    円弧表示にならない．逆方向リンクの存在確認．
    """

    @pytest.fixture(scope="class")
    def result(self):
        return _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO)

    def test_bidirectional_pairs_exist(self, result):
        """A→B があれば B→A も存在する（双方向リンクの場合）"""
        features = result["geojson"]["features"]
        edges = set()
        for f in features:
            coords = f["geometry"]["coordinates"]
            key = (tuple(coords[0]), tuple(coords[1]))
            edges.add(key)

        for f in features:
            coords = f["geometry"]["coordinates"]
            reverse = (tuple(coords[1]), tuple(coords[0]))
            if "r" in f["properties"]["name"]:
                # 逆方向リンクには対応する順方向がある
                assert reverse in edges, f"逆方向リンク {f['properties']['name']} の順方向が見つからない"

    def test_both_directions_have_vehicles(self, result):
        """双方向需要がある場合，両方向にリンクに車両がいる"""
        link_names = result["link_names"]
        link_names_with_vehicles = set()
        for key, cols in result["frames"].items():
            for li in cols["li"]:
                link_names_with_vehicles.add(link_names[li])

        # 順方向・逆方向の両方に車両がいるはず
        assert any(not n.endswith("r") for n in link_names_with_vehicles), "順方向リンクに車両がいない"
        assert any(n.endswith("r") for n in link_names_with_vehicles), "逆方向リンクに車両がいない"


# ============================================================
# 2a. リンク容量テスト
# ============================================================

class TestLinkCapacity:
    """
    link.capacity（台/s，リンク全体）で容量を明示制御できること．
    UXsim の capacity_out にマップされ，下流端がボトルネックになる．
    """

    def _run(self, capacity):
        # 注意: capacity（capacity_out）はリンク終点が目的地そのものの場合は
        # 作用しない（車両は境界を通らず到着・消滅する）ため，
        # ボトルネックリンクの下流にもう 1 リンク置く
        link = {"name": "r1", "start": "A", "end": "B", "length": 1000}
        if capacity is not None:
            link["capacity"] = capacity
        scenario = SimulationInput(
            name="cap_test", tmax=2400, deltan=5,
            nodes=[{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0},
                   {"name": "C", "x": 2000, "y": 0}],
            links=[link, {"name": "r2", "start": "B", "end": "C", "length": 1000}],
            demands=[{"orig": "A", "dest": "C", "t_start": 0, "t_end": 600, "flow": 0.5}],
        )
        return _run_uxsim(scenario)

    def test_capacity_caps_throughput(self):
        """容量指定で旅行時間が明確に悪化する（ボトルネック形成）"""
        free = self._run(None)
        capped = self._run(0.2)  # 需要 0.5 台/s > 容量 0.2 台/s
        assert free["stats"]["average_travel_time_s"] is not None
        assert capped["stats"]["average_travel_time_s"] > free["stats"]["average_travel_time_s"] * 2, \
            f"capacity が効いていない: free={free['stats']}, capped={capped['stats']}"

    def test_capacity_roundtrip_in_scenario(self):
        """再現用シナリオ（envelope）に capacity が保存される"""
        capped = self._run(0.2)
        lk = capped["_scenario"]["links"][0]
        assert lk["capacity"] == 0.2

    def test_no_capacity_is_none(self):
        free = self._run(None)
        assert free["_scenario"]["links"][0]["capacity"] is None


# ============================================================
# 2b. 信号メタデータテスト
# ============================================================

class TestSignalMetadata:
    """
    [修正履歴] signals[].groups が交差点ごとにフィルタされておらず，
    複数の信号交差点があると同じ信号機が交差点の数だけ重複描画された．
    groups は「その交差点に流入する signal_group 付きリンク」のみを含むこと．
    """

    @pytest.fixture(scope="class")
    def result(self):
        # 信号交差点を 2 つ持つ直線ネットワーク: W → I1 → I2 → E
        scenario = SimulationInput(
            name="two_signals",
            tmax=600,
            deltan=5,
            nodes=[
                {"name": "W",  "x": 0,    "y": 0},
                {"name": "I1", "x": 1000, "y": 0, "signal": [30, 30]},
                {"name": "I2", "x": 2000, "y": 0, "signal": [40, 20]},
                {"name": "E",  "x": 3000, "y": 0},
            ],
            links=[
                {"name": "W_I1",  "start": "W",  "end": "I1", "length": 1000, "signal_group": 0},
                {"name": "I1_I2", "start": "I1", "end": "I2", "length": 1000, "signal_group": 0},
                {"name": "I2_E",  "start": "I2", "end": "E",  "length": 1000},
            ],
            demands=[{"orig": "W", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.4}],
        )
        return _run_uxsim(scenario)

    def test_signals_present(self, result):
        """信号ノードごとに 1 エントリ"""
        names = sorted(s["node"] for s in result["signals"])
        assert names == ["I1", "I2"]

    def test_groups_are_lists_and_deltat_is_sent(self, result):
        """groups は実際に適用された現示番号のリスト．deltat は UXsim の実 DELTAT．"""
        by_node = {s["node"]: s for s in result["signals"]}
        assert by_node["I1"]["groups"]["W_I1"] == [0]
        assert by_node["I1"]["deltat"] == 5.0        # deltan 5 × reaction_time 1.0
        assert len(by_node["I1"]["phase_log"]) == 600 / 5

    def test_omitted_signal_group_means_all_phases(self):
        """[修正履歴] signal_group 省略時，説明は「常時通行可能」なのに UXsim の既定 [0]
        （現示 0 だけ青）が使われ，画面の信号データにもそのリンクが出なかった．"""
        from uxsim_bridge import build_world, scenario_from_dict
        sc = {
            "name": "omit_group", "tmax": 600, "deltan": 5, "reaction_time": 1.7,
            "nodes": [{"name": "W", "x": 0, "y": 0}, {"name": "S", "x": 1000, "y": -1000},
                      {"name": "I", "x": 1000, "y": 0, "signal": [30, 30]},
                      {"name": "E", "x": 2000, "y": 0}],
            "links": [{"name": "W_I", "start": "W", "end": "I", "length": 1000, "signal_group": 0},
                      {"name": "S_I", "start": "S", "end": "I", "length": 1000},   # 省略
                      {"name": "I_E", "start": "I", "end": "E", "length": 1000}],
            "demands": [{"orig": "W", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.3},
                        {"orig": "S", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.3}],
        }
        W = build_world(scenario_from_dict(sc))
        assert list(W._risu_link_map["S_I"].signal_group) == [0, 1]
        assert list(W._risu_link_map["W_I"].signal_group) == [0]
        assert list(W._risu_link_map["I_E"].signal_group) == [0]  # 信号なしノードへは既定のまま

        res = _run_uxsim(SimulationInput(**sc))
        sig = {s["node"]: s for s in res["signals"]}["I"]
        assert sig["groups"] == {"W_I": [0], "S_I": [0, 1]}
        assert sig["deltat"] == 8.5   # 5 × 1.7．tmax/len(phase_log) = 600/70 = 8.571 ではない
        assert len(sig["phase_log"]) == int(600 / 8.5)

        # --emit した単体スクリプトも同じ展開をする
        import importlib.util
        import tempfile
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
        import run_scenario
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "emitted_sig.py")
            run_scenario.emit_python({"scenario": sc}, out, source="test")
            spec = importlib.util.spec_from_file_location("emitted_sig_mod", out)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            W2 = mod.build()
            groups = {lk.name: list(lk.signal_group) for lk in W2.LINKS}
            assert groups["S_I"] == [0, 1] and groups["W_I"] == [0]

    def test_groups_scoped_to_intersection(self, result):
        """groups はその交差点への流入リンクのみ（重複描画バグの回帰テスト）"""
        by_node = {s["node"]: s for s in result["signals"]}
        assert set(by_node["I1"]["groups"].keys()) == {"W_I1"}, \
            "I1 の groups に他交差点のリンクが混入している"
        assert set(by_node["I2"]["groups"].keys()) == {"I1_I2"}, \
            "I2 の groups に他交差点のリンクが混入している"

    def test_phases_match_scenario(self, result):
        by_node = {s["node"]: s for s in result["signals"]}
        assert by_node["I1"]["phases"] == [30, 30]
        assert by_node["I2"]["phases"] == [40, 20]


# ============================================================
# 5. フレームキー正規化テスト（フロントエンド互換性）
# ============================================================

class TestFrameKeyCompatibility:
    """
    [修正履歴] Python の str(round(25.0, 1)) = "25.0" だが
    JavaScript の String(25.0) = "25"．
    フロントエンドで parseFloat 正規化しているため，
    サーバー側のキーが一貫していることを確認．
    """

    @pytest.fixture(scope="class")
    def result(self):
        return _run_uxsim(BOTTLENECK_SCENARIO)

    def test_frame_keys_are_strings(self, result):
        """フレームキーが文字列"""
        for key in result["frames"].keys():
            assert isinstance(key, str)

    def test_frame_keys_parseable_as_float(self, result):
        """フレームキーが float に変換可能"""
        for key in result["frames"].keys():
            float(key)  # 例外が出なければOK

    def test_js_string_conversion_mismatch_documented(self, result):
        """
        Python の "25.0" と JS の "25" の不一致を文書化．
        フロントエンドの loadResult() で正規化している:
            framesData[String(parseFloat(k))] = v
        サーバー側は "25.0" 形式を返す．
        """
        has_decimal_key = any("." in k for k in result["frames"].keys())
        assert has_decimal_key, (
            "フレームキーが小数点を含んでいない．"
            "フロントエンドの正規化ロジックとの整合性を確認すること．"
        )


# ============================================================
# 9. エッジケーステスト
# ============================================================

class TestEdgeCases:
    """境界値・異常系のテスト"""

    def test_zero_demand(self):
        """需要ゼロでもエラーにならない"""
        scenario = SimulationInput(
            name="zero_demand",
            tmax=100,
            deltan=5,
            nodes=[{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}],
            links=[{"name": "r", "start": "A", "end": "B", "length": 1000}],
            demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 10, "flow": 0.0}],
        )
        result = _run_uxsim(scenario)
        assert result["stats"]["total_trips"] == 0

    def test_single_link(self):
        """最小構成（1リンク）でエラーにならない"""
        scenario = SimulationInput(
            name="minimal",
            tmax=200,
            deltan=5,
            nodes=[{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 500, "y": 0}],
            links=[{"name": "r", "start": "A", "end": "B", "length": 500}],
            demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 50, "flow": 0.3}],
        )
        result = _run_uxsim(scenario)
        assert "geojson" in result
        assert "frames" in result

    def test_frame_count_reasonable(self):
        """
        フレーム数がtmaxに対して妥当な範囲にある．
        _run_uxsim はフレームを間引きしない（全ステップを返す）．
        間引きは _get_simulation_data で行われる（最大40点サンプリング）．
        """
        scenario = SimulationInput(
            name="long_sim",
            tmax=5000,
            deltan=5,
            nodes=[{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 5000, "y": 0}],
            links=[{"name": "r", "start": "A", "end": "B", "length": 5000}],
            demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 2000, "flow": 0.5}],
        )
        result = _run_uxsim(scenario)
        n_frames = len(result["frame_times"])
        # フレーム数はtmax / recording_interval 程度（100〜500の範囲）
        assert 50 <= n_frames <= 600, f"Unexpected frame count: {n_frames}"


# ============================================================
# 9. 後処理パイプライン（ベクトル化・間引き・/results キャッシュ）
# ============================================================

class TestPostProcessingPipeline:
    """
    [修正履歴] 大規模ネットワークで後処理（frames / timeline 生成）が UXsim 本体と
    同程度に遅く，/results の JSON が数百 MB になってブラウザで開けなかった．
    後処理を numpy 一括処理に置き換え，フレーム点数に上限（車両サンプリング）を設け，
    /results は gzip 済みバイト列を executor で 1 回だけ生成してキャッシュする．
    """

    def test_select_frames_no_thinning_when_small(self):
        import numpy as np
        from risu.simulation import _select_frames
        tk = np.array([0, 50, 50, 100, 150, 150, 150], dtype=np.int64)
        kept, fidx = _select_frames(tk, max_frames=200)
        assert kept.tolist() == [0, 50, 100, 150]
        assert fidx.tolist() == [0, 1, 1, 2, 3, 3, 3]

    def test_select_frames_thins_to_max(self):
        import numpy as np
        from risu.simulation import _select_frames
        # 0.1 秒精度キーで 1000 ユニーク時刻 → max 200 なら 5 個おき
        tk = np.repeat(np.arange(1000, dtype=np.int64) * 50, 3)
        kept, fidx = _select_frames(tk, max_frames=200)
        assert kept.size == 200
        assert kept.tolist() == (np.arange(0, 1000, 5) * 50).tolist()
        # 落ちた点は -1，残った点は kept への index
        assert ((fidx == -1) | (kept[np.maximum(fidx, 0)] == tk)).all()
        assert (fidx >= 0).sum() == 200 * 3

    def test_select_frames_matches_unique_fallback(self):
        """LUT 経路と np.unique 経路（想定外に大きな時刻）は同じ結果を返す"""
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
        rng = np.random.default_rng(0)
        tk = rng.integers(0, 3000, size=5000, dtype=np.int64) * 10
        kept_a, fidx_a = risu.simulation._select_frames(tk, 100)
        # 巨大な値を足して汎用経路を強制し，同じオフセットを引いて比較
        off = 60_000_000
        kept_b, fidx_b = risu.simulation._select_frames(tk + off, 100)
        assert (kept_b - off).tolist() == kept_a.tolist()
        assert fidx_b.tolist() == fidx_a.tolist()

    @pytest.fixture(scope="class")
    def result(self):
        return _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO)

    def test_fast_path_is_active_for_installed_uxsim(self):
        """uxsim の内部 API（CLAUDE.md §3.6）が使えなくなると，動作は止まらず車両別ログの
        フォールバックで「遅くなるだけ」なので気づけない．cpp backend がある環境では
        高速経路が効いていることを CI で固定する（uxsim 更新時の検知）．"""
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
        rt = _run_uxsim(BOTTLENECK_SCENARIO)["_runtime"]
        assert rt["uxsim_version"] == risu.runtime.UXSIM_VERSION
        if rt["backend"] != "cpp":
            pytest.skip("uxsim cpp backend が無い環境（高速経路は cpp 前提）")
        assert rt["fast_path"] is True, (
            f"uxsim {rt['uxsim_version']} で車両ログの高速経路が使えずフォールバックしている．"
            f"§3.6 の内部 API（build_all_vehicle_logs_flat_compact / _LOG_STATE_MAP / offsets）を確認: "
            f"{risu.runtime.RUNTIME_STATUS.get('fast_path_error')}")
        assert risu.runtime.RUNTIME_STATUS["fast_path"] is True

    def test_healthz_reports_uxsim_runtime(self):
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
        _run_uxsim(BOTTLENECK_SCENARIO)
        body = TestClient(risu.api.app).get("/healthz").json()
        assert body["status"] == "ok"
        assert body["uxsim"]["uxsim_version"] == risu.runtime.UXSIM_VERSION
        assert body["uxsim"]["backend"] in ("cpp", "python")
        assert body["uxsim"]["fast_path"] in (True, False)

    def test_frames_are_numpy_columns_sorted_by_vehicle(self, result):
        """frames の列は numpy 配列で，フレーム内の ids は昇順（フロントのマージ結合前提）"""
        import numpy as np
        assert result["vehicle_sample_step"] == 1
        assert list(result["frames"].keys()) == [str(t) for t in result["frame_times"]]
        for cols in result["frames"].values():
            for c in ("ids", "xs", "ys", "vs", "alphas", "li"):
                assert isinstance(cols[c], np.ndarray)
            ids = cols["ids"]
            assert (np.diff(ids) >= 0).all()

    def test_timeline_matches_frame_grid(self, result):
        """リンク timeline の t は frame_times と同じグリッド"""
        for f in result["geojson"]["features"]:
            tl = f["properties"]["timeline"]
            assert [e["t"] for e in tl] == result["frame_times"]

    def test_vehicle_sampling_when_over_point_limit(self, monkeypatch):
        """総点数が上限を超えたら車両を等間隔サンプリングし，timeline / 統計は変わらない"""
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
        base = _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO)
        total = sum(int(len(c["ids"])) for c in base["frames"].values())
        monkeypatch.setattr(risu.simulation, "MAX_FRAME_POINTS", max(1, total // 3))
        sampled = _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO)
        step = sampled["vehicle_sample_step"]
        assert step >= 2
        sampled_total = sum(int(len(c["ids"])) for c in sampled["frames"].values())
        assert sampled_total <= total // 3 + 1
        # サンプリング対象は vid % step == 0 の車両だけ
        for cols in sampled["frames"].values():
            assert (cols["ids"] % step == 0).all()
        # フレーム時刻・timeline・統計は間引き前と同一
        assert sampled["frame_times"] == base["frame_times"]
        assert sampled["stats"]["total_trips"] == base["stats"]["total_trips"]
        tl_b = {f["properties"]["name"]: f["properties"]["timeline"] for f in base["geojson"]["features"]}
        tl_s = {f["properties"]["name"]: f["properties"]["timeline"] for f in sampled["geojson"]["features"]}
        assert tl_s == tl_b
        # LLM 向け集計は台数を補正し，注記を付ける
        sid = "test_sampling"
        results_store[sid] = sampled
        try:
            sd = _get_simulation_data(sid)
            assert "vehicle_sample_note" in sd
            json.dumps(sd)  # 標準 json で直列化できる（numpy 型が漏れていない）
            results_store["test_sampling_base"] = base
            sd_base = _get_simulation_data("test_sampling_base")
            # 台数は描画用の間引き前に数えるので，間引きの有無で系列が一致する
            # （旧実装は「サンプル数 × step」の近似で，時刻ごとに誤差が出ていた）
            assert sampled["vehicle_counts"] == base["vehicle_counts"]
            assert sd["network_vehicle_count"] == sd_base["network_vehicle_count"]
            # 平均速度・速度分布・流入到着も間引き前の全点から作るので不変
            assert sampled["frame_avg_speed"] == base["frame_avg_speed"]
            assert sampled["speed_histogram"] == base["speed_histogram"]
            assert sampled["trip_series"] == base["trip_series"]
            assert sd["network_avg_speed"] == sd_base["network_avg_speed"]
            assert sd["speed_histogram"] == sd_base["speed_histogram"]
            deltan = base["_scenario"]["deltan"]
            assert all(a % deltan == 0 for a in sd["network_vehicle_count"])
        finally:
            results_store.pop(sid, None)
            results_store.pop("test_sampling_base", None)

    def test_results_endpoint_gzip_cached(self):
        """/results は gzip 済みバイト列を返し，2 回目はキャッシュを使う．identity でも同じ内容．"""
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
        sid = "test_results_gz"
        risu.results._store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            c = TestClient(risu.api.app)
            r = c.get(f"/results/{sid}", headers={"Accept-Encoding": "gzip"})
            assert r.status_code == 200
            assert r.headers.get("content-encoding") == "gzip"
            assert results_store[sid]["_enc_cache"].get("gzip"), "gzip 結果がキャッシュされていない"
            d = r.json()
            # 送出時は columnar_v3（量子化＋ids 差分符号化）．
            # results_store 側は v2 のまま（TestFrameWireEncodingV3 を参照）．
            assert d["sim_id"] == sid and d["result"]["frame_format"] == "columnar_v3"
            assert d["result"]["vehicle_sample_step"] == 1
            fk = str(d["result"]["frame_times"][-1])
            cols = d["result"]["frames"][fk]
            assert isinstance(cols["ids"], list) and len(cols["ids"]) == len(cols["xs"])
            r2 = c.get(f"/results/{sid}", headers={"Accept-Encoding": "gzip"})
            assert r2.content == r.content
            r3 = c.get(f"/results/{sid}", headers={"Accept-Encoding": "identity"})
            assert r3.headers.get("content-encoding") is None
            assert r3.json() == d
            # 軽量エンベロープ（scenario のみ）は従来通り
            r4 = c.get(f"/results/{sid}/scenario")
            assert r4.status_code == 200 and "result" not in r4.json()
        finally:
            results_store.pop(sid, None)

    def test_envelope_json_stdlib_parseable(self):
        """orjson が直列化した numpy 列は標準 json で読み戻せる（クライアント互換）"""
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
        sid = "test_env_json"
        risu.results._store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            d = json.loads(risu.results._envelope_json_bytes(sid))
            assert d["result"]["link_names"] == results_store[sid]["link_names"]
            assert d["result"]["frame_times"] == results_store[sid]["frame_times"]
        finally:
            results_store.pop(sid, None)


# ============================================================
# 素の UXsim で実行するパイプライン（uxsim_bridge + scripts/run_scenario.py）
# ============================================================

class TestStandalonePipeline:
    """RISU サーバーを介さずシナリオを UXsim で実行する経路のテスト．

    server.py の _run_uxsim と scripts/run_scenario.py は同じ uxsim_bridge.build_world
    を通る．ここが割れるとサーバーとオフライン実行で結果が変わるので，
    構築結果の同一性と，生成スクリプトが実際に import できることを確認する．
    """

    SCENARIO_DICT = {
        "name": "pipeline_test",
        "tmax": 1000,
        "deltan": 5,
        "nodes": [
            {"name": "A", "x": 0, "y": 0},
            {"name": "B", "x": 2000, "y": 0, "flow_capacity": 0.4},
            {"name": "C", "x": 4000, "y": 0},
        ],
        "links": [
            {"name": "AB", "start": "A", "end": "B", "length": 2000},
            {"name": "BC", "start": "B", "end": "C", "length": 2000, "free_flow_speed": 10},
        ],
        "demands": [
            {"orig": "A", "dest": "C", "t_start": 0, "t_end": 400, "flow": 0.6},
        ],
    }

    def test_bridge_imports_without_server(self):
        """uxsim_bridge は FastAPI / anthropic を引っ張らない（素の Python から使える）．"""
        import subprocess

        code = (
            "import sys; import uxsim_bridge; "
            "mods = set(sys.modules); "
            "assert 'fastapi' not in mods, 'fastapi を読み込んでいる'; "
            "assert 'anthropic' not in mods, 'anthropic を読み込んでいる'; "
            "print('ok')"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "ok" in r.stdout

    def test_scenario_from_dict_accepts_envelope(self):
        """エンベロープ {"scenario": {...}} でも生シナリオでも同じ結果になる．"""
        from uxsim_bridge import scenario_from_dict

        raw = scenario_from_dict(self.SCENARIO_DICT)
        env = scenario_from_dict({"scenario": self.SCENARIO_DICT, "source": {"type": "t"}})
        assert [n.name for n in raw.nodes] == [n.name for n in env.nodes]
        assert raw.tmax == env.tmax == 1000

    def test_scenario_from_dict_applies_defaults(self):
        """省略された link 属性に SimulationInput と同じ既定値が入る．"""
        from uxsim_bridge import scenario_from_dict

        sc = scenario_from_dict(self.SCENARIO_DICT)
        ab = next(lk for lk in sc.links if lk.name == "AB")
        assert ab.free_flow_speed == 20.0
        assert ab.jam_density == 0.2
        assert ab.number_of_lanes == 1
        assert ab.capacity is None and ab.signal_group is None

    def test_scenario_from_dict_rejects_incomplete(self):
        from uxsim_bridge import scenario_from_dict

        with pytest.raises(ValueError, match="links"):
            scenario_from_dict({"nodes": [], "demands": []})

    def test_bridge_matches_server_run(self):
        """build_world 経由の素の実行と _run_uxsim の統計が一致する．"""
        from uxsim_bridge import build_world, scenario_from_dict

        # サーバー経路
        server_res = _run_uxsim(SimulationInput(**self.SCENARIO_DICT))
        s = server_res["stats"]

        # 素の UXsim 経路
        W = build_world(scenario_from_dict(self.SCENARIO_DICT),
                        disable_basic_analysis=True)
        W.exec_simulation()
        from risu.simulation import _trip_stats
        total, completed, avg_tt, _arrivals = _trip_stats(W)

        assert total == s["total_trips"]
        assert completed == s["completed_trips"]
        if avg_tt is not None and s["average_travel_time_s"] is not None:
            assert abs(avg_tt - s["average_travel_time_s"]) < 1.0

    def test_emitted_script_is_valid_python(self, tmp_path):
        r"""--emit が出すスクリプトが構文的に正しく，単体で World を組めること．

        Windows パス（C:\Users\...）を docstring に埋めると \U が unicode
        エスケープと解釈されて SyntaxError になる回帰があったため，
        バックスラッシュを含む生成元パスで検証する．
        """
        import ast
        import importlib.util

        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
        import run_scenario

        out = tmp_path / "emitted.py"
        run_scenario.emit_python(
            {"scenario": self.SCENARIO_DICT},
            str(out),
            source=r"C:\Users\test\Unicode\tmp\scenario.json",  # \U \t が含まれる
        )
        text = out.read_text(encoding="utf-8")
        ast.parse(text)  # SyntaxError なら失敗

        # 実際に import して World を組めるか
        spec = importlib.util.spec_from_file_location("emitted_mod", out)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        W = mod.build()
        assert len(W.NODES) == 3
        assert len(W.LINKS) == 2

    def test_emitted_script_preserves_optional_attrs(self, tmp_path):
        """flow_capacity / free_flow_speed など省略可能な属性が生成物に残る．"""
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
        import run_scenario

        out = tmp_path / "emitted2.py"
        run_scenario.emit_python({"scenario": self.SCENARIO_DICT}, str(out), source="test")
        text = out.read_text(encoding="utf-8")
        assert '"flow_capacity": 0.4' in text
        assert '"free_flow_speed": 10' in text

    # ── 乱数シード: シナリオに持たせ，全経路で維持する ──
    SEEDED = {**SCENARIO_DICT, "random_seed": 42, "reaction_time": 1.5,
              "demands": [{"orig": "A", "dest": "C", "t_start": 0, "t_end": 400, "flow": 0.6},
                          {"orig": "A", "dest": "B", "t_start": 0, "t_end": 400, "flow": 0.3}]}

    def test_bridge_passes_seed_and_reaction_time_to_world(self):
        from uxsim_bridge import build_world, scenario_from_dict
        W = build_world(scenario_from_dict(self.SEEDED))
        assert W.random_seed == 42
        assert abs(W.REACTION_TIME - 1.5) < 1e-9
        W0 = build_world(scenario_from_dict(self.SCENARIO_DICT))
        assert W0.random_seed is None

    def test_seeded_server_runs_are_reproducible(self):
        """同じ seed の 2 回の _run_uxsim は統計もフレームも一致し，別 seed では乱数列が変わる．"""
        import numpy as np
        a = _run_uxsim(SimulationInput(**self.SEEDED))
        b = _run_uxsim(SimulationInput(**self.SEEDED))
        assert a["stats"]["completed_trips"] == b["stats"]["completed_trips"]
        assert a["stats"]["average_travel_time_s"] == b["stats"]["average_travel_time_s"]
        assert a["vehicle_counts"] == b["vehicle_counts"]
        for k in a["frames"]:
            assert np.array_equal(a["frames"][k]["xs"], b["frames"][k]["xs"])
        assert a["_scenario"]["random_seed"] == 42  # 保存シナリオに残る
        # 別 seed では World の乱数列が変わる（結果が同じでも rng 状態は別）
        from uxsim_bridge import build_world, scenario_from_dict
        Wa = build_world(scenario_from_dict(self.SEEDED))
        Wb = build_world(scenario_from_dict({**self.SEEDED, "random_seed": 43}))
        assert Wa.rng.random() != Wb.rng.random()

    def test_emitted_script_carries_seed(self, tmp_path):
        import importlib.util
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
        import run_scenario

        out = tmp_path / "emitted_seed.py"
        run_scenario.emit_python({"scenario": self.SEEDED}, str(out), source="test")
        text = out.read_text(encoding="utf-8")
        assert "RANDOM_SEED = 42" in text
        spec = importlib.util.spec_from_file_location("emitted_seed_mod", out)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        W = mod.build()
        assert W.random_seed == 42
        assert abs(W.REACTION_TIME - 1.5) < 1e-9

    def test_scenario_envelope_keeps_seed(self):
        """/results/{id}/scenario（再現用 DL）に random_seed / reaction_time が残る．"""
        from risu.results import _build_envelope, _store_sim
        res = _run_uxsim(SimulationInput(**self.SEEDED))
        _store_sim("seed_env_test", res)
        try:
            env = _build_envelope("seed_env_test", include_result=False)
            assert env["scenario"]["random_seed"] == 42
            assert env["scenario"]["reaction_time"] == 1.5
            full = _build_envelope("seed_env_test", include_result=True)
            assert full["result"]["vehicle_counts"] == res["vehicle_counts"]
        finally:
            results_store.pop("seed_env_test", None)
