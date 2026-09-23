"""UXsim の実行と後処理: World 構築（uxsim_bridge 経由）→ 実行 → フレーム / 統計 / 系列．
CLAUDE.md §3.5 / §3.6 の不変条件はこのモジュールのもの．
"""

from __future__ import annotations

import asyncio
import math
import os
import time
import traceback as _tb
from typing import Any, NamedTuple

from fastapi import HTTPException

from uxsim_bridge import build_world

from .runtime import RUNTIME_STATUS, UXSIM_VERSION, executor
from .schema import SimulationInput

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "MAX_NODES",
    "MAX_TMAX",
    "apply_link_geometries",
    "run_uxsim",
    "run_uxsim_async",
    "select_frames",
    "startup_selfcheck",
    "trip_stats",
    "validate_scenario_size",
]

# UXsim 実行タイムアウト（秒）．環境変数で上書き可能．
UXSIM_TIMEOUT_SEC = int(os.getenv("RISU_UXSIM_TIMEOUT", "120"))

# 可視化フレーム数の上限（時刻方向の間引き）
MAX_FRAMES = int(os.getenv("RISU_MAX_FRAMES", "200"))
# 可視化フレームの総車両点数の上限．超過時は車両 ID を等間隔にサンプリングして
# 描画対象を減らす（0 で無効）．3,000,000 点 ≒ JSON 100MB / gzip 20MB が目安で，
# これを超えるとブラウザ側の JSON.parse とメモリが破綻する．
# リンク別速度 timeline と統計は間引き前の全点から計算するので影響を受けない．
MAX_FRAME_POINTS = int(os.getenv("RISU_MAX_FRAME_POINTS", "3000000"))


async def run_uxsim_async(scenario) -> dict:
    """
    UXsim をタイムアウト付きで非同期実行するラッパー．
    エラーを HTTPException に分類してユーザー向けメッセージを返す．
    """
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, run_uxsim, scenario),
            timeout=UXSIM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            408,
            detail=(
                f"シミュレーションがタイムアウトしました（{UXSIM_TIMEOUT_SEC}s 超過）．"
                "ネットワーク規模や tmax を縮小してください．"
                "OSM インポートの場合は road_types='arterial' または 'major' を指定して"
                "細街路を除外すると大幅に高速化できます．"
            ),
        )
    except HTTPException:
        raise
    except MemoryError:
        raise HTTPException(
            413,
            detail="メモリ不足でシミュレーションが中断されました．ネットワーク規模を縮小してください．",
        )
    except KeyError as e:
        # シナリオ内で存在しないノード/リンクを参照
        raise HTTPException(
            400,
            detail=f"シナリオ定義エラー: 参照先「{e.args[0] if e.args else '?'}」が見つかりません．",
        )
    except ValueError as e:
        # UXsim の不正入力 or validate_scenario_size
        raise HTTPException(400, detail=f"シナリオが不正です: {e}")
    except Exception as e:
        # その他の UXsim 内部エラー
        print(f"[RISU uxsim error] {e.__class__.__name__}: {e}")
        print(_tb.format_exc())
        raise HTTPException(
            500,
            detail=f"シミュレーション計算エラー: {e.__class__.__name__}",
        )

# ──────────────────────────────────────────────
# UXsim 実行（同期 → Executor で非同期化）
# ──────────────────────────────────────────────
def trip_stats(W) -> tuple[int, int, float | None, list[float]]:
    """basic_analysis / od_analysis と同じ数え方でトリップ統計を計算する．

    dest を持つ車両 × DELTAN がトリップ数，travel_time != -1 が完了，
    平均旅行時間は完了車両の travel_time の平均．
    戻り値の 4 番目は完了車両の到着時刻（秒）のリスト（到着累積の集計用）．
    cpp backend では CppVehicle のプロパティ（毎回 state 判定で C++ を複数回参照）を
    経由せず C++ オブジェクトを直接読む．
    """
    dn = W.DELTAN
    trip_all = 0
    trip_completed = 0
    tt_sum = 0.0
    arrivals: list[float] = []
    for veh in W.VEHICLES.values():
        cv = veh.__dict__.get("_cpp_vehicle")
        if cv is not None:
            if cv.dest is None:
                continue
            tt = cv.travel_time
            # CppVehicle.travel_time と同じ abort 判定（abort なら -1 扱い）
            if cv.flag_trip_aborted or (cv.state == 3 and cv.arrival_time < 0 and tt <= 0):
                tt = -1
        else:
            if veh.dest is None:
                continue
            tt = veh.travel_time
        trip_all += dn
        if tt != -1:
            trip_completed += dn
            tt_sum += tt
            if cv is not None:
                arrivals.append(float(cv.arrival_time))
            else:
                # 純 Python: departure_time はステップ単位．秒の出発 + 旅行時間 = 到着（秒）
                arrivals.append(float(veh.departure_time_in_second) + float(tt))
    avg_tt = (tt_sum * dn / trip_completed) if trip_completed else None
    return trip_all, trip_completed, avg_tt, arrivals


def _speed_histogram(v) -> dict:
    """速度分布（全フレーム・全車両の観測点）．labels は "lo-hi" m/s，counts は観測点数．

    描画用の間引き前の全点から作る（run_uxsim）．get_simulation_data はこれをそのまま返す．
    """
    import numpy as np
    v = np.asarray(v, dtype=np.float64)
    if v.size == 0:
        return {"labels": [], "counts": []}
    max_spd = float(v.max())
    bin_size = max(1, round(max_spd / 12))
    bins = list(range(0, int(max_spd) + bin_size + 1, bin_size))
    n_bins = len(bins) - 1
    idx = np.minimum((v / bin_size).astype(np.int64), n_bins - 1)
    counts = np.bincount(idx, minlength=n_bins).tolist()
    return {"labels": [f"{bins[i]}-{bins[i+1]}" for i in range(n_bins)], "counts": counts}


def select_frames(tk, max_frames: int):
    """時刻キー配列から可視化フレームを選ぶ．

    戻り値: (kept, fidx) — kept は昇順のフレーム時刻キー，fidx は各点のフレーム index
    （間引きで落ちた点は -1）．ユニーク時刻キーが max_frames を超える場合のみ
    len // max_frames 間隔で間引く（len // max_frames が 1 のときは間引かない）．

    tk は 0.1 秒精度の整数（≤ tmax×10）なので，ソートベースの np.unique / isin ではなく
    bincount + ルックアップテーブルで O(N) に処理する（5,000 万点で約 5 倍速）．
    """
    import numpy as np
    if tk.size == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    tk_max = int(tk.max())
    if tk_max < 0 or tk_max > 50_000_000:  # 想定外の時刻（LUT が巨大になる）→ 汎用経路
        kept = np.unique(tk)
        if kept.size > max_frames:
            kept = kept[::max(1, kept.size // max_frames)]
        fidx = np.searchsorted(kept, tk)
        fidx[(fidx >= kept.size) | (kept[np.minimum(fidx, kept.size - 1)] != tk)] = -1
        return kept, fidx
    present = np.bincount(tk, minlength=tk_max + 1) > 0
    kept = np.flatnonzero(present).astype(np.int64)
    if kept.size > max_frames:
        kept = kept[::max(1, kept.size // max_frames)]
    lut = np.full(tk_max + 1, -1, dtype=np.int64)
    lut[kept] = np.arange(kept.size, dtype=np.int64)
    return kept, lut[tk]


class _RunPoints(NamedTuple):
    """_collect_run_points の戻り値．fast_path は使われた経路（テスト・/healthz が参照）．"""
    kept: Any
    fidx: Any
    li: Any
    vid: Any
    x: Any
    v: Any
    entry_t: Any
    fast_path: bool


def _collect_run_points(W, n_links: int, max_frames: int) -> _RunPoints:
    """全車両のログから「run 状態かつ有効リンク上」の点を，可視化フレーム分だけ列として取り出す．

    戻り値 _RunPoints:
      kept: 昇順のフレーム時刻キー（0.1 秒精度の整数）
      fidx: 各点のフレーム index（kept への index），li: リンク index，
      vid: W.VEHICLES の登録順 index，x: リンク上位置，v: 速度
      entry_t: 車両ごと（W.VEHICLES 順）の実流入時刻（最初に run になった秒．未流入は NaN）．
               流入累積の集計に使う．departure_time は「予定」で，入口待ちの車両は含んでしまう．
      fast_path: True なら C++ のフラット配列経路，False なら車両別ログのフォールバック（遅い）

    fast path (uxsim cpp backend): C++ 側の build_all_vehicle_logs_flat_compact() で
    全車両のログを 1 回でフラット配列として受け取り，車両ごとの Python ループを行わない．
    間引きで落ちるフレームの点は列抽出の前にマスクして，抽出コストを保持分だけにする．
    fallback (純 Python uxsim / 旧 API): 車両ごとに log_* を読む．
    """
    import numpy as np

    cpp = getattr(W, "_cpp_world", None)
    if cpp is not None and hasattr(cpp, "build_all_vehicle_logs_flat_compact"):
        try:
            flat = cpp.build_all_vehicle_logs_flat_compact()
            offsets = np.asarray(flat["offsets"], dtype=np.int64)
            n_veh = offsets.size - 1
            if n_veh != len(W.VEHICLES):
                raise RuntimeError(f"vehicle count mismatch: flat={n_veh} VEHICLES={len(W.VEHICLES)}")
            state = np.asarray(flat["log_state"])
            link = np.asarray(flat["log_link"])
            veh_cls = type(next(iter(W.VEHICLES.values()))) if n_veh else None
            state_map = getattr(veh_cls, "_LOG_STATE_MAP", None)
            run_code = state_map.index("run") if state_map else 2
            # run 状態かつ有効リンク上の点（entry index）
            idx = np.flatnonzero((state == run_code) & (link >= 0) & (link < n_links))
            tk = np.rint(np.asarray(flat["log_t"], dtype=np.float64)[idx] * 10.0).astype(np.int64)
            kept, fidx = select_frames(tk, max_frames)
            if kept.size and fidx.size and (fidx < 0).any():
                m = fidx >= 0
                idx, fidx = idx[m], fidx[m]
            # entry index → 車両 index（offsets[v] <= e < offsets[v+1]）．
            # W.VEHICLES の登録順 == C++ vehicle index 順（_register_new_cpp_vehicles）．
            vid = np.searchsorted(offsets, idx, side="right") - 1
            # 車両ごとの実流入時刻: 最初の run 状態のログ時刻（間引き・リンク範囲に依らない）
            log_t_all = np.asarray(flat["log_t"], dtype=np.float64)
            run_all = np.flatnonzero(state == run_code)
            entry_t = np.full(n_veh, np.nan)
            if run_all.size:
                vid_run = np.searchsorted(offsets, run_all, side="right") - 1
                uniq, first = np.unique(vid_run, return_index=True)  # 車両ごとの最初の run
                entry_t[uniq] = log_t_all[run_all[first]]
            RUNTIME_STATUS["fast_path_error"] = None
            return _RunPoints(
                kept, fidx,
                link[idx].astype(np.int64),
                vid,
                np.asarray(flat["log_x"], dtype=np.float64)[idx],
                np.asarray(flat["log_v"], dtype=np.float64)[idx],
                entry_t,
                True,
            )
        except Exception as e:  # 内部 API 変更時は遅い経路にフォールバック
            RUNTIME_STATUS["fast_path_error"] = f"{e.__class__.__name__}: {e}"
            print(f"[RISU] flat vehicle log fast path unavailable ({e.__class__.__name__}: {e}); "
                  f"falling back to per-vehicle logs")

    link_idx_map = {lk.name: i for i, lk in enumerate(W.LINKS)}
    tk_parts, vid_parts, li_parts, x_parts, v_parts = [], [], [], [], []
    entry_t = np.full(len(W.VEHICLES), np.nan)
    for vid, veh in enumerate(W.VEHICLES.values()):
        cache = getattr(veh, "_log_cache", None)
        if cache is None and hasattr(veh, "_ensure_log_raw"):
            veh._ensure_log_raw()
            cache = veh._log_cache
        if cache is not None and "log_state" in cache and "log_link" in cache:
            state_map = getattr(type(veh), "_LOG_STATE_MAP", None)
            run_code = state_map.index("run") if state_map else 2
            state_raw = np.asarray(cache["log_state"])
            link_raw = np.asarray(cache["log_link"], dtype=np.int64)
            sel = np.nonzero((state_raw == run_code) & (link_raw >= 0) & (link_raw < n_links))[0]
            if sel.size == 0:
                continue
            li_sel = link_raw[sel]
        else:
            log_state = veh.log_state
            log_link = veh.log_link
            li = np.fromiter(
                (link_idx_map.get(lk.name, -1) if (s == "run" and hasattr(lk, "name")) else -1
                 for s, lk in zip(log_state, log_link)),
                dtype=np.int64, count=len(log_state),
            )
            sel = np.nonzero(li >= 0)[0]
            if sel.size == 0:
                continue
            li_sel = li[sel]
        log_t = np.asarray(veh.log_t, dtype=np.float64)
        entry_t[vid] = log_t[sel[0]]
        tk_parts.append(np.rint(log_t[sel] * 10.0).astype(np.int64))
        li_parts.append(li_sel)
        vid_parts.append(np.full(sel.size, vid, dtype=np.int64))
        x_parts.append(np.asarray(veh.log_x, dtype=np.float64)[sel])
        v_parts.append(np.asarray(veh.log_v, dtype=np.float64)[sel])
    if not tk_parts:
        e = np.empty(0, dtype=np.int64)
        return _RunPoints(e, e, e, e, np.empty(0), np.empty(0), entry_t, False)
    tk = np.concatenate(tk_parts)
    li, vid = np.concatenate(li_parts), np.concatenate(vid_parts)
    x, v = np.concatenate(x_parts), np.concatenate(v_parts)
    kept, fidx = select_frames(tk, max_frames)
    m = fidx >= 0
    return _RunPoints(kept, fidx[m], li[m], vid[m], x[m], v[m], entry_t, False)


def run_uxsim(scenario: SimulationInput) -> dict:
    # リソース保護: ユーザー入力 / LLM 経由のいずれでも上限を適用
    try:
        validate_scenario_size(scenario)
    except NameError:
        # validate_scenario_size 定義前に呼ばれた場合（起動順序保険）は素通し
        pass
    # 信号メタデータをシナリオから抽出（結果の signals メタデータ用）
    _orig_signal_nodes = [
        {"name": n.name, "x": float(n.x), "y": float(n.y),
         "signal": [float(p) for p in n.signal]}
        for n in scenario.nodes if n.signal and sum(n.signal) > 0
    ]

    # ネットワーク構築は uxsim_bridge に集約している（素の UXsim から実行する
    # scripts/run_scenario.py と同じコードを通す．二重に持つと片方だけ直して挙動がずれる）．
    # basic_analysis は内部で od_analysis → floyd_warshall（全点対最短路, O(ノード数^3)）を
    # 実行し 5000 ノード級で数十秒かかる．RISU が必要な統計 3 値は後段で車両ログから直接計算する．
    W = build_world(scenario, cpp=True, disable_basic_analysis=True)

    # cpp backend は終了時に全車両の _log_cache（車両ごとの numpy スライス辞書）を
    # 構築するが，RISU は後段で C++ のフラット配列を直接読むので不要（数万台で数秒）．
    # フォールバック経路では _ensure_log_raw() が必要に応じて構築する．
    if hasattr(W, "_skip_log_on_terminate"):
        W._skip_log_on_terminate = True

    t0 = time.perf_counter()
    W.exec_simulation()
    elapsed = time.perf_counter() - t0

    # ---- 基本統計（basic_analysis 相当を直接計算） ----
    # od_analysis と同じ数え方: dest を持つ車両 × DELTAN がトリップ数，
    # travel_time != -1 が完了，平均旅行時間は完了車両の travel_time の平均．
    _trip_all, _trip_completed, _avg_tt, _arrivals = trip_stats(W)

    # ---- リンク情報（GeoJSON） ----
    # 同一座標ペアのリンクを検出し，重複分にオフセットを付与して視覚的に区別
    coord_count = {}  # (sx,sy,ex,ey) -> count（同一方向）
    for lk in W.LINKS:
        key = (lk.start_node.x, lk.start_node.y, lk.end_node.x, lk.end_node.y)
        coord_count[key] = coord_count.get(key, 0) + 1

    coord_idx = {}  # 同一座標ペアの何番目か
    features = []
    for lk in W.LINKS:
        sx, sy = lk.start_node.x, lk.start_node.y
        ex, ey = lk.end_node.x, lk.end_node.y
        key = (sx, sy, ex, ey)
        idx = coord_idx.get(key, 0)
        coord_idx[key] = idx + 1
        total = coord_count[key]

        # 重複がある場合，垂直方向にオフセット
        if total > 1:
            dx, dy = ex - sx, ey - sy
            length = (dx**2 + dy**2) ** 0.5 or 1
            # 法線方向の単位ベクトル
            nx, ny = -dy / length, dx / length
            # オフセット量（リンク長の2%，中央揃え）
            offset = (idx - (total - 1) / 2) * max(length * 0.02, 5)
            sx += nx * offset
            sy += ny * offset
            ex += nx * offset
            ey += ny * offset

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [sx, sy],
                    [ex, ey],
                ],
            },
            "properties": {
                "name":            lk.name,
                "length":          lk.length,
                "free_flow_speed": lk.free_flow_speed,
                "number_of_lanes": lk.number_of_lanes,
            },
        })

    # ---- 個車軌跡データ ----
    # 「run 状態かつ有効リンク上」の全ログ点を (tk, li, vid, x, v) の列として一括取得し，
    # 以降は numpy だけで集計する（車両ごと・点ごとの Python ループなし）．
    #  - リンク別平均速度: (フレーム, リンク) キーで bincount
    #  - フレーム分割: フレーム index でソートして searchsorted で境界を求める
    #  - frames の各列は numpy 配列のまま保持（Python float 化しない．orjson が直接直列化）
    t_post0 = time.perf_counter()
    import numpy as np

    link_names = [lk.name for lk in W.LINKS]
    _n_links = len(link_names)
    _sx = np.fromiter((lk.start_node.x for lk in W.LINKS), dtype=np.float64, count=_n_links)
    _sy = np.fromiter((lk.start_node.y for lk in W.LINKS), dtype=np.float64, count=_n_links)
    _ex = np.fromiter((lk.end_node.x   for lk in W.LINKS), dtype=np.float64, count=_n_links)
    _ey = np.fromiter((lk.end_node.y   for lk in W.LINKS), dtype=np.float64, count=_n_links)
    _ll = np.fromiter((lk.length       for lk in W.LINKS), dtype=np.float64, count=_n_links)
    _ffs = np.fromiter((lk.free_flow_speed for lk in W.LINKS), dtype=np.float64, count=_n_links)

    # 時刻方向の間引き（MAX_FRAMES）は _collect_run_points 内で列抽出前に適用済み
    rp = _collect_run_points(W, _n_links, MAX_FRAMES)
    kept, fidx, li_all, vid_all, x_all, v_all, entry_t = (
        rp.kept, rp.fidx, rp.li, rp.vid, rp.x, rp.v, rp.entry_t)
    backend = "cpp" if getattr(W, "_cpp_world", None) is not None else "python"
    RUNTIME_STATUS.update({"backend": backend, "fast_path": rp.fast_path})

    frames = {}
    frame_times = []
    link_timeline = {ln: [] for ln in link_names}
    vehicle_sample_step = 1
    vehicle_counts: list[int] = []
    frame_avg_speed: list = []
    speed_hist = {"labels": [], "counts": []}

    if kept.size:
        n_frames = kept.size
        # 旧実装互換のキー: str(round(tk/10, 1)) → 整数時刻は "12.0"
        t_vals = [round(tk / 10.0, 1) for tk in kept.tolist()]
        frame_keys = [str(t) for t in t_vals]
        frame_times = t_vals

        # ── リンク別平均速度（間引き前の全点で集計） ──
        key = fidx * _n_links + li_all
        cnt = np.bincount(key, minlength=n_frames * _n_links).reshape(n_frames, _n_links)
        vsum = np.bincount(key, weights=v_all, minlength=n_frames * _n_links).reshape(n_frames, _n_links)
        avg = np.where(cnt > 0, vsum / np.maximum(cnt, 1), _ffs[None, :])
        avg_cols = np.round(avg, 2).T.tolist()  # リンクごとの時系列
        for i, ln in enumerate(link_names):
            link_timeline[ln] = [{"t": t, "speed": s} for t, s in zip(t_vals, avg_cols[i])]

        # ── フレームごとの走行中台数（分析用．描画用の間引きとは独立に全点から数える） ──
        # UXsim の 1 車両（プラトン）は deltan 台を表すので，実台数に換算して保持する．
        _cnt_f = np.bincount(fidx, minlength=n_frames)[:n_frames]
        vehicle_counts = (_cnt_f * int(W.DELTAN)).tolist()
        # ── フレームごとの車両平均速度（台数重み）と速度分布．どちらも間引き前の全点 ──
        # 「平均速度」の定義はこれ 1 つ（画面の AVG SPEED と LLM の network_avg_speed が共有）．
        # リンク別 timeline の単純平均（リンク重み）とは別物なので混ぜない．
        _vsum_f = np.bincount(fidx, weights=v_all, minlength=n_frames)[:n_frames]
        frame_avg_speed = [round(float(s / c), 2) if c > 0 else None
                           for s, c in zip(_vsum_f.tolist(), _cnt_f.tolist())]
        speed_hist = _speed_histogram(v_all)

        # ── 総点数の上限: 車両 ID を等間隔サンプリング（描画用のみ） ──
        if MAX_FRAME_POINTS > 0 and fidx.size > MAX_FRAME_POINTS:
            vehicle_sample_step = int(math.ceil(fidx.size / MAX_FRAME_POINTS))
            keep = (vid_all % vehicle_sample_step) == 0
            print(f"[RISU] frame points {fidx.size:,} > {MAX_FRAME_POINTS:,}: "
                  f"sampling every {vehicle_sample_step} vehicles -> {int(keep.sum()):,} points")
            fidx, li_all, vid_all = fidx[keep], li_all[keep], vid_all[keep]
            x_all, v_all = x_all[keep], v_all[keep]

        # ── 座標・alpha を一括計算 ──
        llen = _ll[li_all]
        with np.errstate(divide="ignore", invalid="ignore"):
            alpha = np.where(llen > 0, np.clip(x_all / llen, 0.0, 1.0), 0.0)
        vx = _sx[li_all] * (1.0 - alpha) + _ex[li_all] * alpha
        vy = _sy[li_all] * (1.0 - alpha) + _ey[li_all] * alpha

        # ── フレーム順に整列し，境界で分割（stable sort で車両順を保持） ──
        order = np.argsort(fidx, kind="stable")
        fidx_s = fidx[order]
        ids_s    = vid_all[order].astype(np.int32)
        li_s     = li_all[order].astype(np.int32)
        xs_s     = np.round(vx[order], 2)
        ys_s     = np.round(vy[order], 2)
        vs_s     = np.round(v_all[order], 2)
        alphas_s = np.round(alpha[order], 4)
        starts = np.searchsorted(fidx_s, np.arange(n_frames), side="left").tolist()
        ends   = np.searchsorted(fidx_s, np.arange(n_frames), side="right").tolist()
        for fk, st, en in zip(frame_keys, starts, ends):
            frames[fk] = {
                "ids":    ids_s[st:en],
                "xs":     xs_s[st:en],
                "ys":     ys_s[st:en],
                "vs":     vs_s[st:en],
                "alphas": alphas_s[st:en],
                "li":     li_s[st:en],
            }

    for f in features:
        f["properties"]["timeline"] = link_timeline[f["properties"]["name"]]

    # ---- 流入・到着の累積台数（実イベントから．描画用フレームとは独立） ----
    # 時間軸は 0・各フレーム時刻・tmax．フレームは「走行車両がいる時刻」にしか無いので，
    # 全車両到着後の状態（走行中 0 台・到着 = 全台）はこの系列でしか表せない．
    _axis = sorted({0.0, float(scenario.tmax), *frame_times})
    _ent = np.sort(entry_t[np.isfinite(entry_t)]) if entry_t.size else np.empty(0)
    _arr = np.sort(np.asarray(_arrivals, dtype=np.float64))
    _dn = int(W.DELTAN)
    trip_series = {
        "t":         _axis,
        "entered":   (np.searchsorted(_ent, _axis, side="right") * _dn).tolist(),
        "completed": (np.searchsorted(_arr, _axis, side="right") * _dn).tolist(),
    }

    post_elapsed = time.perf_counter() - t_post0

    # ---- 集計統計 ----
    # 座標ペアのユニーク数を確認（重複リンク検出）
    coord_pairs = set()
    for f in features:
        c = f["geometry"]["coordinates"]
        coord_pairs.add((c[0][0], c[0][1], c[1][0], c[1][1]))
    print(f"[RISU] Simulation done: {len(W.NODES)} nodes, {len(W.LINKS)} links, "
          f"{len(features)} GeoJSON features, {len(coord_pairs)} unique coord pairs "
          f"(exec={elapsed:.2f}s, post={post_elapsed:.2f}s, backend={backend}, "
          f"fast_path={'on' if rp.fast_path else 'OFF'})")
    stats = {
        "total_trips":           int(_trip_all),
        "completed_trips":       int(_trip_completed),
        "average_travel_time_s": round(float(_avg_tt), 1)
            if _trip_completed > 0 else None,
        "simulation_time_s":     round(elapsed, 2),
        "post_processing_s":     round(post_elapsed, 2),
    }

    # 信号現示メタデータ（可視化用）
    # phase_log は UXsim の実シミュレーション結果（signal_log）を採用し，
    # クライアント表示と内部挙動のタイミングずれをなくす．
    signals = []
    try:
        # リンク名 → 終端ノード名（信号機は「その交差点に流入するリンク」だけに描く）
        link_end_map = {lk.name: lk.end for lk in scenario.links}
        # 信号ノード ID → signal_log
        log_by_orig_id = {}
        for w_node in W.NODES:
            sl = getattr(w_node, "signal_log", None)
            if sl is None or len(sl) == 0:
                continue
            if w_node.name in {n["name"] for n in _orig_signal_nodes}:
                log_by_orig_id[w_node.name] = list(sl)

        w_link_map = getattr(W, "_risu_link_map", {})
        for orig_node in _orig_signal_nodes:
            # この交差点に流入する全リンクと，**実際に適用された**現示番号のリスト．
            # signal_group 省略時は build_world が全現示に展開する（UXsim の既定 [0] ではない）．
            # 他の交差点のリンクを混ぜない（混ぜると同じ信号機が交差点の数だけ重複描画される）．
            groups = {}
            for lk_name, end_name in link_end_map.items():
                if end_name != orig_node["name"]:
                    continue
                w_link = w_link_map.get(lk_name)
                g = getattr(w_link, "signal_group", None) if w_link is not None else None
                if g is None:
                    g = list(range(len(orig_node["signal"])))
                groups[lk_name] = [int(x) for x in g] if isinstance(g, (list, tuple)) else [int(g)]
            phase_log = log_by_orig_id.get(orig_node["name"], [])
            signals.append({
                "node":     orig_node["name"],
                "x":        orig_node["x"],
                "y":        orig_node["y"],
                "phases":   orig_node["signal"],
                "groups":   groups,
                "phase_log": phase_log,    # UXsim 実 phase（ログ 1 件 = deltat 秒）
                # ログの時間刻み（deltan × reaction_time）．tmax / len(phase_log) は
                # tmax が deltat の整数倍でないとずれるので，実値を送る
                "deltat":   float(W.DELTAT),
            })
    except Exception as e:
        print(f"[RISU] signal metadata extraction failed: {e}")
        signals = []

    return {
        # 再現用: 実行に使った正規化済みシナリオ（SimulationInput）の完全コピー
        "_scenario": scenario.model_dump(),
        "geojson": {"type": "FeatureCollection", "features": features},
        "frames":  frames,
        "frame_times": frame_times,
        "stats":   stats,
        "tmax":    scenario.tmax,
        "signals": signals,
        # コンパクト列指向フォーマット用: link 名一覧（li インデックスで参照）
        "link_names": link_names,
        # スキーマバージョン（クライアントのフォーマット分岐用）
        "frame_format": "columnar_v2",
        # 描画用フレームの車両サンプリング間隔（1 = 全車両）．MAX_FRAME_POINTS 参照．
        "vehicle_sample_step": vehicle_sample_step,
        # フレームごとの走行中台数（frame_times と同じ長さ．実台数 = プラトン数 × deltan）．
        # 間引き前の全点から数えるので，vehicle_sample_step の影響を受けない．
        "vehicle_counts": vehicle_counts,
        # フレームごとの車両平均速度（台数重み，間引き前）．画面と LLM で共通の定義
        "frame_avg_speed": frame_avg_speed,
        # 速度分布（全フレーム・全車両の観測点，間引き前）
        "speed_histogram": speed_hist,
        # 流入・到着の累積台数（実イベント，時間軸 0〜tmax）
        "trip_series": trip_series,
        # 実行環境（内部用．エンベロープには載せない）．fast_path=False は §3.6 のフォールバック
        "_runtime": {"uxsim_version": UXSIM_VERSION, "backend": backend, "fast_path": rp.fast_path},
    }


def startup_selfcheck() -> None:
    """起動時に最小シナリオを 1 回流し，uxsim のバックエンドと高速経路の可否をログに出す．

    uxsim を更新して内部 API（§3.6）が変わっても例外にはならず，車両別ログの
    フォールバックで「遅くなるだけ」なので，起動時に必ず見える形にする．
    RISU_STARTUP_SELFCHECK=0 で無効化できる．
    """
    try:
        tiny = SimulationInput(
            name="selfcheck", tmax=60, deltan=5,
            nodes=[{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 500, "y": 0}],
            links=[{"name": "ab", "start": "a", "end": "b", "length": 500}],
            demands=[{"orig": "a", "dest": "b", "t_start": 0, "t_end": 30, "flow": 0.5}],
        )
        rt = run_uxsim(tiny)["_runtime"]
    except Exception as e:  # 起動は止めない
        print(f"[RISU] startup self-check failed: {e.__class__.__name__}: {e}")
        return
    if rt["backend"] == "cpp" and rt["fast_path"]:
        print(f"[RISU] uxsim {rt['uxsim_version']}: backend=cpp, vehicle-log fast path=on")
    else:
        print(f"[RISU] WARNING: uxsim {rt['uxsim_version']}: backend={rt['backend']}, "
              f"fast path=OFF -> 後処理が車両別ログのフォールバックになり数万台で数秒遅くなります．"
              f"CLAUDE.md §3.6 の内部 API を確認してください"
              + (f" ({RUNTIME_STATUS['fast_path_error']})" if RUNTIME_STATUS.get("fast_path_error") else ""))


def apply_link_geometries(result: dict, link_geometries: dict):
    """GeoJSON features のリンク座標を OSMnx の道路形状（多点 LineString）で置き換える．"""
    if not link_geometries:
        return
    for f in result.get("geojson", {}).get("features", []):
        name = f["properties"]["name"]
        if name in link_geometries:
            f["geometry"]["coordinates"] = link_geometries[name]

# ---- REST エンドポイント ----

# ──────────────────────────────────────────────
# シナリオサイズ上限（リソース保護）
# 環境変数で上書き可能．プラン制御を本番化する際はユーザーのプランを参照すること．
# ──────────────────────────────────────────────
MAX_NODES   = int(os.getenv("RISU_MAX_NODES",   "50000"))
MAX_LINKS   = int(os.getenv("RISU_MAX_LINKS",   "100000"))
MAX_DEMANDS = int(os.getenv("RISU_MAX_DEMANDS", "10000"))
MAX_TMAX    = int(os.getenv("RISU_MAX_TMAX",    "86400"))  # 24 時間

def validate_scenario_size(scenario: SimulationInput) -> None:
    """シナリオのサイズ上限チェック．超過時は 413 を返す．"""
    n_nodes   = len(scenario.nodes)
    n_links   = len(scenario.links)
    n_demands = len(scenario.demands)
    if n_nodes > MAX_NODES:
        raise HTTPException(413, detail=f"ノード数が上限を超過: {n_nodes} > {MAX_NODES}")
    if n_links > MAX_LINKS:
        raise HTTPException(413, detail=f"リンク数が上限を超過: {n_links} > {MAX_LINKS}")
    if n_demands > MAX_DEMANDS:
        raise HTTPException(413, detail=f"需要データ数が上限を超過: {n_demands} > {MAX_DEMANDS}")
    if scenario.tmax and scenario.tmax > MAX_TMAX:
        raise HTTPException(413, detail=f"シミュレーション時間が上限を超過: {scenario.tmax}s > {MAX_TMAX}s")
