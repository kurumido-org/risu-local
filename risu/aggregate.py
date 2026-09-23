"""LLM に渡す集計データ（get_simulation_data）．数 KB に収める（CLAUDE.md §3.3）．
"""

from __future__ import annotations

from .results import results_store

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "compare_simulations",
    "get_simulation_data",
]


# ── シミュレーションデータ集計（LLM に渡す） ──
def get_simulation_data(sim_id: str, points: int = 30, max_links: int = 20) -> dict | None:
    """シミュレーション結果から集計データを返す（LLMがチャート生成に使用）．

    LLM のコンテキストに入るので小さく保つ: 時系列は最大 points 点，リンク別速度は
    混雑度上位 max_links 本，速度は 0.1 m/s に丸める．
    """
    if sim_id not in results_store:
        return None
    points = max(5, min(int(points or 30), 60))
    max_links = max(0, min(int(max_links if max_links is not None else 20), 50))
    data = results_store[sim_id]
    frames = data.get("frames", {})
    geojson = data.get("geojson", {})
    features = geojson.get("features", [])
    tmax = data.get("tmax", 3600)
    stats = data.get("stats", {})

    frame_times = data.get("frame_times") or sorted([float(k) for k in frames.keys()])
    if not frame_times:
        return None
    # 台数の換算:
    #  - vehicle_counts（間引き前の全点から数えた実台数）があればそれを使う
    #  - 無い（古い結果）場合はフレームのプラトン数 × deltan × サンプリング間隔で近似する
    #    （フレームの ids はプラトン = deltan 台の単位．掛け忘れると deltan 分の 1 に見える）
    sample_step = int(data.get("vehicle_sample_step") or 1)
    deltan = int((data.get("_scenario") or {}).get("deltan") or 1)
    vehicle_counts = data.get("vehicle_counts")
    if vehicle_counts is not None and len(vehicle_counts) != len(frame_times):
        vehicle_counts = None
    # 車両平均速度（台数重み，間引き前）．無い古い結果は描画フレームの車両から近似
    frame_avg_speed = data.get("frame_avg_speed")
    if frame_avg_speed is not None and len(frame_avg_speed) != len(frame_times):
        frame_avg_speed = None
    trip_series = data.get("trip_series") or None

    # 間引き（最大 points 点）
    step = max(1, -(-len(frame_times) // points))
    sampled = frame_times[::step]
    sampled_idx = list(range(0, len(frame_times), step))

    # ネットワーク全体の時系列
    # frames はコンパクト列指向フォーマット: {t_key: {ids:[], xs:[], ys:[], vs:[], ...}}
    # （各列は list または numpy 配列）
    import numpy as np
    time_labels = []
    net_avg_speed = []
    net_vehicle_count = []
    sampled_speeds = []
    for fi, t in zip(sampled_idx, sampled):
        t_key = str(t) if str(t) in frames else str(round(t, 1))
        cols = frames.get(t_key) or {}
        speeds = np.asarray(cols.get("vs", ()), dtype=np.float64)
        time_labels.append(round(t))
        if vehicle_counts is not None:
            net_vehicle_count.append(int(vehicle_counts[fi]))
        else:
            net_vehicle_count.append(int(speeds.size) * sample_step * deltan)
        if frame_avg_speed is not None:
            v = frame_avg_speed[fi]
            net_avg_speed.append(round(float(v), 1) if v is not None else None)
        elif speeds.size:
            net_avg_speed.append(round(float(speeds.mean()), 1))
        else:
            net_avg_speed.append(None)
        if speeds.size:
            sampled_speeds.append(speeds)

    # 流入・到着の累積（実イベント）を time_labels に合わせて引く
    net_entered = net_completed = None
    if trip_series and trip_series.get("t"):
        ts_t = np.asarray(trip_series["t"], dtype=np.float64)
        pos = np.clip(np.searchsorted(ts_t, np.asarray(sampled, dtype=np.float64), side="right") - 1,
                      0, ts_t.size - 1)
        net_entered   = [int(trip_series["entered"][p])   for p in pos.tolist()]
        net_completed = [int(trip_series["completed"][p]) for p in pos.tolist()]

    # リンク別速度
    # 大規模ネットワーク（数千〜1万リンク）で全リンクを返すと LLM の
    # コンテキストに収まらないため，混雑度（平均速度 / 自由流速度 が低い順）
    # 上位 MAX_DETAIL_LINKS 本に制限する．
    MAX_DETAIL_LINKS = max_links
    total_links = len(features)

    def _congestion_ratio(f):
        tl = f["properties"].get("timeline", [])
        ffs = f["properties"].get("free_flow_speed") or 20
        if not tl or ffs <= 0:
            return 1.0
        avg = sum(e["speed"] for e in tl) / len(tl)
        return avg / ffs

    detail_features = features
    truncated = False
    if total_links > MAX_DETAIL_LINKS:
        detail_features = (sorted(features, key=_congestion_ratio)[:MAX_DETAIL_LINKS]
                           if MAX_DETAIL_LINKS > 0 else [])
        truncated = True

    link_names = [f["properties"]["name"] for f in detail_features]
    link_speeds = {}
    for f in detail_features:
        ln = f["properties"]["name"]
        tl = f["properties"].get("timeline", [])
        if not tl:
            continue
        speeds = []
        for t in sampled:
            lo, hi = 0, len(tl) - 1
            while lo < hi:
                m = (lo + hi + 1) >> 1
                if tl[m]["t"] <= t: lo = m
                else: hi = m - 1
            speeds.append(round(tl[lo]["speed"], 1))
        link_speeds[ln] = speeds

    # 速度分布: サーバーが間引き前の全点から作ったものを優先．
    # 無い古い結果はサンプル時刻の描画フレームから近似
    speed_hist = {"labels": [], "counts": []}
    stored_hist = data.get("speed_histogram")
    if stored_hist and stored_hist.get("labels"):
        speed_hist = {"labels": list(stored_hist["labels"]), "counts": list(stored_hist["counts"])}
    elif sampled_speeds:
        all_speeds = np.round(np.concatenate(sampled_speeds), 1)
        max_spd = float(all_speeds.max())
        bin_size = max(1, round(max_spd / 12))
        bins = list(range(0, int(max_spd) + bin_size + 1, bin_size))
        n_bins = len(bins) - 1
        idx = np.minimum((all_speeds / bin_size).astype(np.int64), n_bins - 1)
        counts = np.bincount(idx, minlength=n_bins).tolist()
        speed_hist["labels"] = [f"{bins[i]}-{bins[i+1]}" for i in range(n_bins)]
        speed_hist["counts"] = counts

    data = {
        "sim_id": sim_id,
        "tmax": tmax,
        "stats": stats,
        "time_labels": time_labels,
        "network_avg_speed": net_avg_speed,
        "network_vehicle_count": net_vehicle_count,
        "network_entered_count": net_entered,
        "network_completed_count": net_completed,
        "total_links": total_links,
        "link_names": link_names,
        "link_speeds": link_speeds,
        "speed_histogram": speed_hist,
    }
    if truncated:
        data["link_speeds_note"] = (
            f"リンク数が多いため（全 {total_links} 本），link_speeds / link_names は"
            f"混雑度上位 {MAX_DETAIL_LINKS} 本のみ．ネットワーク全体の傾向は"
            f" network_avg_speed / speed_histogram を参照．"
        )
    data["metric_note"] = (
        f"network_vehicle_count: その時刻に走行中の実台数（deltan={deltan} 換算済み，間引き前の全車両）．"
        "network_avg_speed: 走行中の全車両の速度の台数重み平均 m/s（画面の AVG SPEED と同じ定義）．"
        "link_speeds: リンク別の平均速度（そのリンク上の車両の平均）で，network_avg_speed の"
        "リンク単純平均とは一致しない．"
        "network_entered_count / network_completed_count: 流入・到着の累積台数（実イベント）．"
        f"speed_histogram の counts は全フレーム・全車両の観測点数（プラトン={deltan} 台単位）．"
    )
    if sample_step > 1:
        data["vehicle_sample_note"] = (
            f"描画用フレームは {sample_step} プラトンに 1 つをサンプリングしている．"
            f"network_vehicle_count はその影響を受けない．"
        )
    return data


def _link_mean_speeds(raw: dict) -> dict[str, float]:
    out = {}
    for f in (raw.get("geojson") or {}).get("features", []):
        tl = f["properties"].get("timeline") or []
        if tl:
            out[f["properties"]["name"]] = sum(e["speed"] for e in tl) / len(tl)
    return out


def _scenario_items_diff(a: list[dict] | None, b: list[dict] | None, limit: int = 20) -> dict:
    da = {str(x.get("name")): x for x in a or []}
    db = {str(x.get("name")): x for x in b or []}
    added = sorted(set(db) - set(da))
    removed = sorted(set(da) - set(db))
    changed = []
    for n in sorted(set(da) & set(db)):
        if da[n] != db[n]:
            keys = sorted(k for k in set(da[n]) | set(db[n]) if da[n].get(k) != db[n].get(k))
            changed.append({"name": n, "changes": {k: [da[n].get(k), db[n].get(k)] for k in keys}})
    return {"n_added": len(added), "n_removed": len(removed), "n_changed": len(changed),
            "added": added[:limit], "removed": removed[:limit], "changed": changed[:limit]}


def compare_simulations(a_id: str, b_id: str, max_links: int = 20) -> dict | None:
    """2 つの結果の差を LLM 向けにまとめる（b − a）．

    - stats（total / completed / average_travel_time_s）と平均速度（走行中全車両の台数重み平均，全フレーム平均）
    - リンク別平均速度の変化（|diff| の大きい順に max_links 本）
    - シナリオの差分（全体パラメータ，リンク / ノードの追加・削除・変更，需要の増減）
    - random_seed が同じか．違う / 未指定なら乱数の揺らぎを含む旨の note
    数 KB に収める（§3.3）．
    """
    if a_id not in results_store or b_id not in results_store:
        return None
    A, B = results_store[a_id], results_store[b_id]
    max_links = max(0, min(int(max_links if max_links is not None else 20), 50))

    def stat(r, k):
        return (r.get("stats") or {}).get(k)

    stats = {}
    for k in ("total_trips", "completed_trips", "average_travel_time_s"):
        va, vb = stat(A, k), stat(B, k)
        diff = round(vb - va, 1) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None
        stats[k] = {"a": va, "b": vb, "diff": diff}

    def mean_speed(r):
        v = [x for x in (r.get("frame_avg_speed") or []) if x is not None]
        return round(sum(v) / len(v), 2) if v else None

    sa_, sb_ = mean_speed(A), mean_speed(B)
    speed = {"a": sa_, "b": sb_,
             "diff": round(sb_ - sa_, 2) if sa_ is not None and sb_ is not None else None}

    la, lb = _link_mean_speeds(A), _link_mean_speeds(B)
    diffs = sorted(((n, la[n], lb[n]) for n in la if n in lb), key=lambda x: -abs(x[2] - x[1]))
    link_changes = [{"link": n, "a": round(x, 1), "b": round(y, 1), "diff": round(y - x, 1)}
                    for n, x, y in diffs[:max_links]]

    sa, sb = A.get("_scenario") or {}, B.get("_scenario") or {}
    params = {k: [sa.get(k), sb.get(k)] for k in ("tmax", "deltan", "reaction_time", "random_seed")
              if sa.get(k) != sb.get(k)}

    def dkey(d):
        return (d.get("orig"), d.get("dest"), d.get("t_start"), d.get("t_end"), d.get("flow"))
    dema = {dkey(d) for d in sa.get("demands") or []}
    demb = {dkey(d) for d in sb.get("demands") or []}
    same_seed = sa.get("random_seed") is not None and sa.get("random_seed") == sb.get("random_seed")
    note = ("random_seed が同じなので，差は条件変更によるもの．" if same_seed else
            "random_seed が異なる / 未指定なので，差には乱数（経路選択・合流順）の揺らぎが含まれる．"
            "条件の効果と断定しないこと（同じ seed で再実行して比べる）．")
    note += "diff は b − a．平均速度は走行中全車両の台数重み平均を全フレームで平均した値（m/s）．"
    return {
        "a": a_id, "b": b_id,
        "stats": stats,
        "network_avg_speed": speed,
        "link_speed_changes": link_changes,
        "total_links_compared": len(diffs),
        "scenario_diff": {
            "params": params,
            "links": _scenario_items_diff(sa.get("links"), sb.get("links")),
            "nodes": _scenario_items_diff(sa.get("nodes"), sb.get("nodes")),
            "demands": {"n_added": len(demb - dema), "n_removed": len(dema - demb),
                        "total_flow": [round(sum(d[4] or 0 for d in dema), 3), round(sum(d[4] or 0 for d in demb), 3)]},
        },
        "same_random_seed": same_seed,
        "note": note,
    }
