"""FastAPI アプリ: ミドルウェア，HTTP エンドポイント，静的ファイル．起動は server.py．
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse as _JSONResp
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request

from .importers import GMNS_API, GMNS_RAW, OSM_ROAD_PRESETS, gmns_to_scenario, parse_csv_scenario, run_osm_import
from .llm import LLM_BACKEND, chat_claude_stream, chat_mock, chat_ollama
from .mcp_server import router as mcp_router
from .results import (
    RESULTS_DIR,
    build_envelope,
    envelope_compressed_bytes,
    envelope_json_bytes,
    list_results,
    negotiate_encoding,
    persisted_ids,
    results_store,
    store_sim,
)
from .runtime import RUNTIME_STATUS, executor, log
from .scenario_ops import generate_osm_demands, osm_demand_summary
from .schema import ChatInput, ChatMessage, SimulationInput, scenario_to_input
from .simulation import MAX_TMAX, apply_link_geometries, run_uxsim_async, startup_selfcheck, validate_scenario_size

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "ALLOWED_ORIGINS",
    "RISU_HOST",
    "RISU_PORT",
    "RISU_RELOAD",
    "app",
]

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


# ──────────────────────────────────────────────
# FastAPI アプリ
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    if RESULTS_DIR:
        log.info(f"results dir: {os.path.abspath(RESULTS_DIR)} "
              f"({len(persisted_ids())} persisted results, loaded on demand)")
    if os.getenv("RISU_STARTUP_SELFCHECK", "1").lower() not in ("0", "false", "no"):
        await asyncio.get_event_loop().run_in_executor(executor, startup_selfcheck)
    yield
    # executor はプロセス共有（runtime）なのでここでは閉じない．アプリの起動/停止が
    # 複数回起きる場面（テストの e2e サーバー，reload）で，停止後に別の app や
    # テストが executor.submit すると "cannot schedule new futures after shutdown" になる．
    # プロセス終了時にスレッドは自動的に片付く．

# orjson があれば高速な JSON シリアライズをデフォルトにする（/results は MB 級）
try:
    from fastapi.responses import ORJSONResponse as _DefaultJSONResponse
except ImportError:  # orjson 未インストール時は標準 JSON にフォールバック
    from fastapi.responses import JSONResponse as _DefaultJSONResponse

app = FastAPI(title="RISU API", lifespan=lifespan,
              default_response_class=_DefaultJSONResponse)

# ──────────────────────────────────────────────
# グローバル例外ハンドラー
# ──────────────────────────────────────────────


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """
    HTTPException 以外の未捕捉例外を 500 に統一．
    詳細はサーバーログだけに残し，ユーザーには汎用メッセージ．
    """
    # HTTPException は FastAPI が処理するが，念のため
    if isinstance(exc, HTTPException):
        raise exc
    path = request.url.path if request else "?"
    log.error(f"unhandled: path={path} error={exc.__class__.__name__}: {exc}", exc_info=exc)
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

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

# ──────────────────────────────────────────────
# HTML はキャッシュさせない（UI 更新時にブラウザが古い画面を出さないように．
# ETag 再検証で 304 が返るので転送コストはほぼゼロ）
# ──────────────────────────────────────────────
@app.middleware("http")
async def no_cache_html_mw(request, call_next):
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    path = request.url.path
    # HTML と自前の JS / CSS は no-cache（更新直後に古い画面・古いロジックが動かないように．
    # /vendor/ の同梱ライブラリはバージョンが上がったときだけ変わるので通常のキャッシュでよい）
    if "text/html" in ctype or path.startswith("/js/") or path.startswith("/css/"):
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


@app.post("/simulate")
async def simulate(scenario: SimulationInput):
    """シミュレーションを実行して結果IDを返す"""
    validate_scenario_size(scenario)
    result = await run_uxsim_async(scenario)
    sim_id = str(uuid.uuid4())[:8]
    store_sim(sim_id, result, {"type": "manual"})
    return {"id": sim_id, "stats": result["stats"]}


@app.get("/results")
async def get_results_list(limit: int = 50):
    """結果の一覧（メモリ + RISU_RESULTS_DIR）．本体は含まない．"""
    return {"results": list_results(limit)}


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
    encoding = negotiate_encoding(request.headers.get("accept-encoding", ""))
    if encoding == "identity":
        body = await loop.run_in_executor(executor, envelope_json_bytes, sim_id)
        # ここでは Vary を付けない．Content-Encoding が無い応答には GZipMiddleware が
        # 素通し時に Vary: Accept-Encoding を足すので，自前で付けると重複する．
        # （圧縮済みの応答は middleware が触らないので，そちらは自前で付ける）
        return Response(body, media_type="application/json")
    body = await loop.run_in_executor(executor, envelope_compressed_bytes, sim_id, encoding)
    return Response(body, media_type="application/json",
                    headers={"Content-Encoding": encoding, "Vary": "Accept-Encoding"})


@app.get("/results/{sim_id}/scenario")
async def get_results_scenario(sim_id: str):
    """再現用の軽量エンベロープ（scenario + meta のみ，result なし）を返す．"""
    if sim_id not in results_store:
        raise HTTPException(404, detail="Result not found")
    return build_envelope(sim_id, include_result=False)


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
        return await chat_mock(last_msg)
    elif LLM_BACKEND == "claude":
        return await chat_claude_stream(body)
    else:
        return await chat_ollama(body)


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
        scenario = gmns_to_scenario(nodes_csv, links_csv, demand_csv, config_csv, tmax)
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

    sim_input = scenario_to_input(scenario)
    result = await run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    store_sim(sim_id, result, {"type": "gmns", "dataset_id": dataset})

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
        sim_input = scenario_to_input(scenario_dict)
        result = await run_uxsim_async(sim_input)
        if link_geometries:
            apply_link_geometries(result, link_geometries)
        sim_id = str(uuid.uuid4())[:8]
        source = {"type": "json", "filename": json_files[0]}
        if imported_from:
            source["imported_from_sim_id"] = imported_from
        store_sim(sim_id, result, source)
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
        parsed = parse_csv_scenario(content)

        if parsed["format"] == "risu_csv":
            scenario = {
                "name": "csv_import",
                "tmax": tmax,
                "deltan": 5,
                "nodes": parsed["nodes"],
                "links": parsed["links"],
                "demands": parsed["demands"],
            }
            sim_input = scenario_to_input(scenario)
            result = await run_uxsim_async(sim_input)
            sim_id = str(uuid.uuid4())[:8]
            store_sim(sim_id, result, {
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

    scenario = gmns_to_scenario(nodes_csv, links_csv, demand_csv, config_csv, tmax)

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

    sim_input = scenario_to_input(scenario)
    result = await run_uxsim_async(sim_input)
    sim_id = str(uuid.uuid4())[:8]
    store_sim(sim_id, result, {
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
    if road_types not in OSM_ROAD_PRESETS:
        raise HTTPException(400, detail=f"road_types は {', '.join(OSM_ROAD_PRESETS)} のいずれかを指定してください")

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(executor, run_osm_import, place, distance_m, road_types)
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
        scenario["demands"] = generate_osm_demands(
            scenario["nodes"], scenario["links"], tmax
        )
        demand_info = osm_demand_summary(scenario["demands"], tmax)
    else:
        demand_info = {"method": "provided", "note": "取込データに含まれていた需要をそのまま使用"}

    sim_input = scenario_to_input(scenario)
    sim_result = await run_uxsim_async(sim_input)
    apply_link_geometries(sim_result, link_geometries)
    sim_id = str(uuid.uuid4())[:8]
    store_sim(sim_id, sim_result, {
        "type": "osm",
        "place": place,
        "road_types": road_types,
        "distance_m": distance_m,
        "demand": demand_info,
    })

    return {
        "id": sim_id,
        "stats": sim_result["stats"],
        "message": result.get("summary", ""),
        "scenario": scenario,
    }


# ---- ヘルスチェック ----
@app.get("/healthz")
async def healthz():
    # uxsim のバックエンドと高速経路の状態（直近の実行）．fast_path=false なら §3.6 を確認
    return {"status": "ok", "uxsim": dict(RUNTIME_STATUS)}


# ---- 静的ファイル（UI）----

app.include_router(mcp_router)
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
