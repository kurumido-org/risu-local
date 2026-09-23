"""LLM 向け集計（risu.aggregate）: 台数・平均速度・累積の定義．"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.aggregate import get_simulation_data  # noqa: E402
from risu.results import results_store  # noqa: E402
from risu.schema import SimulationInput  # noqa: E402
from risu.simulation import run_uxsim  # noqa: E402

from helpers import (  # noqa: E402
    BOTTLENECK_SCENARIO,
)

# ============================================================
# 4. シミュレーションデータ集計テスト
# ============================================================

class TestSimulationDataAggregation:
    """
    get_simulation_data() がグラフ生成に十分なデータを返すことを検証．
    [修正履歴] LLM がチャート生成するにはデータが必要．
    """

    @pytest.fixture(scope="class")
    def sim_data(self):
        result = run_uxsim(BOTTLENECK_SCENARIO)
        sid = "test_aggregation"
        results_store[sid] = result
        return get_simulation_data(sid)

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
        assert get_simulation_data("nonexistent_id_12345") is None

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
        res = run_uxsim(sc)
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
            d = get_simulation_data("test_aggregation_old")
            t0 = d["time_labels"][0]
            f0 = old["frames"][str(float(t0))]
            assert d["network_vehicle_count"][0] == len(f0["ids"]) * 5 * 2
        finally:
            results_store.pop("test_aggregation_old", None)
