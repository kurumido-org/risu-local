"""独立した数値基準と保存則（テスト設計 §6 / §14: N01〜N08，T07 / T12 / T17 / T18 / T20）．

サーバー経路（run_uxsim）と CLI 経路は uxsim_bridge.build_world を共有するので，両者の一致だけでは
共有する変換の誤りを通してしまう．ここでは
  - RISU を使わず UXsim World を直接構築した結果（N08），
  - 物理的な条件（自由流旅行時間，容量境界，保存則）
  - UXsim の需要生成規則を写した純 Python の複製（demand_count）
と照合する．

離散化の許容差（§6.1 / §14.1）はこのファイルの定数に固定する．初期候補を独立ログで確認した
うえで置いた値で，失敗を消す目的で事後的に広げない．
  FREE_FLOW_TOL_S     自由流旅行時間の許容差: 1 リンクあたり 2 ステップ（DELTAT=1 s）× 2 リンク = 4 s．
                      実測（uxsim 1.14.0b7, cpp）: 誤差 0.0 s
  CAPACITY_TOL_PLATOONS  容量境界の累積差: 2 プラトン．実測: 超過 0
  SIGNAL_TOL_STEPS    現示境界の許容: ±1 ステップ．実測: 0（境界の不一致なし）
"""

import copy
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import risu.simulation  # noqa: E402
from risu.aggregate import get_simulation_data  # noqa: E402
from risu.results import results_store  # noqa: E402
from risu.schema import SimulationInput  # noqa: E402
from risu.simulation import run_uxsim  # noqa: E402
from uxsim_bridge import build_world, scenario_from_dict  # noqa: E402

from fixtures import (  # noqa: E402
    COMMON_PARAMS, F1, F1_CLOSED, F1_FREE_FLOW_TRAVEL_S, F1_NO_JAM, F1_TIGHT, F2, F3, F3_SIGNAL,
    IsolatedStore, changed_paths, demand_vehicles, f1, frozen_copy, store_result,
)

FREE_FLOW_TOL_S = 4.0
CAPACITY_TOL_PLATOONS = 2
SIGNAL_TOL_STEPS = 1


# ──────────────────────────────────────────────
# 独立した基準
# ──────────────────────────────────────────────
def demand_count(flow: float, t_start: float, t_end: float, deltat: float, deltan: int) -> int:
    """UXsim の World.adddemand と同じ規則で生成されるプラトン数（純 Python の複製）．

    flow×deltat をステップごとに累積し，deltan 以上になるたびに 1 台（プラトン）を出す．
    浮動小数の累積誤差で，設定上の台数 flow×(t_end−t_start) より 1 少なくなることがある
    （F1: 0.6 × 600 = 360 だが生成は 359）．
    """
    f = 0.0
    n = 0
    for _ in range(int(t_start / deltat), int(t_end / deltat)):
        f += flow * deltat
        while f >= deltan:
            n += 1
            f -= deltan
    return n


def expected_vehicles(scenario: dict) -> int:
    deltan = int(scenario.get("deltan") or 5)
    deltat = deltan * float(scenario.get("reaction_time") or 1.0)
    return sum(demand_count(float(d["flow"]), float(d["t_start"]), float(d["t_end"]), deltat, deltan) * deltan
               for d in scenario["demands"])


def link_exit_times(W, link_name: str) -> list[float]:
    """各車両が link_name を離れた時刻（次のリンク上で最初に記録された時刻）．UXsim の車両ログから．"""
    out = []
    for veh in W.VEHICLES.values():
        on = False
        for t, s, lk in zip(veh.log_t, veh.log_state, veh.log_link):
            name = getattr(lk, "name", None)
            if s == "run" and name == link_name:
                on = True
                continue
            if on and name != link_name:
                out.append(float(t))
                break
    return sorted(out)


def run_world(scenario: dict):
    W = build_world(scenario_from_dict(copy.deepcopy(scenario)), cpp=True, disable_basic_analysis=True)
    W.exec_simulation()
    return W


def independent_world(scenario: dict):
    """RISU のブリッジを使わず，UXsim の API を直接呼んで同じ条件の World を作る（N08）．

    build_world と同じ対応を「別のコード」で書き下す: capacity → capacity_out，signal → Node(signal=)，
    signal_group 省略 → 全現示．analyzer は有効（save_mode=1）にして UXsim 自身の集計を基準にする．
    """
    from uxsim import World
    W = World(name="independent", tmax=scenario["tmax"], deltan=scenario["deltan"],
              reaction_time=scenario.get("reaction_time") or 1.0,
              random_seed=scenario.get("random_seed"), print_mode=0, save_mode=1, show_mode=0, cpp=True)
    nodes = {}
    n_phases = {n["name"]: len(n["signal"]) for n in scenario["nodes"] if n.get("signal")}
    for n in scenario["nodes"]:
        kw = {}
        if n.get("signal"):
            kw["signal"] = list(n["signal"])
        if n.get("flow_capacity") is not None:
            kw["flow_capacity"] = n["flow_capacity"]
        nodes[n["name"]] = W.addNode(n["name"], n["x"], n["y"], **kw)
    for lk in scenario["links"]:
        kw = {}
        if lk.get("capacity") is not None:
            kw["capacity_out"] = lk["capacity"]
        if lk.get("signal_group") is not None:
            kw["signal_group"] = lk["signal_group"]
        elif lk["end"] in n_phases:
            kw["signal_group"] = list(range(n_phases[lk["end"]]))
        W.addLink(lk["name"], nodes[lk["start"]], nodes[lk["end"]], length=lk["length"],
                  free_flow_speed=lk.get("free_flow_speed", 20), jam_density=lk.get("jam_density", 0.2),
                  number_of_lanes=lk.get("number_of_lanes", 1), **kw)
    for d in scenario["demands"]:
        W.adddemand(nodes[d["orig"]], nodes[d["dest"]], d["t_start"], d["t_end"], d["flow"])
    W.exec_simulation()
    return W


# ──────────────────────────────────────────────
# N01〜N08
# ──────────────────────────────────────────────
class TestFreeFlow:
    """N01: 小需要・単一路・容量非拘束 → 全到着，旅行時間は length/ffs の和（200 秒）．"""

    def test_travel_time_and_counts(self):
        res = run_uxsim(SimulationInput(**F1_NO_JAM))
        st = res["stats"]
        assert st["total_trips"] == expected_vehicles(F1_NO_JAM) == 30
        assert st["completed_trips"] == 30
        assert abs(st["average_travel_time_s"] - F1_FREE_FLOW_TRAVEL_S) <= FREE_FLOW_TOL_S
        # 個々の車両でも確認（独立ログ）
        W = run_world(F1_NO_JAM)
        tts = [float(v.travel_time) for v in W.VEHICLES.values()]
        assert len(tts) == 30 and max(abs(t - F1_FREE_FLOW_TRAVEL_S) for t in tts) <= FREE_FLOW_TOL_S
        ts = res["trip_series"]
        assert ts["entered"][-1] == 30 and ts["completed"][-1] == 30


class TestInternalBottleneck:
    """N02: AB 容量 0.25 台/秒，需要 0.6 台/秒 × 600 秒．AB 下流端の通過が容量を超えない．"""

    def test_outflow_bounded_by_capacity(self):
        W = run_world(F1_TIGHT)
        exits = np.asarray(link_exit_times(W, "AB"))
        n = expected_vehicles(F1_TIGHT)
        assert exits.size == n == 359     # 浮動小数の累積で 360 ではなく 359（demand_count 参照）
        cap = F1_TIGHT["links"][0]["capacity"]
        for window in (60.0, 120.0, 300.0):
            starts = np.arange(exits[0], exits[-1], window)
            counts = [int(((exits >= a) & (exits < a + window)).sum()) for a in starts]
            assert max(counts) <= cap * window + CAPACITY_TOL_PLATOONS, (window, max(counts))
        # 流出は容量いっぱいで続く（渋滞中は 60 秒あたり 15 台前後）
        mid = [int(((exits >= a) & (exits < a + 60)).sum()) for a in np.arange(exits[0] + 60, exits[-1] - 120, 60)]
        assert min(mid) >= cap * 60 - CAPACITY_TOL_PLATOONS
        # 下流（BC）は閉塞せず，原点へのスピルバックも無い（全車両が需要時間内に流入）
        res = run_uxsim(SimulationInput(**F1_TIGHT))
        ts = res["trip_series"]
        i = max(k for k, t in enumerate(ts["t"]) if t <= 600 + 2 * res["frame_interval_s"])
        assert ts["entered"][i] == n
        assert res["stats"]["completed_trips"] == n
        # 最終到着の時刻軸（BC の走行時間を含む）と AB 下流端の通過を混同しない
        assert ts["completed"][max(k for k, t in enumerate(ts["t"]) if t <= exits[-1])] < n


class TestClosure:
    """N03 / T20: AB 容量 0 → 到着 0，平均旅行時間は未定義（None）．残った車両を消さない．"""

    def test_no_arrivals_and_undefined_average(self):
        with IsolatedStore():
            res = store_result("n03_closed", F1_CLOSED)
            st = res["stats"]
            assert st["completed_trips"] == 0 and st["average_travel_time_s"] is None
            assert st["total_trips"] == expected_vehicles(F1_CLOSED) > 0
            assert res["trip_series"]["entered"][-1] > 0 and res["trip_series"]["completed"][-1] == 0
            assert res["vehicle_counts"][-1] > 0                    # 終了時刻でも AB 上に残っている
            sd = get_simulation_data("n03_closed")
            assert sd["stats"]["average_travel_time_s"] is None      # 0 秒にしない
            assert sd["network_completed_count"][-1] == 0 and max(sd["network_vehicle_count"]) > 0
            assert isinstance(res["vehicle_counts"][-1], int) and sum(res["vehicle_counts"]) > 0


class TestDemandGaps:
    """N04 / U06: 需要なしは総台数 0．2 バーストでは空白時刻に走行車両が無く，累積は保持される．"""

    def test_zero_demand(self):
        res = run_uxsim(SimulationInput(**{**F1, "demands": []}))
        assert res["stats"]["total_trips"] == 0 and res["stats"]["average_travel_time_s"] is None
        assert res["frame_times"] == [] and res["trip_series"]["entered"] == [0, 0]

    def test_two_bursts(self):
        sc = f1(capacity=None, flow=0.1, t_start=0, t_end=30)
        sc["demands"].append({"orig": "A", "dest": "C", "t_start": 600, "t_end": 630, "flow": 0.1})
        res = run_uxsim(SimulationInput(**sc))
        n = expected_vehicles(sc)
        assert res["stats"]["total_trips"] == n == 4       # 0.1×30 は浮動小数の累積で 3 ではなく 2 ずつ
        assert res["stats"]["completed_trips"] == n
        gap = res["frame_interval_s"] * 1.5
        for t_probe in (500.0, 1000.0):
            assert not any(abs(t - t_probe) <= gap for t in res["frame_times"]), t_probe
            # 実イベントの累積: 空白時刻では 流入 = 到着（走行中 0）
            ts = res["trip_series"]
            i = max(k for k, t in enumerate(ts["t"]) if t <= t_probe)
            assert ts["entered"][i] == ts["completed"][i], (t_probe, ts["t"][i], ts["entered"][i], ts["completed"][i])
        ts = res["trip_series"]
        assert ts["completed"][max(k for k, t in enumerate(ts["t"]) if t <= 500)] == 2
        assert ts["completed"][-1] == 4


class TestPlatoonAggregation:
    """N05 / T17 / T18: deltan 1 と 5，描画間引きの不変性，台数重み平均速度．"""

    def test_deltan_scaling_and_drawing_sample_invariance(self, monkeypatch):
        for deltan in (1, 5):
            sc = f1(deltan=deltan, reaction_time=1.0)
            base = run_uxsim(SimulationInput(**sc))
            assert base["stats"]["total_trips"] == expected_vehicles(sc)
            assert abs(base["stats"]["total_trips"] - demand_vehicles(sc)) <= deltan   # 設定上は 360 台
            assert all(c % deltan == 0 for c in base["vehicle_counts"])
            total_points = sum(int(len(c["ids"])) for c in base["frames"].values())
            monkeypatch.setattr(risu.simulation, "MAX_FRAME_POINTS", max(1, total_points // 4))
            sampled = run_uxsim(SimulationInput(**sc))
            monkeypatch.setattr(risu.simulation, "MAX_FRAME_POINTS", 3_000_000)
            assert sampled["vehicle_sample_step"] >= 2
            assert sum(int(len(c["ids"])) for c in sampled["frames"].values()) < total_points
            # 描画の間引きだけを変えても，交通統計と分析系列は同一（壁時計の計測値は比較から外す）
            def strip(st):
                return {k: v for k, v in st.items() if not k.endswith("_s") or k == "average_travel_time_s"}

            def tl(r):
                return {f["properties"]["name"]: f["properties"]["timeline"] for f in r["geojson"]["features"]}
            assert strip(sampled["stats"]) == strip(base["stats"])
            for k in ("vehicle_counts", "frame_avg_speed", "speed_histogram", "trip_series", "frame_times"):
                assert sampled[k] == base[k], (deltan, k)
            assert tl(sampled) == tl(base)

    def test_average_speed_is_vehicle_weighted(self):
        """T18: 10 m/秒 × 1 プラトン + 30 m/秒 × 3 プラトン → 25 m/秒．deltan=5 なら 20 台．
        リンク単純平均（10 と 30 の平均 20 m/秒）を返さない．"""
        with IsolatedStore():
            sc = {**F1, "deltan": 5,
                  "links": [{"name": "slow", "start": "A", "end": "B", "length": 3000, "free_flow_speed": 10},
                            {"name": "fast", "start": "B", "end": "C", "length": 1000, "free_flow_speed": 30}]}
            synthetic = {
                "_scenario": SimulationInput(**sc).model_dump(),
                "geojson": {"type": "FeatureCollection", "features": [
                    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [3000, 0]]},
                     "properties": {"name": "slow", "length": 3000, "free_flow_speed": 10, "number_of_lanes": 1,
                                    "timeline": [{"t": 100.0, "speed": 10.0}]}},
                    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[3000, 0], [4000, 0]]},
                     "properties": {"name": "fast", "length": 1000, "free_flow_speed": 30, "number_of_lanes": 1,
                                    "timeline": [{"t": 100.0, "speed": 30.0}]}}]},
                "frames": {"100.0": {"ids": np.array([0, 1, 2, 3], dtype=np.int32), "xs": np.zeros(4), "ys": np.zeros(4),
                                     "vs": np.array([10.0, 30.0, 30.0, 30.0]), "alphas": np.zeros(4),
                                     "li": np.array([0, 1, 1, 1], dtype=np.int32)}},
                "frame_times": [100.0], "stats": {"total_trips": 20, "completed_trips": 0, "average_travel_time_s": None},
                "tmax": 2400, "signals": [], "link_names": ["slow", "fast"], "frame_format": "columnar_v2",
                "vehicle_sample_step": 1, "frame_interval_s": 5.0,
                # 古い結果相当: vehicle_counts / frame_avg_speed なし → フレームから近似する経路
            }
            results_store["n05_weighted"] = synthetic
            sd = get_simulation_data("n05_weighted")
            assert sd["network_avg_speed"] == [25.0]
            assert sd["network_vehicle_count"] == [20]
            link_mean = sum(v[0] for v in sd["link_speeds"].values()) / len(sd["link_speeds"])
            assert link_mean == 20.0 and sd["network_avg_speed"][0] != link_mean
            # 実行結果でも frame_avg_speed はフレーム内車両速度の台数重み平均
            res = run_uxsim(SimulationInput(**F1_TIGHT))
            for t, v in zip(res["frame_times"], res["frame_avg_speed"]):
                vs = np.asarray(res["frames"][str(t)]["vs"])
                assert abs(v - vs.mean()) < 0.02


class TestSignalIntersection:
    """N06: 東西 60 秒 / 南北 30 秒の 2 現示．停止線通過イベントが実現示ログと整合する．"""

    def test_exits_only_during_green(self):
        W = run_world(F3)
        node = next(n for n in W.NODES if n.name == "I")
        phase_log = list(node.signal_log)
        deltat = float(W.DELTAT)
        assert len(phase_log) == int(F3["tmax"] / deltat)
        # 公称サイクル（t % 90）ではなく実ログで判定する
        for lk, green in (("W_I", 0), ("E_I", 0), ("S_I", 1), ("N_I", 1)):
            exits = link_exit_times(W, lk)
            if lk in ("E_I", "N_I"):
                assert exits == []                          # 需要が無い流入
                continue
            assert len(exits) == expected_vehicles({**F3, "demands": [F3["demands"][0]]})
            for t in exits:
                i = int(t / deltat)
                window = phase_log[max(0, i - SIGNAL_TOL_STEPS): i + SIGNAL_TOL_STEPS + 1]
                assert green in window, (lk, t, phase_log[i])
        # 赤の最中（境界から離れた時刻）には通過が無い
        exits_w = link_exit_times(W, "W_I")
        red_mid = [t for t in exits_w if phase_log[int(t / deltat)] == 1
                   and all(phase_log[j] == 1 for j in range(int(t / deltat) - 2, int(t / deltat) + 3))]
        assert red_mid == []
        # 結果の signals メタも同じログを載せる（画面はこれで現示を描く）
        res = run_uxsim(SimulationInput(**F3))
        sig = res["signals"][0]
        assert sig["phases"] == F3_SIGNAL and sig["phase_log"] == phase_log and sig["deltat"] == deltat
        assert sig["groups"] == {"W_I": [0], "E_I": [0], "S_I": [1], "N_I": [1]}


DIAMOND = {
    "name": "diamond", **COMMON_PARAMS,
    "nodes": [{"name": "O", "x": 0, "y": 0}, {"name": "P", "x": 1000, "y": 500},
              {"name": "Q", "x": 1000, "y": -500}, {"name": "D", "x": 2000, "y": 0}],
    "links": [{"name": "OP", "start": "O", "end": "P", "length": 1118},
              {"name": "OQ", "start": "O", "end": "Q", "length": 1118},
              {"name": "PD", "start": "P", "end": "D", "length": 1118},
              {"name": "QD", "start": "Q", "end": "D", "length": 1118}],
    "demands": [{"orig": "O", "dest": "D", "t_start": 0, "t_end": 300, "flow": 0.6}],
}


class TestReproducibility:
    """N07: 経路選択を含む小さなダイヤモンド網．同じ seed・版・backend なら統計と系列が一致する．"""

    def _series(self, res):
        stats = {k: v for k, v in res["stats"].items() if k in ("total_trips", "completed_trips", "average_travel_time_s")}
        return {"stats": stats, "counts": res["vehicle_counts"], "speed": res["frame_avg_speed"],   # 壁時計は比べない
                "trips": res["trip_series"],
                "timeline": {f["properties"]["name"]: f["properties"]["timeline"] for f in res["geojson"]["features"]}}

    def test_same_seed_reproduces(self):
        a = run_uxsim(SimulationInput(**DIAMOND))
        b = run_uxsim(SimulationInput(**DIAMOND))
        assert self._series(a) == self._series(b)
        assert a["_runtime"]["backend"] == b["_runtime"]["backend"]
        assert a["stats"]["total_trips"] == expected_vehicles(DIAMOND) == a["stats"]["completed_trips"]
        # 別 seed との一致は要求しない（DUE への収束や最適性の試験ではない）．台数は seed に依らない
        c = run_uxsim(SimulationInput(**{**DIAMOND, "random_seed": 43}))
        assert c["stats"]["total_trips"] == a["stats"]["total_trips"]


class TestIndependentConstruction:
    """N08: RISU を使わず UXsim で直接構築した World と，主要設定・交通統計が一致する．"""

    @pytest.mark.parametrize("scenario", [F1, F2, F3], ids=["F1", "F2", "F3"])
    def test_stats_match_uxsim_analyzer(self, scenario):
        W = independent_world(scenario)
        an = W.analyzer
        res = run_uxsim(SimulationInput(**scenario))
        st = res["stats"]
        assert st["total_trips"] == int(an.trip_all)
        assert st["completed_trips"] == int(an.trip_completed)
        assert st["average_travel_time_s"] == pytest.approx(float(an.average_travel_time), abs=0.05)

    def test_bridge_maps_parameters_like_the_direct_construction(self):
        W = run_world(F3)
        Wi = independent_world(F3)
        for w in (W, Wi):
            assert float(w.REACTION_TIME) == 1.0 and int(w.DELTAN) == 1 and int(w.TMAX) == 2400
            assert w.random_seed == 42
            node = next(n for n in w.NODES if n.name == "I")
            assert [float(p) for p in node.signal] == F3_SIGNAL
            links = {lk.name: lk for lk in w.LINKS}
            for name, g in (("W_I", [0]), ("E_I", [0]), ("S_I", [1]), ("N_I", [1])):
                sg = links[name].signal_group
                assert (list(sg) if isinstance(sg, (list, tuple)) else [sg]) == g, (name, sg)
        # signal_group 省略 = 全現示で青（終端が信号ノードの進入リンクだけに意味がある）
        sc = copy.deepcopy(F3); sc["links"][0].pop("signal_group")
        Wb = run_world(sc)
        assert sorted({lk.name: lk for lk in Wb.LINKS}["W_I"].signal_group) == [0, 1]
        W1, W1i = run_world(F1), independent_world(F1)
        assert {lk.name: lk.capacity_out for lk in W1.LINKS}["AB"] == 0.5
        assert {lk.name: lk.capacity_out for lk in W1i.LINKS}["AB"] == 0.5


# ──────────────────────────────────────────────
# 差分と不変性（T07）
# ──────────────────────────────────────────────
class TestCapacityOnlyChange:
    """T07: F1 で AB 容量のみ 0.25 に変更．正規化後の差分集合は AB.capacity の 1 項目だけ，基準は不変．"""

    def test_single_field_diff_and_base_frozen(self):
        import asyncio
        import types

        from risu.tools import handle_rerun_simulation
        with IsolatedStore():
            store_result("t07_base", F1)
            before = frozen_copy("t07_base")
            content, new_id, err = asyncio.run(handle_rerun_simulation(
                {"base_sim_id": "t07_base", "modifications": [
                    {"action": "update_links", "names": ["AB"], "set": {"capacity": 0.25}}]},
                types.SimpleNamespace(messages=[])))
            assert not err, content
            new = results_store[new_id]
            assert changed_paths(before["_scenario"], new["_scenario"]) == {"links.AB.capacity": (0.5, 0.25)}
            assert frozen_copy("t07_base") == before
            # 単一路・同需要・全到着の条件なので，容量を下げれば平均旅行時間は伸びる
            assert new["stats"]["average_travel_time_s"] > before["stats"]["average_travel_time_s"]
            assert new["stats"]["total_trips"] == before["stats"]["total_trips"]
            # 検証器の確認（§16.4）: BC を変えた結果は「AB だけ」の判定に落ちる
            wrong = copy.deepcopy(new["_scenario"]); wrong["links"][0]["capacity"] = 0.5; wrong["links"][1]["capacity"] = 0.25
            assert set(changed_paths(before["_scenario"], wrong)) != {"links.AB.capacity"}


class TestUnitConversions:
    """§6.1 単位: 1,800 台/時 → 0.5 台/秒，72 km/時 → 20 m/秒，1 km → 1,000 m（LLM と取込の共通契約）．"""

    def test_conversions(self):
        assert 1800 / 3600 == 0.5 and 72 / 3.6 == 20.0 and 1 * 1000 == 1000
        assert 900 / 3600 == 0.25 and 2160 / 3600 == 0.6 and 36 / 3.6 == 10.0
