"""
UXsim API + MCP Server
  - POST /simulate      : シミュレーション実行
  - GET  /results/{id}  : 結果取得
  - POST /chat          : LLM (Ollama) との対話 ＋ ツール呼び出し
  - GET  /mcp           : MCP エンドポイント (SSE)
"""

import asyncio
import csv
import gzip
import io
import json
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field, model_validator
from starlette.requests import Request

from uxsim_bridge import build_world

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
# .env の読込は，以下の os.environ.get より必ず先に行うこと
load_dotenv()

# LLM バックエンド: "mock" / "claude" / "ollama"
LLM_BACKEND = os.environ.get("LLM_BACKEND", "claude")

# Ollama 設定
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL    = "qwen2.5:3b"

# Claude 設定
CLAUDE_MODEL = "claude-sonnet-5"  # 高精度・ツール呼び出し安定（claude-sonnet-4 は 2026-06 廃止）
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ──────────────────────────────────────────────
# 再現性のためのバージョン情報 / DL JSON スキーマ
# ──────────────────────────────────────────────
RISU_SCHEMA_VERSION = "1.0"
RISU_VERSION = "0.1"
try:
    import uxsim as _uxsim_module
    UXSIM_VERSION = getattr(_uxsim_module, "__version__", "unknown")
except Exception:
    UXSIM_VERSION = "unknown"

# ──────────────────────────────────────────────
# グローバル状態（本番はRedis等に置き換える）
# ──────────────────────────────────────────────
results_store: dict[str, Any] = {}
executor = ThreadPoolExecutor(max_workers=4)
# 結果ストアに保持する件数の上限．超えたら古いものから捨てる（frames の numpy 列と
# gzip キャッシュで 1 件数十 MB になり得るため，無制限だとメモリ不足で落ちる）．
MAX_RESULTS = int(os.getenv("RISU_MAX_RESULTS", "30"))

# UXsim 実行タイムアウト（秒）．環境変数で上書き可能．
UXSIM_TIMEOUT_SEC = int(os.getenv("RISU_UXSIM_TIMEOUT", "120"))

# 待ち受けアドレス．**既定は 127.0.0.1（このマシンからのみ接続可）**．
# RISU は認証を持たないので，0.0.0.0 で待ち受けると同一 LAN の誰でも
#   - シミュレーションを実行できる（CPU を消費される）
#   - 保存済みの結果を読める
#   - /chat を叩いて **サーバー所有者の API キーで課金を発生させられる**
# 別マシンから使いたい場合だけ RISU_HOST=0.0.0.0 を明示すること．
RISU_HOST = os.getenv("RISU_HOST", "127.0.0.1")
RISU_PORT = int(os.getenv("RISU_PORT", "8001"))
# 開発時のオートリロード．既定は無効（利用者の環境ではプロセスが 2 つ起動し，
# ファイル変更のたびに再起動してしまうため）．
RISU_RELOAD = os.getenv("RISU_RELOAD", "").lower() in ("1", "true", "yes")

# 可視化フレーム数の上限（時刻方向の間引き）
MAX_FRAMES = int(os.getenv("RISU_MAX_FRAMES", "200"))
# 可視化フレームの総車両点数の上限．超過時は車両 ID を等間隔にサンプリングして
# 描画対象を減らす（0 で無効）．3,000,000 点 ≒ JSON 100MB / gzip 20MB が目安で，
# これを超えるとブラウザ側の JSON.parse とメモリが破綻する．
# リンク別速度 timeline と統計は間引き前の全点から計算するので影響を受けない．
MAX_FRAME_POINTS = int(os.getenv("RISU_MAX_FRAME_POINTS", "3000000"))
# /results の gzip レベル．level 1 は level 5 の約 3 倍速で，サイズ増は 1 割程度．
RESULTS_GZIP_LEVEL = int(os.getenv("RISU_RESULTS_GZIP_LEVEL", "1"))
# /results の zstd 圧縮レベル．gzip lv1 と比べて grid20 相当（2.4M 点 / JSON 64MB）で
# 16.1MB → 5.4MB，圧縮時間 0.47s → 0.07s．lv3 以上は縮みがほぼ頭打ちになる．
RESULTS_ZSTD_LEVEL = int(os.getenv("RISU_RESULTS_ZSTD_LEVEL", "3"))

# zstd は任意依存．入っていなければ gzip にフォールバックする（機能差はない）．
try:
    import zstandard as _zstd
except ImportError:
    _zstd = None


async def _run_uxsim_async(scenario) -> dict:
    """
    UXsim をタイムアウト付きで非同期実行するラッパー．
    エラーを HTTPException に分類してユーザー向けメッセージを返す．
    """
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, _run_uxsim, scenario),
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
        # UXsim の不正入力 or _validate_scenario_size
        raise HTTPException(400, detail=f"シナリオが不正です: {e}")
    except Exception as e:
        # その他の UXsim 内部エラー
        print(f"[RISU uxsim error] {e.__class__.__name__}: {e}")
        print(_tb.format_exc())
        raise HTTPException(
            500,
            detail=f"シミュレーション計算エラー: {e.__class__.__name__}",
        )


def _last_user_message_text(body) -> str | None:
    """ChatRequest body から最後のユーザーメッセージを抽出（source 記録用）．"""
    try:
        for m in reversed(body.messages):
            if getattr(m, "role", None) == "user":
                c = getattr(m, "content", None)
                if isinstance(c, str):
                    return c[:2000]  # 念のため上限
                return str(c)[:2000] if c else None
    except Exception:
        return None
    return None


def _store_sim(sim_id: str, result: dict, source: dict | None = None) -> None:
    """シミュレーション結果を results_store に保存し，再現性メタを付与する．

    result は _run_uxsim の戻り値（"_scenario" を含む）．
    source は呼び出し経路の由来を表す任意の辞書（省略時は manual）．
    """
    src = source or {"type": "manual"}
    result["_meta"] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": src,
    }
    results_store[sim_id] = result
    # 上限超過分を古い順に追い出す（dict は挿入順）
    while MAX_RESULTS > 0 and len(results_store) > MAX_RESULTS:
        old_id = next(iter(results_store))
        if old_id == sim_id:
            break
        results_store.pop(old_id, None)
        print(f"[RISU] results_store evicted {old_id} (limit {MAX_RESULTS})")


def _build_envelope(sim_id: str, *, include_result: bool = True) -> dict:
    """results_store の内部表現を DL/取得用の正規エンベロープに変換する．

    include_result=False の場合は再現に必要な scenario と meta のみを返す
    （Scenario DL ボタン用）．
    """
    raw = results_store[sim_id]
    meta = raw.get("_meta", {})
    envelope = {
        "risu_schema_version": RISU_SCHEMA_VERSION,
        "sim_id": sim_id,
        "created_at": meta.get("created_at"),
        "uxsim_version": UXSIM_VERSION,
        "risu_version": RISU_VERSION,
        "scenario": raw.get("_scenario", {}),
        "source": meta.get("source", {"type": "unknown"}),
    }
    if include_result:
        envelope["result"] = {
            "stats":        raw.get("stats", {}),
            "geojson":      raw.get("geojson"),
            "frames":       raw.get("frames"),
            "frame_times":  raw.get("frame_times"),
            "tmax":         raw.get("tmax"),
            # コンパクト列指向フレーム用メタ
            "link_names":   raw.get("link_names"),
            "frame_format": raw.get("frame_format"),
            # 信号現示メタデータ
            "signals":      raw.get("signals"),
            # 描画用フレームの車両サンプリング間隔（1 = 全車両）
            "vehicle_sample_step": raw.get("vehicle_sample_step", 1),
            # フレームごとの走行中台数（実台数 = プラトン数 × deltan．間引き前の全点から集計）
            "vehicle_counts": raw.get("vehicle_counts"),
        }
    return envelope

# ──────────────────────────────────────────────
# Pydantic スキーマ
# ──────────────────────────────────────────────
class NodeInput(BaseModel):
    name: str
    x: float
    y: float
    flow_capacity: float | None = Field(default=None, ge=0)
    signal: list[float] | None = None  # 信号現示の青時間リスト（秒）．例: [60,60] → 2現示各60秒

class LinkInput(BaseModel):
    name: str
    start: str
    end: str
    length: float = Field(gt=0)
    free_flow_speed: float = Field(default=20.0, gt=0)
    jam_density: float = Field(default=0.2, gt=0)
    number_of_lanes: int = Field(default=1, ge=1)
    capacity: float | None = Field(default=None, ge=0)  # リンク容量（台/s，リンク全体）．UXsim の capacity_out にマップ．
                                   # None なら FD（速度・密度・車線数）由来の容量のまま
    signal_group: int | None = None  # この進入リンクが青になる信号現示番号（0始まり）

class DemandInput(BaseModel):
    orig: str
    dest: str
    t_start: float = Field(ge=0)
    t_end: float = Field(ge=0)
    flow: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_interval(self):
        if self.t_end <= self.t_start:
            raise ValueError(
                f"需要 {self.orig}→{self.dest} の t_end ({self.t_end}) は "
                f"t_start ({self.t_start}) より大きい必要があります")
        return self

class SimulationInput(BaseModel):
    name: str = "sim"
    tmax: int = Field(default=3600, gt=0)
    # 車両集計単位（プラトンサイズ）．UXsim 内部の「1 車両」が deltan 台を表す．
    # フレームの ids の個数はプラトン数なので，台数として扱う箇所では deltan を掛けること
    # （_run_uxsim の vehicle_counts / _trip_stats / フロントの ACTIVE VEHICLES）．
    deltan: int = Field(default=5, ge=1)
    # 車頭時間（反応時間）秒．UXsim 既定 1.0 → 1 車線容量 ≈ 2,770 台/時（ffs 60km/h, kjam 0.2）．
    # 高速道路の実勢（1,800〜2,000 台/時/車線）に合わせるなら 1.5〜1.7．None なら UXsim 既定．
    reaction_time: float | None = Field(default=None, gt=0)
    # UXsim World の乱数シード（経路選択のノイズ・合流の優先順位に効く）．
    # None なら実行ごとに結果が変わる．条件比較・追試ではシナリオに持たせて全経路で維持する
    # （保存・rerun_simulation・GUI 再実行・scripts/run_scenario.py --emit）．
    random_seed: int | None = None
    nodes: list[NodeInput]
    links: list[LinkInput]
    demands: list[DemandInput]

    @model_validator(mode="after")
    def _normalize_and_validate(self):
        """重複・参照切れを UXsim に渡す前に検出する．

        - 完全一致の重複ノード/リンク（全属性が同じ）は黙って統合
          （「リンクごとにノード行を繰り返す」形式の CSV 等でよくあるため）
        - 同名で属性が異なる場合は，どの名前が問題かを列挙してエラー
        - リンク・需要が存在しないノードを参照している場合もエラー
          （UXsim の KeyError より分かりやすいメッセージにする）
        """
        # ── ノード重複 ──
        seen_nodes: dict[str, NodeInput] = {}
        uniq_nodes = []
        conflicts = []
        for n in self.nodes:
            prev = seen_nodes.get(n.name)
            if prev is None:
                seen_nodes[n.name] = n
                uniq_nodes.append(n)
            elif prev.model_dump() != n.model_dump():
                conflicts.append(n.name)
        if conflicts:
            raise ValueError(
                f"ノード名が重複しています（属性が異なるため自動統合できません）: "
                f"{sorted(set(conflicts))[:10]}．名前を一意にしてください．")
        self.nodes = uniq_nodes

        # ── リンク重複 ──
        seen_links: dict[str, LinkInput] = {}
        uniq_links = []
        conflicts = []
        for lk in self.links:
            prev = seen_links.get(lk.name)
            if prev is None:
                seen_links[lk.name] = lk
                uniq_links.append(lk)
            elif prev.model_dump() != lk.model_dump():
                conflicts.append(lk.name)
        if conflicts:
            raise ValueError(
                f"リンク名が重複しています（属性が異なるため自動統合できません）: "
                f"{sorted(set(conflicts))[:10]}．名前を一意にしてください．")
        self.links = uniq_links

        # ── 参照整合性 ──
        node_names = set(seen_nodes)
        missing = sorted({e for lk in self.links for e in (lk.start, lk.end)
                          if e not in node_names})
        if missing:
            raise ValueError(
                f"リンクが存在しないノードを参照しています: {missing[:10]}．"
                f"ノード定義を追加するか，リンクの start/end を修正してください．")
        missing_d = sorted({e for d in self.demands for e in (d.orig, d.dest)
                            if e not in node_names})
        if missing_d:
            raise ValueError(
                f"需要が存在しないノードを参照しています: {missing_d[:10]}．")
        return self

def _scenario_to_input(scenario: dict) -> SimulationInput:
    """dict → SimulationInput．Pydantic 検証エラーを 422 の平易なメッセージに変換する．

    （変換しないと global handler が 500「予期しないエラー」にしてしまい，
    重複ノード名など修正可能な問題がユーザーに伝わらない）
    """
    from pydantic import ValidationError
    try:
        return SimulationInput(**scenario)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            m = str(err.get("msg", ""))
            if m.startswith("Value error, "):
                m = m[len("Value error, "):]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msgs.append(f"{m}" + (f"（{loc}）" if loc else ""))
        raise HTTPException(422, detail="シナリオが不正です: " + " / ".join(msgs[:3]))


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatInput(BaseModel):
    messages: list[ChatMessage]
    # この会話でフロントに表示中のシミュレーションID．
    # LLM へのコンテキスト注入はこの sim だけを対象にする
    # （別会話・アップロード等で作られたグローバル最新の sim を流用しない）．
    last_sim_id: str | None = None

# ──────────────────────────────────────────────
# UXsim 実行（同期 → Executor で非同期化）
# ──────────────────────────────────────────────
def _trip_stats(W) -> tuple[int, int, float | None]:
    """basic_analysis / od_analysis と同じ数え方でトリップ統計を計算する．

    dest を持つ車両 × DELTAN がトリップ数，travel_time != -1 が完了，
    平均旅行時間は完了車両の travel_time の平均．
    cpp backend では CppVehicle のプロパティ（毎回 state 判定で C++ を複数回参照）を
    経由せず C++ オブジェクトを直接読む．
    """
    dn = W.DELTAN
    trip_all = 0
    trip_completed = 0
    tt_sum = 0.0
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
    avg_tt = (tt_sum * dn / trip_completed) if trip_completed else None
    return trip_all, trip_completed, avg_tt


def _select_frames(tk, max_frames: int):
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


def _collect_run_points(W, n_links: int, max_frames: int):
    """全車両のログから「run 状態かつ有効リンク上」の点を，可視化フレーム分だけ列として取り出す．

    戻り値: (kept, fidx, li, vid, x, v)
      kept: 昇順のフレーム時刻キー（0.1 秒精度の整数）
      fidx: 各点のフレーム index（kept への index），li: リンク index，
      vid: W.VEHICLES の登録順 index，x: リンク上位置，v: 速度

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
            kept, fidx = _select_frames(tk, max_frames)
            if kept.size and fidx.size and (fidx < 0).any():
                m = fidx >= 0
                idx, fidx = idx[m], fidx[m]
            # entry index → 車両 index（offsets[v] <= e < offsets[v+1]）．
            # W.VEHICLES の登録順 == C++ vehicle index 順（_register_new_cpp_vehicles）．
            vid = np.searchsorted(offsets, idx, side="right") - 1
            return (
                kept, fidx,
                link[idx].astype(np.int64),
                vid,
                np.asarray(flat["log_x"], dtype=np.float64)[idx],
                np.asarray(flat["log_v"], dtype=np.float64)[idx],
            )
        except Exception as e:  # 内部 API 変更時は遅い経路にフォールバック
            print(f"[RISU] flat vehicle log fast path unavailable ({e.__class__.__name__}: {e}); "
                  f"falling back to per-vehicle logs")

    link_idx_map = {lk.name: i for i, lk in enumerate(W.LINKS)}
    tk_parts, vid_parts, li_parts, x_parts, v_parts = [], [], [], [], []
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
        tk_parts.append(np.rint(log_t[sel] * 10.0).astype(np.int64))
        li_parts.append(li_sel)
        vid_parts.append(np.full(sel.size, vid, dtype=np.int64))
        x_parts.append(np.asarray(veh.log_x, dtype=np.float64)[sel])
        v_parts.append(np.asarray(veh.log_v, dtype=np.float64)[sel])
    if not tk_parts:
        e = np.empty(0, dtype=np.int64)
        return e, e, e, e, np.empty(0), np.empty(0)
    tk = np.concatenate(tk_parts)
    li, vid = np.concatenate(li_parts), np.concatenate(vid_parts)
    x, v = np.concatenate(x_parts), np.concatenate(v_parts)
    kept, fidx = _select_frames(tk, max_frames)
    m = fidx >= 0
    return kept, fidx[m], li[m], vid[m], x[m], v[m]


def _run_uxsim(scenario: SimulationInput) -> dict:
    # リソース保護: ユーザー入力 / LLM 経由のいずれでも上限を適用
    try:
        _validate_scenario_size(scenario)
    except NameError:
        # _validate_scenario_size 定義前に呼ばれた場合（起動順序保険）は素通し
        pass
    # 信号メタデータをシナリオから抽出（結果の signals メタデータ用）
    _orig_signal_nodes = [
        {"name": n.name, "x": float(n.x), "y": float(n.y),
         "signal": [float(p) for p in n.signal]}
        for n in scenario.nodes if n.signal and sum(n.signal) > 0
    ]
    _orig_signal_groups = {
        lk.name: int(lk.signal_group)
        for lk in scenario.links if lk.signal_group is not None
    }

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
    _trip_all, _trip_completed, _avg_tt = _trip_stats(W)

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
    kept, fidx, li_all, vid_all, x_all, v_all = _collect_run_points(W, _n_links, MAX_FRAMES)

    frames = {}
    frame_times = []
    link_timeline = {ln: [] for ln in link_names}
    vehicle_sample_step = 1
    vehicle_counts: list[int] = []

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
        vehicle_counts = (np.bincount(fidx, minlength=n_frames)[:n_frames]
                          * int(W.DELTAN)).tolist()

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

    post_elapsed = time.perf_counter() - t_post0

    # ---- 集計統計 ----
    # 座標ペアのユニーク数を確認（重複リンク検出）
    coord_pairs = set()
    for f in features:
        c = f["geometry"]["coordinates"]
        coord_pairs.add((c[0][0], c[0][1], c[1][0], c[1][1]))
    print(f"[RISU] Simulation done: {len(W.NODES)} nodes, {len(W.LINKS)} links, "
          f"{len(features)} GeoJSON features, {len(coord_pairs)} unique coord pairs "
          f"(exec={elapsed:.2f}s, post={post_elapsed:.2f}s)")
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

        for orig_node in _orig_signal_nodes:
            # この交差点に流入する signal_group 付きリンクのみ．
            # （全 signal_group リンクを渡すと，複数の信号交差点があるとき
            #   同じ信号機が交差点の数だけ重複描画されてしまう）
            groups = {
                lk_name: g for lk_name, g in _orig_signal_groups.items()
                if link_end_map.get(lk_name) == orig_node["name"]
            }
            phase_log = log_by_orig_id.get(orig_node["name"], [])
            signals.append({
                "node":     orig_node["name"],
                "x":        orig_node["x"],
                "y":        orig_node["y"],
                "phases":   orig_node["signal"],
                "groups":   groups,
                "phase_log": phase_log,    # UXsim 実 phase（秒単位）
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
    }


def _apply_link_geometries(result: dict, link_geometries: dict):
    """GeoJSON features のリンク座標を OSMnx の道路形状（多点 LineString）で置き換える．"""
    if not link_geometries:
        return
    for f in result.get("geojson", {}).get("features", []):
        name = f["properties"]["name"]
        if name in link_geometries:
            f["geometry"]["coordinates"] = link_geometries[name]


# ──────────────────────────────────────────────
# MCP サーバー定義
# ──────────────────────────────────────────────
mcp_server = Server("uxsim-mcp")

@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_simulation",
            description=(
                "UXsim 交通流シミュレーションを実行します．"
                "ノード・リンク・需要を指定してください．"
                "結果は GeoJSON 形式と集計統計で返されます．"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name":    {"type": "string",  "description": "シミュレーション名"},
                    "tmax":    {"type": "integer",  "description": "シミュレーション終了時刻（秒）"},
                    "deltan":  {"type": "integer",  "description": "車両集計単位（デフォルト5台）"},
                    "reaction_time": {"type": "number", "description": "車頭時間（秒）．省略で UXsim 既定"},
                    "random_seed":   {"type": "integer", "description": "乱数シード（省略で毎回変わる）"},
                    "nodes":   {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "x":    {"type": "number"},
                                "y":    {"type": "number"},
                                "flow_capacity": {"type": "number"},
                                "signal": {"type": "array", "items": {"type": "number"}, "description": "信号現示の青時間リスト（秒）"},
                            },
                            "required": ["name", "x", "y"],
                        },
                    },
                    "links": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name":             {"type": "string"},
                                "start":            {"type": "string"},
                                "end":              {"type": "string"},
                                "length":           {"type": "number"},
                                "free_flow_speed":  {"type": "number"},
                                "jam_density":      {"type": "number"},
                                "number_of_lanes":  {"type": "integer"},
                                "capacity":         {"type": "number", "description": "リンク容量（台/秒）"},
                                "signal_group":     {"type": "integer", "description": "信号現示番号（0始まり）"},
                            },
                            "required": ["name", "start", "end", "length"],
                        },
                    },
                    "demands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "orig":    {"type": "string"},
                                "dest":    {"type": "string"},
                                "t_start": {"type": "number"},
                                "t_end":   {"type": "number"},
                                "flow":    {"type": "number"},
                            },
                            "required": ["orig", "dest", "t_start", "t_end", "flow"],
                        },
                    },
                },
                "required": ["nodes", "links", "demands"],
            },
        ),
        Tool(
            name="get_result",
            description="以前実行したシミュレーションの結果を ID で取得します．",
            inputSchema={
                "type": "object",
                "properties": {
                    "simulation_id": {"type": "string", "description": "run_simulation が返した ID"},
                },
                "required": ["simulation_id"],
            },
        ),
    ]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "run_simulation":
        scenario, _info = _expand_run_simulation_args(arguments)  # grid / auto_demands 対応
        sim_input = SimulationInput(**scenario)
        result = await _run_uxsim_async(sim_input)
        sim_id = str(uuid.uuid4())[:8]
        _store_sim(sim_id, result, {"type": "llm", "via": "mcp"})
        summary = result["stats"]
        return [TextContent(
            type="text",
            text=(
                f"シミュレーション完了．ID: {sim_id}\n"
                f"総トリップ数: {summary['total_trips']}\n"
                f"完了トリップ数: {summary['completed_trips']}\n"
                f"平均旅行時間: {summary['average_travel_time_s']} 秒\n"
                f"計算時間: {summary['simulation_time_s']} 秒\n"
                f"結果取得: GET /results/{sim_id}"
            ),
        )]

    elif name == "get_result":
        sim_id = arguments["simulation_id"]
        if sim_id not in results_store:
            return [TextContent(type="text", text=f"ID {sim_id} の結果が見つかりません．")]
        stats = results_store[sim_id]["stats"]
        return [TextContent(type="text", text=json.dumps(stats, ensure_ascii=False))]

    return [TextContent(type="text", text=f"未知のツール: {name}")]


# ──────────────────────────────────────────────
# FastAPI アプリ
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    executor.shutdown(wait=False)

# orjson があれば高速な JSON シリアライズをデフォルトにする（/results は MB 級）
try:
    import orjson
    from fastapi.responses import ORJSONResponse as _DefaultJSONResponse
except ImportError:  # orjson 未インストール時は標準 JSON にフォールバック
    orjson = None
    from fastapi.responses import JSONResponse as _DefaultJSONResponse

app = FastAPI(title="RISU API", lifespan=lifespan,
              default_response_class=_DefaultJSONResponse)

# ──────────────────────────────────────────────
# グローバル例外ハンドラー
# ──────────────────────────────────────────────
import traceback as _tb

from fastapi.responses import JSONResponse as _JSONResp


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """
    HTTPException 以外の未捕捉例外を 500 に統一．
    詳細はサーバーログだけに残し，ユーザーには汎用メッセージ．
    """
    # HTTPException は FastAPI が処理するが，念のため
    if isinstance(exc, HTTPException):
        raise exc
    trace = _tb.format_exc()
    path = request.url.path if request else "?"
    print(f"[RISU unhandled] path={path} error={exc.__class__.__name__}: {exc}")
    print(trace)
    return _JSONResp(
        status_code=500,
        content={
            "detail": "予期しないエラーが発生しました．時間を空けて再度お試しください．",
            "error_id": id(exc) & 0xFFFFFF,  # 運用ログと突き合わせ可能な簡易 ID
        },
    )

_default_origins = "http://localhost:8001,http://127.0.0.1:8001"
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("RISU_ALLOWED_ORIGINS", _default_origins).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 大きな JSON レスポンス（/results は数 MB）を圧縮して転送量を ~90% 削減
from starlette.middleware.gzip import GZipMiddleware

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

# ──────────────────────────────────────────────
# HTML はキャッシュさせない（UI 更新時にブラウザが古い画面を出さないように．
# ETag 再検証で 304 が返るので転送コストはほぼゼロ）
# ──────────────────────────────────────────────
@app.middleware("http")
async def no_cache_html_mw(request, call_next):
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-cache"
    return response

# ──────────────────────────────────────────────
# アップロードサイズ上限（DoS 防止）
# ──────────────────────────────────────────────
MAX_UPLOAD_BYTES = int(os.getenv("RISU_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))  # 10MB
_UPLOAD_PATHS = ("/upload", "/gmns/import", "/import/osm")

@app.middleware("http")
async def limit_upload_mw(request, call_next):
    if request.method == "POST" and request.url.path in _UPLOAD_PATHS:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_BYTES:
            from fastapi.responses import JSONResponse
            mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            return JSONResponse(
                status_code=413,
                content={"detail": f"リクエストが大きすぎます（上限 {mb}MB）"},
            )
    return await call_next(request)

# ---- REST エンドポイント ----

# ──────────────────────────────────────────────
# シナリオサイズ上限（リソース保護）
# 環境変数で上書き可能．プラン制御を本番化する際はユーザーのプランを参照すること．
# ──────────────────────────────────────────────
MAX_NODES   = int(os.getenv("RISU_MAX_NODES",   "50000"))
MAX_LINKS   = int(os.getenv("RISU_MAX_LINKS",   "100000"))
MAX_DEMANDS = int(os.getenv("RISU_MAX_DEMANDS", "10000"))
MAX_TMAX    = int(os.getenv("RISU_MAX_TMAX",    "86400"))  # 24 時間

def _validate_scenario_size(scenario: SimulationInput) -> None:
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


@app.post("/simulate")
async def simulate(scenario: SimulationInput):
    """シミュレーションを実行して結果IDを返す"""
    _validate_scenario_size(scenario)
    result = await _run_uxsim_async(scenario)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {"type": "manual"})
    return {"id": sim_id, "stats": result["stats"]}

def _envelope_json_bytes(sim_id: str) -> bytes:
    """完全エンベロープを JSON バイト列に直列化する（frames の numpy 列も直接）．"""
    env = _build_envelope(sim_id, include_result=True)
    _res = env.get("result")
    if _res is not None and _res.get("frame_format") == "columnar_v2" and _res.get("frames"):
        # 送出時だけ columnar_v3（量子化＋差分符号化）に変換する．
        # results_store 側の配列は触らない（_get_simulation_data など
        # サーバー内の消費側は素の値を前提にしているため）．
        _res["frames"] = _encode_frames_v3(_res["frames"])
        _res["frame_format"] = "columnar_v3"
    if orjson is not None:
        return orjson.dumps(env, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)
    import numpy as np
    def _default(o):
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, np.generic):
            return o.item()
        raise TypeError(f"not serializable: {type(o)}")
    return json.dumps(env, ensure_ascii=False, default=_default).encode("utf-8")


def _encode_frames_v3(frames: dict) -> dict:
    """columnar_v2 の frames を送出用の columnar_v3 に変換する（新しい dict を返す）．

    JSON はテキストなので，`4500.0` のような冗長な表現がそのままバイト数になる．
    描画に不要な精度を落として整数にするだけで，grid20 相当で
    json 78.1MB → 53.5MB，gzip 21.1MB → 15.0MB，直列化 0.32s → 0.09s になる
    （ブラウザ側の JSON.parse も同じ割合で軽くなる）．

    v3 の符号化:
      ids    差分符号化した int32．フレーム内の ids は昇順なので差分は小さな値に収まる．
             復元は累積和（フロントの `_decodeFrame`）．
      xs, ys 1 m に丸めた int32．車両位置は基本的に alphas + リンク形状から決まり，
             xs/ys はリンク情報が無いときのフォールバックなので 1 m で十分．
      vs     0.1 m/s 単位の int16（元々 0.1 丸め済み）．
      alphas 0.001 単位の int16（リンク長 5 km でも 5 m 分解能）．元は 4 桁丸め．
      li     変更なし（link_names への index）．

    フロントは frame_format を見て v2 / v3 を切り替える．
    ダウンロード済みの古い JSON を読めるよう，v2 の読み込み経路は残してある．
    """
    import numpy as np

    out = {}
    for key, f in frames.items():
        ids = np.asarray(f["ids"])
        if ids.size:
            d = np.empty(ids.size, dtype=np.int64)
            d[0] = ids[0]
            if ids.size > 1:
                np.subtract(ids[1:], ids[:-1], out=d[1:])
            ids_enc = d.astype(np.int32)
        else:
            ids_enc = ids.astype(np.int32)
        out[key] = {
            "ids":    ids_enc,
            "xs":     np.rint(np.asarray(f["xs"])).astype(np.int32),
            "ys":     np.rint(np.asarray(f["ys"])).astype(np.int32),
            "vs":     np.rint(np.asarray(f["vs"]) * 10).astype(np.int16),
            "alphas": np.rint(np.asarray(f["alphas"]) * 1000).astype(np.int16),
            "li":     np.asarray(f["li"]),
        }
    return out


def _negotiate_encoding(accept_encoding: str) -> str:
    """Accept-Encoding から使う圧縮方式を選ぶ．返り値は "zstd" / "gzip" / "identity"．

    zstd は gzip より小さく・速いので優先する（grid20 相当で 16.1MB/0.47s → 5.4MB/0.07s）．
    ただし zstandard が未インストールなら gzip に落ちる．zstd 非対応のクライアントも
    Accept-Encoding に zstd を入れてこないので，そのまま gzip になる．
    """
    ae = (accept_encoding or "").lower()
    if _zstd is not None and "zstd" in ae:
        return "zstd"
    if "gzip" in ae:
        return "gzip"
    return "identity"


def _envelope_compressed_bytes(sim_id: str, encoding: str) -> bytes:
    """圧縮済みエンベロープ．結果は不変なので sim × 方式ごとに 1 回だけ作ってキャッシュする．

    キャッシュは `_enc_cache` に方式名をキーにして持つ．実際に要求された方式しか
    作らないので，1 種類しか使われない通常運用ではメモリは従来と変わらない
    （1 件数十 MB になり得るため，両方を先回りして作らない）．
    """
    raw = results_store[sim_id]
    cache = raw.get("_enc_cache")
    if cache is None:
        cache = raw["_enc_cache"] = {}
    blob = cache.get(encoding)
    if blob is not None:
        return blob

    t0 = time.perf_counter()
    data = _envelope_json_bytes(sim_id)
    t1 = time.perf_counter()
    if encoding == "zstd":
        blob = _zstd.ZstdCompressor(level=RESULTS_ZSTD_LEVEL).compress(data)
    else:
        blob = gzip.compress(data, compresslevel=RESULTS_GZIP_LEVEL)
    cache[encoding] = blob
    print(f"[RISU] /results/{sim_id}: json={len(data)/1e6:.1f}MB ({t1-t0:.2f}s) "
          f"{encoding}={len(blob)/1e6:.1f}MB ({time.perf_counter()-t1:.2f}s)")
    return blob


def _envelope_gzip_bytes(sim_id: str) -> bytes:
    """gzip 済みエンベロープ（後方互換のための薄いラッパー）．"""
    return _envelope_compressed_bytes(sim_id, "gzip")


@app.get("/results/{sim_id}")
async def get_results(sim_id: str, request: Request):
    """新スキーマ（risu_schema_version 1.0）の完全エンベロープを返す．

    数十 MB になり得るため，
      - jsonable_encoder（全要素の再帰変換）をバイパスして orjson で直接直列化し，
      - 直列化と圧縮はイベントループをブロックしないよう executor スレッドで行い，
      - 圧縮結果は sim × 方式ごとにキャッシュして 2 回目以降は即応答する．
    Content-Encoding を自前で付けるので GZipMiddleware は二重圧縮しない．

    圧縮方式は Accept-Encoding で交渉する（zstd 優先，無ければ gzip，それも無ければ非圧縮）．
    ブラウザ側の解凍は透過的なのでフロントの変更は要らない．
    """
    if sim_id not in results_store:
        raise HTTPException(404, detail="Result not found")
    loop = asyncio.get_event_loop()
    encoding = _negotiate_encoding(request.headers.get("accept-encoding", ""))
    if encoding == "identity":
        body = await loop.run_in_executor(executor, _envelope_json_bytes, sim_id)
        # ここでは Vary を付けない．Content-Encoding が無い応答には GZipMiddleware が
        # 素通し時に Vary: Accept-Encoding を足すので，自前で付けると重複する．
        # （圧縮済みの応答は middleware が触らないので，そちらは自前で付ける）
        return Response(body, media_type="application/json")
    body = await loop.run_in_executor(executor, _envelope_compressed_bytes, sim_id, encoding)
    return Response(body, media_type="application/json",
                    headers={"Content-Encoding": encoding, "Vary": "Accept-Encoding"})


@app.get("/results/{sim_id}/scenario")
async def get_results_scenario(sim_id: str):
    """再現用の軽量エンベロープ（scenario + meta のみ，result なし）を返す．"""
    if sim_id not in results_store:
        raise HTTPException(404, detail="Result not found")
    return _build_envelope(sim_id, include_result=False)

# ---- Chat エンドポイント ----
# LLM_BACKEND 環境変数で切り替え: "mock" / "claude" / "ollama"

SYSTEM_PROMPT = """私はRISUです．交通流シミュレーター UXsim を対話的に操作するアシスタントです．
一人称は常に「RISU」を使います．

【最重要ルール】
ユーザーがシミュレーションの実行を求めている場合（ネットワーク作成，渋滞シミュレーション，交通シナリオなど），
「実行します」「作成します」と言うだけでなく，必ずその場で run_simulation ツールを呼び出してください．
テキストだけで応答してツール呼び出しを省略することは絶対にしないでください．

【ツール呼び出しの指針】
- ノード座標の単位はメートル（例: x=0, y=0 ～ x=5000, y=5000）
- flow の単位は台/秒（例: 0.5 = 1秒に0.5台）
- 知らないネットワーク名を求められた場合でも，妥当な仮定でシナリオを構築してツールを呼び出すこと

【リンクの方向ルール（必須）】
- すべての link は有向リンク（start → end の一方向）として扱われる
- 双方向道路は必ず "2 本の並行有向リンク" として表現すること
- 推奨: A→B と B→A を両方明示的に作成する
- 一方通行を表現したい場合のみ片方向のリンクだけを作る

【容量・ボトルネックの表現】
- リンクの容量を明示したい場合は link の capacity（台/秒，リンク全体）を指定する
  例: {"name": "r1", "start": "A", "end": "B", "length": 2000, "capacity": 0.5}
- capacity 指定時は下流端の流出容量として作用し，渋滞の待ち行列はそのリンク上に形成される
- 【注意】リンクの終点ノードがそのまま目的地（demand の dest）の場合，capacity は作用しない
  （車両は境界を通過せず到着・消滅する）．ボトルネックを見せたい場合は
  その下流にもう 1 本リンクを置き，目的地を先に延ばすこと
- ユーザーが「容量 1800 台/時」のように台/時で言った場合は 3600 で割って台/秒に変換する（1800台/時 = 0.5台/秒）
- ノードの flow_capacity は「交差点の処理能力」を表す（全流入リンク合計の流出容量）．
  特定の道路のボトルネックは link capacity，交差点のボトルネックは node flow_capacity を使い分ける

【信号制御】
UXsim は交差点ノードに信号制御を設定できる．2 つのパラメータで記述する:

■ ノード側: signal パラメータ（各現示の青時間リスト）
  - signal: [60, 60]  → 2現示，各60秒青 → サイクル長120秒
  - signal: [30, 10, 50, 5]  → 4現示，サイクル長95秒
  - signal を省略 or null → 信号なし（常時通行可能）

■ リンク側: signal_group パラメータ（どの現示で青になるか）
  - signal_group: 0  → signal[0] の現示で通行可能
  - signal_group: 1  → signal[1] の現示で通行可能
  - signal_group を省略 → 信号に関係なく常時通行可能（退出リンクはこれ）

■ 使い方のルール
  - signal はノード（交差点）に設定する．signal_group は進入リンクに設定する
  - 退出リンク（交差点→外部）には signal_group を付けない（常時通行可能）
  - 同じ signal_group の進入リンクは同じ現示で同時に青になる
  - 典型例: 東西方向 signal_group=0，南北方向 signal_group=1

■ 4枝交差点の例（2現示，東西青/南北青）
  nodes:
    {"name": "I", "x": 0, "y": 0, "signal": [60, 60]}  ← サイクル120秒
  links (進入リンクのみ signal_group を設定):
    {"name": "EI", "start": "E", "end": "I", "signal_group": 0}  ← phase 0 で青
    {"name": "WI", "start": "W", "end": "I", "signal_group": 0}  ← phase 0 で青
    {"name": "SI", "start": "S", "end": "I", "signal_group": 1}  ← phase 1 で青
    {"name": "NI", "start": "N", "end": "I", "signal_group": 1}  ← phase 1 で青
    {"name": "IE", "start": "I", "end": "E"}  ← 退出: signal_group なし
    {"name": "IW", "start": "I", "end": "W"}  ← 退出: signal_group なし

■ ユーザーが「信号をつけて」「信号制御して」と言った場合
  - まず交差点ノードに signal パラメータを追加する
  - 進入リンクに signal_group を割り当てる（対向方向は同じ group）
  - 青時間はユーザーの指示に従う．指示がなければ均等（例: [60, 60]）にする

【既存ネットワークの修正・再実行（最重要ルール）】
- 直前のシミュレーション（sim_id は【現在のコンテキスト】に記載）のネットワークを
  修正して再実行する場合は，必ず rerun_simulation を使う
- rerun_simulation はサーバーに保存されたシナリオへ「差分命令」だけを適用する．
  ネットワーク全体（nodes/links）を run_simulation で再送してはいけない．
  特に OSM 取込・ファイルアップロード由来の大規模ネットワークでは，
  再送するとサイズ超過で必ず失敗する
- run_simulation を使うのは「ゼロから新しいネットワークを設計する」ときだけ
- 格子状（グリッド）ネットワークは nodes/links を列挙せず run_simulation の grid テンプレート
  {"grid":{"nx":5,"ny":5,"spacing":500}} を使う（サーバーが展開する．ノード名 n{i}_{j}，
  リンク名 n0_0-n1_0 形式）．需要も auto_demands（random / boundary）で生成できる．
  例: run_simulation({"grid":{"nx":5,"ny":5,"spacing":500},"auto_demands":{"strategy":"boundary"},"tmax":3600})
- シナリオ比較（容量変更前後など）も rerun_simulation を複数回呼べばよい．
  各実行の sim_id が返るので，get_simulation_data でそれぞれの結果を取得して比較する
- シミュレーションは確率的（経路選択ノイズ・合流順）で，同じ入力でも実行ごとに結果が変わる．
  条件比較や追試では random_seed を固定する（run_simulation の random_seed，または
  rerun_simulation の {"action":"set_params","random_seed":42}）．seed は保存シナリオに残るので，
  同じ base から派生させる限り以降の rerun でも同じ seed が使われる．
  差の解釈では「シード違いによる揺らぎ」の可能性も述べる
- 台数の単位: 統計と network_vehicle_count は実台数（deltan 換算済み）．
  「1 プラトン = deltan 台」なので，フレームの点数をそのまま台数と呼ばないこと
- ネットワークの中身（ノード名・リンク名・構造）が必要なときは get_network_info で照会する:
  - まず include="summary" で規模・座標範囲・次数上位ノード・名前のサンプルを把握
  - 特定の名前が必要なら include="nodes"/"links" + name_contains / limit / offset で絞り込む
  - 全件を取得しようとしないこと（上限 200 件/回．要約と絞り込みで足りるはず）
- 「ランダムに OD を作って」「需要を自動生成して」と言われたら，ノード名を調べる必要はない．
  rerun_simulation の generate_demands アクションでサーバー側に生成させる:
  {"action":"generate_demands","strategy":"random","n_pairs":10,"flow_per_pair":0.2,"clear_existing":true}
  周縁ノード間の現実的な通過交通なら strategy="boundary" を使う
- 「時差出勤」「ピークを分散」と言われたら rerun_simulation の shift_demands アクションを使う:
  {"action":"shift_demands","t_from":3600,"t_to":10800,"fraction":0.3,"shift_s":-3600}
  （時刻はシミュレーション開始からの秒．前倒しと後ろ倒しに分けるなら 2 つ並べる）

【OSM（OpenStreetMap）連携】
- ユーザーが実在の地名・場所・駅名・ランドマーク等を言及した場合，import_osm_network ツールを使う
  例: 「東京駅周辺」「渋谷の道路」「大阪城公園あたり」「新宿駅」
- import_osm_network は地名を自動でジオコーディングし，道路ネットワークをダウンロードする
- distance_m パラメータで範囲を制御する（デフォルト500m）．ユーザーの要望に応じて調整する
  - 「広い範囲」→ 1000〜2000m，「狭い範囲」「駅前だけ」→ 200〜300m
- road_types パラメータで取得する道路の種類を制御する:
  - major: 高速道路・国道級のみ（都市間・広域シミュレーション向け）
  - arterial: 幹線道路まで（都市スケールの標準）
  - drive: 一般車道（デフォルト．住宅街の道路含む，サービス道路除外）
  - all: 全車道（駐車場内通路等も含む．最も細かいが最も重い）
- 【重要】distance_m が 1000 以上のときは road_types を "arterial" か "major" にすること．
  細街路込みで広範囲を取得するとノード数が数千を超え，計算がタイムアウトする．
  ユーザーが「主要道路」「幹線道路」「大きい道路だけ」と言った場合も major / arterial を使う
- 取得後は自動的にダミー需要でシミュレーションが実行される
- ユーザーが範囲や需要を調整したい場合は対話的にヒアリングしてよい
- 結果は地図として表示される

【グラフ機能】
- ユーザーがグラフ・チャート・分析・可視化を求めた場合，get_simulation_data ツールを呼んでデータを取得する
- データを受け取ったら，ユーザーの要望に合った Chart.js 設定を ```chart ... ``` コードブロックで出力する
- Chart.js設定は完全なJSON: {type, data: {labels, datasets}, options} 形式
- 【必須】get_simulation_data の配列は書き写さず参照で指定する（出力トークン節約）:
  {"$data":"time_labels"} / {"$data":"network_avg_speed"} / {"$data":"network_vehicle_count"} /
  {"$data":"link_speeds.<リンク名>"} / {"$data":"speed_histogram.labels"} / {"$data":"speed_histogram.counts"}
  複数の sim を比較するときは {"$data":"network_avg_speed","sim_id":"xxxxxxxx"} と sim_id を付ける
- 例: ```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},"datasets":[{"label":"平均速度","data":{"$data":"network_avg_speed"},"borderColor":"#0d9668"}]}}\n```
- 差分・比率など加工した値が必要なときだけ数値を直接書く
- どんな種類のグラフでも自由に作成できる（折れ線，棒，散布図，レーダー等）
- 複数のグラフを返す場合は複数の ```chart ブロックを使う
- データの加工・計算・フィルタリングは自由に行ってよい（平均，差分，比率，累積など）

【ファイル添付】
- ユーザーがCSV/JSONファイルを添付した場合，メッセージ内にパース結果が含まれる
- RISU CSV形式のパース結果にはnodes/links/demandsがJSON形式で含まれるので，そのまま run_simulation に渡すこと
- ユーザーのメッセージも確認し，パラメータ変更の要望があれば反映すること

【結果の説明】
結果を説明する際は，渋滞箇所・平均旅行時間・完了率などを分かりやすく日本語で解説してください．
常に「RISUが〜しました」のように一人称で話してください．"""

# ── モック用シナリオ定義 ──
MOCK_SCENARIOS = {
    "bottleneck": {
        "scenario": {
            "name": "bottleneck",
            "tmax": 2000,
            "deltan": 5,
            "nodes": [
                {"name": "start", "x": 0, "y": 0},
                {"name": "neck",  "x": 5000, "y": 0, "flow_capacity": 0.4},
                {"name": "goal",  "x": 7500, "y": 0},
            ],
            "links": [
                {"name": "road1", "start": "start", "end": "neck",  "length": 5000},
                {"name": "road2", "start": "neck",  "end": "goal",  "length": 2500, "free_flow_speed": 10},
            ],
            "demands": [
                {"orig": "start", "dest": "goal", "t_start": 0, "t_end": 600, "flow": 0.8},
            ],
        },
        "description": (
            "ボトルネック道路のシミュレーションを実行しました．\n\n"
            "【シナリオ】\n"
            "・start → neck → goal の3ノード直線道路\n"
            "・neck ノードの流出容量を 0.4 台/秒に制限（ボトルネック）\n"
            "・road2 の自由流速度を 10 m/s に低下\n"
            "・0〜600秒に 0.8 台/秒の需要を投入\n\n"
            "【結果の見方】\n"
            "・タイムスライダーを動かすと，時間経過に伴う速度変化が確認できます\n"
            "・緑=自由流（スムーズ），赤=渋滞を示します\n"
            "・ボトルネック手前（road1）で渋滞が発生し，速度が低下している様子が観察できます"
        ),
    },
    "grid": {
        "scenario": {
            "name": "grid_3x3",
            "tmax": 3000,
            "deltan": 5,
            "nodes": [
                {"name": "n00", "x": 0,    "y": 0},
                {"name": "n10", "x": 2000, "y": 0},
                {"name": "n20", "x": 4000, "y": 0},
                {"name": "n01", "x": 0,    "y": 2000},
                {"name": "n11", "x": 2000, "y": 2000},
                {"name": "n21", "x": 4000, "y": 2000},
                {"name": "n02", "x": 0,    "y": 4000},
                {"name": "n12", "x": 2000, "y": 4000},
                {"name": "n22", "x": 4000, "y": 4000},
            ],
            "links": [
                # 水平方向（→）
                {"name": "h00",  "start": "n00", "end": "n10", "length": 2000},
                {"name": "h10",  "start": "n10", "end": "n20", "length": 2000},
                {"name": "h01",  "start": "n01", "end": "n11", "length": 2000},
                {"name": "h11",  "start": "n11", "end": "n21", "length": 2000},
                {"name": "h02",  "start": "n02", "end": "n12", "length": 2000},
                {"name": "h12",  "start": "n12", "end": "n22", "length": 2000},
                # 水平方向（←）
                {"name": "h00r", "start": "n10", "end": "n00", "length": 2000},
                {"name": "h10r", "start": "n20", "end": "n10", "length": 2000},
                {"name": "h01r", "start": "n11", "end": "n01", "length": 2000},
                {"name": "h11r", "start": "n21", "end": "n11", "length": 2000},
                {"name": "h02r", "start": "n12", "end": "n02", "length": 2000},
                {"name": "h12r", "start": "n22", "end": "n12", "length": 2000},
                # 垂直方向（↑）
                {"name": "v00",  "start": "n00", "end": "n01", "length": 2000},
                {"name": "v10",  "start": "n10", "end": "n11", "length": 2000},
                {"name": "v20",  "start": "n20", "end": "n21", "length": 2000},
                {"name": "v01",  "start": "n01", "end": "n02", "length": 2000},
                {"name": "v11",  "start": "n11", "end": "n12", "length": 2000},
                {"name": "v21",  "start": "n21", "end": "n22", "length": 2000},
                # 垂直方向（↓）
                {"name": "v00r", "start": "n01", "end": "n00", "length": 2000},
                {"name": "v10r", "start": "n11", "end": "n10", "length": 2000},
                {"name": "v20r", "start": "n21", "end": "n20", "length": 2000},
                {"name": "v01r", "start": "n02", "end": "n01", "length": 2000},
                {"name": "v11r", "start": "n12", "end": "n11", "length": 2000},
                {"name": "v21r", "start": "n22", "end": "n21", "length": 2000},
            ],
            "demands": [
                {"orig": "n00", "dest": "n22", "t_start": 0, "t_end": 800, "flow": 0.5},
                {"orig": "n02", "dest": "n20", "t_start": 0, "t_end": 800, "flow": 0.3},
                {"orig": "n20", "dest": "n02", "t_start": 200, "t_end": 600, "flow": 0.4},
            ],
        },
        "description": (
            "3×3 グリッドネットワークのシミュレーションを実行しました．\n\n"
            "【シナリオ】\n"
            "・9ノード（3×3格子），24リンク（全道路双方向）のグリッド道路網\n"
            "・3つの OD 需要: 左下→右上，左上→右下，右下→左上\n"
            "・交差点（n11）付近で交通が集中\n\n"
            "【結果の見方】\n"
            "・双方向リンクは円弧で表示されます（進行方向の右側に膨らむ）\n"
            "・中央の交差点付近のリンクが赤くなり，混雑が確認できます\n"
            "・タイムスライダーで需要投入前後の変化を観察してください"
        ),
    },
    "default": {
        "scenario": {
            "name": "simple_road",
            "tmax": 1500,
            "deltan": 5,
            "nodes": [
                {"name": "A", "x": 0, "y": 0},
                {"name": "B", "x": 3000, "y": 1000},
                {"name": "C", "x": 6000, "y": 0},
            ],
            "links": [
                {"name": "AB", "start": "A", "end": "B", "length": 3200},
                {"name": "BC", "start": "B", "end": "C", "length": 3200},
            ],
            "demands": [
                {"orig": "A", "dest": "C", "t_start": 0, "t_end": 500, "flow": 0.6},
            ],
        },
        "description": (
            "シミュレーションを実行しました．\n\n"
            "【シナリオ】\n"
            "・A → B → C の3ノード道路\n"
            "・0〜500秒に 0.6 台/秒の需要\n\n"
            "【結果の見方】\n"
            "・タイムスライダーで速度の時間変化を確認できます\n"
            "・緑=スムーズ，赤=渋滞です"
        ),
    },
}

def _mock_llm_response(user_text: str) -> dict:
    """キーワードマッチでシナリオ選択 or テキスト応答を返す"""
    text = user_text.lower()

    # シミュレーション不要な質問
    greetings = ["hello", "こんにちは", "はじめ", "ありがとう", "thanks"]
    if any(g in text for g in greetings):
        return {"content": "こんにちは！交通流シミュレーター UXsim のアシスタントです．\n\nシミュレーションしたいシナリオを入力してください．例えば：\n・「ボトルネック道路を作って」\n・「3×3 グリッドネットワーク」\n・「渋滞をシミュレーションして」\n\nお気軽にどうぞ！", "scenario_key": None}

    info_keywords = ["教えて", "説明", "とは", "仕組み", "条件"]
    if any(k in text for k in info_keywords) and not any(k in text for k in ["作って", "シミュレ", "実行"]):
        return {"content": "交通流の渋滞は，道路の容量を超える交通需要が発生したときに起こります．\n\n主な要因：\n・ボトルネック（車線減少，合流部）\n・交通需要の集中（ラッシュアワー）\n・信号制御の不適切な設定\n\n実際にシミュレーションで確認してみましょう！\n「ボトルネック道路を作って」と入力してみてください．", "scenario_key": None}

    # シナリオ選択
    if any(k in text for k in ["ボトルネック", "bottleneck", "単純", "渋滞"]):
        return {"content": None, "scenario_key": "bottleneck"}
    if any(k in text for k in ["グリッド", "grid", "格子", "3×3", "3x3"]):
        return {"content": None, "scenario_key": "grid"}

    # デフォルト: シミュレーション実行
    return {"content": None, "scenario_key": "default"}


@app.post("/chat")
async def chat(body: ChatInput):
    """チャットエンドポイント（mock / claude / ollama）
    フロントエンドは常に JSON で送信．
    ファイル添付時はフロントでテキスト読み取りしてメッセージに含める．
    Claude バックエンド時は SSE ストリーミングで進捗を返す．
    """
    # 空メッセージ防止
    for i, m in enumerate(body.messages):
        if not m.content or not m.content.strip():
            body.messages[i] = ChatMessage(role=m.role, content="(空メッセージ)")

    last_msg = body.messages[-1].content if body.messages else ""

    if LLM_BACKEND == "mock":
        return await _chat_mock(last_msg)
    elif LLM_BACKEND == "claude":
        return await _chat_claude_stream(body)
    else:
        return await _chat_ollama(body)


async def _chat_mock(user_text: str):
    """モック LLM: キーワードマッチでシナリオを選択し，実際の UXsim を実行"""
    mock = _mock_llm_response(user_text)

    # テキスト応答のみ（シミュレーション不要）
    if mock["scenario_key"] is None:
        return {"role": "assistant", "content": mock["content"], "sim_id": None}

    # シミュレーション実行
    scenario_data = MOCK_SCENARIOS[mock["scenario_key"]]
    sim_input = SimulationInput(**scenario_data["scenario"])
    result = await _run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {
        "type": "llm",
        "llm_backend": "mock",
        "scenario_key": mock["scenario_key"],
        "llm_user_message": user_text,
    })

    stats = result["stats"]
    description = scenario_data["description"]
    summary = (
        f"{description}\n\n"
        f"【統計情報】\n"
        f"・総トリップ数: {stats['total_trips']}\n"
        f"・完了トリップ数: {stats['completed_trips']}\n"
        f"・平均旅行時間: {stats['average_travel_time_s']} 秒\n"
        f"・計算時間: {stats['simulation_time_s']} 秒"
    )

    return {"role": "assistant", "content": summary, "sim_id": sim_id}


# ── シミュレーションデータ集計（LLM に渡す） ──
def _get_simulation_data(sim_id: str, points: int = 30, max_links: int = 20) -> dict | None:
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
        if speeds.size:
            net_avg_speed.append(round(float(speeds.mean()), 1))
            sampled_speeds.append(speeds)
        else:
            net_avg_speed.append(None)

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

    # 速度分布（全期間，サンプル時刻の全車両点）
    speed_hist = {"labels": [], "counts": []}
    if sampled_speeds:
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
    data["vehicle_count_note"] = (
        f"network_vehicle_count は実台数（deltan={deltan} 換算済み，間引き前の全車両から集計）．"
        f"speed_histogram の counts はプラトン（{deltan} 台単位）のサンプル数．"
    )
    if sample_step > 1:
        data["vehicle_sample_note"] = (
            f"描画用フレームは {sample_step} プラトンに 1 つをサンプリングしている．"
            f"network_vehicle_count はその影響を受けない．"
        )
    return data


# ──────────────────────────────────────────────
# シナリオパッチエンジン（rerun_simulation 用）
# 大規模ネットワークを LLM に往復させず，保存済みシナリオへの
# 「小さな差分命令」だけで修正・再実行できるようにする．
# ──────────────────────────────────────────────
import copy as _copy


def _mod_match_indices(items: list[dict], mod: dict, kind: str) -> list[int]:
    """modification の対象指定（names / name_contains / all）から index 群を返す"""
    if mod.get("all"):
        return list(range(len(items)))
    if "names" in mod:
        wanted = list(mod["names"]) if isinstance(mod["names"], list) else [mod["names"]]
        wanted_set = set(map(str, wanted))
        idxs = [i for i, it in enumerate(items) if str(it.get("name")) in wanted_set]
        found = {str(items[i]["name"]) for i in idxs}
        missing = sorted(wanted_set - found)
        if missing:
            raise ValueError(f"{kind} が見つかりません: {missing}")
        return idxs
    if "name_contains" in mod:
        sub = str(mod["name_contains"])
        idxs = [i for i, it in enumerate(items) if sub in str(it.get("name", ""))]
        if not idxs:
            raise ValueError(f"名前に「{sub}」を含む {kind} がありません")
        return idxs
    raise ValueError(f"{kind} の対象指定が必要です（names / name_contains / all のいずれか）")


_LINK_SET_FIELDS = {"capacity", "free_flow_speed", "number_of_lanes",
                    "jam_density", "signal_group", "length"}
_NODE_SET_FIELDS = {"signal", "flow_capacity", "x", "y"}
_SCENARIO_PARAM_FIELDS = {"tmax", "deltan", "reaction_time", "random_seed"}
_DEMAND_SET_FIELDS = {"flow", "t_start", "t_end"}


def _apply_modifications(scenario: dict, mods: list[dict]) -> tuple[dict, list[str]]:
    """保存済みシナリオ dict に modification 命令列を適用する．

    戻り値: (新しいシナリオ dict, 適用ログ)．不正な命令は ValueError．
    """
    sc = _copy.deepcopy(scenario)
    sc.setdefault("nodes", []); sc.setdefault("links", []); sc.setdefault("demands", [])
    applied: list[str] = []

    for mi, mod in enumerate(mods):
        if not isinstance(mod, dict) or "action" not in mod:
            raise ValueError(f"modifications[{mi}]: action が必要です")
        action = mod["action"]

        if action == "update_links":
            idxs = _mod_match_indices(sc["links"], mod, "リンク")
            sets = mod.get("set") or {}
            bad = set(sets) - _LINK_SET_FIELDS
            if bad:
                raise ValueError(f"update_links の set に未対応のフィールド: {sorted(bad)}（対応: {sorted(_LINK_SET_FIELDS)}）")
            if not sets:
                raise ValueError("update_links には set が必要です")
            for i in idxs:
                sc["links"][i].update(sets)
            applied.append(f"update_links: {len(idxs)} 本に {sorted(sets)} を設定")

        elif action == "update_nodes":
            idxs = _mod_match_indices(sc["nodes"], mod, "ノード")
            sets = mod.get("set") or {}
            bad = set(sets) - _NODE_SET_FIELDS
            if bad:
                raise ValueError(f"update_nodes の set に未対応のフィールド: {sorted(bad)}（対応: {sorted(_NODE_SET_FIELDS)}）")
            if not sets:
                raise ValueError("update_nodes には set が必要です")
            for i in idxs:
                sc["nodes"][i].update(sets)
            applied.append(f"update_nodes: {len(idxs)} 個に {sorted(sets)} を設定")

        elif action == "update_demands":
            orig, dest = mod.get("orig"), mod.get("dest")
            idxs = [i for i, d in enumerate(sc["demands"])
                    if (orig is None or d.get("orig") == orig)
                    and (dest is None or d.get("dest") == dest)]
            if not idxs:
                raise ValueError(f"該当する需要がありません（orig={orig}, dest={dest}）")
            sets = mod.get("set") or {}
            bad = set(sets) - _DEMAND_SET_FIELDS
            if bad:
                raise ValueError(f"update_demands の set に未対応のフィールド: {sorted(bad)}")
            scale = mod.get("scale_flow")
            for i in idxs:
                sc["demands"][i].update(sets)
                if scale is not None:
                    sc["demands"][i]["flow"] = round(float(sc["demands"][i]["flow"]) * float(scale), 4)
            desc = []
            if sets: desc.append(f"{sorted(sets)} を設定")
            if scale is not None: desc.append(f"flow を {scale} 倍")
            applied.append(f"update_demands: {len(idxs)} 件に " + "，".join(desc))

        elif action == "add_node":
            node = mod.get("node")
            if not isinstance(node, dict) or "name" not in node:
                raise ValueError("add_node には node（name, x, y ...）が必要です")
            if any(n.get("name") == node["name"] for n in sc["nodes"]):
                raise ValueError(f"ノード名が重複: {node['name']}")
            sc["nodes"].append(node)
            applied.append(f"add_node: {node['name']}")

        elif action == "add_link":
            link = mod.get("link")
            if not isinstance(link, dict) or "name" not in link:
                raise ValueError("add_link には link（name, start, end, length ...）が必要です")
            if any(l.get("name") == link["name"] for l in sc["links"]):
                raise ValueError(f"リンク名が重複: {link['name']}")
            sc["links"].append(link)
            applied.append(f"add_link: {link['name']}")

        elif action == "add_demand":
            demand = mod.get("demand")
            if not isinstance(demand, dict):
                raise ValueError("add_demand には demand（orig, dest, t_start, t_end, flow）が必要です")
            sc["demands"].append(demand)
            applied.append(f"add_demand: {demand.get('orig')}→{demand.get('dest')}")

        elif action == "remove_links":
            idxs = set(_mod_match_indices(sc["links"], mod, "リンク"))
            sc["links"] = [l for i, l in enumerate(sc["links"]) if i not in idxs]
            applied.append(f"remove_links: {len(idxs)} 本を削除")

        elif action == "remove_nodes":
            idxs = set(_mod_match_indices(sc["nodes"], mod, "ノード"))
            names = {sc["nodes"][i]["name"] for i in idxs}
            sc["nodes"] = [n for i, n in enumerate(sc["nodes"]) if i not in idxs]
            n_links_before = len(sc["links"]); n_dem_before = len(sc["demands"])
            sc["links"] = [l for l in sc["links"] if l.get("start") not in names and l.get("end") not in names]
            sc["demands"] = [d for d in sc["demands"] if d.get("orig") not in names and d.get("dest") not in names]
            applied.append(
                f"remove_nodes: {len(idxs)} 個を削除（接続リンク {n_links_before - len(sc['links'])} 本，"
                f"需要 {n_dem_before - len(sc['demands'])} 件も削除）")

        elif action == "remove_demands":
            orig, dest = mod.get("orig"), mod.get("dest")
            if orig is None and dest is None and not mod.get("all"):
                raise ValueError("remove_demands には orig / dest / all のいずれかが必要です")
            before = len(sc["demands"])
            sc["demands"] = [d for d in sc["demands"]
                             if not ((orig is None or d.get("orig") == orig)
                                     and (dest is None or d.get("dest") == dest))] \
                if not mod.get("all") else []
            applied.append(f"remove_demands: {before - len(sc['demands'])} 件を削除")

        elif action == "set_tmax":
            sc["tmax"] = int(mod.get("tmax", sc.get("tmax", 3600)))
            applied.append(f"set_tmax: {sc['tmax']}s")

        elif action == "set_params":
            # シナリオ全体のパラメータ（tmax / deltan / reaction_time / random_seed）．
            # 値の妥当性は後段の SimulationInput で検証される．
            sets = {k: mod[k] for k in _SCENARIO_PARAM_FIELDS if k in mod}
            bad = set(mod) - _SCENARIO_PARAM_FIELDS - {"action"}
            if bad:
                raise ValueError(f"set_params に未対応のフィールド: {sorted(bad)}"
                                 f"（対応: {sorted(_SCENARIO_PARAM_FIELDS)}）")
            if not sets:
                raise ValueError(f"set_params には {sorted(_SCENARIO_PARAM_FIELDS)} のいずれかが必要です")
            for k, v in sets.items():
                if v is None:
                    sc.pop(k, None)   # None = 既定に戻す（reaction_time / random_seed）
                else:
                    sc[k] = v
            applied.append("set_params: " + ", ".join(f"{k}={v}" for k, v in sets.items()))

        elif action == "generate_demands":
            # サーバー側で OD 需要を自動生成する．LLM がノード名を列挙する
            # 必要がないため，大規模ネットワークでも「ランダムに OD を生成」の
            # ような指示に対応できる．
            strategy = mod.get("strategy", "random")
            if len(sc["nodes"]) < 2:
                raise ValueError("generate_demands にはノードが 2 つ以上必要です")
            if mod.get("clear_existing"):
                n_cleared = len(sc["demands"])
                sc["demands"] = []
            else:
                n_cleared = None
            try:
                new_demands = _generate_demands_spec(sc["nodes"], sc["links"], mod,
                                                     int(sc.get("tmax", 3600)))
            except ValueError as e:
                raise ValueError(f"generate_demands の {e}") from e
            sc["demands"].extend(new_demands)
            msg = f"generate_demands({strategy}): {len(new_demands)} 件を生成"
            if n_cleared is not None:
                msg += f"（既存 {n_cleared} 件はクリア）"
            applied.append(msg)

        elif action == "shift_demands":
            # 需要パターンの時間シフト（時差出勤・ピークカット）．
            # 時間帯 [t_from, t_to) に入る需要行の flow を fraction だけ減らし，
            # 同じ OD で t_start/t_end を shift_s ずらした行を追加する．
            # 例: 7-9 時の 30% を 1 時間前倒し → {"t_from":3600,"t_to":10800,"fraction":0.3,"shift_s":-3600}
            t_from = float(mod.get("t_from", 0)); t_to = float(mod.get("t_to", sc.get("tmax", 3600)))
            frac = float(mod.get("fraction", 0.3)); shift_s = float(mod.get("shift_s", -3600))
            if not (0 < frac <= 1):
                raise ValueError("shift_demands の fraction は 0 より大きく 1 以下")
            orig, dest = mod.get("orig"), mod.get("dest")
            moved = 0.0; n_rows = 0; new_rows = []
            for d in sc["demands"]:
                if orig is not None and d.get("orig") != orig: continue
                if dest is not None and d.get("dest") != dest: continue
                if float(d["t_start"]) < t_from or float(d["t_end"]) > t_to: continue
                part = round(float(d["flow"]) * frac, 4)
                if part <= 0: continue
                d["flow"] = round(float(d["flow"]) - part, 4)
                ns, ne = float(d["t_start"]) + shift_s, float(d["t_end"]) + shift_s
                if ns < 0:
                    raise ValueError(f"shift_demands: シフト後の開始時刻が負になります（{ns}s）")
                new_rows.append({"orig": d["orig"], "dest": d["dest"], "t_start": ns, "t_end": ne, "flow": part})
                moved += part * (float(d["t_end"]) - float(d["t_start"])); n_rows += 1
            if n_rows == 0:
                raise ValueError(f"shift_demands: 時間帯 [{t_from}, {t_to}) に該当する需要がありません")
            sc["demands"].extend(new_rows)
            applied.append(f"shift_demands: {n_rows} 行の {frac:.0%}（約 {moved:,.0f} 台）を {shift_s:+.0f}s 移動")

        else:
            raise ValueError(
                f"未対応の action: {action}（対応: update_links / update_nodes / update_demands / "
                "add_node / add_link / add_demand / remove_links / remove_nodes / remove_demands / "
                "generate_demands / shift_demands / set_tmax）")

    return sc, applied


def _handle_get_network_info(fn_args: dict) -> tuple[str, bool]:
    """get_network_info ツールの共通ハンドラ．(content, is_error) を返す．

    LLM がネットワークの中身（ノード名・リンク名・構造）を必要な分だけ
    照会するためのツール．常に上限つきで返し，コンテキストを溢れさせない．
    """
    sim_id = str(fn_args.get("sim_id", "")).strip()
    if sim_id not in results_store:
        known = list(results_store.keys())[-5:]
        return (f"sim_id '{sim_id}' が見つかりません．有効な sim_id: {known}", True)
    sc = results_store[sim_id].get("_scenario") or {}
    nodes = sc.get("nodes") or []
    links = sc.get("links") or []
    demands = sc.get("demands") or []

    include = str(fn_args.get("include", "summary")).lower()
    limit = max(1, min(int(fn_args.get("limit", 50) or 50), 200))
    offset = max(0, int(fn_args.get("offset", 0) or 0))
    name_contains = fn_args.get("name_contains")

    # 次数（ノードの接続本数）
    degree: dict[str, int] = {}
    for lk in links:
        degree[lk["start"]] = degree.get(lk["start"], 0) + 1
        degree[lk["end"]] = degree.get(lk["end"], 0) + 1

    out: dict = {"sim_id": sim_id,
                 "total": {"nodes": len(nodes), "links": len(links), "demands": len(demands)},
                 "tmax": sc.get("tmax")}

    if include == "summary":
        xs = [n["x"] for n in nodes]; ys = [n["y"] for n in nodes]
        lengths = [lk["length"] for lk in links]
        top_deg = sorted(nodes, key=lambda n: -degree.get(n["name"], 0))[:10]
        signal_nodes = [n["name"] for n in nodes if n.get("signal")]
        out["bbox"] = ({"x_min": min(xs), "x_max": max(xs),
                        "y_min": min(ys), "y_max": max(ys)} if xs else None)
        out["link_length"] = ({"min": round(min(lengths), 1), "max": round(max(lengths), 1),
                               "avg": round(sum(lengths) / len(lengths), 1)} if lengths else None)
        out["top_degree_nodes"] = [
            {"name": n["name"], "degree": degree.get(n["name"], 0)} for n in top_deg]
        out["signal_nodes"] = signal_nodes[:20]
        out["sample_node_names"] = [n["name"] for n in nodes[:20]]
        out["sample_link_names"] = [lk["name"] for lk in links[:20]]
        out["hint"] = ("詳細は include='nodes'/'links'/'demands'（limit/offset/name_contains 指定可）．"
                       "ランダム OD は rerun_simulation の generate_demands アクションが使える．")
    elif include == "nodes":
        items = nodes
        if name_contains:
            items = [n for n in items if str(name_contains) in str(n["name"])]
        out["matched"] = len(items)
        out["nodes"] = [
            {"name": n["name"], "x": n["x"], "y": n["y"],
             "degree": degree.get(n["name"], 0),
             **({"signal": n["signal"]} if n.get("signal") else {}),
             **({"flow_capacity": n["flow_capacity"]} if n.get("flow_capacity") is not None else {})}
            for n in items[offset:offset + limit]
        ]
        if len(items) > offset + limit:
            out["note"] = f"{offset + limit} 件目まで表示（全 {len(items)} 件）．offset で続きを取得"
    elif include == "links":
        items = links
        if name_contains:
            items = [l for l in items if str(name_contains) in str(l["name"])]
        out["matched"] = len(items)
        out["links"] = [
            {k: v for k, v in {
                "name": l["name"], "start": l["start"], "end": l["end"],
                "length": l["length"], "free_flow_speed": l.get("free_flow_speed"),
                "number_of_lanes": l.get("number_of_lanes"),
                "capacity": l.get("capacity"), "signal_group": l.get("signal_group"),
            }.items() if v is not None}
            for l in items[offset:offset + limit]
        ]
        if len(items) > offset + limit:
            out["note"] = f"{offset + limit} 件目まで表示（全 {len(items)} 件）．offset で続きを取得"
    elif include == "demands":
        out["matched"] = len(demands)
        out["demands"] = demands[offset:offset + limit]
        if len(demands) > offset + limit:
            out["note"] = f"{offset + limit} 件目まで表示（全 {len(demands)} 件）"
    else:
        return (f"include は summary / nodes / links / demands のいずれか（指定: {include}）", True)

    return (json.dumps(out, ensure_ascii=False), False)


async def _handle_rerun_simulation(fn_args: dict, body) -> tuple[str, str | None, bool]:
    """rerun_simulation ツールの共通ハンドラ（stream / sync 両系統から使用）．

    戻り値: (tool_result content, 新 sim_id または None, is_error)
    """
    base_id = str(fn_args.get("base_sim_id", "")).strip()
    if base_id not in results_store:
        known = list(results_store.keys())[-5:]
        return (f"base_sim_id '{base_id}' が見つかりません．有効な sim_id: {known}", None, True)
    base = results_store[base_id]
    base_scenario = base.get("_scenario")
    if not base_scenario:
        return (f"sim_id '{base_id}' にはシナリオが保存されていません．", None, True)

    try:
        mods = fn_args.get("modifications") or []
        scenario, applied = _apply_modifications(base_scenario, mods)
        if fn_args.get("tmax"):
            scenario["tmax"] = int(fn_args["tmax"])
            applied.append(f"tmax={scenario['tmax']}s")
        if fn_args.get("name"):
            scenario["name"] = str(fn_args["name"])
        si = SimulationInput(**scenario)
        _validate_scenario_size(si)
        result = await _run_uxsim_async(si)

        # OSM 由来の道路形状（曲線座標）を名前一致で引き継ぐ
        base_geom = {}
        for f in (base.get("geojson") or {}).get("features", []):
            coords = f.get("geometry", {}).get("coordinates")
            if coords and len(coords) > 2:
                base_geom[f["properties"]["name"]] = coords
        if base_geom:
            _apply_link_geometries(result, base_geom)

        new_id = str(uuid.uuid4())[:8]
        _store_sim(new_id, result, {
            "type": "llm",
            "llm_backend": "claude",
            "tool": "rerun_simulation",
            "base_sim_id": base_id,
            "llm_user_message": _last_user_message_text(body),
        })
        content = json.dumps({
            **result["stats"],
            "sim_id": new_id,
            "base_sim_id": base_id,
            "applied": applied,
            "network": {"nodes": len(scenario["nodes"]), "links": len(scenario["links"]),
                        "demands": len(scenario["demands"])},
        }, ensure_ascii=False)
        return (content, new_id, False)
    except HTTPException as e:
        return (f"再実行エラー: {e.detail}", None, True)
    except (ValueError, TypeError) as e:
        return (f"modifications が不正です: {e}\n修正して再度 rerun_simulation を呼んでください．", None, True)
    except Exception as e:
        return (f"再実行エラー: {e.__class__.__name__}: {e}", None, True)


# ── Claude ツール定義 ──
CLAUDE_TOOLS = [
    {
        "name": "run_simulation",
        "description": (
            "UXsim 交通流シミュレーションを実行する．"
            "ノード（交差点），リンク（道路），需要（交通量）を指定する．"
            "座標の単位はメートル，flow の単位は台/秒．"
            "格子状ネットワークは nodes/links を列挙せず grid テンプレートで指定すること"
            "（サーバー側で展開．ノード名は n{i}_{j}，リンク名は n0_0-n1_0 形式で返る）．"
            "OD をランダム/周縁に自動生成したいときは auto_demands を使う（demands は省略可）．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":    {"type": "string", "description": "シミュレーション名"},
                "tmax":    {"type": "integer", "description": "シミュレーション終了時刻（秒）．デフォルト2000"},
                "deltan":  {"type": "integer", "description": "車両集計単位（デフォルト5）"},
                "reaction_time": {"type": "number", "description": "車頭時間（秒）．省略で UXsim 既定 1.0（1 車線 ≈ 2,770 台/時）．実勢容量 1,800〜2,000 台/時/車線なら 1.5〜1.7"},
                "random_seed": {"type": "integer", "description": "シミュレーション本体の乱数シード．条件比較・追試で結果を固定したいときに指定（省略で毎回変わる）"},
                "grid": {
                    "type": "object",
                    "description": "格子ネットワークをサーバー側で生成する（nodes/links の列挙不要）",
                    "properties": {
                        "nx": {"type": "integer", "description": "横方向のノード数"},
                        "ny": {"type": "integer", "description": "縦方向のノード数（省略で nx）"},
                        "spacing": {"type": "number", "description": "ノード間隔＝リンク長（m）．デフォルト500"},
                        "bidirectional": {"type": "boolean", "description": "双方向道路にする（デフォルト true）"},
                        "free_flow_speed": {"type": "number"},
                        "number_of_lanes": {"type": "integer"},
                        "capacity": {"type": "number"},
                        "name_prefix": {"type": "string", "description": "ノード名の接頭辞（デフォルト n）"},
                    },
                    "required": ["nx"],
                },
                "auto_demands": {
                    "type": "object",
                    "description": "OD 需要の自動生成．strategy=random（ランダムなノードペア）/ boundary（周縁ノード全ペア）",
                    "properties": {
                        "strategy": {"type": "string", "enum": ["random", "boundary"]},
                        "n_pairs": {"type": "integer", "description": "random のペア数（デフォルト10）"},
                        "flow_per_pair": {"type": "number", "description": "ペアあたり流率（台/秒）"},
                        "flow_total": {"type": "number", "description": "合計流率（台/秒，flow_per_pair の代わり）"},
                        "seed": {"type": "integer"},
                        "t_start": {"type": "number"},
                        "t_end": {"type": "number", "description": "デフォルト tmax の半分"},
                    },
                },
                "nodes": {
                    "type": "array",
                    "description": "交差点リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "x":    {"type": "number", "description": "X座標（メートル）"},
                            "y":    {"type": "number", "description": "Y座標（メートル）"},
                            "flow_capacity": {"type": "number", "description": "ノード流出容量（台/秒）．省略で無制限"},
                            "signal": {
                                "type": "array",
                                "items": {"type": "number"},
                                "description": "信号現示の青時間リスト（秒）．例: [60,60]→2現示各60秒．省略=信号なし",
                            },
                        },
                        "required": ["name", "x", "y"],
                    },
                },
                "links": {
                    "type": "array",
                    "description": "道路リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":             {"type": "string"},
                            "start":            {"type": "string", "description": "始点ノード名"},
                            "end":              {"type": "string", "description": "終点ノード名"},
                            "length":           {"type": "number", "description": "道路長（メートル）"},
                            "free_flow_speed":  {"type": "number", "description": "自由流速度（m/s）．デフォルト20"},
                            "jam_density":      {"type": "number", "description": "渋滞密度（台/m）．デフォルト0.2"},
                            "number_of_lanes":  {"type": "integer", "description": "車線数．デフォルト1"},
                            "capacity":         {"type": "number", "description": "リンク容量（台/秒，リンク全体）．ボトルネックの明示表現に使う（例: 0.5）．省略時は速度・密度・車線数から決まる容量"},
                            "signal_group":     {"type": "integer", "description": "この進入リンクが青になる信号現示番号（0始まり）．退出リンクには不要．省略=常時通行可能"},
                        },
                        "required": ["name", "start", "end", "length"],
                    },
                },
                "demands": {
                    "type": "array",
                    "description": "交通需要リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "orig":    {"type": "string", "description": "出発ノード名"},
                            "dest":    {"type": "string", "description": "到着ノード名"},
                            "t_start": {"type": "number", "description": "需要開始時刻（秒）"},
                            "t_end":   {"type": "number", "description": "需要終了時刻（秒）"},
                            "flow":    {"type": "number", "description": "交通量（台/秒）"},
                        },
                        "required": ["orig", "dest", "t_start", "t_end", "flow"],
                    },
                },
            },
            "required": [],
        },
    },
    {
        "name": "rerun_simulation",
        "description": (
            "保存済みシミュレーション（base_sim_id）のネットワークを起点に，"
            "小さな修正（modifications）を適用して再実行する．"
            "OSM 取込やファイルアップロードで作られた既存ネットワークの調整・比較は"
            "【必ず】このツールを使うこと．ネットワーク全体を run_simulation で"
            "再送してはいけない（大規模ネットワークではサイズ超過になる）．"
            "modifications の例:\n"
            '・リンク容量変更: {"action":"update_links","names":["r1"],"set":{"capacity":0.5}}\n'
            '・全リンク速度変更: {"action":"update_links","all":true,"set":{"free_flow_speed":15}}\n'
            '・部分一致: {"action":"update_links","name_contains":"link_1","set":{"number_of_lanes":2}}\n'
            '・信号設置: {"action":"update_nodes","names":["I1"],"set":{"signal":[60,60]}}\n'
            '・需要 1.5 倍: {"action":"update_demands","all":true,"scale_flow":1.5}\n'
            '・需要追加: {"action":"add_demand","demand":{"orig":"A","dest":"B","t_start":0,"t_end":1800,"flow":0.3}}\n'
            '・リンク削除（通行止め）: {"action":"remove_links","names":["r2"]}\n'
            '・ノード/リンク追加: {"action":"add_node","node":{...}} / {"action":"add_link","link":{...}}\n'
            '・時間変更: {"action":"set_tmax","tmax":7200}\n'
            '・全体パラメータ: {"action":"set_params","reaction_time":1.7,"random_seed":42}\n'
            '  （tmax / deltan / reaction_time / random_seed．シードは保存シナリオに残るので，'
            '同条件比較では base と同じ seed のまま差分だけ当てればよい）\n'
            '・OD 自動生成: {"action":"generate_demands","strategy":"random","n_pairs":10,'
            '"flow_per_pair":0.2,"clear_existing":true}\n'
            '  （strategy: random=ランダムなノードペア / boundary=ネットワーク周縁の全ペア．'
            'seed で再現可，flow_total で合計流率指定も可．ノード名を知らなくても使える）\n'
            '・需要の時間シフト（時差出勤）: {"action":"shift_demands","t_from":3600,"t_to":10800,'
            '"fraction":0.3,"shift_s":-3600}\n'
            '  （時間帯 [t_from,t_to) の需要の fraction を shift_s 秒ずらす．前倒しは負，後ろ倒しは正．'
            '2 方向に分けるなら 2 回指定）\n'
            "modifications: [] で無修正の再実行（tmax だけ変える等）も可能．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "base_sim_id": {"type": "string", "description": "起点となるシミュレーション ID"},
                "modifications": {
                    "type": "array",
                    "description": "修正命令の配列（description の例を参照）．各要素は action フィールドを持つ",
                    "items": {"type": "object"},
                },
                "tmax": {"type": "integer", "description": "シミュレーション時間の上書き（秒，省略可）"},
                "name": {"type": "string", "description": "新しいシミュレーション名（省略可）"},
            },
            "required": ["base_sim_id", "modifications"],
        },
    },
    {
        "name": "get_network_info",
        "description": (
            "保存済みシミュレーションのネットワーク構造（ノード・リンク・需要）を照会する．"
            "大規模ネットワークはチャットに全体が渡らないため，ノード名やリンク名が"
            "必要な操作（OD 設定・信号設置・特定リンクの修正など）の前に，"
            "このツールで必要な分だけ調べる．"
            "include='summary' で規模・座標範囲・次数上位ノード・名前のサンプルを取得．"
            "include='nodes'/'links'/'demands' で一覧（limit/offset でページング，"
            "name_contains で絞り込み）．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sim_id": {"type": "string", "description": "シミュレーション ID"},
                "include": {"type": "string", "enum": ["summary", "nodes", "links", "demands"],
                            "description": "取得内容．デフォルト summary"},
                "name_contains": {"type": "string", "description": "名前の部分一致フィルタ（nodes/links 用）"},
                "limit": {"type": "integer", "description": "最大件数（デフォルト 50，上限 200）"},
                "offset": {"type": "integer", "description": "ページングオフセット"},
            },
            "required": ["sim_id"],
        },
    },
    {
        "name": "get_simulation_data",
        "description": (
            "シミュレーション結果の集計データを取得する．"
            "ユーザーがグラフ・チャート・分析を求めた場合に呼び出す．"
            "返されるデータ: time_labels, network_avg_speed, network_vehicle_count, "
            "link_speeds(混雑度上位リンク別), speed_histogram, stats．"
            "チャートでは配列を書き写さず {\"$data\":\"network_avg_speed\"} のような参照を使う"
            "（サーバーが実データに置換する）．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sim_id": {"type": "string", "description": "シミュレーションID"},
                "points": {"type": "integer", "description": "時系列のサンプル点数（デフォルト 30，上限 60）"},
                "max_links": {"type": "integer", "description": "link_speeds に含めるリンク数（デフォルト 20，上限 50）．リンク別の分析が不要なら 0"},
            },
            "required": ["sim_id"],
        },
    },
    {
        "name": "import_osm_network",
        "description": (
            "OpenStreetMap から実在の道路ネットワークをダウンロードしてシミュレーションを実行する．"
            "地名・ランドマーク名を指定すると，自動でジオコーディングし，周辺の道路を取得する．"
            "例: '東京駅', 'Shibuya Station', '大阪城公園', 'Times Square, New York' など．"
            "取得後，自動的にダミー需要を設定してシミュレーションを実行する．"
            "結果はブラウザ上に地図として表示される．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "地名・ランドマーク名・住所（日本語・英語どちらも可）",
                },
                "distance_m": {
                    "type": "integer",
                    "description": "中心からの取得半径（メートル）．デフォルト500．大きいほど広い範囲だが処理に時間がかかる．100〜2000が推奨．",
                },
                "road_types": {
                    "type": "string",
                    "enum": ["major", "arterial", "drive", "all"],
                    "description": (
                        "取得する道路の種類．"
                        "major=高速道路・国道級のみ / arterial=幹線道路まで / "
                        "drive=一般車道（デフォルト，住宅街の道路含む） / "
                        "all=サービス道路・駐車場内通路含む全車道．"
                        "半径 1000m 以上では major か arterial を推奨"
                        "（ノード数が減り計算が大幅に速くなる）．"
                    ),
                },
                "tmax": {
                    "type": "integer",
                    "description": "シミュレーション時間（秒）．デフォルト3600",
                },
            },
            "required": ["place"],
        },
    }
]


# ──────────────────────────────────────────────
# Prompt Caching ヘルパー
# ──────────────────────────────────────────────
# Anthropic の Prompt Caching は system プロンプト + tool 定義をキャッシュすることで
# 入力トークンの 90% OFF を実現する（5 分 TTL）．
# RISU は system が ~3,000 tokens，tools が ~2,000 tokens なので効果絶大．
# https://docs.anthropic.com/en/docs/prompt-caching
def _cache_ctl() -> dict:
    """cache_control ブロック（TTL は CACHE_TTL）"""
    cc = {"type": "ephemeral"}
    if CACHE_TTL and CACHE_TTL != "5m":
        cc["ttl"] = CACHE_TTL
    return cc


def _cached_system(text: str) -> list:
    """system プロンプトをキャッシュ有効形式で返す"""
    return [{
        "type": "text",
        "text": text,
        "cache_control": _cache_ctl(),
    }]

def _cached_tools() -> list:
    """tool 定義の末尾に cache_control を付与（全 tool 定義を一括キャッシュ）"""
    if not CLAUDE_TOOLS:
        return []
    tools = [dict(t) for t in CLAUDE_TOOLS]
    tools[-1] = {**tools[-1], "cache_control": _cache_ctl()}
    return tools


# ──────────────────────────────────────────────
# 会話履歴のトークン節約
#
# キャッシュ順序は tools → system → messages．以前は sim_id ごとに変わる
# 【現在のコンテキスト】を system に足していたため，rerun のたびに system 以降の
# キャッシュが全滅していた．今は
#   1. system は SYSTEM_PROMPT のみ（不変）
#   2. 動的コンテキストは最後のユーザーメッセージに別ブロックとして付ける
#   3. 履歴の最後の assistant メッセージに cache_control（ターン跨ぎで prefix ヒット）
#   4. 各リクエストの最後のメッセージに cache_control（同一ターン内の tool ラウンドで
#      prefix ヒット．以前は tool ラウンドごとに履歴全体を再課金していた）
# breakpoint は tools / system / 履歴 assistant / 末尾 の 4 つ（API 上限）．
# ──────────────────────────────────────────────
def _msg_text(content) -> str:
    """メッセージ content（str または block list）からテキストを取り出す"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") or "")
            for b in content
        )
    return str(content or "")


def _trim_history(msgs: list[dict]) -> list[dict]:
    """文字数予算を超えた履歴を古いターンから落とす．

    予算超過時は予算の半分まで落とす（ヒステリシス）．毎ターン 1 件ずつ落とすと
    prefix が毎回変わってキャッシュが一度も当たらないため．先頭は必ず user．
    """
    if MAX_HISTORY_CHARS <= 0 or not msgs:
        return msgs
    sizes = [len(_msg_text(m["content"])) for m in msgs]
    if sum(sizes) <= MAX_HISTORY_CHARS:
        return msgs
    budget = MAX_HISTORY_CHARS // 2
    kept = []
    acc = 0
    for m, sz in zip(reversed(msgs), reversed(sizes)):
        if kept and acc + sz > budget:
            break
        kept.append(m)
        acc += sz
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    if not kept:
        kept = [msgs[-1]]
    if len(kept) < len(msgs):
        first = kept[0]
        kept[0] = {"role": "user",
                   "content": "（これより前の会話は省略）\n\n" + _msg_text(first["content"])}
        print(f"[RISU] history trimmed: {len(msgs)} -> {len(kept)} messages "
              f"({sum(sizes)} -> {sum(len(_msg_text(m['content'])) for m in kept)} chars)")
    return kept


def _build_llm_messages(body: ChatInput) -> list[dict]:
    """ChatInput → Claude API messages（履歴トリミング + キャッシュ境界 + 動的コンテキスト）"""
    msgs = _trim_history([{"role": m.role, "content": m.content} for m in body.messages])
    # ターン跨ぎのキャッシュ境界: 履歴の最後の assistant メッセージ
    last_asst = None
    for i, m in enumerate(msgs):
        if m["role"] == "assistant":
            last_asst = i
    if last_asst is not None:
        msgs[last_asst] = {"role": "assistant", "content": [
            {"type": "text", "text": _msg_text(msgs[last_asst]["content"]),
             "cache_control": _cache_ctl()},
        ]}
    # 動的コンテキストは最後の user メッセージの追加ブロック（system を不変に保つ）
    if msgs and msgs[-1]["role"] == "user":
        blocks = [{"type": "text", "text": _msg_text(msgs[-1]["content"])}]
        ctx = _conversation_context_block(body).strip()
        if ctx:
            blocks.append({"type": "text", "text": ctx})
        msgs[-1] = {"role": "user", "content": blocks}
    return _mark_cache_tail(msgs)


def _mark_cache_tail(msgs: list[dict]) -> list[dict]:
    """リクエスト末尾メッセージの最後のブロックに cache_control を付ける（同一ターン内の
    tool ラウンドで prefix がヒットする）．前のリクエストで付けた末尾の印は外す
    （履歴 assistant の印は _tail_marked を持たないので残る）．breakpoint は API 上限 4 つ:
    tools / system / 履歴 assistant / 末尾．"""
    for m in msgs[:-1]:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.pop("_tail_marked", False):
                    b.pop("cache_control", None)
    last = msgs[-1]
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = [{"type": "text", "text": c}]
        c = last["content"]
    if isinstance(c, list) and c and isinstance(c[-1], dict):
        if "cache_control" not in c[-1]:
            c[-1]["cache_control"] = _cache_ctl()
            c[-1]["_tail_marked"] = True
    return msgs


def _api_messages(msgs: list[dict]) -> list[dict]:
    """内部フラグ（_tail_marked）を除いた API 送信用メッセージ"""
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            c = [({k: v for k, v in b.items() if k != "_tail_marked"} if isinstance(b, dict) else b)
                 for b in c]
        out.append({"role": m["role"], "content": c})
    return out


class _UsageTally:
    """1 ターン（1 回の /chat）で使ったトークンの集計．done イベントでフロントに返す"""

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_creation = 0
        self.cost_jpy = 0.0

    def add(self, context: str, response) -> None:
        u = _log_usage(context, response)
        if not u:
            return
        self.calls += 1
        self.input_tokens += u["input_tokens"]
        self.output_tokens += u["output_tokens"]
        self.cache_read += u["cache_read_input_tokens"]
        self.cache_creation += u["cache_creation_input_tokens"]
        self.cost_jpy += u["cost_jpy"]

    def as_dict(self) -> dict:
        total_in = self.input_tokens + self.cache_read + self.cache_creation
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read,
            "cache_creation_tokens": self.cache_creation,
            "output_tokens": self.output_tokens,
            "cache_hit_pct": round(self.cache_read / max(1, total_in) * 100),
            "cost_jpy": round(self.cost_jpy, 2),
        }


# ──────────────────────────────────────────────
# チャートの $data 参照
#
# LLM が get_simulation_data の配列を Chart.js 設定に書き写すと，出力トークン
# （入力の 5 倍単価）を大量に使う．{"$data": "network_avg_speed"} のような参照を
# サーバー側で実データに置き換える．
# ──────────────────────────────────────────────
def _resolve_chart_refs(obj, data_cache: dict, default_sim_id: str | None):
    """chart JSON 内の {"$data": "<path>", "sim_id"?: "..."} を集計データで置換する"""
    if isinstance(obj, dict):
        if "$data" in obj and isinstance(obj["$data"], str):
            sim_id = str(obj.get("sim_id") or default_sim_id or "")
            data = data_cache.get(sim_id)
            if data is None and sim_id:
                data = _get_simulation_data(sim_id)
                if data is not None:
                    data_cache[sim_id] = data
            cur = data
            for part in obj["$data"].split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    print(f"[RISU] chart $data unresolved: {obj['$data']!r} (sim {sim_id!r})")
                    return []
            return cur
        return {k: _resolve_chart_refs(v, data_cache, default_sim_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_chart_refs(v, data_cache, default_sim_id) for v in obj]
    return obj


_CHART_PATTERN = None

def _extract_charts(text: str, data_cache: dict, default_sim_id: str | None) -> tuple[list, str]:
    """```chart ... ``` ブロックを抽出して $data を解決し，(charts, 本文) を返す"""
    global _CHART_PATTERN
    if _CHART_PATTERN is None:
        import re
        _CHART_PATTERN = re.compile(r'```\s*chart\w*\s*\n?(.*?)\n?\s*```', re.DOTALL | re.IGNORECASE)
    charts = []
    for m in _CHART_PATTERN.finditer(text):
        try:
            chart_json = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            print(f"[RISU] chart JSON parse failed: {e}; raw={m.group(1)[:200]!r}")
            continue
        charts.append(_resolve_chart_refs(chart_json, data_cache, default_sim_id))
    clean_text = _CHART_PATTERN.sub('', text).strip()
    return charts, clean_text


# ──────────────────────────────────────────────
# run_simulation の入力展開（grid テンプレート / 需要自動生成）
#
# 「10×10 のグリッド」を LLM がノード 100 個・リンク 360 本を列挙して生成すると
# 出力トークン 2 万以上・数十秒かかる．テンプレートを渡すとサーバー側で展開する．
# ──────────────────────────────────────────────
def _expand_grid(spec: dict) -> tuple[list[dict], list[dict], dict]:
    nx = max(2, int(spec.get("nx", 3)))
    ny = max(2, int(spec.get("ny", nx)))
    spacing = float(spec.get("spacing", 500))
    prefix = str(spec.get("name_prefix", "n"))
    bidir = bool(spec.get("bidirectional", True))
    if nx * ny > MAX_NODES:
        raise ValueError(f"grid が大きすぎます: {nx}x{ny} > {MAX_NODES} ノード")
    link_attrs = {k: spec[k] for k in ("free_flow_speed", "jam_density", "number_of_lanes", "capacity")
                  if spec.get(k) is not None}
    nodes = [{"name": f"{prefix}{i}_{j}", "x": i * spacing, "y": j * spacing}
             for i in range(nx) for j in range(ny)]
    links = []
    def add(a, b):
        links.append({"name": f"{a}-{b}", "start": a, "end": b, "length": spacing, **link_attrs})
    for i in range(nx):
        for j in range(ny):
            a = f"{prefix}{i}_{j}"
            if i + 1 < nx:
                b = f"{prefix}{i+1}_{j}"; add(a, b)
                if bidir: add(b, a)
            if j + 1 < ny:
                b = f"{prefix}{i}_{j+1}"; add(a, b)
                if bidir: add(b, a)
    info = {"type": "grid", "nx": nx, "ny": ny, "spacing": spacing,
            "node_naming": f"{prefix}{{i}}_{{j}} (i=0..{nx-1}, j=0..{ny-1}, x=i*{spacing:g}, y=j*{spacing:g})",
            "link_naming": f"{prefix}0_0-{prefix}1_0 のように 始点名-終点名"}
    return nodes, links, info


def _generate_demands_spec(nodes: list[dict], links: list[dict], spec: dict, tmax: int) -> list[dict]:
    """OD 需要をサーバー側で生成する（rerun の generate_demands と run_simulation の auto_demands 共用）"""
    import random as _random
    strategy = spec.get("strategy", "random")
    if len(nodes) < 2:
        raise ValueError("需要の自動生成にはノードが 2 つ以上必要です")
    t_start = float(spec.get("t_start", 0))
    t_end = float(spec.get("t_end", tmax * 0.5))
    if strategy == "boundary":
        # ネットワーク周縁ノード全ペア（OSM インポートと同じロジック）
        new_demands = _generate_osm_demands(nodes, links, tmax)
        if spec.get("flow_per_pair") is not None:
            for d in new_demands:
                d["flow"] = float(spec["flow_per_pair"])
        for d in new_demands:
            d["t_start"] = t_start
            d["t_end"] = t_end
    elif strategy == "random":
        n_pairs = max(1, int(spec.get("n_pairs", 10)))
        if spec.get("flow_per_pair") is not None:
            flow = float(spec["flow_per_pair"])
        elif spec.get("flow_total") is not None:
            flow = round(float(spec["flow_total"]) / n_pairs, 4)
        else:
            flow = 0.2
        rng = _random.Random(spec.get("seed"))
        names = [n["name"] for n in nodes]
        new_demands = []
        for _ in range(n_pairs):
            orig, dest = rng.sample(names, 2)
            new_demands.append({"orig": orig, "dest": dest,
                                "t_start": t_start, "t_end": t_end, "flow": flow})
    else:
        raise ValueError(f"strategy は random / boundary（指定: {strategy}）")
    return new_demands


def _expand_run_simulation_args(fn_args: dict) -> tuple[dict, dict]:
    """run_simulation の tool 入力を SimulationInput 用 dict に展開する．

    戻り値: (scenario dict, 展開情報 dict — tool_result に含めて LLM に命名規則を伝える)
    """
    args = dict(fn_args)
    info = {}
    grid = args.pop("grid", None)
    auto = args.pop("auto_demands", None)
    nodes = list(args.get("nodes") or [])
    links = list(args.get("links") or [])
    if grid:
        g_nodes, g_links, g_info = _expand_grid(grid)
        nodes = g_nodes + nodes
        links = g_links + links
        info["grid"] = g_info
    if not nodes or not links:
        raise ValueError("nodes / links を指定するか，grid テンプレートを使ってください")
    demands = list(args.get("demands") or [])
    if auto:
        tmax = int(args.get("tmax") or 3600)
        gen = _generate_demands_spec(nodes, links, auto, tmax)
        demands = demands + gen
        info["auto_demands"] = {"strategy": auto.get("strategy", "random"), "generated": len(gen)}
    if not demands:
        raise ValueError("demands を指定するか auto_demands で自動生成してください")
    args["nodes"], args["links"], args["demands"] = nodes, links, demands
    return args, info


async def _handle_run_simulation(fn_args: dict, body, *, source_round: str | None = None
                                 ) -> tuple[str, str | None, bool]:
    """run_simulation ツールの共通ハンドラ（stream / sync 両系統から使用）．

    戻り値: (tool_result content, 新 sim_id または None, is_error)
    """
    try:
        scenario, info = _expand_run_simulation_args(fn_args)
        sim_input = SimulationInput(**scenario)
        result = await _run_uxsim_async(sim_input)
        sim_id = str(uuid.uuid4())[:8]
        src = {
            "type": "llm",
            "llm_backend": "claude",
            "tool": "run_simulation",
            "llm_user_message": _last_user_message_text(body),
        }
        if source_round:
            src["round"] = source_round
        _store_sim(sim_id, result, src)
        payload = {**result["stats"], "sim_id": sim_id,
                   "network": {"nodes": len(scenario["nodes"]), "links": len(scenario["links"]),
                               "demands": len(scenario["demands"])}}
        payload.update(info)
        return (json.dumps(payload, ensure_ascii=False), sim_id, False)
    except HTTPException as e:
        return (f"シミュレーション実行エラー: {e.detail}\n入力を修正して再度 run_simulation を呼んでください．",
                None, True)
    except Exception as e:
        return (f"シミュレーション実行エラー: {str(e)}\n入力を修正して再度 run_simulation を呼んでください．",
                None, True)


# ──────────────────────────────────────────────
# トークン使用量ロガー
# ──────────────────────────────────────────────
def _log_usage(context: str, response) -> dict:
    """Claude レスポンスから usage を抽出してログ出力．将来 DB 保存フックに繋げられる"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    in_tok  = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create  = getattr(usage, "cache_creation_input_tokens", 0) or 0
    # claude-sonnet-5 の概算単価: 入力 $2 / 出力 $10 per MTok，キャッシュ読み $0.20，キャッシュ書き $2.50．
    # usage.input_tokens はキャッシュ読み・書きの分を含まない（重複して引くと負になる）．
    # JPY @ 150円/USD 概算
    cost_usd = (
        in_tok * 2 / 1_000_000
        + cache_read * 0.20 / 1_000_000
        + cache_create * 2.50 / 1_000_000
        + out_tok * 10 / 1_000_000
    )
    cost_jpy = cost_usd * 150
    total_in = in_tok + cache_read + cache_create
    cache_pct = (cache_read / max(1, total_in)) * 100
    try:
        msg = (
            f"[RISU usage] {context}: "
            f"in={in_tok}+cache_read={cache_read}+cache_create={cache_create}/out={out_tok} "
            f"(cache {cache_pct:.0f}%) ~JPY {cost_jpy:.2f}"
        )
        # Windows cp932 コンソール対策: エンコード不能文字は置換
        try:
            print(msg)
        except UnicodeEncodeError:
            import sys
            sys.stdout.buffer.write(msg.encode("utf-8", errors="replace") + b"\n")
            sys.stdout.flush()
    except Exception as e:
        print(f"[RISU usage] log failed: {e!r}")
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "cost_jpy": cost_jpy,
    }


# ──────────────────────────────────────────────
# tool_use ループ上限（暴走・コスト爆弾対策）
# ──────────────────────────────────────────────
MAX_TOOL_ROUNDS = int(os.getenv("RISU_MAX_TOOL_ROUNDS", "3"))

# ──────────────────────────────────────────────
# トークン節約の設定
# ──────────────────────────────────────────────
# LLM に送る会話履歴の文字数予算．超過したら古いターンから落とす（ヒステリシス付き，
# _trim_history 参照）．0 で無制限．
MAX_HISTORY_CHARS = int(os.getenv("RISU_MAX_HISTORY_CHARS", "24000"))
# Prompt Caching の TTL: "5m"（既定）または "1h"．考えながら操作して 5 分以上空くことが
# 多いなら "1h" の方が安い（書き込み単価は 2 倍だが，再作成が要らない）．
CACHE_TTL = os.getenv("RISU_CACHE_TTL", "5m").strip() or "5m"


def _sse_event(data: dict) -> str:
    """SSE イベント文字列を生成"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _conversation_sim_id(body: ChatInput) -> str | None:
    """会話に紐づく有効なシミュレーションIDを返す（なければ None）"""
    sim_id = (body.last_sim_id or "").strip()
    return sim_id if sim_id and sim_id in results_store else None


def _conversation_context_block(body: ChatInput) -> str:
    """システムプロンプト末尾に付けるコンテキスト注入文字列を生成．

    会話に紐づく sim（フロントが /chat で送る last_sim_id）だけを対象にする．
    results_store のグローバル最新を使うと，別会話や CSV アップロードで作られた
    無関係なシナリオを LLM が rerun_simulation で流用してしまうため．
    """
    sim_id = _conversation_sim_id(body)
    if not sim_id:
        return ""
    link_names = [f["properties"]["name"] for f in results_store[sim_id].get("geojson", {}).get("features", [])]
    _sc = results_store[sim_id].get("_scenario") or {}
    _sizes = (f"{len(_sc.get('nodes') or [])} ノード / {len(_sc.get('links') or [])} リンク / "
              f"{len(_sc.get('demands') or [])} 需要, tmax={_sc.get('tmax', '?')}s")
    _more = f"（他 {len(link_names) - 20} 本）" if len(link_names) > 20 else ""
    return f"""

【現在のコンテキスト】
この会話のシミュレーションID: {sim_id}
ネットワーク規模: {_sizes}
リンク名の例: {', '.join(link_names[:20])}{_more}
この結果への修正・再実行・比較は rerun_simulation(base_sim_id="{sim_id}") を使うこと．
これ以外の依頼（新しいネットワークの設計）は run_simulation でゼロから作ること．"""


# ──────────────────────────────────────────────
# ツール実行の共通ディスパッチ
# ──────────────────────────────────────────────
class _ToolTurnState:
    """1 ターン分のツール実行で持ち回る状態．

    sim_id            このターンで最後に作られたシミュレーション ID
    last_data_sim_id  get_simulation_data が最後に集計した sim_id（$data 解決の既定）
    sim_data_cache    このターンで取得した集計データ（チャートの $data 参照解決用）
    """

    __slots__ = ("body", "sim_id", "last_data_sim_id", "sim_data_cache")

    def __init__(self, body: ChatInput):
        self.body = body
        self.sim_id: str | None = None
        self.last_data_sim_id: str | None = None
        self.sim_data_cache: dict[str, dict] = {}


def _tool_result(tool_use_id: str, content: str, is_err: bool = False) -> dict:
    tr = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_err:
        tr["is_error"] = True
    return tr


async def _dispatch_tool_blocks(tool_blocks, state: _ToolTurnState, *, follow_up: bool = False):
    """tool_use ブロック群を実行する非同期ジェネレータ．

    ストリーミング経路（_chat_claude_stream）と同期経路（_chat_claude）で
    **同じコードを通す**ためにここへ集約している．以前は初回ラウンドと追加ラウンド ×
    2 経路の計 4 箇所に同じ dispatch があり，片方だけ直すと挙動がずれる状態だった．

    yield するもの:
        ("progress", "メッセージ")   進捗．SSE 経路だけが転送し，同期経路は捨てる
        ("results", [tool_result])   最後に 1 回だけ．API へ返す tool_result のリスト

    follow_up=True は 2 ラウンド目以降．進捗の文言と，保存メタの "round" が変わる．

    tool_use には**必ず対応する tool_result を返すこと**（Anthropic API の要求）．
    未知のツール名でも結果を積む．
    """
    results = []

    for tb in tool_blocks:
        name = tb.name
        args = tb.input

        if name == "run_simulation":
            yield ("progress", "追加シミュレーションを実行中..." if follow_up
                   else "Step 2/3: UXsim でシミュレーション実行中...")
            content, new_sim_id, is_err = await _handle_run_simulation(
                args, state.body, source_round="follow_up" if follow_up else None)
            if new_sim_id:
                state.sim_id = new_sim_id
            results.append(_tool_result(tb.id, content, is_err))

        elif name == "rerun_simulation":
            yield ("progress", "修正を適用して再実行中..." if follow_up
                   else "Step 2/3: 修正を適用して再実行中...")
            content, new_sim_id, is_err = await _handle_rerun_simulation(args, state.body)
            if new_sim_id:
                state.sim_id = new_sim_id
            results.append(_tool_result(tb.id, content, is_err))

        elif name == "get_network_info":
            if not follow_up:
                yield ("progress", "ネットワーク情報を照会中...")
            content, is_err = _handle_get_network_info(args)
            results.append(_tool_result(tb.id, content, is_err))

        elif name == "import_osm_network":
            place = args.get("place", "")
            dist = args.get("distance_m", 500)
            osm_tmax = args.get("tmax", 3600)
            road_types = args.get("road_types", "drive")
            yield ("progress",
                   f"OpenStreetMap から「{place}」のデータを取得中..." if follow_up
                   else f"Step 2/3: OpenStreetMap から「{place}」周辺のデータを取得中...")
            try:
                loop = asyncio.get_event_loop()
                osm_result = await loop.run_in_executor(
                    executor, _run_osm_import, place, dist, road_types
                )
                scenario = dict(osm_result)
                link_geometries = scenario.pop("link_geometries", {})
                scenario.pop("center", None)
                scenario.pop("distance_m", None)
                summary = scenario.pop("summary", "")
                scenario["tmax"] = osm_tmax

                if not scenario["demands"] and len(scenario["nodes"]) >= 2:
                    scenario["demands"] = _generate_osm_demands(
                        scenario["nodes"], scenario["links"], osm_tmax
                    )

                if not follow_up:
                    yield ("progress", "Step 2/3: UXsim でシミュレーション実行中...")
                result = await _run_uxsim_async(SimulationInput(**scenario))
                _apply_link_geometries(result, link_geometries)
                new_id = str(uuid.uuid4())[:8]
                meta = {
                    "type": "osm",
                    "via": "llm",
                    "llm_backend": "claude",
                    "place": place,
                    "distance_m": dist,
                    "llm_user_message": _last_user_message_text(state.body),
                }
                if follow_up:
                    meta["round"] = "follow_up"
                _store_sim(new_id, result, meta)
                state.sim_id = new_id

                results.append(_tool_result(tb.id, json.dumps({
                    **result["stats"],
                    "sim_id": new_id,
                    "summary": summary,
                    "node_count": len(scenario["nodes"]),
                    "link_count": len(scenario["links"]),
                }, ensure_ascii=False)))
            except Exception as e:
                import traceback
                traceback.print_exc()
                results.append(_tool_result(tb.id, f"OSMインポートエラー: {e!s}", True))

        elif name == "get_simulation_data":
            yield ("progress", "チャートデータを取得中..." if follow_up else "データを集計中...")
            # sim_id が空または存在しない場合，このターンで実行した sim →
            # 会話に紐づく sim の順でフォールバック（グローバル最新は使わない）
            req_sim_id = args.get("sim_id", "")
            if not req_sim_id or req_sim_id not in results_store:
                req_sim_id = state.sim_id or _conversation_sim_id(state.body) or ""
            sd = _get_simulation_data(req_sim_id,
                                      points=args.get("points") or 30,
                                      max_links=args.get("max_links", 20))
            if sd:
                state.sim_data_cache[req_sim_id] = sd
                state.last_data_sim_id = req_sim_id
                results.append(_tool_result(tb.id, json.dumps(sd, ensure_ascii=False)))
            else:
                results.append(_tool_result(
                    tb.id,
                    "データが見つかりません．まず run_simulation でシミュレーションを実行してください．"))

        else:
            results.append(_tool_result(tb.id, f"未知のツール: {name}"))

    yield ("results", results)


async def _collect_tool_results(tool_blocks, state: _ToolTurnState, *, follow_up: bool = False):
    """_dispatch_tool_blocks の進捗を捨てて tool_result だけ取る（同期経路用）．"""
    results = []
    async for kind, payload in _dispatch_tool_blocks(tool_blocks, state, follow_up=follow_up):
        if kind == "results":
            results = payload
    return results


async def _chat_claude_stream(body: ChatInput):
    """Claude API チャット — SSE ストリーミングで進捗を返す"""

    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    # 履歴トリミング + キャッシュ境界 + 動的コンテキスト（system は不変に保つ）
    messages = _build_llm_messages(body)
    system = SYSTEM_PROMPT
    usage = _UsageTally()
    # ツール実行の状態（sim_id / 集計キャッシュ）は _dispatch_tool_blocks と共有する
    state = _ToolTurnState(body)

    async def event_generator():
        try:
            # ── 1回目: LLM 呼び出し（ストリーミング）──
            yield _sse_event({"type": "progress", "message": "Step 1/3: シナリオを設計中..."})

            first_streamed_text = False

            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=64000,
                system=_cached_system(system),
                messages=_api_messages(messages),
                tools=_cached_tools(),
            ) as first_stream:
                for event in first_stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            if not first_streamed_text:
                                yield _sse_event({"type": "stream_start"})
                                first_streamed_text = True
                            yield _sse_event({"type": "text_delta", "text": event.delta.text})

                response = first_stream.get_final_message()
                usage.add("stream first-round", response)

            # テキストのみの応答（ツール呼び出しなし）
            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")

                # シミュレーション意図がありそうならリトライ（tool_choice で強制）
                last_user_msg = _last_user_message_text(body) or ""
                sim_keywords = ["シミュレーション", "シミュレート", "実行", "グリッド", "ネットワーク", "渋滞", "ボトルネック", "道路", "交通"]
                if any(k in last_user_msg for k in sim_keywords):
                    if first_streamed_text:
                        yield _sse_event({"type": "stream_end_partial"})
                    yield _sse_event({"type": "progress", "message": "Step 1/3: シナリオを再設計中..."})
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": "run_simulation ツールを使って今すぐシミュレーションを実行してください．"})
                    response = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=32000,
                        system=_cached_system(system),
                        messages=_api_messages(_mark_cache_tail(messages)),
                        tools=_cached_tools(),
                        tool_choice={"type": "tool", "name": "run_simulation"},
                    )
                    usage.add("stream retry (tool_choice)", response)
                    if response.stop_reason != "tool_use":
                        yield _sse_event({"type": "done", "role": "assistant", "content": text,
                                          "sim_id": None, "usage": usage.as_dict()})
                        return
                    # 下のツール実行に続行
                else:
                    yield _sse_event({"type": "done", "role": "assistant", "content": text,
                                      "sim_id": None, "usage": usage.as_dict()})
                    return

            # ── ツール実行（同期経路と同じ _dispatch_tool_blocks を通す）──
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = []
            async for _kind, _payload in _dispatch_tool_blocks(tool_blocks, state):
                if _kind == "progress":
                    yield _sse_event({"type": "progress", "message": _payload})
                else:
                    tool_results = _payload

            # ── ツール結果を渡して最終回答をストリーミング生成 ──
            yield _sse_event({"type": "progress", "message": "Step 3/3: 結果を分析中..."})

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            _mark_cache_tail(messages)

            # ストリーミングで最終回答を生成するヘルパー
            async def _stream_final_response(msgs):
                """messages を渡して streaming 呼び出し．テキストは text_delta で逐次送信し，
                ツール呼び出しがあれば蓄積して返す．最終テキストも返す．"""
                collected_text = []
                collected_tool_blocks = []

                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=16000,
                    system=_cached_system(system),
                    messages=_api_messages(msgs),
                    tools=_cached_tools(),
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_start":
                            if event.content_block.type == "tool_use":
                                collected_tool_blocks.append({
                                    "index": event.index,
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input_json": "",
                                })
                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                collected_text.append(event.delta.text)
                                yield {"event": "text_delta", "data": event.delta.text}
                            elif event.delta.type == "input_json_delta":
                                if collected_tool_blocks:
                                    collected_tool_blocks[-1]["input_json"] += event.delta.partial_json

                    # stream 終了後，最終 response を取得
                    final_response = stream.get_final_message()
                    usage.add("stream post-tool", final_response)

                # ツールブロックを anthropic オブジェクトとして返す
                real_tool_blocks = [b for b in final_response.content if b.type == "tool_use"]
                full_text = "".join(collected_text)
                yield {"event": "stream_done", "text": full_text, "tool_blocks": real_tool_blocks, "response": final_response}

            # 1回目のストリーミング
            yield _sse_event({"type": "stream_start"})
            stream_result = None
            async for item in _stream_final_response(messages):
                if item["event"] == "text_delta":
                    yield _sse_event({"type": "text_delta", "text": item["data"]})
                elif item["event"] == "stream_done":
                    stream_result = item

            final_text = stream_result["text"]
            next_tool_blocks = stream_result["tool_blocks"]

            # さらにツール呼び出しがある場合はループで追加処理（最大3ラウンド）
            for _round in range(MAX_TOOL_ROUNDS):
                if not next_tool_blocks:
                    break
                yield _sse_event({"type": "stream_end_partial"})
                next_tool_results = []
                async for _kind, _payload in _dispatch_tool_blocks(
                        next_tool_blocks, state, follow_up=True):
                    if _kind == "progress":
                        yield _sse_event({"type": "progress", "message": _payload})
                    else:
                        next_tool_results = _payload

                messages.append({"role": "assistant", "content": stream_result["response"].content})
                messages.append({"role": "user", "content": next_tool_results})
                _mark_cache_tail(messages)

                # 次ラウンドもストリーミング
                yield _sse_event({"type": "stream_start"})
                next_tool_blocks = []
                async for item in _stream_final_response(messages):
                    if item["event"] == "text_delta":
                        yield _sse_event({"type": "text_delta", "text": item["data"]})
                    elif item["event"] == "stream_done":
                        final_text = item["text"]
                        next_tool_blocks = item["tool_blocks"]
                        stream_result = item

            # チャート抽出（$data 参照はこのターンで取得した集計データで解決）
            charts, clean_text = _extract_charts(
                final_text, state.sim_data_cache,
                state.last_data_sim_id or state.sim_id or _conversation_sim_id(body))
            if "```chart" in clean_text.lower() or "```\nchart" in clean_text.lower():
                # 抽出漏れの兆候．ログに残してデバッグ可能に
                idx = clean_text.lower().find("```")
                print(f"[RISU] WARN: chart fence still present after strip; near={clean_text[max(0,idx-20):idx+200]!r}")
            print(f"[RISU] chat done: charts={len(charts)}, clean_text_len={len(clean_text)}")

            # このターンでシミュレーションを実行していなければ sim_id は None のまま
            # （グローバル最新へのフォールバックは無関係な結果を表示させるため廃止）
            resp = {"type": "done", "role": "assistant", "content": clean_text, "sim_id": state.sim_id,
                    "usage": usage.as_dict()}
            if charts:
                resp["charts"] = charts
            yield _sse_event(resp)

        except anthropic.AuthenticationError:
            print("[RISU] CRITICAL: ANTHROPIC_API_KEY invalid!")
            yield _sse_event({"type": "error", "message": "サーバー側で LLM に接続できません．運営にお問い合わせください．"})
        except anthropic.RateLimitError:
            yield _sse_event({"type": "error", "message": "LLM が混雑しています．しばらく待って再試行してください．"})
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            if status == 529:
                yield _sse_event({"type": "error", "message": "LLM が一時的に過負荷です．1〜2 分後に再試行してください．"})
            elif status and 500 <= status < 600:
                yield _sse_event({"type": "error", "message": "LLM サーバー側のエラーです．しばらく後で再試行してください．"})
            else:
                print(f"[RISU] Claude APIStatusError {status}: {e}")
                yield _sse_event({"type": "error", "message": f"LLM エラー（コード: {status}）"})
        except anthropic.APIConnectionError:
            yield _sse_event({"type": "error", "message": "LLM に接続できません．ネットワーク接続を確認してください．"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            # ユーザーには詳細を伏せる
            print(f"[RISU] chat error: {e.__class__.__name__}: {e}")
            yield _sse_event({"type": "error", "message": "処理中にエラーが発生しました．もう一度お試しください．"})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _chat_claude(body: ChatInput):
    """Claude API を使ったチャット（tool_use 対応）"""
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 履歴トリミング + キャッシュ境界 + 動的コンテキスト（system は不変に保つ）
    messages = _build_llm_messages(body)
    system = SYSTEM_PROMPT
    usage = _UsageTally()
    # ツール実行の状態はストリーミング経路と共通（_dispatch_tool_blocks）
    state = _ToolTurnState(body)

    try:
        # 1回目：ツール付きリクエスト
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            system=_cached_system(system),
            messages=_api_messages(messages),
            tools=_cached_tools(),
        )
        usage.add("sync first-round", response)

        # テキストのみの応答（ツール呼び出しなし）
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text")

            # LLM が「実行します」と言ったのにツールを呼ばなかった場合，再試行
            sim_keywords = ["実行します", "作成します", "シミュレーション", "構築します"]
            if any(k in text for k in sim_keywords):
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "run_simulation ツールを呼び出して，今すぐシミュレーションを実行してください．テキストだけでなくツールを使ってください．"})
                retry = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=8192,
                    system=_cached_system(system),
                    messages=_api_messages(_mark_cache_tail(messages)),
                    tools=_cached_tools(),
                )
                usage.add("sync retry", retry)
                if retry.stop_reason == "tool_use":
                    response = retry
                    # 下のツール処理に続行
                else:
                    return {"role": "assistant", "content": text, "sim_id": None, "usage": usage.as_dict()}
            else:
                return {"role": "assistant", "content": text, "sim_id": None, "usage": usage.as_dict()}

        # ツール呼び出しがある場合（複数ツール呼び出しにも対応）
        # dispatch はストリーミング経路と共通．進捗イベントはここでは捨てる．
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results = await _collect_tool_results(tool_blocks, state)

        # ツール結果を渡して次の回答を生成（最大3ラウンド）
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
        _mark_cache_tail(messages)

        for _round in range(MAX_TOOL_ROUNDS):
            resp_next = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                system=_cached_system(system),
                messages=_api_messages(messages),
                tools=_cached_tools(),
            )
            usage.add(f"sync round {_round + 1}", resp_next)

            # テキスト部分を収集
            final_text = "".join(b.text for b in resp_next.content if b.type == "text")

            # さらにツール呼び出しがある場合は処理を続ける
            next_tool_blocks = [b for b in resp_next.content if b.type == "tool_use"]
            if not next_tool_blocks:
                break

            next_tool_results = await _collect_tool_results(
                next_tool_blocks, state, follow_up=True)

            messages.append({"role": "assistant", "content": resp_next.content})
            messages.append({"role": "user", "content": next_tool_results})
            _mark_cache_tail(messages)

        # ```chart ... ``` ブロックからChart.js設定を抽出（$data 参照を解決）
        charts, clean_text = _extract_charts(
            final_text, state.sim_data_cache,
            state.last_data_sim_id or state.sim_id or _conversation_sim_id(body))

        resp = {"role": "assistant", "content": clean_text, "sim_id": state.sim_id,
                "usage": usage.as_dict()}
        if charts:
            resp["charts"] = charts
        return resp

    except anthropic.AuthenticationError:
        raise HTTPException(401, detail="Anthropic API キーが無効です．ANTHROPIC_API_KEY を確認してください")
    except anthropic.RateLimitError:
        raise HTTPException(429, detail="Claude API のレートリミットに達しました．しばらく待ってください")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, detail=f"Claude エラー: {str(e)}")


async def _chat_ollama(body: ChatInput):
    """本番用: Ollama LLM と対話"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": m.role, "content": m.content} for m in body.messages]

    TOOLS_FOR_OLLAMA = [
        {
            "type": "function",
            "function": {
                "name": "run_simulation",
                "description": "UXsim 交通流シミュレーションを実行する",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":    {"type": "string"},
                        "tmax":    {"type": "integer"},
                        "deltan":  {"type": "integer"},
                        "reaction_time": {"type": "number"},
                        "random_seed":   {"type": "integer"},
                        "nodes":   {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}, "flow_capacity": {"type": "number"}}, "required": ["name", "x", "y"]}},
                        "links":   {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "length": {"type": "number"}, "free_flow_speed": {"type": "number"}}, "required": ["name", "start", "end", "length"]}},
                        "demands": {"type": "array", "items": {"type": "object", "properties": {"orig": {"type": "string"}, "dest": {"type": "string"}, "t_start": {"type": "number"}, "t_end": {"type": "number"}, "flow": {"type": "number"}}, "required": ["orig", "dest", "t_start", "t_end", "flow"]}},
                    },
                    "required": ["nodes", "links", "demands"],
                },
            },
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "tools": TOOLS_FOR_OLLAMA, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
            assistant_msg = data["message"]

            if not assistant_msg.get("tool_calls"):
                return {"role": "assistant", "content": assistant_msg["content"], "sim_id": None}

            tool_call = assistant_msg["tool_calls"][0]
            fn_args = tool_call["function"]["arguments"]
            if isinstance(fn_args, str):
                fn_args = json.loads(fn_args)

            sim_input = SimulationInput(**fn_args)
            result = await _run_uxsim_async(sim_input)
            sim_id = str(uuid.uuid4())[:8]
            _store_sim(sim_id, result, {
                "type": "llm",
                "llm_backend": "ollama",
                "tool": "run_simulation",
                "llm_user_message": _last_user_message_text(body),
            })

            messages.append({"role": "assistant", "content": "", "tool_calls": assistant_msg["tool_calls"]})
            messages.append({"role": "tool", "content": json.dumps(result["stats"], ensure_ascii=False)})

            resp2 = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            )
            resp2.raise_for_status()
            final_msg = resp2.json()["message"]["content"]

            return {"role": "assistant", "content": final_msg, "sim_id": sim_id}

    except httpx.TimeoutException:
        raise HTTPException(504, detail="Ollama の応答がタイムアウトしました")
    except httpx.ConnectError:
        raise HTTPException(502, detail="Ollama に接続できません．ollama serve が起動しているか確認してください")
    except Exception as e:
        raise HTTPException(500, detail=f"チャットエラー: {str(e)}")


# ──────────────────────────────────────────────
# CSV / GMNS / OSM インポート
# ──────────────────────────────────────────────

def _find_col(fields: list[str], candidates: list[str], default: str | None = None) -> str | None:
    """fields 内から candidates のいずれかに一致するカラム名を返す（大文字小文字無視）"""
    field_lower = {f.lower(): f for f in fields}
    for c in candidates:
        if c.lower() in field_lower:
            return field_lower[c.lower()]
    return default


def _get_val(row: dict, col: str | None, default=None):
    """行から指定カラムの値を取得（None/空文字はデフォルト値）"""
    if col is None:
        return default
    val = row.get(col, "")
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip()


def _get_float(row: dict, col: str | None, default: float = 0.0) -> float:
    val = _get_val(row, col, None)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_int(row: dict, col: str | None, default: int = 1) -> int:
    val = _get_val(row, col, None)
    if val is None:
        return default
    try:
        return int(float(val))
    except ValueError:
        return default


def _parse_csv_scenario(content: str) -> dict:
    """
    単一 CSV からシナリオを推定する．
    カラム名を柔軟にマッチング．対応フォーマット:
      1) RISU 独自形式: type 列で node/link/demand を区別
      2) ノード CSV: name/id + x/y 座標系カラム
      3) リンク CSV: start/from + end/to 系カラム
      4) 需要 CSV: orig/from + dest/to + volume/flow 系カラム
      5) GMNS 形式 (node_id, from_node_id 等)
    """
    reader = csv.DictReader(io.StringIO(content))
    raw_fields = list(reader.fieldnames or [])
    fields_lower = [f.strip().lower() for f in raw_fields]

    # ============ RISU 独自 CSV: type 列あり ============
    if "type" in fields_lower:
        nodes, links, demands = [], [], []
        col_type = _find_col(raw_fields, ["type"])
        col_name = _find_col(raw_fields, ["name", "id", "node_id", "link_id"])
        col_x = _find_col(raw_fields, ["x", "x_coord", "lon", "longitude"])
        col_y = _find_col(raw_fields, ["y", "y_coord", "lat", "latitude"])
        col_start = _find_col(raw_fields, ["start", "start_node", "from", "from_node", "from_node_id", "source"])
        col_end = _find_col(raw_fields, ["end", "end_node", "to", "to_node", "to_node_id", "target", "dest"])
        col_length = _find_col(raw_fields, ["length", "distance", "dist"])
        col_ffs = _find_col(raw_fields, ["free_flow_speed", "speed", "free_speed", "speed_limit", "ffs"])
        col_lanes = _find_col(raw_fields, ["number_of_lanes", "lanes", "num_lanes"])
        col_lcap = _find_col(raw_fields, ["capacity", "cap"])  # リンク容量（台/s）
        col_orig = _find_col(raw_fields, ["orig", "origin", "o_zone_id", "from", "source"])
        col_dest_d = _find_col(raw_fields, ["dest", "destination", "d_zone_id", "to", "target"])
        col_tstart = _find_col(raw_fields, ["t_start", "start_time", "time_start"])
        col_tend = _find_col(raw_fields, ["t_end", "end_time", "time_end"])
        col_flow = _find_col(raw_fields, ["flow", "volume", "demand", "rate"])

        for row in reader:
            t = _get_val(row, col_type, "").lower()
            if t == "node":
                nodes.append({
                    "name": _get_val(row, col_name, ""),
                    "x": _get_float(row, col_x, 0),
                    "y": _get_float(row, col_y, 0),
                })
            elif t == "link":
                link = {
                    "name": _get_val(row, col_name, ""),
                    "start": _get_val(row, col_start, ""),
                    "end": _get_val(row, col_end, ""),
                    "length": _get_float(row, col_length, 1000),
                    "free_flow_speed": _get_float(row, col_ffs, 20),
                    "number_of_lanes": _get_int(row, col_lanes, 1),
                }
                lcap = _get_float(row, col_lcap, -1)
                if lcap > 0:
                    link["capacity"] = lcap
                links.append(link)
            elif t == "demand":
                demands.append({
                    "orig": _get_val(row, col_orig, ""),
                    "dest": _get_val(row, col_dest_d, ""),
                    "t_start": _get_float(row, col_tstart, 0),
                    "t_end": _get_float(row, col_tend, 3600),
                    "flow": _get_float(row, col_flow, 0.5),
                })
        return {"format": "risu_csv", "nodes": nodes, "links": links, "demands": demands}

    # ============ ノード CSV 判定 ============
    # name/id + x/y 系カラムがあるか
    # 識別子カラムの優先順位: ID 系 > name 系．
    # GMNS 等では node_id が主キーで name は表示ラベル（重複可）のため，
    # name を優先すると「ノード名が重複」エラーになる．
    # （リンクの from_node_id / to_node_id も node_id を参照するので整合する）
    NODE_NAME_COLS = ["node_id", "id", "name", "node_name", "node"]
    NODE_X_COLS = ["x", "x_coord", "lon", "longitude", "lng", "経度"]
    NODE_Y_COLS = ["y", "y_coord", "lat", "latitude", "緯度"]

    col_nname = _find_col(raw_fields, NODE_NAME_COLS)
    col_nx = _find_col(raw_fields, NODE_X_COLS)
    col_ny = _find_col(raw_fields, NODE_Y_COLS)

    if col_nx and col_ny:
        col_cap = _find_col(raw_fields, ["flow_capacity", "capacity", "cap"])
        nodes = []
        for row in reader:
            name = _get_val(row, col_nname, f"n{len(nodes)}")
            x = _get_float(row, col_nx, 0)
            y = _get_float(row, col_ny, 0)
            node = {"name": name, "x": x, "y": y}
            cap = _get_float(row, col_cap, -1)
            if cap > 0:
                node["flow_capacity"] = cap
            nodes.append(node)
        return {"format": "node_csv", "nodes": nodes}

    # ============ リンク CSV 判定 ============
    LINK_START_COLS = ["start", "start_node", "from", "from_node", "from_node_id",
                       "source", "origin", "始点", "start_id"]
    LINK_END_COLS = ["end", "end_node", "to", "to_node", "to_node_id",
                     "target", "dest", "destination", "終点", "end_id"]

    col_ls = _find_col(raw_fields, LINK_START_COLS)
    col_le = _find_col(raw_fields, LINK_END_COLS)

    if col_ls and col_le:
        # 識別子は ID 系を優先（link_id が主キー，name はラベルの可能性がある）
        col_lname = _find_col(raw_fields, ["link_id", "id", "name", "link_name", "link"])
        col_length = _find_col(raw_fields, ["length", "distance", "dist", "長さ"])
        col_ffs = _find_col(raw_fields, ["free_flow_speed", "speed", "free_speed",
                                          "speed_limit", "ffs", "制限速度", "速度"])
        col_lanes = _find_col(raw_fields, ["number_of_lanes", "lanes", "num_lanes", "車線数"])
        col_jd = _find_col(raw_fields, ["jam_density", "kjam"])
        col_lcap = _find_col(raw_fields, ["capacity", "cap", "容量"])

        links = []
        for row in reader:
            name = _get_val(row, col_lname, f"link{len(links)}")
            start = _get_val(row, col_ls, "")
            end = _get_val(row, col_le, "")
            length = _get_float(row, col_length, 1000)
            ffs = _get_float(row, col_ffs, 20)
            lanes = _get_int(row, col_lanes, 1)
            link = {
                "name": name, "start": start, "end": end,
                "length": length, "free_flow_speed": ffs,
                "number_of_lanes": lanes,
            }
            jd = _get_float(row, col_jd, -1)
            if jd > 0:
                link["jam_density"] = jd
            lcap = _get_float(row, col_lcap, -1)
            if lcap > 0:
                link["capacity"] = lcap
            links.append(link)
        return {"format": "link_csv", "links": links}

    # ============ 需要 CSV 判定 ============
    DEMAND_ORIG_COLS = ["orig", "origin", "o_zone_id", "from", "source", "出発"]
    DEMAND_DEST_COLS = ["dest", "destination", "d_zone_id", "to", "target", "到着"]
    DEMAND_VOL_COLS = ["flow", "volume", "demand", "rate", "交通量"]

    col_do = _find_col(raw_fields, DEMAND_ORIG_COLS)
    col_dd = _find_col(raw_fields, DEMAND_DEST_COLS)
    col_dv = _find_col(raw_fields, DEMAND_VOL_COLS)

    if col_do and col_dd and col_dv:
        col_tstart = _find_col(raw_fields, ["t_start", "start_time", "time_start"])
        col_tend = _find_col(raw_fields, ["t_end", "end_time", "time_end"])
        demands = []
        for row in reader:
            vol = _get_float(row, col_dv, 0)
            if vol <= 0:
                continue
            demands.append({
                "orig": _get_val(row, col_do, ""),
                "dest": _get_val(row, col_dd, ""),
                "t_start": _get_float(row, col_tstart, 0),
                "t_end": _get_float(row, col_tend, 3600),
                "flow": vol,
            })
        return {"format": "demand_csv", "demands": demands}

    raise ValueError("CSV 形式を認識できません．ノード（name,x,y），リンク（start,end,length），または RISU CSV 形式を使用してください．")


def _gmns_to_scenario(
    nodes_csv: str | None,
    links_csv: str | None,
    demand_csv: str | None,
    config_csv: str | None = None,
    tmax: int = 3600,
) -> dict:
    """GMNS CSV ファイル群からシナリオ dict を組み立てる"""

    # 単位設定の読み取り
    length_unit = "meters"
    speed_unit = "kmh"
    if config_csv:
        reader = csv.DictReader(io.StringIO(config_csv))
        for row in reader:
            length_unit = row.get("long_length", "meters").strip().lower()
            speed_unit = row.get("speed", "kmh").strip().lower()
            break

    # 単位変換係数
    length_to_m = 1.0
    if length_unit in ("miles", "mile", "mi"):
        length_to_m = 1609.344
    elif length_unit in ("km", "kilometers"):
        length_to_m = 1000.0
    elif length_unit in ("feet", "ft"):
        length_to_m = 0.3048

    speed_to_ms = 1.0 / 3.6  # km/h → m/s default
    if speed_unit in ("mph",):
        speed_to_ms = 0.44704  # mph → m/s
    elif speed_unit in ("m/s", "ms"):
        speed_to_ms = 1.0

    # ノード
    nodes = []
    node_zone_map = {}  # node_id → zone_id
    if nodes_csv:
        parsed = _parse_csv_scenario(nodes_csv)
        coord_is_latlon = False
        # 座標が緯度経度か判定（-180~180 範囲なら）
        for n in parsed["nodes"]:
            if -180 <= n["x"] <= 180 and -90 <= n["y"] <= 90:
                coord_is_latlon = True
            break
        # 緯度経度→メートル変換
        if coord_is_latlon and parsed["nodes"]:
            ref_y = parsed["nodes"][0]["y"]
            deg_to_m = 111320.0
            cos_lat = math.cos(math.radians(ref_y))
            for n in parsed["nodes"]:
                n["x"] = n["x"] * deg_to_m * cos_lat
                n["y"] = n["y"] * deg_to_m
        nodes = parsed["nodes"]

        # zone マッピング（demand 用）
        reader2 = csv.DictReader(io.StringIO(nodes_csv))
        for row in reader2:
            nid = str(row.get("node_id", "").strip())
            zid = str(row.get("zone_id", "").strip())
            if zid and zid != "0" and zid != "":
                node_zone_map[zid] = nid

    # リンク
    links = []
    if links_csv:
        parsed = _parse_csv_scenario(links_csv)
        for lk in parsed["links"]:
            lk["length"] = lk["length"] * length_to_m
            raw_speed = lk["free_flow_speed"]
            lk["free_flow_speed"] = max(raw_speed * speed_to_ms, 1.0)
            # GMNS の capacity は台/時/車線 → 台/s（リンク全体）に変換
            if "capacity" in lk:
                lanes = max(1, int(lk.get("number_of_lanes", 1) or 1))
                lk["capacity"] = round(lk["capacity"] * lanes / 3600.0, 4)
        links = parsed["links"]

    # 需要
    demands = []
    if demand_csv:
        parsed = _parse_csv_scenario(demand_csv)
        if parsed["format"] in ("gmns_demand", "demand_csv"):
            for d in parsed["demands"]:
                orig_zone = str(d["orig"])
                dest_zone = str(d["dest"])
                # zone_id → node_id マッピング
                orig_node = node_zone_map.get(orig_zone, orig_zone)
                dest_node = node_zone_map.get(dest_zone, dest_zone)
                # flow がすでに台/秒の場合はそのまま，volume が大きい場合は台/時→台/秒変換
                flow = d.get("flow", 0)
                if flow > 10:  # 10 台/秒超 → 台/時と推定
                    flow = flow / 3600.0
                if flow > 0:
                    demands.append({
                        "orig": orig_node,
                        "dest": dest_node,
                        "t_start": d.get("t_start", 0),
                        "t_end": d.get("t_end", min(tmax * 0.6, 3600)),
                        "flow": round(flow, 6),
                    })

    return {
        "name": "gmns_import",
        "tmax": tmax,
        "deltan": 5,
        "nodes": nodes,
        "links": links,
        "demands": demands,
    }


# OSM 道路種別プリセット
# custom_filter は Overpass QL の highway タグフィルタ．
# None のプリセットは network_type で取得する．
_OSM_ROAD_PRESETS = {
    # 高速道路・国道級のみ（広域・大半径向け．ノード数が大幅に減り高速）
    "major": '["highway"~"motorway|trunk|primary|motorway_link|trunk_link|primary_link"]',
    # 幹線道路まで（major + 2次・3次幹線．都市スケールの標準）
    "arterial": '["highway"~"motorway|trunk|primary|secondary|tertiary'
                '|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"]',
    # 一般車道（住宅街の道路含む．サービス道路・駐車場内通路は除外）
    "drive": None,
    # 全車道（サービス道路・駐車場内通路含む．最も細かいが最も重い）
    "all": None,
}
_OSM_NETWORK_TYPE = {"drive": "drive", "all": "drive_service"}


def _run_osm_import(place: str, distance_m: int = 1000, road_types: str = "drive") -> dict:
    """OSM から道路ネットワークを取得して UXsim シナリオに変換．
    OSMnx のグラフを直接活用し，道路形状・速度推定・車線数を取得する．

    road_types: "major" | "arterial" | "drive" | "all"（_OSM_ROAD_PRESETS 参照）
    """
    import osmnx as ox

    # OSM キャッシュ先の上書き（Docker 等で永続ボリュームに向ける用）
    _cache_dir = os.getenv("RISU_OSM_CACHE_DIR")
    if _cache_dir:
        ox.settings.cache_folder = _cache_dir

    road_types = (road_types or "drive").strip().lower()
    if road_types not in _OSM_ROAD_PRESETS:
        raise ValueError(
            f"road_types は {', '.join(_OSM_ROAD_PRESETS)} のいずれかを指定してください: {road_types}"
        )

    # ── ジオコーディング: 自然言語 → (lat, lon) ──
    center = ox.geocode(place)  # (lat, lon)
    center_lat, center_lon = center

    # ── 道路ネットワーク取得 ──
    custom_filter = _OSM_ROAD_PRESETS[road_types]
    try:
        if custom_filter is not None:
            G = ox.graph_from_point(center, dist=distance_m, custom_filter=custom_filter)
        else:
            G = ox.graph_from_point(
                center, dist=distance_m, network_type=_OSM_NETWORK_TYPE[road_types]
            )
    except Exception as e:
        # 対象道路が範囲内に存在しない場合（郊外で major 指定など）
        raise ValueError(
            f"「{place}」周辺（半径{distance_m}m）で road_types='{road_types}' に該当する"
            f"道路が見つかりませんでした．road_types を 'arterial' や 'drive' に広げるか，"
            f"半径を大きくしてください．（{e.__class__.__name__}）"
        ) from e
    G = ox.add_edge_speeds(G)       # highway 種別から速度推定 (speed_kph)

    # メートル座標に投影
    Gp = ox.project_graph(G)

    # ── ノード抽出 ──
    scenario_nodes = []
    node_name_map = {}  # osm_node_id -> name
    for node_id, data in Gp.nodes(data=True):
        name = str(node_id)
        node_name_map[node_id] = name
        scenario_nodes.append({
            "name": name,
            "x": round(float(data["x"]), 2),
            "y": round(float(data["y"]), 2),
        })

    # ── リンク抽出（道路形状の中間点も含む） ──
    scenario_links = []
    link_geometries = {}  # link_name -> [[x,y], [x,y], ...]
    link_idx = 0
    for u, v, _key, data in Gp.edges(keys=True, data=True):
        u_name = node_name_map.get(u)
        v_name = node_name_map.get(v)
        if u_name is None or v_name is None:
            continue

        link_name = f"link_{link_idx}"
        link_idx += 1

        length = float(data.get("length", 100))
        speed_kph = float(data.get("speed_kph", 30))
        speed_ms = round(speed_kph / 3.6, 2)

        # 車線数
        lanes_raw = data.get("lanes", 1)
        if isinstance(lanes_raw, list):
            lanes_raw = lanes_raw[0]
        try:
            lanes = max(1, int(lanes_raw))
        except (ValueError, TypeError):
            lanes = 1

        scenario_links.append({
            "name": link_name,
            "start": u_name,
            "end": v_name,
            "length": round(length, 2),
            "free_flow_speed": speed_ms,
            "number_of_lanes": lanes,
        })

        # 道路形状（GeoJSON 用の座標列）
        if "geometry" in data:
            coords = [[round(x, 2), round(y, 2)] for x, y in data["geometry"].coords]
        else:
            u_data = Gp.nodes[u]
            v_data = Gp.nodes[v]
            coords = [
                [round(u_data["x"], 2), round(u_data["y"], 2)],
                [round(v_data["x"], 2), round(v_data["y"], 2)],
            ]
        link_geometries[link_name] = coords

    _road_labels = {
        "major": "主要道路（高速・国道級）",
        "arterial": "幹線道路",
        "drive": "一般車道",
        "all": "全車道",
    }
    return {
        "name": f"osm_{place[:30]}",
        "tmax": 3600,
        "deltan": 5,
        "nodes": scenario_nodes,
        "links": scenario_links,
        "demands": [],
        "link_geometries": link_geometries,
        "center": {"lat": center_lat, "lon": center_lon},
        "distance_m": distance_m,
        "summary": (
            f"OSM から「{place}」周辺（半径{distance_m}m，{_road_labels[road_types]}）の"
            f"道路ネットワークを取得しました．"
            f"{len(scenario_nodes)} ノード，{len(scenario_links)} リンク．"
        ),
    }


def _generate_osm_demands(nodes: list[dict], links: list[dict], tmax: int = 3600) -> list[dict]:
    """OSM ネットワークの境界ノードから多方向の需要を生成し，全道路を利用させる．

    ネットワーク周縁（境界）のノードを特定し，それら全ペア間に需要を設定する．
    これにより交通がネットワーク全体に分散する．
    """
    if len(nodes) < 2:
        return []

    # ノード座標
    xs = [n["x"] for n in nodes]
    ys = [n["y"] for n in nodes]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)

    # 接続情報: 各ノードの次数（リンク端点として登場する回数）
    degree = {}
    for lk in links:
        degree[lk["start"]] = degree.get(lk["start"], 0) + 1
        degree[lk["end"]] = degree.get(lk["end"], 0) + 1

    # 境界ノード候補: 次数が少ない（行き止まり・端点）ノード，
    # またはネットワーク中心から遠いノード
    node_by_name = {n["name"]: n for n in nodes}
    max_dist = max(math.hypot(n["x"] - cx, n["y"] - cy) for n in nodes) or 1

    # スコア: 中心からの距離が大きい + 次数が小さい → 境界ノードらしい
    scored = []
    for n in nodes:
        d = math.hypot(n["x"] - cx, n["y"] - cy) / max_dist  # 0~1
        deg = degree.get(n["name"], 0)
        # 次数1（行き止まり）=高スコア，次数2=中，次数3以上=低
        deg_score = 1.0 if deg <= 1 else (0.6 if deg == 2 else 0.3)
        scored.append((d * 0.6 + deg_score * 0.4, n["name"]))

    scored.sort(reverse=True)

    # 上位ノードから境界ノードを選択（最大8個，最小4個）
    # 近すぎるノード同士は除外
    min_sep = max_dist * 0.3  # 中心からの最大距離の30%以上離れていること
    boundary_nodes = []
    for _, name in scored:
        n = node_by_name[name]
        too_close = False
        for bn in boundary_nodes:
            bn_node = node_by_name[bn]
            if math.hypot(n["x"] - bn_node["x"], n["y"] - bn_node["y"]) < min_sep:
                too_close = True
                break
        if not too_close:
            boundary_nodes.append(name)
            if len(boundary_nodes) >= 8:
                break

    # 最低4ノード確保できない場合はしきい値を下げて再試行
    if len(boundary_nodes) < 4:
        boundary_nodes = [name for _, name in scored[:min(8, len(scored))]]

    # 全ペア間に需要を生成
    demands = []
    n_boundary = len(boundary_nodes)
    # ペア数に応じてフロー量を調整（多すぎると渋滞しすぎる）
    n_pairs = n_boundary * (n_boundary - 1)
    flow_per_pair = max(0.05, min(0.3, 2.0 / max(n_pairs, 1)))

    for i in range(n_boundary):
        for j in range(n_boundary):
            if i == j:
                continue
            demands.append({
                "orig": boundary_nodes[i],
                "dest": boundary_nodes[j],
                "t_start": 0,
                "t_end": tmax * 0.5,
                "flow": round(flow_per_pair, 3),
            })

    return demands


# ---- GMNS GitHub データセット取得 ----

GMNS_REPO = "HanZhengIntelliTransport/GMNS_Plus_Dataset"
GMNS_API  = f"https://api.github.com/repos/{GMNS_REPO}/contents"
GMNS_RAW  = f"https://raw.githubusercontent.com/{GMNS_REPO}/main"


@app.get("/gmns/datasets")
async def list_gmns_datasets():
    """GMNS リポジトリのデータセット一覧を取得"""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(GMNS_API)
        if r.status_code != 200:
            raise HTTPException(502, detail="GitHub API に接続できません")
        items = r.json()
        datasets = [
            {"name": item["name"], "path": item["path"]}
            for item in items
            if item["type"] == "dir" and not item["name"].startswith(".")
               and item["name"] not in ("Documents", "GMNS_Tools", "Incomplete_Networks")
        ]
    return {"datasets": datasets}


@app.post("/gmns/import")
async def import_gmns(dataset: str = Form(...), tmax: int = Form(3600)):
    """GMNS データセットを GitHub からダウンロードしてシミュレーション実行"""
    async with httpx.AsyncClient(timeout=30) as client:
        # データセット内のファイル一覧を取得
        r = await client.get(f"{GMNS_API}/{dataset}")
        if r.status_code != 200:
            raise HTTPException(404, detail=f"データセット '{dataset}' が見つかりません")

        files = {item["name"].lower(): item["name"] for item in r.json() if item["type"] == "file"}

        # 必要な CSV をダウンロード
        async def fetch_csv(filename):
            actual = files.get(filename)
            if not actual:
                return None
            resp = await client.get(f"{GMNS_RAW}/{dataset}/{actual}")
            return resp.text if resp.status_code == 200 else None

        nodes_csv  = await fetch_csv("node.csv")
        links_csv  = await fetch_csv("link.csv")
        demand_csv = await fetch_csv("demand.csv")
        config_csv = await fetch_csv("config.csv")

    if not nodes_csv or not links_csv:
        raise HTTPException(400, detail=f"データセット '{dataset}' に node.csv / link.csv がありません")

    try:
        scenario = _gmns_to_scenario(nodes_csv, links_csv, demand_csv, config_csv, tmax)
    except Exception as e:
        raise HTTPException(422, detail=f"GMNS パースエラー: {str(e)}")

    if not scenario["links"]:
        raise HTTPException(422, detail="リンクが0件です")

    # 需要がなければダミー生成
    if not scenario["demands"] and len(scenario["nodes"]) >= 2:
        scenario["demands"] = [{
            "orig": scenario["nodes"][0]["name"],
            "dest": scenario["nodes"][-1]["name"],
            "t_start": 0,
            "t_end": 1800,
            "flow": 0.3,
        }]

    scenario["name"] = dataset

    sim_input = _scenario_to_input(scenario)
    result = await _run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {"type": "gmns", "dataset_id": dataset})

    return {
        "id": sim_id,
        "stats": result["stats"],
        "message": f"GMNS '{dataset}' をインポート（{len(scenario['nodes'])} ノード, {len(scenario['links'])} リンク）",
    }


# ---- ファイルアップロードエンドポイント ----

@app.post("/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    tmax: int = Form(3600),
):
    """CSV / JSON ファイルをアップロードしてシミュレーションを実行"""
    # ファイルの内容を読み取り
    file_contents = {}
    for f in files:
        raw = await f.read()
        content = raw.decode("utf-8-sig")  # BOM 対応
        name = f.filename.lower() if f.filename else ""
        file_contents[name] = content

    # ---- JSON ファイルの場合 ----
    json_files = [n for n in file_contents if n.endswith(".json")]
    if json_files:
        payload = json.loads(file_contents[json_files[0]])
        # 新スキーマ (risu_schema_version 1.0) に対応
        # 形式 1: { "risu_schema_version": "1.0", "scenario": {...}, ... }
        # 形式 2: 旧 RISU JSON: トップレベルに nodes/links/demands
        imported_from = None
        if isinstance(payload, dict) and (
            "risu_schema_version" in payload or "scenario" in payload
        ):
            scenario_dict = payload.get("scenario", payload)
            imported_from = payload.get("sim_id")
        else:
            scenario_dict = payload
        # 道路形状（多点 LineString）が同梱されていれば描画に使う（OSM 取込と同じ仕組み）
        link_geometries = scenario_dict.pop("link_geometries", None) if isinstance(scenario_dict, dict) else None
        sim_input = _scenario_to_input(scenario_dict)
        result = await _run_uxsim_async(sim_input)
        if link_geometries:
            _apply_link_geometries(result, link_geometries)
        sim_id = str(uuid.uuid4())[:8]
        source = {"type": "json", "filename": json_files[0]}
        if imported_from:
            source["imported_from_sim_id"] = imported_from
        _store_sim(sim_id, result, source)
        return {
            "id": sim_id,
            "stats": result["stats"],
            "message": "JSON ファイルからシミュレーション実行完了"
                       + (f"（再現: {imported_from}）" if imported_from else ""),
        }

    # ---- CSV ファイルの場合 ----
    csv_files = {n: c for n, c in file_contents.items() if n.endswith(".csv")}

    if not csv_files:
        raise HTTPException(400, detail="JSON または CSV ファイルをアップロードしてください")

    # 単一 CSV の場合（RISU 独自形式を試行）
    if len(csv_files) == 1:
        name, content = next(iter(csv_files.items()))
        parsed = _parse_csv_scenario(content)

        if parsed["format"] == "risu_csv":
            scenario = {
                "name": "csv_import",
                "tmax": tmax,
                "deltan": 5,
                "nodes": parsed["nodes"],
                "links": parsed["links"],
                "demands": parsed["demands"],
            }
            sim_input = _scenario_to_input(scenario)
            result = await _run_uxsim_async(sim_input)
            sim_id = str(uuid.uuid4())[:8]
            _store_sim(sim_id, result, {
                "type": "csv",
                "format": "risu_csv",
                "filename": name,
            })
            return {
                "id": sim_id,
                "stats": result["stats"],
                "message": "RISU CSV からシミュレーション実行完了",
            }

        # 単一の GMNS node/link ファイルの場合，シミュレーションはせずネットワークだけ返す
        return {
            "id": None,
            "parsed": parsed,
            "message": f"GMNS {parsed['format']} を読み込みました．node.csv + link.csv + demand.csv を一緒にアップロードするとシミュレーションを実行します．",
        }

    # 複数 CSV → GMNS セットとして処理
    nodes_csv = None
    links_csv = None
    demand_csv = None
    config_csv = None
    for name, content in csv_files.items():
        if "node" in name:
            nodes_csv = content
        elif "link" in name:
            links_csv = content
        elif "demand" in name:
            demand_csv = content
        elif "config" in name:
            config_csv = content

    if not nodes_csv and not links_csv:
        raise HTTPException(400, detail="node.csv または link.csv が見つかりません")

    scenario = _gmns_to_scenario(nodes_csv, links_csv, demand_csv, config_csv, tmax)

    if not scenario["links"]:
        raise HTTPException(400, detail="link.csv が見つからないかリンクが0件です")

    # 需要がない場合はダミー需要を生成（可視化だけ可能に）
    if not scenario["demands"] and len(scenario["nodes"]) >= 2:
        scenario["demands"] = [{
            "orig": scenario["nodes"][0]["name"],
            "dest": scenario["nodes"][-1]["name"],
            "t_start": 0,
            "t_end": 1800,
            "flow": 0.3,
        }]

    sim_input = _scenario_to_input(scenario)
    result = await _run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, result, {
        "type": "gmns",
        "format": "files",
        "filenames": list(csv_files.keys()),
    })
    return {
        "id": sim_id,
        "stats": result["stats"],
        "message": f"GMNS データからシミュレーション実行完了（{len(scenario['nodes'])} ノード, {len(scenario['links'])} リンク）",
    }


@app.post("/import/osm")
async def import_osm(place: str = Form(...), tmax: int = Form(3600),
                     distance_m: int = Form(500),
                     road_types: str = Form("drive")):
    """OpenStreetMap から道路ネットワークを取得

    road_types: major（高速・国道級のみ） / arterial（幹線まで） /
                drive（一般車道，デフォルト） / all（サービス道路含む全車道）
    """
    # 入力バリデーション
    place = place.strip()
    if not place or len(place) > 200:
        raise HTTPException(400, detail="地名が不正です（空または長すぎます）")
    if any(c in place for c in "\x00\n\r\t\u2028\u2029"):
        raise HTTPException(400, detail="地名に制御文字が含まれています")
    if distance_m < 50 or distance_m > 5000:
        raise HTTPException(400, detail="半径は 50m〜5000m の範囲で指定してください")
    if tmax < 60 or tmax > MAX_TMAX:
        raise HTTPException(400, detail=f"tmax は 60〜{MAX_TMAX} 秒の範囲で指定してください")
    road_types = road_types.strip().lower()
    if road_types not in _OSM_ROAD_PRESETS:
        raise HTTPException(400, detail=f"road_types は {', '.join(_OSM_ROAD_PRESETS)} のいずれかを指定してください")

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(executor, _run_osm_import, place, distance_m, road_types)
    except Exception as e:
        raise HTTPException(500, detail=f"OSM インポートエラー: {str(e)}")

    # 需要なしでも可視化用にダミー需要を追加してシミュレーション実行
    scenario = dict(result)
    link_geometries = scenario.pop("link_geometries", {})
    scenario.pop("center", None)
    scenario.pop("distance_m", None)
    scenario.pop("summary", None)
    scenario["tmax"] = tmax

    if not scenario["demands"] and len(scenario["nodes"]) >= 2:
        scenario["demands"] = _generate_osm_demands(
            scenario["nodes"], scenario["links"], tmax
        )

    sim_input = _scenario_to_input(scenario)
    sim_result = await _run_uxsim_async(sim_input)
    _apply_link_geometries(sim_result, link_geometries)
    sim_id = str(uuid.uuid4())[:8]
    _store_sim(sim_id, sim_result, {
        "type": "osm",
        "place": place,
        "road_types": road_types,
        "distance_m": distance_m,
    })

    return {
        "id": sim_id,
        "stats": sim_result["stats"],
        "message": result.get("summary", ""),
        "scenario": scenario,
    }


# ---- MCP SSE エンドポイント ----

sse_transport = SseServerTransport("/mcp/messages/")

@app.get("/mcp")
async def mcp_sse(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1], mcp_server.create_initialization_options()
        )

@app.post("/mcp/messages/")
async def mcp_messages(request: Request):
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# ---- ヘルスチェック ----
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---- 静的ファイル（UI）----
import os

if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    if RISU_HOST not in ("127.0.0.1", "localhost", "::1"):
        # 認証が無いので，LAN に開くのは利用者の明示的な選択であるべき．
        # 気づかないまま公開されている状態を作らないよう，起動時に警告する．
        print(
            f"[RISU] 警告: {RISU_HOST} で待ち受けます．RISU は認証を持たないため，"
            "このネットワークから接続できる全員がシミュレーション実行・結果閲覧・"
            "LLM 呼び出し（= API キーの課金）を行えます．"
        )
    print(f"[RISU] http://{'localhost' if RISU_HOST in ('127.0.0.1', '0.0.0.0') else RISU_HOST}:{RISU_PORT}")
    uvicorn.run("server:app", host=RISU_HOST, port=RISU_PORT, reload=RISU_RELOAD)
