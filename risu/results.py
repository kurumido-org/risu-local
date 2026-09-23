"""結果ストアと送出: results_store，エンベロープ，v3 符号化，圧縮キャッシュ，永続化（RISU_RESULTS_DIR）．
"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "RESULTS_DIR",
    "build_envelope",
    "encode_frames_v3",
    "envelope_compressed_bytes",
    "envelope_gzip_bytes",
    "envelope_json_bytes",
    "list_results",
    "negotiate_encoding",
    "persisted_ids",
    "persisted_path",
    "results_store",
    "store_sim",
]

try:
    import orjson
except ImportError:  # orjson 未インストール時は標準 JSON にフォールバック
    orjson = None

from .runtime import RISU_SCHEMA_VERSION, RISU_VERSION, UXSIM_VERSION, executor, log

# ──────────────────────────────────────────────
# グローバル状態（本番はRedis等に置き換える）
# ──────────────────────────────────────────────
# 結果の永続化先（任意）．設定するとシミュレーション結果をここに書き，再起動後も
# results_store が透過的に読み戻す（_ResultsStore）．形式はダウンロードの .json+result と同じ．
RESULTS_DIR = os.getenv("RISU_RESULTS_DIR", "").strip()
_SAFE_SIM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # ファイル名に使うので経路要素を許さない


class _ResultsStore(dict):
    """シミュレーション結果のメモリストア（sim_id → run_uxsim の戻り値）．

    RISU_RESULTS_DIR が設定されているときは，メモリに無い sim_id をディスクから
    遅延ロードする．呼び出し側は普通の dict として扱えばよい（`in` / `[]` / `.get`）．
    メモリ側は MAX_RESULTS 件のキャッシュで，追い出された結果もディスクから戻る．
    keys() / len() はメモリにあるものだけを数える（ディスクの一覧は persisted_ids）．

    スレッド: イベントループ・executor（シミュレーション後の保存，永続化，圧縮）が
    同時に触るので，複数手順になる操作（追加＋追い出し，遅延ロード，圧縮キャッシュの
    生成）は `lock`（RLock）で囲む．単発の dict 操作は GIL で壊れないので囲まない．
    """

    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()

    def __contains__(self, key):
        return dict.__contains__(self, key) or persisted_path(key) is not None

    def __missing__(self, key):
        with self.lock:
            if dict.__contains__(self, key):      # 他スレッドが先にロードした
                return dict.__getitem__(self, key)
            path = persisted_path(key)
            if path is None:
                raise KeyError(key)
            result = _load_persisted(path)
            dict.__setitem__(self, key, result)
            return result

    def get(self, key, default=None):   # dict.get は __missing__ を呼ばない
        try:
            return self[key]
        except KeyError:
            return default

    def put(self, sim_id: str, result: dict, max_results: int) -> list[str]:
        """追加して上限超過分を古い順に追い出す（1 つのロック区間で）．戻り値は追い出した id．"""
        evicted = []
        with self.lock:
            dict.__setitem__(self, sim_id, result)
            while max_results > 0 and len(self) > max_results:
                old_id = next(iter(self))
                if old_id == sim_id:
                    break
                dict.pop(self, old_id, None)
                evicted.append(old_id)
        return evicted


results_store: dict[str, Any] = _ResultsStore()
# 結果ストアに保持する件数の上限．超えたら古いものから捨てる（frames の numpy 列と
# gzip キャッシュで 1 件数十 MB になり得るため，無制限だとメモリ不足で落ちる）．
MAX_RESULTS = int(os.getenv("RISU_MAX_RESULTS", "30"))
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


def store_sim(sim_id: str, result: dict, source: dict | None = None) -> None:
    """シミュレーション結果を results_store に保存し，再現性メタを付与する．

    result は run_uxsim の戻り値（"_scenario" を含む）．
    source は呼び出し経路の由来を表す任意の辞書（省略時は manual）．
    """
    src = source or {"type": "manual"}
    result["_meta"] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": src,
    }
    # 追加と追い出し（古い順．永続化していればディスクから戻る）は 1 つのロック区間で
    for old_id in results_store.put(sim_id, result, MAX_RESULTS):
        log.info(f"results_store evicted {old_id} (limit {MAX_RESULTS})")
    # 永続化は executor で（圧縮に数百 ms かかることがあり，イベントループを塞がない）．
    # 戻り値の Future はテストが完了を待つために使う．
    if RESULTS_DIR:
        return executor.submit(_persist_sim, sim_id)
    return None


# ──────────────────────────────────────────────
# 結果の永続化（RISU_RESULTS_DIR）
#   ファイル形式はダウンロードの .json+result（build_envelope）と同じ．
#   圧縮は zstd（zstandard が無ければ gzip）．ダウンロードした JSON をそのまま置いても読める．
#   frames はエンベロープと同じく v3（量子化）で保存されるので，読み戻した結果の位置・速度は
#   v3 の分解能（1 m / 0.1 m/s / 0.001）になる．統計・台数・累積系列は無損失．
# ──────────────────────────────────────────────
def persisted_path(sim_id: str) -> str | None:
    """RESULTS_DIR に sim_id の保存ファイルがあればそのパス．"""
    if not RESULTS_DIR or not isinstance(sim_id, str) or not _SAFE_SIM_ID.match(sim_id):
        return None
    for ext in (".json.zst", ".json.gz", ".json"):
        p = os.path.join(RESULTS_DIR, sim_id + ext)
        if os.path.isfile(p):
            return p
    return None


def persisted_ids() -> list[str]:
    """RESULTS_DIR にある sim_id の一覧（新しい順）．"""
    if not RESULTS_DIR or not os.path.isdir(RESULTS_DIR):
        return []
    found = []
    for name in os.listdir(RESULTS_DIR):
        if name.endswith(".meta.json"):     # 一覧用のサイドカー（result_summary）
            continue
        for ext in (".json.zst", ".json.gz", ".json"):
            if name.endswith(ext):
                sid = name[: -len(ext)]
                if _SAFE_SIM_ID.match(sid):
                    found.append((os.path.getmtime(os.path.join(RESULTS_DIR, name)), sid))
                break
    return [sid for _, sid in sorted(found, reverse=True)]


def _persist_sim(sim_id: str) -> str | None:
    """results_store[sim_id] を RESULTS_DIR に書く．戻り値は書いたパス．

    envelope_compressed_bytes を使うので，あとで /results が同じ方式を要求したときは
    キャッシュがそのまま使われる．一時ファイルに書いてから rename する（途中で落ちても壊れない）．
    """
    if not RESULTS_DIR or sim_id not in results_store:
        return None
    try:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        encoding = "zstd" if _zstd is not None else "gzip"
        blob = envelope_compressed_bytes(sim_id, encoding)
        path = os.path.join(RESULTS_DIR, f"{sim_id}.json.{'zst' if encoding == 'zstd' else 'gz'}")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
        # 一覧用のサイドカー（本体を読まずに GET /results で出せるように）
        meta_path = os.path.join(RESULTS_DIR, f"{sim_id}.meta.json")
        with open(meta_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(result_summary(sim_id, results_store[sim_id]), f, ensure_ascii=False)
        os.replace(meta_path + ".tmp", meta_path)
        log.info(f"persisted {sim_id} -> {path} ({len(blob)/1e6:.1f}MB)")
        return path
    except Exception as e:   # 永続化の失敗でシミュレーション自体は失敗させない
        log.warning(f"persist {sim_id} failed: {e.__class__.__name__}: {e}")
        return None


def result_summary(sim_id: str, raw: dict) -> dict:
    """一覧・サイドカー用の要約（本体のフレームは含めない．数百バイト）．"""
    sc = raw.get("_scenario") or {}
    meta = raw.get("_meta") or {}
    src = meta.get("source") or {}
    return {
        "sim_id": sim_id,
        "name": sc.get("name"),
        "created_at": meta.get("created_at"),
        # 出所は識別に要るものだけ（ユーザーの発話文は載せない）
        "source": {k: v for k, v in src.items()
                   if k in ("type", "via", "tool", "place", "distance_m", "road_types", "base_sim_id", "demand")},
        "tmax": sc.get("tmax"),
        "random_seed": sc.get("random_seed"),
        "nodes": len(sc.get("nodes") or []),
        "links": len(sc.get("links") or []),
        "demands": len(sc.get("demands") or []),
        "stats": raw.get("stats") or {},
        "in_memory": dict.__contains__(results_store, sim_id),
        "persisted": persisted_path(sim_id) is not None,
    }


def list_results(limit: int = 50) -> list[dict]:
    """メモリ上とディスク上（RESULTS_DIR）の結果一覧を新しい順に返す．

    ディスクだけにあるものはサイドカー（<sim_id>.meta.json）を読む．サイドカーの無い
    ファイル（ダウンロード JSON を置いただけ等）は sim_id と更新時刻だけの行になる．
    本体は読まないので，件数が多くても軽い．
    """
    limit = max(1, min(int(limit or 50), 500))
    with results_store.lock:
        items = list(dict.items(results_store))
    rows = {sid: result_summary(sid, raw) for sid, raw in items}
    for sid in persisted_ids():
        if sid in rows:
            continue
        meta_path = os.path.join(RESULTS_DIR, f"{sid}.meta.json")
        row = None
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    row = json.load(f)
            except (OSError, ValueError) as e:
                log.warning(f"sidecar {meta_path} unreadable: {e}")
        if row is None:
            path = persisted_path(sid)
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat() if path else None
            row = {"sim_id": sid, "name": None, "created_at": mtime, "source": {"type": "file"},
                   "stats": {}, "nodes": None, "links": None, "demands": None}
        row["in_memory"] = False
        row["persisted"] = True
        rows[sid] = row
    return sorted(rows.values(), key=lambda r: r.get("created_at") or "", reverse=True)[:limit]


def _decode_frames_v3(frames: dict) -> dict:
    """columnar_v3（送出/保存形式）を results_store 内部の columnar_v2 に戻す．

    encode_frames_v3 の逆変換．static/js/risu-core.js の decodeFrame と同じ規則
    （ids 累積和 / xs,ys 整数 m / vs ×0.1 / alphas ×0.001）．
    """
    import numpy as np
    out = {}
    for key, f in frames.items():
        ids = np.cumsum(np.asarray(f.get("ids", ()), dtype=np.int64)).astype(np.int32)
        out[key] = {
            "ids":    ids,
            "xs":     np.asarray(f.get("xs", ()), dtype=np.float64),
            "ys":     np.asarray(f.get("ys", ()), dtype=np.float64),
            "vs":     np.round(np.asarray(f.get("vs", ()), dtype=np.float64) * 0.1, 2),
            "alphas": np.round(np.asarray(f.get("alphas", ()), dtype=np.float64) * 0.001, 4),
            "li":     np.asarray(f.get("li", ()), dtype=np.int32),
        }
    return out


def _result_from_envelope(env: dict) -> dict:
    """ダウンロード/保存形式のエンベロープを results_store の内部表現に組み立てる．"""
    import numpy as np
    res = env.get("result") or {}
    frames = res.get("frames") or {}
    if res.get("frame_format") == "columnar_v3":
        frames = _decode_frames_v3(frames)
    else:   # v2（素の値のリスト）→ numpy 列
        frames = {k: {c: np.asarray(v.get(c, ()), dtype=(np.int32 if c in ("ids", "li") else np.float64))
                      for c in ("ids", "xs", "ys", "vs", "alphas", "li")}
                  for k, v in frames.items() if isinstance(v, dict)}
    return {
        "_scenario":  env.get("scenario") or {},
        "_meta": {
            "created_at": env.get("created_at"),
            "source": env.get("source") or {"type": "unknown"},
            "persisted": True,
        },
        "geojson":     res.get("geojson"),
        "frames":      frames,
        "frame_times": res.get("frame_times") or [],
        "stats":       res.get("stats") or {},
        "tmax":        res.get("tmax"),
        "signals":     res.get("signals") or [],
        "link_names":  res.get("link_names"),
        "frame_format": "columnar_v2",
        "vehicle_sample_step": res.get("vehicle_sample_step", 1),
        "vehicle_counts":  res.get("vehicle_counts"),
        "frame_avg_speed": res.get("frame_avg_speed"),
        "speed_histogram": res.get("speed_histogram"),
        "trip_series":     res.get("trip_series"),
    }


def _load_persisted(path: str) -> dict:
    """保存ファイル（.json.zst / .json.gz / .json）を読み，内部表現に戻す．"""
    with open(path, "rb") as f:
        blob = f.read()
    if path.endswith(".zst"):
        if _zstd is None:
            raise RuntimeError(f"{path}: zstandard が無いので読めません（pip install zstandard）")
        data = _zstd.ZstdDecompressor().decompressobj().decompress(blob)
    elif path.endswith(".gz"):
        data = gzip.decompress(blob)
    else:
        data = blob
    env = orjson.loads(data) if orjson is not None else json.loads(data.decode("utf-8"))
    result = _result_from_envelope(env)
    # 保存したバイト列は圧縮済みエンベロープそのものなので，同じ方式の応答キャッシュに使う
    enc = "zstd" if path.endswith(".zst") else ("gzip" if path.endswith(".gz") else None)
    if enc:
        result["_enc_cache"] = {enc: blob}
    log.info(f"loaded persisted result {os.path.basename(path)} ({len(blob)/1e6:.1f}MB)")
    return result


def build_envelope(sim_id: str, *, include_result: bool = True) -> dict:
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
            # フレームごとの車両平均速度（台数重み，間引き前）
            "frame_avg_speed": raw.get("frame_avg_speed"),
            # 速度分布（全フレーム・全車両の観測点，間引き前）
            "speed_histogram": raw.get("speed_histogram"),
            # 流入・到着の累積台数（実イベント．時間軸は 0・各フレーム・tmax）
            "trip_series": raw.get("trip_series"),
        }
    return envelope

def envelope_json_bytes(sim_id: str) -> bytes:
    """完全エンベロープを JSON バイト列に直列化する（frames の numpy 列も直接）．"""
    env = build_envelope(sim_id, include_result=True)
    _res = env.get("result")
    if _res is not None and _res.get("frame_format") == "columnar_v2" and _res.get("frames"):
        # 送出時だけ columnar_v3（量子化＋差分符号化）に変換する．
        # results_store 側の配列は触らない（get_simulation_data など
        # サーバー内の消費側は素の値を前提にしているため）．
        _res["frames"] = encode_frames_v3(_res["frames"])
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


def encode_frames_v3(frames: dict) -> dict:
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


def negotiate_encoding(accept_encoding: str) -> str:
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


def envelope_compressed_bytes(sim_id: str, encoding: str) -> bytes:
    """圧縮済みエンベロープ．結果は不変なので sim × 方式ごとに 1 回だけ作ってキャッシュする．

    キャッシュは `_enc_cache` に方式名をキーにして持つ．実際に要求された方式しか
    作らないので，1 種類しか使われない通常運用ではメモリは従来と変わらない
    （1 件数十 MB になり得るため，両方を先回りして作らない）．
    """
    raw = results_store[sim_id]
    with results_store.lock:
        cache = raw.get("_enc_cache")
        if cache is None:
            cache = raw["_enc_cache"] = {}
        blob = cache.get(encoding)
    if blob is not None:
        return blob

    # 圧縮はロックの外で（数百 ms かかる．同時要求が重なっても同じ内容を作るだけ）
    t0 = time.perf_counter()
    data = envelope_json_bytes(sim_id)
    t1 = time.perf_counter()
    if encoding == "zstd":
        blob = _zstd.ZstdCompressor(level=RESULTS_ZSTD_LEVEL).compress(data)
    else:
        blob = gzip.compress(data, compresslevel=RESULTS_GZIP_LEVEL)
    with results_store.lock:
        blob = cache.setdefault(encoding, blob)   # 先に入れた方を採用
    log.info(f"/results/{sim_id}: json={len(data)/1e6:.1f}MB ({t1-t0:.2f}s) "
          f"{encoding}={len(blob)/1e6:.1f}MB ({time.perf_counter()-t1:.2f}s)")
    return blob


def envelope_gzip_bytes(sim_id: str) -> bytes:
    """gzip 済みエンベロープ（後方互換のための薄いラッパー）．"""
    return envelope_compressed_bytes(sim_id, "gzip")
