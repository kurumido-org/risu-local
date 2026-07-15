"""
RISU 安定性テスト
================
これまでの開発で発見・修正した問題をテストとして記録。
新機能追加時にこのテストが全て通ることを確認すること。

テスト実行:
    cd risu-local
    .venv/Scripts/activate
    pip install pytest httpx
    pytest tests/ -v

注意: サーバー (python server.py) が起動している必要があるテストは
      test_api_* で始まるもの。それ以外はサーバー不要。
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
        タイムラインの各エントリに t と speed が含まれる。
        [修正履歴] タイムラインを一度削除してしまい LINK モードが壊れた。
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
        frame_times の各値を str() したものが frames のキーに存在する。
        [修正履歴] Python は "25.0" をキーにするが、
        JS の String(25.0) は "25" になりマッチしなかった。
        → フロントエンドで parseFloat 正規化で対処。
        このテストはサーバー側のキー形式を記録する。
        """
        frames = result["frames"]
        for t in result["frame_times"]:
            key = str(round(t, 1))
            assert key in frames, f"frame_times の {t} に対応するキー '{key}' が frames にない"

    def test_vehicle_data_fields(self, result):
        """
        フレームがコンパクト列指向フォーマット（columnar_v2）である。
        各フレームは {ids, xs, ys, vs, alphas, li} の同じ長さの列を持つ。
        [修正履歴] 旧形式は車両ごとの dict のリスト。ペイロード削減のため
        列指向に移行した（li はリンク index、名前は link_names で解決）。
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
        """車両速度が非負で、リンク自由流速度の2倍以内"""
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
    円弧表示にならない。逆方向リンクの存在確認。
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
            forward = (tuple(coords[0]), tuple(coords[1]))
            reverse = (tuple(coords[1]), tuple(coords[0]))
            if "r" in f["properties"]["name"]:
                # 逆方向リンクには対応する順方向がある
                assert reverse in edges, f"逆方向リンク {f['properties']['name']} の順方向が見つからない"

    def test_both_directions_have_vehicles(self, result):
        """双方向需要がある場合、両方向にリンクに車両がいる"""
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
    「Node name X already used by another node」のまま 400 で返り、
    どこを直せばいいか分からなかった。SimulationInput で事前検証する。
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


# ============================================================
# 2z. シナリオパッチエンジン（rerun_simulation）
# ============================================================

class TestScenarioModifications:
    """
    _apply_modifications: 大規模ネットワークを LLM に往復させないための
    差分命令エンジン。保存済みシナリオに小さなパッチを適用する。
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
        import asyncio, types, json as _json
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
        # 新シナリオに capacity が反映され、渋滞で旅行時間が悪化している
        new_sc = results_store[new_id]["_scenario"]
        assert new_sc["links"][0]["capacity"] == 0.15
        assert payload["average_travel_time_s"] > base_result["stats"]["average_travel_time_s"]

    def test_rerun_handler_bad_sim_id(self):
        import asyncio, types
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


# ============================================================
# 2a. リンク容量テスト
# ============================================================

class TestLinkCapacity:
    """
    link.capacity（台/s、リンク全体）で容量を明示制御できること。
    UXsim の capacity_out にマップされ、下流端がボトルネックになる。
    """

    def _run(self, capacity):
        # 注意: capacity（capacity_out）はリンク終点が目的地そのものの場合は
        # 作用しない（車両は境界を通らず到着・消滅する）ため、
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
    [修正履歴] signals[].groups が交差点ごとにフィルタされておらず、
    複数の信号交差点があると同じ信号機が交差点の数だけ重複描画された。
    groups は「その交差点に流入する signal_group 付きリンク」のみを含むこと。
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
    [修正履歴] 空セルで float("") エラーが発生した。
    _f() / _i() ヘルパーで対処済み。
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
        [修正履歴] node_id（主キー）と name（表示ラベル、重複可）の両方を持つ
        GMNS 風データで、name を識別子に選んで「ノード名が重複」エラーになった。
        ID 系カラムを優先する。
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
        空セルが含まれる RISU CSV でエラーにならない。
        [修正履歴] float("") で ValueError が発生した。
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
    _get_simulation_data() がグラフ生成に十分なデータを返すことを検証。
    [修正履歴] LLM がチャート生成するにはデータが必要。
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
        JSON サイズが LLM のコンテキストに収まるサイズ。
        間引きが機能していることを確認。
        """
        json_str = json.dumps(sim_data)
        # 100KB 以下であること（LLM に渡せるサイズ）
        assert len(json_str) < 100_000, f"データが大きすぎる: {len(json_str)} bytes"


# ============================================================
# 5. フレームキー正規化テスト（フロントエンド互換性）
# ============================================================

class TestFrameKeyCompatibility:
    """
    [修正履歴] Python の str(round(25.0, 1)) = "25.0" だが
    JavaScript の String(25.0) = "25"。
    フロントエンドで parseFloat 正規化しているため、
    サーバー側のキーが一貫していることを確認。
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
        Python の "25.0" と JS の "25" の不一致を文書化。
        フロントエンドの loadResult() で正規化している:
            framesData[String(parseFloat(k))] = v
        サーバー側は "25.0" 形式を返す。
        """
        has_decimal_key = any("." in k for k in result["frames"].keys())
        assert has_decimal_key, (
            "フレームキーが小数点を含んでいない。"
            "フロントエンドの正規化ロジックとの整合性を確認すること。"
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
        [修正履歴] AI の一人称を RISU に変更した。
        """
        from server import SYSTEM_PROMPT
        assert "RISU" in SYSTEM_PROMPT
        assert "一人称" in SYSTEM_PROMPT or "RISU" in SYSTEM_PROMPT

    def test_system_prompt_chart_instructions(self):
        """
        [修正履歴] チャート生成の指示がシステムプロンプトに含まれる。
        """
        from server import SYSTEM_PROMPT
        assert "chart" in SYSTEM_PROMPT.lower() or "チャート" in SYSTEM_PROMPT or "グラフ" in SYSTEM_PROMPT


# ============================================================
# 7. API エンドポイントテスト（サーバー起動が必要）
# ============================================================

class TestAPIEndpoints:
    """
    サーバーが起動している場合のみ実行。
    pytest tests/ -v -k "api" で選択実行可。
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
    LLM レスポンスからの ```chart ブロック抽出をテスト。
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
        フレーム数がtmaxに対して妥当な範囲にある。
        _run_uxsim はフレームを間引きしない（全ステップを返す）。
        間引きは _get_simulation_data で行われる（最大40点サンプリング）。
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
