"""公開可能な固定シナリオ F1〜F6 と，期待条件を判定する共通ヘルパー（テスト設計 §3 / §15.1）．

ここにあるものは全て合成データ．OSM の取得結果や非公開データ（test_data/ 等）には依存しない．
数値はテスト用の提案値で，現地の交通条件を表すものではない．

  F1  直列ボトルネック A→B→C（AB が容量制約，下流に BC）     単位・容量・差分・数値基準
  F2  双方向道路 AB / BA                                      方向の取り違え
  F3  信号交差点 I（東西 / 南北の 2 現示）                       信号の設定と表示
  F4  3×3 双方向格子（規模評価用の nx は呼び出し側で指定）        パラメトリック生成
  F5  OSM 相当の合成道路網（多点形状 + 自動生成需要の出所メタ）   通信なしの取込・出所・再実行
  F6  説明・chart 用の合成結果（全完了 / 一部完了，異なる時間軸）  数値根拠・時間軸の整列

ヘルパー:
  normalized / scenario_diff / changed_paths  正規化シナリオの差分（名前キー，需要は多重集合）
  demand_vehicles / demand_multiset           需要台数は flow × 継続時間で評価する
  store_result / synthetic_result             results_store への投入
  IsolatedStore                                テスト間で results_store を汚さない
"""
from __future__ import annotations

import copy
import os
import sys
from collections import Counter
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from risu.results import results_store, store_sim  # noqa: E402
from risu.schema import SimulationInput  # noqa: E402
from risu.simulation import run_uxsim  # noqa: E402

# F2〜F4 の全体パラメータ（§15.1: 別指定がなければこれ）
COMMON_PARAMS: dict[str, Any] = {"tmax": 2400, "deltan": 1, "reaction_time": 1.0, "random_seed": 42}


# ──────────────────────────────────────────────
# F1: 直列ボトルネック
# ──────────────────────────────────────────────
# AB 容量 0.5 台/秒 = 1,800 台/時．需要 0.6 台/秒 = 2,160 台/時．総需要 0.6 × 600 = 360 台．
# 自由流旅行時間 = 3000/20 + 1000/20 = 200 秒．
# AB の下流に BC を置くのは，目的地に直接入る最終リンクの流出容量がボトルネックとして
# 作用しないため（SYSTEM_PROMPT の注意と同じ）．
F1: dict[str, Any] = {
    "name": "corridor_reference",
    "tmax": 2400, "deltan": 1, "reaction_time": 1.0, "random_seed": 42,
    "nodes": [
        {"name": "A", "x": 0, "y": 0},
        {"name": "B", "x": 3000, "y": 0},
        {"name": "C", "x": 4000, "y": 0},
    ],
    "links": [
        {"name": "AB", "start": "A", "end": "B", "length": 3000, "free_flow_speed": 20, "capacity": 0.5},
        {"name": "BC", "start": "B", "end": "C", "length": 1000, "free_flow_speed": 20},
    ],
    "demands": [
        {"orig": "A", "dest": "C", "t_start": 0, "t_end": 600, "flow": 0.6},
    ],
}
F1_FREE_FLOW_TRAVEL_S = 200.0
F1_TOTAL_VEHICLES = 360


def f1(*, capacity: float | None = 0.5, flow: float = 0.6, t_start: float = 0, t_end: float = 600,
       **params) -> dict:
    """F1 の変種．capacity=None で AB の明示容量を外す．params で tmax 等を上書き．"""
    sc = copy.deepcopy(F1)
    if capacity is None:
        sc["links"][0].pop("capacity", None)
    else:
        sc["links"][0]["capacity"] = capacity
    sc["demands"][0].update({"t_start": t_start, "t_end": t_end, "flow": flow})
    sc.update(params)
    return sc


F1_NO_JAM = f1(flow=0.05)                 # 無渋滞（N01: 総需要 30 台，自由流）
F1_TIGHT = f1(capacity=0.25)              # 容量 0.25 台/秒 = 900 台/時（S1 相当）
F1_SHIFT = f1(t_start=600, t_end=1200)    # 需要区間だけ 600〜1200 秒（L12 / T10 用）
F1_CLOSED = f1(capacity=0)                # 閉鎖（N03）


# ──────────────────────────────────────────────
# F2: 双方向道路
# ──────────────────────────────────────────────
F2: dict[str, Any] = {
    "name": "bidirectional_reference", **COMMON_PARAMS,
    "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}],
    "links": [
        {"name": "AB", "start": "A", "end": "B", "length": 1000, "free_flow_speed": 20},
        {"name": "BA", "start": "B", "end": "A", "length": 1000, "free_flow_speed": 20},
    ],
    "demands": [
        {"orig": "A", "dest": "B", "t_start": 0, "t_end": 300, "flow": 0.1},
        {"orig": "B", "dest": "A", "t_start": 0, "t_end": 300, "flow": 0.1},
    ],
}

# ──────────────────────────────────────────────
# F3: 信号交差点（東西 = 現示 0，南北 = 現示 1）
# ──────────────────────────────────────────────
F3_SIGNAL = [60.0, 30.0]
F3: dict[str, Any] = {
    "name": "signal_reference", **COMMON_PARAMS,
    "nodes": [
        {"name": "I", "x": 0, "y": 0, "signal": F3_SIGNAL},
        {"name": "W", "x": -1000, "y": 0}, {"name": "E", "x": 1000, "y": 0},
        {"name": "S", "x": 0, "y": -1000}, {"name": "N", "x": 0, "y": 1000},
    ],
    "links": [
        {"name": "W_I", "start": "W", "end": "I", "length": 1000, "free_flow_speed": 20, "signal_group": 0},
        {"name": "E_I", "start": "E", "end": "I", "length": 1000, "free_flow_speed": 20, "signal_group": 0},
        {"name": "S_I", "start": "S", "end": "I", "length": 1000, "free_flow_speed": 20, "signal_group": 1},
        {"name": "N_I", "start": "N", "end": "I", "length": 1000, "free_flow_speed": 20, "signal_group": 1},
        {"name": "I_W", "start": "I", "end": "W", "length": 1000, "free_flow_speed": 20},
        {"name": "I_E", "start": "I", "end": "E", "length": 1000, "free_flow_speed": 20},
        {"name": "I_S", "start": "I", "end": "S", "length": 1000, "free_flow_speed": 20},
        {"name": "I_N", "start": "I", "end": "N", "length": 1000, "free_flow_speed": 20},
    ],
    "demands": [
        {"orig": "W", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.1},
        {"orig": "S", "dest": "N", "t_start": 0, "t_end": 300, "flow": 0.1},
    ],
}


def f3_without_signal() -> dict:
    """F3 の信号なし版（L03「信号を付けて」の事前状態）．"""
    sc = copy.deepcopy(F3)
    sc["nodes"][0].pop("signal", None)
    for lk in sc["links"]:
        lk.pop("signal_group", None)
    return sc


# ──────────────────────────────────────────────
# F4: 格子（3×3 は正しさ確認用．規模評価は nx を変える）
# ──────────────────────────────────────────────
def f4(nx: int = 3, spacing: float = 500, demands: list[dict] | None = None, **params) -> dict:
    """双方向格子．ノード名 n{i}_{j}，リンク名 n0_0-n1_0（risu.scenario_ops の grid テンプレートと同じ）．"""
    from risu.scenario_ops import expand_run_simulation_args
    if demands is None:
        demands = [
            {"orig": "n0_0", "dest": f"n{nx-1}_{nx-1}", "t_start": 0, "t_end": 300, "flow": 0.1},
            {"orig": "n1_0", "dest": f"n1_{nx-1}", "t_start": 0, "t_end": 300, "flow": 0.1},
        ]
    sc, _info = expand_run_simulation_args({
        "name": f"grid_{nx}x{nx}", **COMMON_PARAMS, **params,
        "grid": {"nx": nx, "ny": nx, "spacing": spacing},
        "demands": demands,
    })
    return sc


F4_SMALL = f4(3)   # 9 ノード / 24 有向リンク / 2 需要


# ──────────────────────────────────────────────
# F5: OSM 相当の合成道路網（通信なし）
# ──────────────────────────────────────────────
# 多点の道路形状（link_geometries）と「OSM 取込時の自動生成需要（仮定値）」の出所メタを持つ．
# 地理的な再現性の評価には使わない．
F5: dict[str, Any] = {
    "name": "osm_like_synthetic", **COMMON_PARAMS,
    "nodes": [
        {"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 3000, "y": 400},
        {"name": "C", "x": 4000, "y": 0}, {"name": "D", "x": 2000, "y": 1500},
    ],
    "links": [
        {"name": "AB", "start": "A", "end": "B", "length": 3100, "free_flow_speed": 16.67, "capacity": 0.5},
        {"name": "BA", "start": "B", "end": "A", "length": 3100, "free_flow_speed": 16.67},
        {"name": "BC", "start": "B", "end": "C", "length": 1100, "free_flow_speed": 16.67},
        {"name": "CB", "start": "C", "end": "B", "length": 1100, "free_flow_speed": 16.67},
        {"name": "BD", "start": "B", "end": "D", "length": 1500, "free_flow_speed": 13.89},
        {"name": "DB", "start": "D", "end": "B", "length": 1500, "free_flow_speed": 13.89},
    ],
    "demands": [
        {"orig": "A", "dest": "C", "t_start": 0, "t_end": 600, "flow": 0.2},
        {"orig": "C", "dest": "A", "t_start": 0, "t_end": 600, "flow": 0.2},
        {"orig": "D", "dest": "A", "t_start": 0, "t_end": 600, "flow": 0.1},
    ],
}
F5_GEOMETRIES: dict[str, list[list[float]]] = {
    "AB": [[0, 0], [1000, 300], [2000, 500], [3000, 400]],
    "BA": [[3000, 400], [2000, 500], [1000, 300], [0, 0]],
    "BD": [[3000, 400], [2600, 900], [2000, 1500]],
    "DB": [[2000, 1500], [2600, 900], [3000, 400]],
}
F5_DEMAND_NOTE = ("OSM 取込時の自動生成（仮定値）: 周縁とみなした 3 ノードの全ペア 3 組に，"
                  "各 0.1〜0.2 台/秒を 0〜600 秒で与えた（合計 0.50 台/秒）．"
                  "実測の交通量ではないので，現地の朝ピーク等の再現精度は保証されない．")
F5_META: dict[str, Any] = {
    "type": "osm", "via": "chat", "place": "合成テスト地点（実在しない）", "distance_m": 500,
    "demand": {"method": "osm_boundary_pairs", "boundary_nodes": ["A", "C", "D"], "pairs": 3,
               "flow_per_pair": [0.1, 0.2], "t_start": 0, "t_end": 600, "total_flow": 0.5,
               "note": F5_DEMAND_NOTE},
}


def store_f5(sim_id: str) -> dict:
    """F5 を実行して OSM 取込相当のメタ（出所・道路形状）付きで保存する．"""
    from risu.simulation import apply_link_geometries
    res = run_uxsim(SimulationInput(**F5))
    apply_link_geometries(res, F5_GEOMETRIES)
    store_sim(sim_id, res, copy.deepcopy(F5_META))
    FIXTURE_IDS.add(sim_id)
    return res


# ──────────────────────────────────────────────
# F6: 説明・chart 用の合成結果（計算精度の試験には使わない）
# ──────────────────────────────────────────────
def synthetic_result(*, scenario: dict, times: list[float], speeds: list[float], counts: list[int],
                     stats: dict, link_speeds: dict[str, list[float]] | None = None,
                     entered: list[int] | None = None, completed: list[int] | None = None) -> dict:
    """get_simulation_data / compare_simulations / チャート解決が読める最小の結果 dict を作る．

    times/speeds/counts は同じ長さ．stats は total_trips / completed_trips / average_travel_time_s．
    """
    import numpy as np
    assert len(times) == len(speeds) == len(counts)
    deltan = int(scenario.get("deltan") or 1)
    frames = {}
    for t, v, c in zip(times, speeds, counts):
        n = max(0, int(c) // deltan)
        frames[str(float(t))] = {
            "ids": np.arange(n, dtype=np.int32), "xs": np.zeros(n), "ys": np.zeros(n),
            "vs": np.full(n, float(v)), "alphas": np.zeros(n), "li": np.zeros(n, dtype=np.int32),
        }
    features = []
    for lk in scenario["links"]:
        tl = link_speeds.get(lk["name"]) if link_speeds else None
        features.append({"type": "Feature",
                         "geometry": {"type": "LineString", "coordinates": [[0, 0], [lk["length"], 0]]},
                         "properties": {"name": lk["name"], "length": lk["length"],
                                        "free_flow_speed": lk.get("free_flow_speed", 20), "number_of_lanes": 1,
                                        "timeline": [{"t": t, "speed": s} for t, s in zip(times, tl)] if tl else []}})
    tmax = int(scenario.get("tmax") or 2400)
    axis = sorted({0.0, float(tmax), *[float(t) for t in times]})
    total = int(stats["total_trips"]); done = int(stats["completed_trips"])
    if entered is None:
        entered = [0] + [total] * (len(axis) - 1)
    if completed is None:
        completed = [0] * (len(axis) - 1) + [done]
    return {
        "_scenario": SimulationInput(**scenario).model_dump(),
        "geojson": {"type": "FeatureCollection", "features": features},
        "frames": frames, "frame_times": [float(t) for t in times],
        "stats": {"total_trips": total, "completed_trips": done,
                  "average_travel_time_s": stats.get("average_travel_time_s"),
                  "simulation_time_s": 0.0, "post_processing_s": 0.0},
        "tmax": tmax, "signals": [], "link_names": [lk["name"] for lk in scenario["links"]],
        "frame_format": "columnar_v2", "vehicle_sample_step": 1, "frame_interval_s": 1.0,
        "vehicle_counts": [int(c) for c in counts], "frame_avg_speed": [float(v) for v in speeds],
        "speed_histogram": {"labels": ["0-10", "10-20", "20-30"], "counts": [1, 1, 1]},
        "trip_series": {"t": axis, "entered": list(entered), "completed": list(completed)},
        "_runtime": {"uxsim_version": "synthetic", "backend": "synthetic", "fast_path": True},
    }


# §15.1 F6-stat: 全完了（総 360・完了 360・平均 400 秒）と一部完了（総 360・完了 180・平均 250 秒）．
# 時間軸は違える（COMPLETE は 0..1200 を 7 点，PARTIAL は 0..2400 を 5 点）．
F6_COMPLETE_TIMES = [0, 200, 400, 600, 800, 1000, 1200]
F6_PARTIAL_TIMES = [0, 600, 1200, 1800, 2400]


def f6_complete() -> dict:
    return synthetic_result(
        scenario=F1, times=F6_COMPLETE_TIMES, speeds=[20, 18, 15, 12, 14, 17, 19],
        counts=[0, 60, 120, 150, 120, 60, 0], link_speeds={"AB": [20, 17, 13, 9, 11, 15, 19], "BC": [20] * 7},
        stats={"total_trips": 360, "completed_trips": 360, "average_travel_time_s": 400.0})


def f6_partial() -> dict:
    return synthetic_result(
        scenario=F1, times=F6_PARTIAL_TIMES, speeds=[20, 8, 5, 4, 3],
        counts=[0, 200, 180, 180, 180], link_speeds={"AB": [20, 6, 3, 2, 2], "BC": [20] * 5},
        stats={"total_trips": 360, "completed_trips": 180, "average_travel_time_s": 250.0})


# ──────────────────────────────────────────────
# 判定ヘルパー
# ──────────────────────────────────────────────
def normalized(scenario: dict) -> dict:
    """SimulationInput を通した正規化シナリオ（既定値を埋め，重複ノードを統合）．"""
    return SimulationInput(**scenario).model_dump()


def demand_multiset(scenario: dict) -> Counter:
    """需要行の多重集合（重複行を保持する．集合化すると 2 本ある行の 1 本減少を見落とす）．"""
    return Counter((d["orig"], d["dest"], float(d["t_start"]), float(d["t_end"]), float(d["flow"]))
                   for d in scenario.get("demands") or [])


def demand_vehicles(scenario: dict) -> float:
    """総需要（台）= Σ flow × (t_end − t_start)．flow の単純和ではない．"""
    return sum(float(d["flow"]) * (float(d["t_end"]) - float(d["t_start"])) for d in scenario.get("demands") or [])


def changed_paths(a: dict, b: dict, *, atol: float = 1e-9, ignore_name: bool = True) -> dict[str, tuple]:
    """正規化した 2 シナリオの差分を {"links.AB.capacity": (旧, 新), "tmax": (...), "demands": (...)} で返す．

    ノード・リンクは名前をキーに比較し，追加 / 削除は "links.X" → (旧, None) / (None, 新)．
    需要は多重集合で比較し，差があれば "demands" に (旧 Counter, 新 Counter) を入れる．
    JSON のキー順や，許容誤差内の浮動小数差は差分としない．シナリオの表示名（name）は交通条件では
    ないので既定では見ない（ignore_name=False で含める）．
    """
    na, nb = normalized(a), normalized(b)
    out: dict[str, tuple] = {}

    def same(x, y):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
            return abs(float(x) - float(y)) <= atol
        return x == y

    for k in (() if ignore_name else ("name",)) + ("tmax", "deltan", "reaction_time", "random_seed"):
        if not same(na.get(k), nb.get(k)):
            out[k] = (na.get(k), nb.get(k))
    for kind in ("nodes", "links"):
        da = {x["name"]: x for x in na[kind]}
        db = {x["name"]: x for x in nb[kind]}
        for name in sorted(set(da) | set(db)):
            if name not in db:
                out[f"{kind}.{name}"] = (da[name], None)
            elif name not in da:
                out[f"{kind}.{name}"] = (None, db[name])
            else:
                for f in sorted(set(da[name]) | set(db[name])):
                    if not same(da[name].get(f), db[name].get(f)):
                        out[f"{kind}.{name}.{f}"] = (da[name].get(f), db[name].get(f))
    ma, mb = demand_multiset(na), demand_multiset(nb)
    if ma != mb:
        out["demands"] = (ma, mb)
    return out


def scenario_diff(a: dict, b: dict) -> set[str]:
    """changed_paths のキー集合（「変更を許した属性の集合を超えていないか」の判定用）．"""
    return set(changed_paths(a, b))


# テストが事前状態として置いた結果の ID（IsolatedStore.new_ids はこれを「新規」に数えない）
FIXTURE_IDS: set[str] = set()


def store_result(sim_id: str, scenario: dict, meta: dict | None = None) -> dict:
    """シナリオを実行して results_store に保存し，結果 dict を返す（事前状態として登録）．"""
    res = run_uxsim(SimulationInput(**scenario))
    store_sim(sim_id, res, meta or {"type": "manual"})
    FIXTURE_IDS.add(sim_id)
    return res


def store_synthetic(sim_id: str, result: dict, meta: dict | None = None) -> dict:
    """synthetic_result の結果を事前状態として保存する．"""
    store_sim(sim_id, result, meta or {"type": "manual"})
    FIXTURE_IDS.add(sim_id)
    return result


def frozen_copy(sim_id: str) -> dict:
    """基準結果の「変わってはいけない」部分のディープコピー（シナリオ・統計・系列・timeline）．"""
    r = results_store[sim_id]
    return copy.deepcopy({
        "_scenario": r.get("_scenario"), "stats": r.get("stats"),
        "vehicle_counts": r.get("vehicle_counts"), "frame_avg_speed": r.get("frame_avg_speed"),
        "trip_series": r.get("trip_series"), "frame_times": r.get("frame_times"),
        "timeline": {f["properties"]["name"]: f["properties"].get("timeline")
                     for f in (r.get("geojson") or {}).get("features", [])},
        "source": copy.deepcopy((r.get("_meta") or {}).get("source")),
    })


class IsolatedStore:
    """with ブロックの間に results_store へ足された結果を終了時に取り除く．

    各試験は独立した事前状態から始める（前の試験の結果に依存しない．§13.1）．
    ブロック前から在った結果には触らない．
    """

    def __enter__(self):
        self._before = set(dict.keys(results_store))
        return self

    def __exit__(self, *exc):
        for k in list(dict.keys(results_store)):
            if k not in self._before:
                dict.pop(results_store, k, None)
        return False

    def new_ids(self) -> list[str]:
        """ブロック内でアプリ側の処理が作った結果の ID（store_result 等で置いた事前状態は除く）．"""
        return [k for k in dict.keys(results_store) if k not in self._before and k not in FIXTURE_IDS]
