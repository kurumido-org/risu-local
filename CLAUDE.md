# RISU (ローカル版) — Claude Code 開発ガイド

> **RISU**: Real-time Interactive Simulator for Urban mobility
> 交通流シミュレーター UXsim をブラウザ上で LLM と対話しながら操作する OSS アプリ。
> 認証・クレジット・管理画面を持たない、ローカル利用向けのシングルユーザー版。
> （マルチユーザー/サーバー版は別リポジトリ `RISU` で管理）

## 構成

```
risu-local/
├── server.py          ← FastAPI + UXsim + LLM + MCP（単一ファイル）
├── static/
│   ├── index.html     ← UI 本体（チャット + Canvas 可視化 + Chart.js）
│   └── sample_risu.csv
├── tests/test_stability.py
├── requirements.txt
└── .env               ← LLM_BACKEND / ANTHROPIC_API_KEY（.env.example をコピー）
```

## 起動

```powershell
.venv\Scripts\activate
python server.py       # http://localhost:8001
```

## server.py の主要部

| 関数/エンドポイント | 説明 |
|---|---|
| `_run_uxsim(scenario)` | UXsim 同期実行 → GeoJSON + 個車フレーム + 統計 |
| `_get_simulation_data(sim_id)` | LLM チャート生成用の集計データ |
| `_parse_csv_scenario` / `_gmns_to_scenario` | CSV / GMNS パーサー |
| `_run_osm_import(place)` | OSMnx で道路ネットワーク取得 |
| `POST /simulate` / `GET /results/{id}` | 直接実行 / 結果取得 |
| `POST /chat` | LLM 対話（claude=SSE ストリーミング / ollama / mock） |
| `POST /upload` / `/import/osm` / `/gmns/import` | データインポート |
| `GET /mcp` | MCP SSE エンドポイント |

LLM ツール: `run_simulation` / `get_simulation_data`。
チャートは LLM が ````chart```` ブロックで Chart.js 設定を出力 → フロントで描画。

フロントの GUI ネットワークエディタ（index.html「EDIT」ボタン）: `editNet` を
編集して `/simulate` に POST する。編集モード中は `drawFrame` が `drawEditFrame`
に委譲され、キャンバスの mousedown / hover も editMode で分岐する。

## テスト

```powershell
pytest tests/ -v
```

## 注意

- 結果ストアは in-memory dict（`results_store`）。再起動で消える。
- 双方向道路は A→B / B→A の 2 リンクで表現（描画は円弧）。
- このリポジトリには認証・課金コードを追加しないこと（サーバー版と分離）。

## パフォーマンス上の前提（変更時に壊さないこと）

- `W.analyzer.basic_analysis` は無効化してある（od_analysis → floyd_warshall が
  O(ノード数^3) で、5000 ノード級 OSM では 1 回 45 秒超）。統計 3 値
  （total/completed/average_travel_time）は `_run_uxsim` 内で車両ログから直接計算。
- OSM インポートは `road_types` プリセット（major/arterial/drive/all）で規模を制御。
  デフォルト drive。半径 1000m 以上は arterial/major を使う（システムプロンプトで LLM に指示済み）。

- uxsim は C++ バックエンド（1.14 beta, `World(cpp=True)`）優先、TypeError で純 Python にフォールバック。
- `_run_uxsim` の後処理は numpy ベクトル化済み。cpp バックエンドでは `veh._log_cache` の
  生 int 配列（state コード / リンク index）を直接読む fast path がある。
  uxsim 更新時はこの内部構造（`_LOG_STATE_MAP`, `_log_cache`）の互換性を確認すること。
- フレームは列指向 `columnar_v2`（{ids, xs, ys, vs, alphas, li}、li は `link_names` への index）。
  `_get_simulation_data`・フロントの描画・テストすべてがこの形式に依存。
- `/results` は ORJSONResponse を直接返して jsonable_encoder を回避 + GZip 圧縮。
- `drawFrame`（60fps）はバウンディングボックス・双方向判定を geoData 単位でキャッシュ。
