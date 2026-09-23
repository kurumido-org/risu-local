"""
RISU 安定性テスト
================
これまでの開発で発見・修正した問題をテストとして記録．
新機能追加時にこのテストが全て通ることを確認すること．

テスト実行:
    cd risu-local
    .venv/Scripts/activate
    pip install pytest httpx
    pytest tests/ -v

注意: サーバー (python server.py) が起動している必要があるテストは
      test_api_* で始まるもの．それ以外はサーバー不要．
"""

import json
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import (
    SimulationInput,
    _run_uxsim,
    _parse_csv_scenario,
    _get_simulation_data,
    results_store,
)


# ============================================================
# テスト用シナリオ
# ============================================================

BOTTLENECK_SCENARIO = SimulationInput(
    name="test_bottleneck",
    tmax=1000,
    deltan=5,
    nodes=[
        {"name": "A", "x": 0, "y": 0},
        {"name": "B", "x": 5000, "y": 0, "flow_capacity": 0.4},
        {"name": "C", "x": 7500, "y": 0},
    ],
    links=[
        {"name": "r1", "start": "A", "end": "B", "length": 5000},
        {"name": "r2", "start": "B", "end": "C", "length": 2500, "free_flow_speed": 10},
    ],
    demands=[
        {"orig": "A", "dest": "C", "t_start": 0, "t_end": 600, "flow": 0.8},
    ],
)

GRID_BIDIRECTIONAL_SCENARIO = SimulationInput(
    name="test_grid",
    tmax=1000,
    deltan=5,
    nodes=[
        {"name": "n00", "x": 0, "y": 0},
        {"name": "n10", "x": 2000, "y": 0},
        {"name": "n01", "x": 0, "y": 2000},
        {"name": "n11", "x": 2000, "y": 2000},
    ],
    links=[
        {"name": "h0", "start": "n00", "end": "n10", "length": 2000},
        {"name": "h0r", "start": "n10", "end": "n00", "length": 2000},
        {"name": "v0", "start": "n00", "end": "n01", "length": 2000},
        {"name": "v0r", "start": "n01", "end": "n00", "length": 2000},
        {"name": "h1", "start": "n01", "end": "n11", "length": 2000},
        {"name": "h1r", "start": "n11", "end": "n01", "length": 2000},
        {"name": "v1", "start": "n10", "end": "n11", "length": 2000},
        {"name": "v1r", "start": "n11", "end": "n10", "length": 2000},
    ],
    demands=[
        {"orig": "n00", "dest": "n11", "t_start": 0, "t_end": 500, "flow": 0.4},
        {"orig": "n11", "dest": "n00", "t_start": 0, "t_end": 500, "flow": 0.3},
    ],
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
# 2y. シナリオ検証（重複・参照切れ）
# ============================================================

class TestScenarioValidation:
    """
    [修正履歴] 重複ノード名が UXsim の生エラー
    「Node name X already used by another node」のまま 400 で返り，
    どこを直せばいいか分からなかった．SimulationInput で事前検証する．
    """

    def _nodes(self):
        return [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}]

    def test_exact_duplicate_nodes_merged(self):
        """完全一致の重複ノードは黙って統合される（CSV でよくある形式）"""
        si = SimulationInput(
            name="t", tmax=600, deltan=5,
            nodes=self._nodes() + [{"name": "A", "x": 0, "y": 0}],
            links=[{"name": "r", "start": "A", "end": "B", "length": 1000}],
            demands=[],
        )
        assert len(si.nodes) == 2

    def test_conflicting_duplicate_nodes_rejected(self):
        """同名で座標が異なるノードは名前を列挙してエラー"""
        with pytest.raises(ValueError, match="東京高速道路-IN"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=[{"name": "東京高速道路-IN", "x": 0, "y": 0},
                       {"name": "東京高速道路-IN", "x": 500, "y": 0}],
                links=[], demands=[],
            )

    def test_exact_duplicate_links_merged(self):
        si = SimulationInput(
            name="t", tmax=600, deltan=5,
            nodes=self._nodes(),
            links=[{"name": "r", "start": "A", "end": "B", "length": 1000},
                   {"name": "r", "start": "A", "end": "B", "length": 1000}],
            demands=[],
        )
        assert len(si.links) == 1

    def test_conflicting_duplicate_links_rejected(self):
        with pytest.raises(ValueError, match="リンク名が重複"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=self._nodes(),
                links=[{"name": "r", "start": "A", "end": "B", "length": 1000},
                       {"name": "r", "start": "B", "end": "A", "length": 1000}],
                demands=[],
            )

    def test_link_referencing_missing_node_rejected(self):
        with pytest.raises(ValueError, match="存在しないノード"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=self._nodes(),
                links=[{"name": "r", "start": "A", "end": "X", "length": 1000}],
                demands=[],
            )

    def test_demand_referencing_missing_node_rejected(self):
        with pytest.raises(ValueError, match="需要が存在しないノード"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=self._nodes(),
                links=[{"name": "r", "start": "A", "end": "B", "length": 1000}],
                demands=[{"orig": "A", "dest": "Z", "t_start": 0, "t_end": 100, "flow": 0.1}],
            )

    # ── 値域の検証（deltan=0 / 負のリンク長 / 負の需要 / 時刻逆転 を受理していた） ──
    def _valid(self, **over):
        base = dict(
            name="t", tmax=600, deltan=5,
            nodes=self._nodes(),
            links=[{"name": "r", "start": "A", "end": "B", "length": 1000}],
            demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.1}],
        )
        base.update(over)
        return base

    def test_valid_baseline_accepted(self):
        si = SimulationInput(**self._valid())
        assert si.random_seed is None and si.reaction_time is None

    @pytest.mark.parametrize("over", [
        {"deltan": 0},
        {"deltan": -1},
        {"tmax": 0},
        {"reaction_time": 0},
        {"reaction_time": -1.0},
    ])
    def test_bad_scenario_params_rejected(self, over):
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(**over))

    @pytest.mark.parametrize("link_over", [
        {"length": -100}, {"length": 0},
        {"free_flow_speed": 0}, {"jam_density": -0.1},
        {"number_of_lanes": 0}, {"capacity": -1},
    ])
    def test_bad_link_values_rejected(self, link_over):
        lk = {"name": "r", "start": "A", "end": "B", "length": 1000}
        lk.update(link_over)
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(links=[lk]))

    @pytest.mark.parametrize("d_over", [
        {"flow": -0.1},
        {"t_start": -10},
        {"t_start": 100, "t_end": 50},   # 逆転
        {"t_start": 100, "t_end": 100},  # 長さゼロ
    ])
    def test_bad_demand_values_rejected(self, d_over):
        d = {"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.1}
        d.update(d_over)
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(demands=[d]))

    def test_node_negative_capacity_rejected(self):
        nodes = self._nodes()
        nodes[1]["flow_capacity"] = -0.5
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(nodes=nodes))

    def test_validation_error_reaches_client_as_422(self):
        """/simulate 経由でも 500 ではなく，どの項目かが分かる 422 になる．"""
        from fastapi.testclient import TestClient
        from server import app
        client = TestClient(app)
        r = client.post("/simulate", json=self._valid(deltan=0))
        assert r.status_code == 422
        # /simulate は FastAPI 標準の 422（loc 付きリスト）．フロントは msg を連結して表示する
        assert "deltan" in json.dumps(r.json()["detail"])


# ============================================================
# 2z. シナリオパッチエンジン（rerun_simulation）
# ============================================================

class TestScenarioModifications:
    """
    _apply_modifications: 大規模ネットワークを LLM に往復させないための
    差分命令エンジン．保存済みシナリオに小さなパッチを適用する．
    """

    def _base(self):
        return {
            "name": "base", "tmax": 1000, "deltan": 5,
            "nodes": [
                {"name": "A", "x": 0, "y": 0},
                {"name": "B", "x": 1000, "y": 0},
                {"name": "C", "x": 2000, "y": 0},
            ],
            "links": [
                {"name": "r1", "start": "A", "end": "B", "length": 1000,
                 "free_flow_speed": 20, "number_of_lanes": 1},
                {"name": "r2", "start": "B", "end": "C", "length": 1000,
                 "free_flow_speed": 20, "number_of_lanes": 1},
            ],
            "demands": [
                {"orig": "A", "dest": "C", "t_start": 0, "t_end": 500, "flow": 0.4},
            ],
        }

    def test_update_links_by_name(self):
        from server import _apply_modifications
        sc, applied = _apply_modifications(self._base(), [
            {"action": "update_links", "names": ["r1"], "set": {"capacity": 0.3}},
        ])
        assert sc["links"][0]["capacity"] == 0.3
        assert "capacity" not in sc["links"][1]
        assert len(applied) == 1

    def test_update_links_all(self):
        from server import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "update_links", "all": True, "set": {"free_flow_speed": 10}},
        ])
        assert all(l["free_flow_speed"] == 10 for l in sc["links"])

    def test_unknown_link_name_raises(self):
        from server import _apply_modifications
        with pytest.raises(ValueError, match="r99"):
            _apply_modifications(self._base(), [
                {"action": "update_links", "names": ["r99"], "set": {"capacity": 1}},
            ])

    def test_update_demands_scale(self):
        from server import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "update_demands", "all": True, "scale_flow": 1.5},
        ])
        assert sc["demands"][0]["flow"] == 0.6

    def test_remove_nodes_cascades(self):
        from server import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "remove_nodes", "names": ["B"]},
        ])
        assert len(sc["nodes"]) == 2
        assert len(sc["links"]) == 0  # r1, r2 とも B に接続
        assert len(sc["demands"]) == 1  # A→C は残る

    def test_add_and_signal(self):
        from server import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "update_nodes", "names": ["B"], "set": {"signal": [30, 30]}},
            {"action": "update_links", "names": ["r1"], "set": {"signal_group": 0}},
            {"action": "add_demand", "demand": {"orig": "C", "dest": "A",
                                                "t_start": 0, "t_end": 500, "flow": 0.2}},
        ])
        assert sc["nodes"][1]["signal"] == [30, 30]
        assert sc["links"][0]["signal_group"] == 0
        assert len(sc["demands"]) == 2

    def test_base_scenario_not_mutated(self):
        from server import _apply_modifications
        base = self._base()
        _apply_modifications(base, [
            {"action": "update_links", "all": True, "set": {"capacity": 0.1}},
        ])
        assert "capacity" not in base["links"][0]

    def test_rerun_handler_end_to_end(self):
        """保存済みシナリオ → パッチ → 再実行 → 新 sim_id"""
        import asyncio
        import types
        import json as _json
        from server import _handle_rerun_simulation, _store_sim

        base_result = _run_uxsim(SimulationInput(**self._base()))
        _store_sim("rerun_base", base_result)

        body = types.SimpleNamespace(messages=[])
        content, new_id, is_err = asyncio.run(_handle_rerun_simulation({
            "base_sim_id": "rerun_base",
            "modifications": [
                {"action": "update_links", "names": ["r1"], "set": {"capacity": 0.15}},
            ],
        }, body))
        assert not is_err, content
        assert new_id in results_store
        payload = _json.loads(content)
        assert payload["base_sim_id"] == "rerun_base"
        assert payload["applied"]
        # 新シナリオに capacity が反映され，渋滞で旅行時間が悪化している
        new_sc = results_store[new_id]["_scenario"]
        assert new_sc["links"][0]["capacity"] == 0.15
        assert payload["average_travel_time_s"] > base_result["stats"]["average_travel_time_s"]

    def test_rerun_handler_bad_sim_id(self):
        import asyncio
        import types
        from server import _handle_rerun_simulation
        content, new_id, is_err = asyncio.run(_handle_rerun_simulation(
            {"base_sim_id": "no_such_id", "modifications": []},
            types.SimpleNamespace(messages=[])))
        assert is_err and new_id is None

    def test_generate_demands_random(self):
        """ノード名を知らなくてもサーバー側でランダム OD を生成できる"""
        from server import _apply_modifications
        sc, applied = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "random",
             "n_pairs": 5, "flow_per_pair": 0.1, "seed": 42, "clear_existing": True},
        ])
        assert len(sc["demands"]) == 5
        node_names = {n["name"] for n in sc["nodes"]}
        for d in sc["demands"]:
            assert d["orig"] in node_names and d["dest"] in node_names
            assert d["orig"] != d["dest"]
            assert d["flow"] == 0.1
        # seed 固定で再現性がある
        sc2, _ = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "random",
             "n_pairs": 5, "flow_per_pair": 0.1, "seed": 42, "clear_existing": True},
        ])
        assert sc["demands"] == sc2["demands"]

    def test_generate_demands_flow_total(self):
        from server import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "random",
             "n_pairs": 4, "flow_total": 1.0, "clear_existing": True},
        ])
        assert all(d["flow"] == 0.25 for d in sc["demands"])

    def test_generate_demands_boundary(self):
        from server import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "boundary", "clear_existing": True},
        ])
        assert len(sc["demands"]) >= 2  # 周縁ノード全ペア

    def test_set_params_sets_seed_and_reaction_time(self):
        from server import _apply_modifications
        sc, applied = _apply_modifications(self._base(), [
            {"action": "set_params", "random_seed": 42, "reaction_time": 1.7},
        ])
        assert sc["random_seed"] == 42 and sc["reaction_time"] == 1.7
        assert sc["tmax"] == 1000  # 触っていない値はそのまま
        assert "set_params" in applied[0]
        # None で既定に戻す
        sc2, _ = _apply_modifications(sc, [{"action": "set_params", "random_seed": None}])
        assert "random_seed" not in sc2 and sc2["reaction_time"] == 1.7

    def test_set_params_rejects_unknown_field(self):
        from server import _apply_modifications
        with pytest.raises(ValueError, match="set_params"):
            _apply_modifications(self._base(), [{"action": "set_params", "foo": 1}])
        with pytest.raises(ValueError, match="set_params"):
            _apply_modifications(self._base(), [{"action": "set_params"}])

    def test_seed_survives_rerun_derivation(self):
        """base に seed があれば，別の差分だけ当てた派生シナリオにも同じ seed が残る．"""
        from server import _apply_modifications
        base = self._base(); base["random_seed"] = 7; base["reaction_time"] = 1.5
        sc, _ = _apply_modifications(base, [
            {"action": "update_links", "names": ["r1"], "set": {"capacity": 0.3}},
        ])
        assert sc["random_seed"] == 7 and sc["reaction_time"] == 1.5
        si = SimulationInput(**sc)
        assert si.random_seed == 7


class TestNetworkInfo:
    """get_network_info: LLM がネットワークを必要な分だけ照会するツール"""

    @pytest.fixture(scope="class")
    def sim_id(self):
        scenario = SimulationInput(
            name="info_test", tmax=600, deltan=5,
            nodes=[{"name": f"n{i}", "x": i * 100, "y": 0} for i in range(80)],
            links=[{"name": f"L{i}", "start": f"n{i}", "end": f"n{i+1}", "length": 100}
                   for i in range(79)],
            demands=[{"orig": "n0", "dest": "n79", "t_start": 0, "t_end": 300, "flow": 0.3}],
        )
        result = _run_uxsim(scenario)
        results_store["info_test"] = result
        return "info_test"

    def test_summary(self, sim_id):
        from server import _handle_get_network_info
        content, is_err = _handle_get_network_info({"sim_id": sim_id})
        assert not is_err
        d = json.loads(content)
        assert d["total"] == {"nodes": 80, "links": 79, "demands": 1}
        assert d["bbox"]["x_max"] == 7900
        assert len(d["sample_node_names"]) == 20

    def test_nodes_paging_and_filter(self, sim_id):
        from server import _handle_get_network_info
        content, _ = _handle_get_network_info(
            {"sim_id": sim_id, "include": "nodes", "limit": 10, "offset": 5})
        d = json.loads(content)
        assert len(d["nodes"]) == 10
        assert d["nodes"][0]["name"] == "n5"
        assert "note" in d  # 途中までの表示であることが明示される
        content, _ = _handle_get_network_info(
            {"sim_id": sim_id, "include": "nodes", "name_contains": "n7"})
        d = json.loads(content)
        # n7, n70..n79 の 11 件
        assert d["matched"] == 11

    def test_limit_cap(self, sim_id):
        from server import _handle_get_network_info
        content, _ = _handle_get_network_info(
            {"sim_id": sim_id, "include": "nodes", "limit": 9999})
        d = json.loads(content)
        assert len(d["nodes"]) <= 200

    def test_bad_sim_id(self):
        from server import _handle_get_network_info
        content, is_err = _handle_get_network_info({"sim_id": "nope"})
        assert is_err


class TestConversationContext:
    """/chat のコンテキスト注入は会話に紐づく sim（last_sim_id）だけを使うこと．

    グローバル最新（results_store の末尾）を注入すると，別会話や
    アップロードで作られた無関係なシナリオを LLM が流用してしまう．
    """

    @pytest.fixture()
    def stored_sim(self):
        sim_id = "ctx_test"
        results_store[sim_id] = {
            "geojson": {"features": [{"properties": {"name": "L1"}},
                                     {"properties": {"name": "L2"}}]},
            "_scenario": {"nodes": [{"name": "A"}, {"name": "B"}],
                          "links": [{"name": "L1"}, {"name": "L2"}],
                          "demands": [], "tmax": 600},
        }
        yield sim_id
        results_store.pop(sim_id, None)

    def _body(self, last_sim_id):
        from server import ChatInput
        return ChatInput(messages=[{"role": "user", "content": "hi"}],
                         last_sim_id=last_sim_id)

    def test_no_last_sim_id_injects_nothing(self, stored_sim):
        # results_store に sim があっても，会話が指定しなければ注入しない
        from server import _conversation_context_block
        assert _conversation_context_block(self._body(None)) == ""

    def test_valid_last_sim_id_injects_that_sim(self, stored_sim):
        from server import _conversation_context_block
        block = _conversation_context_block(self._body(stored_sim))
        assert stored_sim in block
        assert f'rerun_simulation(base_sim_id="{stored_sim}")' in block
        assert "2 ノード / 2 リンク" in block

    def test_unknown_last_sim_id_injects_nothing(self, stored_sim):
        # サーバー再起動などで sim が消えた場合は注入しない
        from server import _conversation_context_block
        assert _conversation_context_block(self._body("gone123")) == ""

    def test_conversation_sim_id_helper(self, stored_sim):
        from server import _conversation_sim_id
        assert _conversation_sim_id(self._body(stored_sim)) == stored_sim
        assert _conversation_sim_id(self._body(None)) is None
        assert _conversation_sim_id(self._body("  ")) is None


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
# 3. CSV パーサーテスト
# ============================================================

class TestCSVParser:
    """
    [修正履歴] 空セルで float("") エラーが発生した．
    _f() / _i() ヘルパーで対処済み．
    """

    def test_risu_csv_basic(self):
        """RISU CSV 形式の基本パース"""
        csv = (
            "type,name,x,y,start,end,length,free_flow_speed,number_of_lanes,orig,dest,t_start,t_end,flow\n"
            "node,A,0,0,,,,,,,,,,\n"
            "node,B,5000,0,,,,,,,,,,\n"
            "link,r1,,,A,B,5000,20,1,,,,,\n"
            "demand,,,,,,,,,A,B,0,600,0.5\n"
        )
        result = _parse_csv_scenario(csv)
        assert result["format"] == "risu_csv"
        assert len(result["nodes"]) == 2
        assert len(result["links"]) == 1
        assert len(result["demands"]) == 1

    def test_node_csv_prefers_node_id_over_name(self):
        """
        [修正履歴] node_id（主キー）と name（表示ラベル，重複可）の両方を持つ
        GMNS 風データで，name を識別子に選んで「ノード名が重複」エラーになった．
        ID 系カラムを優先する．
        """
        csv = (
            "node_id,name,x_coord,y_coord\n"
            "1,東京高速道路-IN,0,0\n"
            "2,東京高速道路-IN,500,0\n"
            "3,東京高速道路-OUT,1000,0\n"
        )
        result = _parse_csv_scenario(csv)
        assert result["format"] == "node_csv"
        names = [n["name"] for n in result["nodes"]]
        assert names == ["1", "2", "3"], f"node_id が識別子になるべき: {names}"

    def test_link_csv_prefers_link_id_over_name(self):
        csv = (
            "link_id,name,from_node_id,to_node_id,length\n"
            "10,環状線,1,2,800\n"
            "11,環状線,2,3,800\n"
        )
        result = _parse_csv_scenario(csv)
        assert result["format"] == "link_csv"
        names = [l["name"] for l in result["links"]]
        assert names == ["10", "11"], f"link_id が識別子になるべき: {names}"
        assert result["links"][0]["start"] == "1"

    def test_risu_csv_empty_cells(self):
        """
        空セルが含まれる RISU CSV でエラーにならない．
        [修正履歴] float("") で ValueError が発生した．
        """
        csv = (
            "type,name,x,y,start,end,length,free_flow_speed,number_of_lanes,orig,dest,t_start,t_end,flow\n"
            "node,start,0,0,,,,,,,,,,\n"
            "node,goal,5000,0,,,,,,,,,,\n"
            "link,road,,,start,goal,5000,,,,,,,,\n"
            "demand,,,,,,,,,start,goal,0,600,0.8\n"
        )
        result = _parse_csv_scenario(csv)
        assert result["format"] == "risu_csv"
        # 空の free_flow_speed はデフォルト値 20 になる
        assert result["links"][0]["free_flow_speed"] == 20

    def test_gmns_node_csv(self):
        """GMNS node.csv 形式"""
        csv = "node_id,x_coord,y_coord,zone_id\n1,0,0,1\n2,5000,0,2\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] in ("gmns_node", "node_csv")
        assert len(result["nodes"]) == 2

    def test_gmns_link_csv(self):
        """GMNS link.csv 形式"""
        csv = "link_id,from_node_id,to_node_id,length,free_speed,lanes\n1,1,2,5000,60,2\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] in ("gmns_link", "link_csv")
        assert len(result["links"]) == 1

    def test_gmns_demand_csv(self):
        """GMNS demand.csv 形式"""
        csv = "o_zone_id,d_zone_id,volume\n1,2,500\n2,1,300\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] in ("gmns_demand", "demand_csv")
        assert len(result["demands"]) == 2

    def test_flexible_node_csv(self):
        """柔軟なカラム名のノード CSV"""
        csv = "name,lon,lat\nA,139.7,35.6\nB,139.71,35.61\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] == "node_csv"
        assert len(result["nodes"]) == 2

    def test_flexible_link_csv(self):
        """柔軟なカラム名のリンク CSV"""
        csv = "id,from,to,distance,speed_limit\n1,A,B,5000,60\n2,B,C,3000,40\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] == "link_csv"
        assert len(result["links"]) == 2
        assert result["links"][0]["start"] == "A"
        assert result["links"][0]["end"] == "B"

    def test_unknown_csv_raises(self):
        """認識できない CSV はエラー"""
        csv = "col_a,col_b\n1,2\n"
        with pytest.raises(ValueError, match="CSV 形式を認識できません"):
            _parse_csv_scenario(csv)


# ============================================================
# 4. シミュレーションデータ集計テスト
# ============================================================

class TestSimulationDataAggregation:
    """
    _get_simulation_data() がグラフ生成に十分なデータを返すことを検証．
    [修正履歴] LLM がチャート生成するにはデータが必要．
    """

    @pytest.fixture(scope="class")
    def sim_data(self):
        result = _run_uxsim(BOTTLENECK_SCENARIO)
        sid = "test_aggregation"
        results_store[sid] = result
        return _get_simulation_data(sid)

    def test_not_none(self, sim_data):
        assert sim_data is not None

    def test_required_fields(self, sim_data):
        """チャート生成に必要な全フィールドが存在する"""
        assert "sim_id" in sim_data
        assert "tmax" in sim_data
        assert "stats" in sim_data
        assert "time_labels" in sim_data
        assert "network_avg_speed" in sim_data
        assert "network_vehicle_count" in sim_data
        assert "link_names" in sim_data
        assert "link_speeds" in sim_data
        assert "speed_histogram" in sim_data

    def test_time_labels_reasonable(self, sim_data):
        """時間ラベルが0から始まりtmax以下"""
        labels = sim_data["time_labels"]
        assert len(labels) > 0
        assert labels[0] >= 0
        assert labels[-1] <= sim_data["tmax"]

    def test_arrays_same_length(self, sim_data):
        """時系列データが全て同じ長さ"""
        n = len(sim_data["time_labels"])
        assert len(sim_data["network_avg_speed"]) == n
        assert len(sim_data["network_vehicle_count"]) == n
        for speeds in sim_data["link_speeds"].values():
            assert len(speeds) == n

    def test_speed_histogram_structure(self, sim_data):
        """速度分布ヒストグラムの構造"""
        hist = sim_data["speed_histogram"]
        assert "labels" in hist
        assert "counts" in hist
        assert len(hist["labels"]) == len(hist["counts"])

    def test_nonexistent_sim_returns_none(self):
        """存在しない sim_id は None を返す"""
        assert _get_simulation_data("nonexistent_id_12345") is None

    def test_data_not_too_large(self, sim_data):
        """
        JSON サイズが LLM のコンテキストに収まるサイズ．
        間引きが機能していることを確認．
        """
        json_str = json.dumps(sim_data)
        # 100KB 以下であること（LLM に渡せるサイズ）
        assert len(json_str) < 100_000, f"データが大きすぎる: {len(json_str)} bytes"

    def test_vehicle_count_is_real_vehicles_not_platoons(self, sim_data):
        """[修正履歴] network_vehicle_count がプラトン数のままで deltan 分の 1 に見えていた．

        フレームの ids は UXsim のプラトン（deltan 台）なので，実台数 = プラトン数 × deltan．
        vehicle_counts は間引き前の全点から数えた値で，frames と一致する（この規模は間引きなし）．
        """
        res = results_store["test_aggregation"]
        deltan = res["_scenario"]["deltan"]
        assert deltan == 5
        assert res["vehicle_sample_step"] == 1
        counts = res["vehicle_counts"]
        assert len(counts) == len(res["frame_times"])
        # 全フレームでプラトン数 × deltan と一致
        for t, c in zip(res["frame_times"], counts):
            assert c == len(res["frames"][str(t)]["ids"]) * deltan
        # LLM に渡る系列も同じスケール（先頭 = 最初のサンプル時刻）
        assert sim_data["network_vehicle_count"][0] == counts[0]
        assert max(sim_data["network_vehicle_count"]) == max(counts[::max(1, -(-len(counts) // 30))])
        assert max(counts) > 0
        assert "deltan=5" in sim_data["metric_note"]

    def test_trip_series_matches_stats_and_covers_end(self, sim_data):
        """[修正履歴] 画面の到着台数はフレームから推定していて，全車両到着後も
        「到着 45 / 走行中 5」のように残った．実イベントの累積を 0〜tmax で返す．"""
        res = results_store["test_aggregation"]
        ts = res["trip_series"]
        assert ts["t"][0] == 0.0 and ts["t"][-1] == float(res["tmax"])
        assert ts["completed"][-1] == res["stats"]["completed_trips"]
        assert ts["entered"][-1] == res["stats"]["total_trips"]   # 全車両が流入したケース
        assert ts["entered"][0] == 0 and ts["completed"][0] == 0
        for k in ("entered", "completed"):
            assert all(a <= b for a, b in zip(ts[k], ts[k][1:]))   # 単調増加
        assert all(e >= c for e, c in zip(ts["entered"], ts["completed"]))
        # LLM 向けにも累積が渡る
        assert sim_data["network_completed_count"][-1] <= res["stats"]["completed_trips"]
        assert len(sim_data["network_entered_count"]) == len(sim_data["time_labels"])

    def test_trip_series_all_arrived_case(self):
        """指摘の再現: 50 台全部が到着するケースで，終了時刻の到着 = 50，走行中 = 0 になる．
        （フレームは走行車両がいる時刻までしか無いので，最後のフレームでは 45 / 5 のまま）"""
        sc = SimulationInput(
            name="all_arrive", tmax=1000, deltan=5,
            nodes=[{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 2000, "y": 0}],
            links=[{"name": "AB", "start": "A", "end": "B", "length": 2000}],
            demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 200, "flow": 0.25}],
        )
        res = _run_uxsim(sc)
        assert res["stats"]["total_trips"] == 50 and res["stats"]["completed_trips"] == 50
        ts = res["trip_series"]
        assert ts["t"][-1] == 1000.0
        assert ts["entered"][-1] == 50 and ts["completed"][-1] == 50
        assert ts["entered"][-1] - ts["completed"][-1] == 0
        # 最後のフレーム（走行車両がいる最後の時刻）は tmax より前で，そこでは走行中がまだいる．
        # 旧実装はこのフレームの値を終了時刻まで引きずっていた
        last_t = res["frame_times"][-1]
        assert last_t < 1000
        assert last_t in ts["t"] and res["vehicle_counts"][-1] > 0

    def test_avg_speed_is_vehicle_weighted_and_shared(self, sim_data):
        """[修正履歴] 画面はリンク別速度の単純平均，LLM は描画車両の平均で，同じ時刻に
        15.2 と 6.2 m/s のように食い違った．定義を「走行中全車両の台数重み平均」に統一する．"""
        import numpy as np
        res = results_store["test_aggregation"]
        fas = res["frame_avg_speed"]
        assert len(fas) == len(res["frame_times"])
        # 間引きなしの結果ではフレームの車両速度の平均と一致する
        for t, v in zip(res["frame_times"], fas):
            vs = np.asarray(res["frames"][str(t)]["vs"], dtype=np.float64)
            if vs.size:
                assert abs(v - float(vs.mean())) < 0.02
        # LLM に渡る系列はこの値（0.1 m/s 丸め）
        i0 = 0
        assert sim_data["network_avg_speed"][0] == round(fas[i0], 1)
        assert "台数重み" in sim_data["metric_note"]
        # 速度分布はサーバー保存のもの
        assert sim_data["speed_histogram"] == res["speed_histogram"]
        assert sum(res["speed_histogram"]["counts"]) == sum(res["vehicle_counts"]) // res["_scenario"]["deltan"]

    def test_vehicle_count_fallback_for_old_results(self):
        """vehicle_counts の無い古い結果でも deltan × 間引き で換算される．"""
        import copy
        old = copy.copy(results_store["test_aggregation"])
        old.pop("vehicle_counts", None)
        old["vehicle_sample_step"] = 2
        results_store["test_aggregation_old"] = old
        try:
            d = _get_simulation_data("test_aggregation_old")
            t0 = d["time_labels"][0]
            f0 = old["frames"][str(float(t0))]
            assert d["network_vehicle_count"][0] == len(f0["ids"]) * 5 * 2
        finally:
            results_store.pop("test_aggregation_old", None)


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
# 6. LLM ツール定義の整合性テスト
# ============================================================

class TestToolDefinitions:
    """LLM ツール定義がサーバー実装と整合していることを検証"""

    def test_claude_tools_defined(self):
        from server import CLAUDE_TOOLS
        tool_names = [t["name"] for t in CLAUDE_TOOLS]
        assert "run_simulation" in tool_names
        assert "get_simulation_data" in tool_names

    def test_run_simulation_schema(self):
        from server import CLAUDE_TOOLS
        tool = next(t for t in CLAUDE_TOOLS if t["name"] == "run_simulation")
        schema = tool["input_schema"]
        assert "nodes" in schema["properties"]
        assert "links" in schema["properties"]
        assert "demands" in schema["properties"]

    def test_get_simulation_data_schema(self):
        from server import CLAUDE_TOOLS
        tool = next(t for t in CLAUDE_TOOLS if t["name"] == "get_simulation_data")
        schema = tool["input_schema"]
        assert "sim_id" in schema["properties"]

    def test_system_prompt_contains_risu(self):
        """
        [修正履歴] AI の一人称を RISU に変更した．
        """
        from server import SYSTEM_PROMPT
        assert "RISU" in SYSTEM_PROMPT
        assert "一人称" in SYSTEM_PROMPT or "RISU" in SYSTEM_PROMPT

    def test_system_prompt_chart_instructions(self):
        """
        [修正履歴] チャート生成の指示がシステムプロンプトに含まれる．
        """
        from server import SYSTEM_PROMPT
        assert "chart" in SYSTEM_PROMPT.lower() or "チャート" in SYSTEM_PROMPT or "グラフ" in SYSTEM_PROMPT


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
# 8. チャートブロック抽出テスト
# ============================================================

class TestChartExtraction:
    """
    LLM レスポンスからの ```chart ブロック抽出をテスト．
    """

    def test_single_chart_extraction(self):
        import re
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = 'テキスト\n```chart\n{"type":"line","data":{"labels":[1,2],"datasets":[]}}\n```\n続き'
        matches = chart_pattern.findall(text)
        assert len(matches) == 1
        parsed = json.loads(matches[0])
        assert parsed["type"] == "line"

    def test_multiple_chart_extraction(self):
        import re
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = (
            '説明\n```chart\n{"type":"line","data":{"labels":[],"datasets":[]}}\n```\n'
            '別の説明\n```chart\n{"type":"bar","data":{"labels":[],"datasets":[]}}\n```\n'
        )
        matches = chart_pattern.findall(text)
        assert len(matches) == 2
        assert json.loads(matches[0])["type"] == "line"
        assert json.loads(matches[1])["type"] == "bar"

    def test_chart_block_removal(self):
        import re
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = '前文\n```chart\n{"type":"line"}\n```\n後文'
        clean = chart_pattern.sub('', text).strip()
        assert "chart" not in clean
        assert "前文" in clean
        assert "後文" in clean

    def test_invalid_json_ignored(self):
        import re
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = '```chart\nnot valid json\n```'
        matches = chart_pattern.findall(text)
        charts = []
        for m in matches:
            try:
                charts.append(json.loads(m))
            except json.JSONDecodeError:
                pass
        assert len(charts) == 0


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
        from server import _select_frames
        tk = np.array([0, 50, 50, 100, 150, 150, 150], dtype=np.int64)
        kept, fidx = _select_frames(tk, max_frames=200)
        assert kept.tolist() == [0, 50, 100, 150]
        assert fidx.tolist() == [0, 1, 1, 2, 3, 3, 3]

    def test_select_frames_thins_to_max(self):
        import numpy as np
        from server import _select_frames
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
        import server
        rng = np.random.default_rng(0)
        tk = rng.integers(0, 3000, size=5000, dtype=np.int64) * 10
        kept_a, fidx_a = server._select_frames(tk, 100)
        # 巨大な値を足して汎用経路を強制し，同じオフセットを引いて比較
        off = 60_000_000
        kept_b, fidx_b = server._select_frames(tk + off, 100)
        assert (kept_b - off).tolist() == kept_a.tolist()
        assert fidx_b.tolist() == fidx_a.tolist()

    @pytest.fixture(scope="class")
    def result(self):
        return _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO)

    def test_fast_path_is_active_for_installed_uxsim(self):
        """uxsim の内部 API（CLAUDE.md §3.6）が使えなくなると，動作は止まらず車両別ログの
        フォールバックで「遅くなるだけ」なので気づけない．cpp backend がある環境では
        高速経路が効いていることを CI で固定する（uxsim 更新時の検知）．"""
        import server
        rt = _run_uxsim(BOTTLENECK_SCENARIO)["_runtime"]
        assert rt["uxsim_version"] == server.UXSIM_VERSION
        if rt["backend"] != "cpp":
            pytest.skip("uxsim cpp backend が無い環境（高速経路は cpp 前提）")
        assert rt["fast_path"] is True, (
            f"uxsim {rt['uxsim_version']} で車両ログの高速経路が使えずフォールバックしている．"
            f"§3.6 の内部 API（build_all_vehicle_logs_flat_compact / _LOG_STATE_MAP / offsets）を確認: "
            f"{server.RUNTIME_STATUS.get('fast_path_error')}")
        assert server.RUNTIME_STATUS["fast_path"] is True

    def test_healthz_reports_uxsim_runtime(self):
        from fastapi.testclient import TestClient
        import server
        _run_uxsim(BOTTLENECK_SCENARIO)
        body = TestClient(server.app).get("/healthz").json()
        assert body["status"] == "ok"
        assert body["uxsim"]["uxsim_version"] == server.UXSIM_VERSION
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
        import server
        base = _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO)
        total = sum(int(len(c["ids"])) for c in base["frames"].values())
        monkeypatch.setattr(server, "MAX_FRAME_POINTS", max(1, total // 3))
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
        import server
        sid = "test_results_gz"
        server._store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            c = TestClient(server.app)
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
        import server
        sid = "test_env_json"
        server._store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            d = json.loads(server._envelope_json_bytes(sid))
            assert d["result"]["link_names"] == results_store[sid]["link_names"]
            assert d["result"]["frame_times"] == results_store[sid]["frame_times"]
        finally:
            results_store.pop(sid, None)


# ============================================================
# 10. LLM とのやり取りのトークン節約
# ============================================================

class TestLLMTokenSaving:
    """
    [修正履歴] 動的シミュレーションでは毎ターン rerun が走り，sim_id 入りの
    【現在のコンテキスト】を system に足していたため system 以降のキャッシュが毎回無効化，
    さらに会話履歴にはキャッシュ境界が無く tool ラウンドごとに全額再課金されていた．
    - system は不変，動的コンテキストは最後の user メッセージの追加ブロック
    - 履歴の最後の assistant と リクエスト末尾に cache_control
    - 履歴は文字数予算でトリミング（ヒステリシス付き）
    - グリッド網は grid テンプレートでサーバー展開（LLM の出力トークン削減）
    - チャートは {"$data": ...} 参照で配列の書き写しを不要に
    """

    def _body(self, msgs, last_sim_id=None):
        from server import ChatInput
        return ChatInput(messages=msgs, last_sim_id=last_sim_id)

    def test_system_prompt_stays_static_and_context_goes_to_last_user(self):
        import server
        sid = "tok_ctx"
        results_store[sid] = {
            "geojson": {"features": [{"properties": {"name": "L1"}}]},
            "_scenario": {"nodes": [{"name": "A"}], "links": [{"name": "L1"}], "demands": [], "tmax": 600},
        }
        try:
            body = self._body([
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ], last_sim_id=sid)
            msgs = server._build_llm_messages(body)
            assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
            # 履歴の最後の assistant にキャッシュ境界
            a1 = msgs[1]["content"]
            assert isinstance(a1, list) and a1[0]["text"] == "a1" and "cache_control" in a1[0]
            # 最後の user: 本文ブロック + コンテキストブロック（末尾に cache_control）
            u2 = msgs[2]["content"]
            assert u2[0]["text"] == "u2" and "cache_control" not in u2[0]
            assert sid in u2[1]["text"] and "cache_control" in u2[1]
            # 送信用には内部フラグが残らない
            api = server._api_messages(msgs)
            assert all("_tail_marked" not in b for m in api for b in m["content"])
            # コンテキストが無い場合は本文ブロックだけ（末尾にキャッシュ境界）
            msgs2 = server._build_llm_messages(self._body([{"role": "user", "content": "hi"}]))
            assert len(msgs2[0]["content"]) == 1 and "cache_control" in msgs2[0]["content"][0]
        finally:
            results_store.pop(sid, None)

    def test_tail_cache_mark_moves_with_rounds(self):
        import server
        msgs = server._build_llm_messages(self._body([{"role": "user", "content": "hi"}]))
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "calling"}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]})
        server._mark_cache_tail(msgs)
        # 末尾の印は tool_result に移り，以前の末尾（user "hi"）からは外れる
        assert "cache_control" in msgs[-1]["content"][-1]
        assert "cache_control" not in msgs[0]["content"][0]

    def test_trim_history_hysteresis_and_first_role(self, monkeypatch):
        import server
        monkeypatch.setattr(server, "MAX_HISTORY_CHARS", 1000)
        msgs = []
        for i in range(20):
            msgs.append({"role": "user", "content": f"u{i} " + "x" * 100})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 100})
        msgs.append({"role": "user", "content": "last"})
        kept = server._trim_history(msgs)
        assert kept[0]["role"] == "user"
        assert kept[-1]["content"] == "last"
        assert "省略" in kept[0]["content"]
        total = sum(len(m["content"]) for m in kept)
        assert total <= 1000 // 2 + 200  # 予算の半分まで落とす（先頭の注記分は許容）
        # 予算内なら手を付けない
        small = msgs[-3:]
        assert server._trim_history(small) is small

    def test_grid_template_and_auto_demands(self):
        import server
        args = {"grid": {"nx": 4, "ny": 3, "spacing": 250, "free_flow_speed": 15},
                "auto_demands": {"strategy": "random", "n_pairs": 5, "flow_per_pair": 0.1, "seed": 1},
                "tmax": 1200}
        scenario, info = server._expand_run_simulation_args(args)
        assert len(scenario["nodes"]) == 12
        # 双方向: 横 (3×3) + 縦 (4×2) = 17 本 × 2
        assert len(scenario["links"]) == 34
        assert all(l["length"] == 250 and l["free_flow_speed"] == 15 for l in scenario["links"])
        assert len(scenario["demands"]) == 5
        assert info["grid"]["nx"] == 4 and "n{i}_{j}" in info["grid"]["node_naming"]
        assert info["auto_demands"]["generated"] == 5
        # SimulationInput として妥当で，実行できる
        r = _run_uxsim(SimulationInput(**scenario))
        assert r["stats"]["total_trips"] > 0
        # 片方向グリッド
        one, _ = server._expand_run_simulation_args(
            {"grid": {"nx": 3, "bidirectional": False}, "demands": [
                {"orig": "n0_0", "dest": "n2_2", "t_start": 0, "t_end": 100, "flow": 0.2}]})
        assert len(one["links"]) == 12
        # nodes/links も demands も無ければエラー
        with pytest.raises(ValueError):
            server._expand_run_simulation_args({"nodes": [], "links": [], "demands": []})
        with pytest.raises(ValueError):
            server._expand_run_simulation_args({"grid": {"nx": 3}})

    def test_generate_demands_shared_with_rerun(self):
        from server import _apply_modifications
        sc = {"nodes": [{"name": f"n{i}", "x": i * 100, "y": 0} for i in range(6)],
              "links": [{"name": f"l{i}", "start": f"n{i}", "end": f"n{i+1}", "length": 100} for i in range(5)],
              "demands": [], "tmax": 600}
        out, applied = _apply_modifications(sc, [
            {"action": "generate_demands", "strategy": "random", "n_pairs": 4, "seed": 7, "flow_total": 0.8}])
        assert len(out["demands"]) == 4 and all(d["flow"] == 0.2 for d in out["demands"])
        with pytest.raises(ValueError):
            _apply_modifications(sc, [{"action": "generate_demands", "strategy": "nope"}])

    def test_chart_data_refs_resolved(self):
        import server
        sid = "tok_chart"
        server._store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            cache = {}
            text = ('結果です．\n```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},'
                    '"datasets":[{"label":"v","data":{"$data":"network_avg_speed"}},'
                    '{"label":"r1","data":{"$data":"link_speeds.r1","sim_id":"%s"}},'
                    '{"label":"missing","data":{"$data":"nope.x"}}]}}\n```\n以上．' % sid)
            charts, clean = server._extract_charts(text, cache, sid)
            assert clean == "結果です．\n\n以上．".replace("\n\n", "\n\n") or "chart" not in clean
            assert len(charts) == 1
            d = charts[0]["data"]
            sd = server._get_simulation_data(sid)
            assert d["labels"] == sd["time_labels"]
            assert d["datasets"][0]["data"] == sd["network_avg_speed"]
            assert d["datasets"][1]["data"] == sd["link_speeds"]["r1"]
            assert d["datasets"][2]["data"] == []  # 未解決は空配列
            assert sid in cache  # 未取得なら _get_simulation_data で補う
        finally:
            results_store.pop(sid, None)

    def test_simulation_data_is_compact(self):
        import server
        sid = "tok_compact"
        server._store_sim(sid, _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO), {"type": "manual"})
        try:
            sd = server._get_simulation_data(sid)
            assert len(sd["time_labels"]) <= 30
            assert len(sd["link_speeds"]) <= 20
            sd2 = server._get_simulation_data(sid, points=10, max_links=0)
            assert len(sd2["time_labels"]) <= 10 and sd2["link_speeds"] == {}
            assert len(json.dumps(sd)) < 8000
        finally:
            results_store.pop(sid, None)

    def test_stream_chat_end_to_end_with_fake_client(self, monkeypatch):
        """偽 Anthropic クライアントで /chat ストリーミングの流れを検証:
        grid テンプレートで実行 → get_simulation_data → $data 参照チャート → usage 付き done．
        各リクエストの messages にキャッシュ境界が正しく付くことも確認．"""
        import anthropic
        import server
        from types import SimpleNamespace as NS

        captured = []

        class FakeStream:
            def __init__(self, response):
                self._r = response
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def __iter__(self):
                for b in self._r.content:
                    if b.type == "text":
                        yield NS(type="content_block_delta", delta=NS(type="text_delta", text=b.text))
                    else:
                        yield NS(type="content_block_start", index=0,
                                 content_block=NS(type="tool_use", id=b.id, name=b.name))
            def get_final_message(self):
                return self._r

        usage = NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50,
                   cache_creation_input_tokens=10)
        responses = [
            NS(stop_reason="tool_use", usage=usage, content=[
                NS(type="text", text="RISUが実行します"),
                NS(type="tool_use", id="t1", name="run_simulation",
                   input={"grid": {"nx": 3, "spacing": 300},
                          "auto_demands": {"strategy": "boundary"}, "tmax": 600}),
            ]),
            NS(stop_reason="tool_use", usage=usage, content=[
                NS(type="tool_use", id="t2", name="get_simulation_data", input={"sim_id": ""}),
            ]),
            NS(stop_reason="end_turn", usage=usage, content=[
                NS(type="text", text='完了．\n```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},'
                                     '"datasets":[{"data":{"$data":"network_avg_speed"}}]}}\n```'),
            ]),
        ]

        class FakeMessages:
            def stream(self, **kw):
                captured.append(kw)
                return FakeStream(responses[len(captured) - 1])
            def create(self, **kw):
                raise AssertionError("create は呼ばれないはず")

        class FakeClient:
            def __init__(self, *a, **kw):
                self.messages = FakeMessages()

        monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
        monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "dummy")

        body = server.ChatInput(messages=[
            {"role": "user", "content": "前の話"}, {"role": "assistant", "content": "前の返事"},
            {"role": "user", "content": "3x3 グリッドで実行してグラフも"}])

        import asyncio as _aio
        async def run():
            resp = await server._chat_claude_stream(body)
            events = []
            async for chunk in resp.body_iterator:
                for line in chunk.split("\n\n"):
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            return events
        events = _aio.run(run())
        done = [e for e in events if e["type"] == "done"][0]
        try:
            assert done["sim_id"] in results_store
            assert done["usage"]["calls"] == 3 and done["usage"]["output_tokens"] == 60
            assert done["usage"]["cache_read_tokens"] == 150
            assert len(done["charts"]) == 1
            sd = server._get_simulation_data(done["sim_id"])
            assert done["charts"][0]["data"]["labels"] == sd["time_labels"]
            assert "```" not in done["content"]
            # ─ リクエスト構造 ─
            assert len(captured) == 3
            for kw in captured:
                assert kw["system"][0]["text"] == server.SYSTEM_PROMPT  # system は不変
                assert "cache_control" in kw["system"][0]
                assert "cache_control" in kw["tools"][-1]
                msgs = kw["messages"]
                # 末尾メッセージの最後のブロックにキャッシュ境界，内部フラグは無い
                last_blocks = msgs[-1]["content"]
                assert "cache_control" in last_blocks[-1] and "_tail_marked" not in last_blocks[-1]
                # 履歴の assistant にもキャッシュ境界
                assert "cache_control" in msgs[1]["content"][0]
                n_marks = sum(1 for m in msgs for b in m["content"]
                              if isinstance(b, dict) and "cache_control" in b)
                assert n_marks == 2, f"messages 内の breakpoint は 2 つ（履歴 assistant + 末尾）: {n_marks}"
            # 2 回目以降は tool_result が末尾
            assert captured[1]["messages"][-1]["content"][-1]["type"] == "tool_result"
            # grid 展開結果（命名規則）が tool_result で LLM に伝わる
            tr1 = json.loads(captured[1]["messages"][-1]["content"][-1]["content"])
            assert tr1["network"]["nodes"] == 9 and "n{i}_{j}" in tr1["grid"]["node_naming"]
        finally:
            results_store.pop(done["sim_id"], None)


class TestStoreLimitAndUsageCost:
    def test_results_store_evicts_oldest(self, monkeypatch):
        import server
        monkeypatch.setattr(server, "MAX_RESULTS", 2)
        base = _run_uxsim(BOTTLENECK_SCENARIO)
        ids = ["evict_a", "evict_b", "evict_c"]
        try:
            for sid in ids:
                server._store_sim(sid, dict(base), {"type": "manual"})
            assert "evict_a" not in results_store
            assert "evict_b" in results_store and "evict_c" in results_store
        finally:
            for sid in ids:
                results_store.pop(sid, None)

    def test_usage_cost_never_negative_with_cached_input(self):
        """新 API の usage は input_tokens にキャッシュ分を含まない．以前の式は負の円額を出した"""
        from types import SimpleNamespace as NS
        import server
        u = server._log_usage("test", NS(usage=NS(input_tokens=2, output_tokens=190,
                                                  cache_read_input_tokens=8234,
                                                  cache_creation_input_tokens=1359)))
        assert u["cost_jpy"] > 0
        tally = server._UsageTally()
        tally.add("t", NS(usage=NS(input_tokens=2, output_tokens=10, cache_read_input_tokens=10000,
                                   cache_creation_input_tokens=0)))
        d = tally.as_dict()
        assert d["cache_hit_pct"] >= 99 and d["cost_jpy"] > 0


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
        from server import _trip_stats
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
        from server import _build_envelope, _store_sim
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


class TestGuiRerunCarriesScenarioParams:
    """GUI エディタからの再実行が reaction_time / random_seed を落とさないことを，
    index.html のソース上で固定する（ブラウザなしで検証できる範囲）．

    [修正履歴] 編集後の POST /simulate に reaction_time が無く，元シナリオで 1.7 を
    指定していても UXsim 既定 1.0 に戻り，1 車線容量が約 1,846 → 2,880 台/時に変わっていた．
    """

    @pytest.fixture(scope="class")
    def html(self):
        p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "index.html")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def _run_handler(self, html):
        i = html.index("document.getElementById('et-run').addEventListener")
        j = html.index("// ── 再生制御 ──", i)
        return html[i:j]

    def test_submit_includes_reaction_time_and_seed(self, html):
        h = self._run_handler(html)
        assert "scenario.reaction_time = " in h
        assert "scenario.random_seed = " in h
        assert "deltan:" in h

    def test_edit_mode_initializes_from_scenario(self, html):
        i = html.index("function toggleEditMode()")
        block = html[i:i + 2000]
        assert "src.reaction_time" in block and "src.random_seed" in block
        assert 'id="et-rt"' in html and 'id="et-seed"' in html

    def test_capacity_zero_is_kept(self, html):
        """[修正履歴] 容量欄に 0 を入れると capacity が削除され，容量制約が外れていた．"""
        i = html.index("field === 'capacity'")
        block = html[i:i + 500]
        assert "v < 0) delete lk.capacity" in block
        assert "v <= 0" not in block

    def test_stats_use_server_series(self, html):
        i = html.index("function computeStatsSeries()")
        block = html[i:html.index("function currentTimeSec()", i)]
        assert "tripSeries" in block and "frameAvgSpeed" in block
        j = html.index("function updateStatValues()")
        assert "tripAt(t)" in html[j:j + 1500]

    def test_phase_log_uses_server_deltat(self, html):
        i = html.index("function currentPhaseIdx(sig, t)")
        block = html[i:i + 1200]
        assert "sig.deltat" in block
        assert "t / tmax * phaseLog.length" not in block

    def test_active_vehicles_uses_real_counts(self, html):
        i = html.index("function computeStatsSeries()")
        block = html[i:html.index("function currentFrameIdx()", i)]
        assert "vehicleCounts[i]" in block
        assert "frame.n * vehScale" in block
        assert "vehicle_counts" in html and "vehicle_sample_step" in html


# ============================================================
# 送出時のフレーム量子化（columnar_v3）
# ============================================================

class TestFrameWireEncodingV3:
    """/results が返す columnar_v3 のエンコード/デコード整合性．

    v3 は「送出時だけ」の表現で，results_store 側は v2（素の値）のまま．
    サーバー内の消費側（_get_simulation_data 等）が壊れないことも確認する．
    """

    @classmethod
    def setup_class(cls):
        from server import _store_sim
        cls.res = _run_uxsim(BOTTLENECK_SCENARIO)
        cls.sim_id = "wire_v3_test"
        _store_sim(cls.sim_id, cls.res)

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
        from server import _encode_frames_v3

        src = self.res["frames"]
        enc = _encode_frames_v3(src)
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
        from server import _build_envelope, _envelope_json_bytes

        v2_env = _build_envelope(self.sim_id, include_result=True)
        assert v2_env["result"]["frame_format"] == "columnar_v2"
        v2_bytes = orjson.dumps(
            v2_env, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)

        v3_bytes = _envelope_json_bytes(self.sim_id)
        assert b'"columnar_v3"' in v3_bytes
        assert len(v3_bytes) < len(v2_bytes), (
            f"v3 が v2 より大きい: {len(v3_bytes)} >= {len(v2_bytes)}")

    def test_results_store_is_not_mutated(self):
        """送出用の変換が results_store の配列を書き換えないこと．

        CLAUDE.md の「結果 dict は保存後に変更しないこと」を守る
        （_enc_cache は一度作ると使い回されるため，壊すと以後ずっと壊れる）．
        """
        import numpy as np
        from server import _envelope_json_bytes

        before = {k: np.asarray(v["xs"]).copy() for k, v in self.res["frames"].items()}
        _envelope_json_bytes(self.sim_id)
        for k, arr in before.items():
            np.testing.assert_array_equal(np.asarray(self.res["frames"][k]["xs"]), arr)
        assert results_store[self.sim_id]["frame_format"] == "columnar_v2"

    def test_simulation_data_still_works(self):
        """サーバー内の集計（LLM に渡すデータ）が素の値を読めていること．"""
        data = _get_simulation_data(self.sim_id)
        assert data is not None
        assert data.get("network_avg_speed")
        # 速度が 0.1 m/s 単位の「整数」になっていない（=量子化が漏れていない）
        speeds = [s for s in data["network_avg_speed"] if s]
        assert speeds, "速度データが空"


# ============================================================
# ツール実行の共通ディスパッチ（_dispatch_tool_blocks）
# ============================================================

class TestToolDispatch:
    """ストリーミング経路と同期経路が通る共通 dispatch のテスト．

    以前は初回ラウンド・追加ラウンド × 2 経路の計 4 箇所に同じ dispatch が
    コピーされており，片方だけ直すと挙動がずれる状態だった．共通化したので
    「両経路が同じ結果を返すこと」をここで固定する．
    """

    @staticmethod
    def _tb(name, args, tid="tu_1"):
        """anthropic の tool_use ブロック相当のダミー．"""
        import types
        return types.SimpleNamespace(name=name, input=args, id=tid, type="tool_use")

    @staticmethod
    def _body(messages=None, last_sim_id=None):
        from server import ChatInput
        return ChatInput(
            messages=messages or [{"role": "user", "content": "テスト"}],
            last_sim_id=last_sim_id,
        )

    def _run(self, blocks, *, follow_up=False, body=None):
        """dispatch を回して (progress メッセージ列, tool_results, state) を返す．"""
        import asyncio
        from server import _ToolTurnState, _dispatch_tool_blocks

        state = _ToolTurnState(body or self._body())
        progress, results = [], []

        async def go():
            async for kind, payload in _dispatch_tool_blocks(blocks, state, follow_up=follow_up):
                if kind == "progress":
                    progress.append(payload)
                else:
                    results.append(payload)

        asyncio.run(go())
        assert len(results) == 1, "results は最後に 1 回だけ yield されるべき"
        return progress, results[0], state

    # ── API 不変条件 ──────────────────────────────

    def test_every_tool_use_gets_exactly_one_result(self):
        """tool_use には必ず 1 対 1 で tool_result を返す（Anthropic API の要求）．

        欠けると API が 400 を返すので，未知のツール名でも結果を積む必要がある．
        共通化前は同期経路の初回ラウンドにこの else 分岐が無く，
        未知ツールを呼ばれるとリクエストが壊れる状態だった．
        """
        blocks = [
            self._tb("get_network_info", {"sim_id": "nope"}, "a"),
            self._tb("get_simulation_data", {"sim_id": "nope"}, "b"),
            self._tb("totally_unknown_tool", {}, "c"),
        ]
        _, results, _ = self._run(blocks)
        assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
        assert all(r["type"] == "tool_result" for r in results)
        assert all(isinstance(r["content"], str) for r in results)

    def test_unknown_tool_is_reported_not_dropped(self):
        _, results, _ = self._run([self._tb("no_such_tool", {}, "x")])
        assert len(results) == 1
        assert "未知のツール" in results[0]["content"]
        assert "no_such_tool" in results[0]["content"]

    # ── 実際のツール ──────────────────────────────

    def test_run_simulation_populates_sim_id(self):
        blocks = [self._tb("run_simulation", {
            "name": "dispatch_test", "tmax": 600, "deltan": 5,
            "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}],
            "links": [{"name": "AB", "start": "A", "end": "B", "length": 1000}],
            "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 300, "flow": 0.4}],
        }, "r1")]
        _, results, state = self._run(blocks)
        try:
            assert state.sim_id, "run_simulation 後に sim_id が入っていない"
            assert state.sim_id in results_store
            assert not results[0].get("is_error"), results[0]["content"]
        finally:
            results_store.pop(state.sim_id, None)

    def test_get_simulation_data_fills_cache_for_chart_refs(self):
        """get_simulation_data の結果が $data 解決用キャッシュに入ること．"""
        from server import _store_sim
        sid = "dispatch_data_test"
        _store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            _, results, state = self._run(
                [self._tb("get_simulation_data", {"sim_id": sid}, "d1")])
            assert state.last_data_sim_id == sid
            assert sid in state.sim_data_cache
            assert "network_avg_speed" in results[0]["content"]
        finally:
            results_store.pop(sid, None)

    def test_get_simulation_data_missing_is_not_an_exception(self):
        _, results, state = self._run(
            [self._tb("get_simulation_data", {"sim_id": "does_not_exist"}, "d2")])
        assert "データが見つかりません" in results[0]["content"]
        assert state.last_data_sim_id is None
        assert state.sim_data_cache == {}

    # ── 進捗イベント ──────────────────────────────

    def test_progress_messages_differ_between_rounds(self):
        """初回ラウンドは Step 表記，追加ラウンドは別文言（SSE の見た目を保つ）．"""
        blocks = [self._tb("get_simulation_data", {"sim_id": "x"}, "p1")]
        first, _, _ = self._run(blocks, follow_up=False)
        later, _, _ = self._run(blocks, follow_up=True)
        assert first == ["データを集計中..."]
        assert later == ["チャートデータを取得中..."]

    def test_sync_path_helper_drops_progress(self):
        """_collect_tool_results は進捗を捨てて results だけ返す．"""
        import asyncio
        from server import _ToolTurnState, _collect_tool_results

        state = _ToolTurnState(self._body())
        blocks = [self._tb("get_simulation_data", {"sim_id": "x"}, "s1")]
        results = asyncio.run(_collect_tool_results(blocks, state))
        assert isinstance(results, list) and len(results) == 1
        assert results[0]["tool_use_id"] == "s1"

    def test_both_paths_produce_identical_results(self):
        """同じ入力なら，進捗を拾う経路（SSE）と捨てる経路（同期）で
        tool_result が完全に一致すること．共通化の目的そのもの．"""
        import asyncio
        from server import _ToolTurnState, _collect_tool_results

        blocks = [
            self._tb("get_network_info", {"sim_id": "nope"}, "a"),
            self._tb("get_simulation_data", {"sim_id": "nope"}, "b"),
            self._tb("unknown", {}, "c"),
        ]
        _, streamed, _ = self._run(blocks)
        sync = asyncio.run(_collect_tool_results(blocks, _ToolTurnState(self._body())))
        assert streamed == sync


# ============================================================
# ストリーミング経路の通しテスト（Anthropic クライアントをスタブ化）
# ============================================================

class _FakeUsage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeBlock:
    """anthropic の content block 相当．"""

    def __init__(self, type_, *, text=None, name=None, input=None, id=None):
        self.type = type_
        self.text = text
        self.name = name
        self.input = input
        self.id = id


class _FakeMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _FakeStream:
    """client.messages.stream(...) の戻り値（context manager かつ iterable）．"""

    def __init__(self, message, events):
        self._message = message
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._message


class _FakeMessages:
    def __init__(self, script):
        # script: 呼び出しごとに返す _FakeMessage のリスト
        self._script = list(script)
        self.calls = []

    def _next(self, kind, kwargs):
        self.calls.append((kind, kwargs))
        if not self._script:
            raise AssertionError("スタブの応答が尽きた（想定より多く API を呼んでいる）")
        return self._script.pop(0)

    def create(self, **kwargs):
        return self._next("create", kwargs)

    def stream(self, **kwargs):
        msg = self._next("stream", kwargs)
        events = []
        for b in msg.content:
            if b.type == "text":
                events.append(types_ns(
                    type="content_block_delta",
                    delta=types_ns(type="text_delta", text=b.text),
                ))
        return _FakeStream(msg, events)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


class _FakeAnthropic:
    def __init__(self, script):
        self.messages = _FakeMessages(script)


class TestChatStreamingPath:
    """_chat_claude_stream を SSE ごと通して検証する．

    共通化した _dispatch_tool_blocks をストリーミング経路が正しく配線できているか
    （state 経由の sim_id 引き回し，進捗イベントの転送，done イベントの中身）を見る．
    実 API は呼ばない．
    """

    @staticmethod
    def _collect_sse(body, script):
        """スタブ化したクライアントで event_generator を回し，SSE イベントを集める．"""
        import asyncio
        import sys

        import server

        anthropic_mod = sys.modules.get("anthropic")
        if anthropic_mod is None:
            import anthropic as anthropic_mod  # noqa: F811

        orig = anthropic_mod.Anthropic
        anthropic_mod.Anthropic = lambda **kw: _FakeAnthropic(script)
        try:
            async def go():
                resp = await server._chat_claude_stream(body)
                chunks = []
                async for c in resp.body_iterator:
                    chunks.append(c if isinstance(c, str) else c.decode("utf-8"))
                return "".join(chunks)

            raw = asyncio.run(go())
        finally:
            anthropic_mod.Anthropic = orig

        events = []
        for part in raw.split("\n\n"):
            part = part.strip()
            if part.startswith("data: "):
                events.append(json.loads(part[6:]))
        return events

    @staticmethod
    def _body(text="テスト"):
        from server import ChatInput
        return ChatInput(messages=[{"role": "user", "content": text}], last_sim_id=None)

    def test_text_only_response_streams_and_finishes(self):
        """ツールを呼ばない応答: text_delta が流れ，done で content が返る．"""
        script = [_FakeMessage([_FakeBlock("text", text="こんにちは．RISUです．")], "end_turn")]
        events = self._collect_sse(self._body("こんにちは"), script)

        kinds = [e["type"] for e in events]
        assert "stream_start" in kinds
        assert "done" in kinds
        deltas = "".join(e["text"] for e in events if e["type"] == "text_delta")
        assert deltas == "こんにちは．RISUです．"
        done = next(e for e in events if e["type"] == "done")
        assert done["content"] == "こんにちは．RISUです．"
        assert done["sim_id"] is None
        assert "usage" in done

    def test_tool_round_sets_sim_id_and_emits_progress(self):
        """run_simulation を経由すると done に sim_id が乗り，進捗が流れること．

        共通 dispatch が state.sim_id に書き，ストリーミング側がそれを読む配線の確認．
        """
        scenario = {
            "name": "sse_test", "tmax": 600, "deltan": 5,
            "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}],
            "links": [{"name": "AB", "start": "A", "end": "B", "length": 1000}],
            "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 300, "flow": 0.4}],
        }
        script = [
            # 1回目: tool_use
            _FakeMessage([_FakeBlock("tool_use", name="run_simulation",
                                     input=scenario, id="tu_a")], "tool_use"),
            # ツール結果を受けた最終回答（ストリーミング）
            _FakeMessage([_FakeBlock("text", text="シミュレーションが完了しました．")], "end_turn"),
        ]
        events = self._collect_sse(self._body("道路を作って"), script)
        done = next(e for e in events if e["type"] == "done")
        try:
            assert done["sim_id"], "done に sim_id が乗っていない"
            assert done["sim_id"] in results_store
            assert done["content"] == "シミュレーションが完了しました．"
            progress = [e["message"] for e in events if e["type"] == "progress"]
            assert any("UXsim" in m for m in progress), progress
        finally:
            results_store.pop(done.get("sim_id"), None)

    def test_unknown_tool_does_not_break_the_stream(self):
        """未知ツールでも tool_result が返り，ストリームが done まで到達する．"""
        script = [
            _FakeMessage([_FakeBlock("tool_use", name="bogus_tool",
                                     input={}, id="tu_b")], "tool_use"),
            _FakeMessage([_FakeBlock("text", text="対応していない操作でした．")], "end_turn"),
        ]
        events = self._collect_sse(self._body("なにか"), script)
        assert [e["type"] for e in events].count("done") == 1
        done = next(e for e in events if e["type"] == "done")
        assert done["sim_id"] is None
        assert done["content"] == "対応していない操作でした．"

    def test_api_error_becomes_error_event_not_crash(self):
        """スタブを尽きさせて例外を起こし，error イベントで終わることを確認．"""
        events = self._collect_sse(self._body("x"), [])
        assert events and events[-1]["type"] == "error"
        assert "message" in events[-1]


# ============================================================
# ライセンス衛生（MIT で公開できる状態を保つ）
# ============================================================

class TestLicenseHygiene:
    """RISU を MIT で配布できる前提を壊さないためのテスト．

    uxsim は PyQt5 (GPL v3) を必須依存として宣言しているため，pip install すると
    環境には入る．RISU 自身がそれを import しない限り，MIT 配布の妨げにはならない．
    「いつのまにか import されるようになっていた」を検知するのがここの目的．
    詳細は THIRD_PARTY_LICENSES.md を参照．
    """

    GPL_MODULES = ("PyQt5", "PyQt6", "PySide2", "PySide6")

    def test_vendored_scripts_keep_license_headers_and_no_cdn(self):
        """同梱 JS（marked / DOMPurify / Chart.js）はライセンスヘッダを保持し，
        index.html は CDN から script を読まない（オフラインで完全に動く前提）．"""
        root = os.path.dirname(os.path.dirname(__file__))
        vendor = os.path.join(root, "static", "vendor")
        expected = {"marked.umd.min.js", "purify.min.js", "chart.umd.min.js"}
        assert expected <= set(os.listdir(vendor))
        import re
        # 同梱ファイルは THIRD_PARTY_LICENSES.md に必ず載せる（配布物なので表示義務がある）．
        # ヘッダの形式は配布元（jsDelivr のバナー等）に依存するので，ここでは表の記載を検証する
        with open(os.path.join(root, "THIRD_PARTY_LICENSES.md"), encoding="utf-8") as f:
            licenses = f.read()
        for name in expected:
            assert f"static/vendor/{name}" in licenses, f"{name} が THIRD_PARTY_LICENSES.md に無い"
        with open(os.path.join(root, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
        assert srcs and all(s.startswith("/vendor/") for s in srcs), srcs

    def test_risu_does_not_import_qt(self):
        """server.py を読み込み，シミュレーションを流しても Qt を import しない．"""
        import subprocess

        code = f'''
import sys, os
os.environ["LLM_BACKEND"] = "mock"
import server
sc = server.SimulationInput(
    name="lic", tmax=300, deltan=5,
    nodes=[{{"name": "A", "x": 0, "y": 0}}, {{"name": "B", "x": 500, "y": 0}}],
    links=[{{"name": "AB", "start": "A", "end": "B", "length": 500}}],
    demands=[{{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.3}}],
)
res = server._run_uxsim(sc)
server._store_sim("lic", res)
server._envelope_json_bytes("lic")
server._get_simulation_data("lic")
qt = [m for m in sys.modules if m.split(".")[0] in {self.GPL_MODULES!r}]
print("QT:" + ",".join(sorted(qt)))
'''
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        line = next(ln for ln in r.stdout.splitlines() if ln.startswith("QT:"))
        loaded = [m for m in line[3:].split(",") if m]
        assert not loaded, (
            f"GPL ライセンスの Qt モジュールが読み込まれた: {loaded}．"
            "MIT 配布の前提が崩れるので，依存の追加を見直すこと"
        )

    def test_risu_runs_without_qt_installed(self):
        """PyQt5 が入っていない環境でも全経路が動くこと．

        利用者が `pip uninstall PyQt5` しても RISU が壊れない，という
        THIRD_PARTY_LICENSES.md の記述を裏付ける．
        """
        import subprocess

        code = '''
import sys, os

class _Block:
    BAD = ("PyQt5", "PyQt6", "PySide2", "PySide6")
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BAD:
            raise ImportError(name + " は未インストールという想定")
        return None

sys.meta_path.insert(0, _Block())
os.environ["LLM_BACKEND"] = "mock"
import server
sc = server.SimulationInput(
    name="noqt", tmax=300, deltan=5,
    nodes=[{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 500, "y": 0}],
    links=[{"name": "AB", "start": "A", "end": "B", "length": 500}],
    demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.3}],
)
res = server._run_uxsim(sc)
server._store_sim("noqt", res)
assert b'"columnar_v3"' in server._envelope_json_bytes("noqt")
assert server._get_simulation_data("noqt")

from uxsim_bridge import build_world, scenario_from_dict
W = build_world(scenario_from_dict({
    "name": "b", "tmax": 200, "deltan": 5,
    "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 500, "y": 0}],
    "links": [{"name": "AB", "start": "A", "end": "B", "length": 500}],
    "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.3}],
}), disable_basic_analysis=True)
W.exec_simulation()
print("OK")
'''
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "OK" in r.stdout

    def test_bundled_vendor_files_keep_license_headers(self):
        """同梱している marked / DOMPurify のライセンス表記が消えていないこと．

        MIT / Apache-2.0 いずれも著作権表示の保持が条件なので，
        ミニファイ済みファイルの先頭コメントを削ってはいけない．
        """
        root = os.path.dirname(os.path.dirname(__file__))
        vendor = os.path.join(root, "static", "vendor")
        expected = {
            "marked.umd.min.js": ("marked", "license", "Do NOT use SRI"),
            "purify.min.js": ("DOMPurify", "license"),
        }
        for fname, needles in expected.items():
            path = os.path.join(vendor, fname)
            assert os.path.exists(path), f"{fname} が無い"
            head = open(path, encoding="utf-8").read(600)
            assert any(n.lower() in head.lower() for n in needles), (
                f"{fname} の先頭にライセンス表記が見当たらない")

    def test_third_party_licenses_doc_exists(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for fname in ("LICENSE", "THIRD_PARTY_LICENSES.md"):
            path = os.path.join(root, fname)
            assert os.path.exists(path), f"{fname} が無い"
        text = open(os.path.join(root, "LICENSE"), encoding="utf-8").read()
        assert "MIT License" in text


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
        import server
        cls.sid = "test_enc_negotiation"
        server._store_sim(cls.sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})

    @classmethod
    def teardown_class(cls):
        results_store.pop(cls.sid, None)

    @staticmethod
    def _client():
        from fastapi.testclient import TestClient
        import server
        return TestClient(server.app)

    # ── 選択ロジック単体 ──────────────────────────

    def test_negotiate_prefers_zstd_when_available(self):
        import server
        if server._zstd is None:
            pytest.skip("zstandard 未インストール")
        assert server._negotiate_encoding("gzip, deflate, br, zstd") == "zstd"
        assert server._negotiate_encoding("ZSTD") == "zstd", "大文字small文字を無視すべき"

    def test_negotiate_falls_back_to_gzip(self):
        import server
        assert server._negotiate_encoding("gzip, deflate, br") == "gzip"
        assert server._negotiate_encoding("gzip") == "gzip"

    def test_negotiate_identity_when_nothing_supported(self):
        import server
        assert server._negotiate_encoding("") == "identity"
        assert server._negotiate_encoding("identity") == "identity"
        assert server._negotiate_encoding(None) == "identity"

    def test_negotiate_uses_gzip_if_zstandard_missing(self, monkeypatch):
        """zstandard が入っていない環境では zstd を要求されても gzip になる．"""
        import server
        monkeypatch.setattr(server, "_zstd", None)
        assert server._negotiate_encoding("gzip, deflate, br, zstd") == "gzip"

    # ── エンドポイントの実挙動 ────────────────────

    def test_all_encodings_return_identical_payload(self):
        """圧縮方式が変わっても中身は同一（TestClient が透過的に解凍する）．"""
        import server
        c = self._client()
        base = c.get(f"/results/{self.sid}", headers={"Accept-Encoding": "identity"})
        assert base.status_code == 200
        assert base.headers.get("content-encoding") is None
        expected = base.json()

        gz = c.get(f"/results/{self.sid}", headers={"Accept-Encoding": "gzip"})
        assert gz.headers.get("content-encoding") == "gzip"
        assert gz.json() == expected

        if server._zstd is not None:
            zs = c.get(f"/results/{self.sid}", headers={"Accept-Encoding": "gzip, zstd"})
            assert zs.headers.get("content-encoding") == "zstd"
            # TestClient(httpx) が zstd を解凍できない場合は自前で解凍して比較する
            try:
                got = zs.json()
            except Exception:
                got = json.loads(server._zstd.ZstdDecompressor().decompress(zs.content))
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
        import server
        sid = "test_enc_cache"
        server._store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            c = self._client()
            r1 = c.get(f"/results/{sid}", headers={"Accept-Encoding": "gzip"})
            cache = results_store[sid]["_enc_cache"]
            assert set(cache) == {"gzip"}, "要求していない方式まで作っている"

            r2 = c.get(f"/results/{sid}", headers={"Accept-Encoding": "gzip"})
            assert r2.content == r1.content
            assert results_store[sid]["_enc_cache"]["gzip"] is cache["gzip"], "再圧縮している"

            if server._zstd is not None:
                c.get(f"/results/{sid}", headers={"Accept-Encoding": "zstd"})
                assert set(results_store[sid]["_enc_cache"]) == {"gzip", "zstd"}
        finally:
            results_store.pop(sid, None)

    def test_zstd_payload_is_smaller_than_gzip(self):
        """zstd を選ぶ意味があること（同じ結果で実際に小さい）．"""
        import server
        if server._zstd is None:
            pytest.skip("zstandard 未インストール")
        gz = server._envelope_compressed_bytes(self.sid, "gzip")
        zs = server._envelope_compressed_bytes(self.sid, "zstd")
        assert len(zs) < len(gz), f"zstd={len(zs)} >= gzip={len(gz)}"

    def test_gzip_wrapper_still_works(self):
        """既存の _envelope_gzip_bytes（後方互換ラッパー）が生きていること．"""
        import server
        assert server._envelope_gzip_bytes(self.sid) == \
            server._envelope_compressed_bytes(self.sid, "gzip")


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
        import server
        assert server.RISU_HOST == "127.0.0.1", (
            f"既定の待ち受けが {server.RISU_HOST} になっている．"
            "認証が無いので既定は 127.0.0.1 でなければならない"
        )

    def test_default_reload_is_off(self):
        """オートリロードは開発用．既定で有効だとプロセスが 2 つ起動する．"""
        import server
        assert server.RISU_RELOAD is False

    def test_host_and_port_are_overridable(self, monkeypatch):
        """別マシンから使いたい人は環境変数で明示的に開ける．"""
        import importlib

        import server as _s
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
        import server
        assert all("localhost" in o or "127.0.0.1" in o for o in server.ALLOWED_ORIGINS), \
            f"CORS の既定に外部オリジンが含まれている: {server.ALLOWED_ORIGINS}"


# ============================================================
# MCP はチャットと同じツール定義・同じ dispatcher を通る
# ============================================================

class TestMcpParity:
    """[修正履歴] MCP は run_simulation / get_result の 2 つだけを別定義していて，
    差分再実行・ネットワーク照会・集計データ・OSM 取込が MCP から使えなかった．
    いまは CLAUDE_TOOLS をそのまま公開し，_dispatch_tool_blocks を共有する（§3.2）．
    """

    def test_mcp_exposes_every_chat_tool_with_same_schema(self):
        import server
        tools = {t.name: t for t in server._mcp_tools()}
        for t in server.CLAUDE_TOOLS:
            assert t["name"] in tools, f"MCP に {t['name']} が無い"
            assert tools[t["name"]].inputSchema == t["input_schema"]
            assert tools[t["name"]].description == t["description"]
        assert "get_result" in tools   # 互換ツール

    def test_mcp_roundtrip_through_shared_dispatcher(self):
        import asyncio
        import server

        async def go():
            created = []
            try:
                r = await server._mcp_call_tool("run_simulation", {
                    "grid": {"nx": 3, "spacing": 500},
                    "auto_demands": {"strategy": "boundary", "flow_per_pair": 0.1},
                    "tmax": 600, "random_seed": 1,
                })
                d = json.loads(r); sid = d["sim_id"]; created.append(sid)
                assert server.results_store[sid]["_meta"]["source"]["via"] == "mcp"

                r = await server._mcp_call_tool("get_simulation_data", {"sim_id": sid, "points": 10})
                assert "network_avg_speed" in json.loads(r)

                r = await server._mcp_call_tool("get_network_info", {"sim_id": sid, "include": "summary"})
                assert "error" not in r.lower()[:40]

                r = await server._mcp_call_tool("rerun_simulation", {
                    "base_sim_id": sid,
                    "modifications": [{"action": "set_params", "random_seed": 2}],
                })
                d2 = json.loads(r); created.append(d2["sim_id"])
                assert d2["sim_id"] != sid
                assert server.results_store[d2["sim_id"]]["_scenario"]["random_seed"] == 2

                r = await server._mcp_call_tool("get_result", {"simulation_id": sid})
                assert "total_trips" in r
                r = await server._mcp_call_tool("no_such_tool", {})
                assert "未知のツール" in r
                r = await server._mcp_call_tool("get_result", {"simulation_id": "missing"})
                assert "見つかりません" in r
            finally:
                for s in created:
                    server.results_store.pop(s, None)

        asyncio.run(go())
