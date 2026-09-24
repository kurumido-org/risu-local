# RISU — 開発ガイド

> **RISU**: Real-time Interactive Simulator for Urban mobility
> 交通流シミュレーター UXsim をブラウザ上で LLM と対話しながら操作する OSS アプリ．
> 認証・課金・管理画面を持たない，ローカル利用向けのシングルユーザー版．
> （マルチユーザー / サーバー版は別リポジトリ `RISU` で管理）

利用者向けの説明は [README.md](README.md) にあります．このファイルは**コードを変更する人**
向けで，とくに [3. 設計上の不変条件](#3-設計上の不変条件) は，知らずに触ると
性能・コスト・互換性が静かに壊れる箇所をまとめたものです．変更前に該当節を読んでください．

| 節 | 読むべき場面 |
|---|---|
| [1. クイックリファレンス](#1-クイックリファレンス) | 起動・テスト・計測をしたい |
| [2. アーキテクチャ](#2-アーキテクチャ) | 全体像を掴みたい |
| [3. 設計上の不変条件](#3-設計上の不変条件) | **コードを変更する前に** |
| [4. 変更時のチェックリスト](#4-変更時のチェックリスト) | 特定の変更をする（逆引き） |
| [5. 既知の落とし穴](#5-既知の落とし穴) | 原因不明の不具合を踏んだ |
| [6. 公開しないもの](#6-公開しないもの) | ファイルを追加する |

---

## 1. クイックリファレンス

```powershell
.venv\Scripts\activate

python server.py                     # 起動 → http://localhost:8001
pytest tests/ -v                     # テスト全件（サーバー起動が要るものは未起動なら自動 skip）
ruff check .                         # lint（CI の lint ジョブと同一設定）
pyright --pythonpath .venv\Scripts\python.exe   # 型チェック（basic．対象は pyproject の [tool.pyright]）
python scripts\bench.py              # 性能ベンチ（§3.5 / §3.6 の前提を確認）
python scripts\bench.py --sizes 20 --profile   # cProfile 付き
```

サーバーを介さず素の UXsim で動かす経路:

```powershell
python scripts\run_scenario.py scenario.json              # 実行して統計を表示
python scripts\run_scenario.py --sim-id 6c49d38a          # 起動中の RISU から取得
python scripts\run_scenario.py scenario.json --csv out\   # UXsim 標準の CSV 出力
python scripts\run_scenario.py scenario.json --emit x.py  # 単体で動くスクリプトを生成
```

---

## 2. アーキテクチャ

### 2.1 ファイル構成

```
risu-local/
├── server.py             ← 起動だけ（python server.py）．関数を足さない（TestPackageLayout）
├── risu/                 ← サーバー本体．import は一方向（上 → 下）で循環させない
│   ├── runtime.py        ← .env 読込・バージョン・executor・uxsim 経路の状態（依存なし）
│   ├── schema.py         ← pydantic の入力スキーマと検証（依存なし）
│   ├── simulation.py     ← UXsim 実行と後処理（フレーム / 統計 / 系列）§3.5 §3.6
│   ├── results.py        ← results_store・エンベロープ・v3 符号化・圧縮・永続化
│   ├── aggregate.py      ← LLM 向け集計 get_simulation_data §3.3
│   ├── scenario_ops.py   ← 差分命令・grid・OD 自動生成
│   ├── importers.py      ← CSV / GMNS / OSM 取込
│   ├── prompts.py        ← SYSTEM_PROMPT・CLAUDE_TOOLS・mock（固定文字列）§3.4
│   ├── tools.py          ← ツール実行 dispatch_tool_blocks と各ハンドラ §3.2
│   ├── llm.py            ← claude / ollama / mock，Prompt Caching，履歴，使用量
│   ├── mcp_server.py     ← MCP（ツール定義と実行は tools と共有）
│   └── api.py            ← FastAPI app・ミドルウェア・エンドポイント
├── uxsim_bridge.py       ← シナリオ → UXsim World（RISU 非依存の純粋モジュール）
├── static/
│   ├── index.html        ← UI のマークアップだけ（CSS / JS は読み込む）
│   ├── css/risu.css      ← スタイル
│   ├── js/risu-core.js   ← フロントの純粋ロジック（フレーム復号・統計・現示）．DOM に触らない．node でテスト
│   ├── js/app.js …       ← UI の JS（app / editor / playback / chat / render / charts / tooltip / ui / stats）．
│   │                        配信は risu/api.py の UI_SCRIPTS の順に 1 本へ連結（/js/risu.bundle.js）．
│   │                        classic script でグローバルを共有するので順序を変えない．ファイルを足したら UI_SCRIPTS へ
│   ├── vendor/           ← marked / DOMPurify / Chart.js（同梱．ライセンス表記を消さないこと．CDN に戻さない＝オフラインで動く前提）
│   └── sample_risu.csv
├── scripts/
│   ├── run_scenario.py   ← 素の UXsim で実行する CLI（サーバー不要）
│   └── bench.py          ← 後処理・直列化のベンチマーク
├── tests/
│   ├── helpers.py        ← 共通のシナリオ定数とフェイク Anthropic クライアント
│   ├── test_<module>.py  ← risu/<module>.py に対応（simulation / schema / results / llm / tools / api …）
│   ├── test_frontend.py  ← フロントのソース検査，test_project.py ← ライセンス衛生・構成ガード
│   ├── js/core.test.js   ← risu-core.js の単体テスト（node --test tests/js/*.test.js，依存なし）
│   └── e2e/              ← headless Chromium のスモークテスト（playwright が無ければ skip）
├── pyproject.toml        ← ruff / pytest 設定 + パッケージメタデータ
├── requirements.txt      ← 範囲指定（上限付き）
├── requirements.lock.txt ← 検証済みの完全な pip freeze（Python 3.12 用）．CI の 3.12 ジョブはこれに固定，他の Python と週次は最新で走る
└── .env                  ← LLM_BACKEND / ANTHROPIC_API_KEY（.env.example をコピー）
```

### 2.2 データの流れ

```
ブラウザ ──① 指示──→ /chat ──② tool_use──→ dispatch_tool_blocks
                                                    │
                                            ③ uxsim_bridge.build_world
                                                    │
                                              UXsim（C++ backend）
                                                    │
                                            ④ run_uxsim の後処理
                                              （frames / GeoJSON / 統計）
                                                    │
                                          results_store[sim_id]（メモリ）
                                                    │
ブラウザ ←─⑥ zstd/gzip─ /results/{id} ←─⑤ columnar_v3 に量子化
```

**要点は「LLM がデータ実体を通らない」こと**（③〜⑤は LLM を経由しない）．
LLM が扱うのは `sim_id`・差分命令・集計値だけです（§3.3）．

### 2.3 モジュールの責務

| 関数 / エンドポイント | モジュール | 役割 |
|---|---|---|
| `build_world` | `uxsim_bridge` | シナリオ → UXsim World．**サーバーと CLI の共通経路**（§3.1） |
| `run_uxsim(scenario)` | `risu.simulation` | UXsim 実行 → GeoJSON + 個車フレーム + 統計 |
| `dispatch_tool_blocks` | `risu.tools` | LLM ツールの実行．**全経路で共通**（§3.2） |
| `get_simulation_data(sim_id)` | `risu.aggregate` | LLM に渡す集計データ（数 KB に制限） |
| `apply_modifications` | `risu.scenario_ops` | `rerun_simulation` の差分命令をシナリオに適用 |
| `parse_csv_scenario` / `gmns_to_scenario` | `risu.importers` | CSV / GMNS パーサー |
| `run_osm_import(place)` | `risu.importers` | OSMnx で道路ネットワーク取得 |
| `envelope_compressed_bytes` | `risu.results` | 結果の直列化 + 圧縮（方式別キャッシュ） |
| `POST /simulate` / `GET /results/{id}` | `risu.api` | 直接実行 / 結果取得 |
| `POST /chat` | `risu.api` → `risu.llm` | LLM 対話（claude = SSE ストリーミング / ollama / mock） |
| `GET /mcp` | `risu.mcp_server` | MCP SSE．ツール定義は `CLAUDE_TOOLS` をそのまま公開し，実行は `mcp_call_tool` → `dispatch_tool_blocks` |

**命名**: モジュールの外（他モジュール・server.py・scripts・tests）から使う名前は `_` なしの
公開名にし，各モジュールの `__all__` に列挙する．モジュール内だけの補助は `_` 付き．
新しく外から使うことになったら `_` を外して `__all__` に足す（テストが private を import しない）．

設定値（`MAX_FRAME_POINTS` や `RESULTS_DIR` など）は**それを使うモジュールが env から読む**．
テストで差し替えるときはそのモジュールを `monkeypatch.setattr` する
（`from risu.x import Y` で束縛した先を差し替えても効かない）．

**LLM ツール**: `run_simulation` / `rerun_simulation` / `get_network_info` /
`get_simulation_data` / `import_osm_network` / `list_simulations` / `compare_simulations`．
チャートは LLM が ````chart```` ブロックで Chart.js 設定を出力し，フロントが描画します．

**結果ストア**は `results_store`（`_ResultsStore`，dict 派生）です．既定では in-memory で
再起動すると消え，`MAX_RESULTS`（既定 30）を超えると `store_sim` が古い順に追い出します
（1 件数十 MB になり得るため）．`RISU_RESULTS_DIR` を設定すると `store_sim` が executor で
`_persist_sim` を走らせ，メモリに無い sim_id は `__missing__` でディスクから遅延ロードします
（呼び出し側は普通の dict として扱う）．複数手順になる操作（追加＋追い出し = `put`，
遅延ロード，圧縮キャッシュの生成）は `results_store.lock` で囲む．遅延ロードも `put` を
通す（読み戻しで上限を超えない）．永続化スレッドには結果 dict を直接渡す（id で
引き直すと，上限で先に追い出された結果が保存されない）．エンベロープの
`uxsim_version` / `risu_version` は `_meta` に記録した**実行当時**の版で，現在の環境は
`exported_with`．**保存形式はダウンロードの `.json+result`
（`build_envelope`）と同一**で，別形式を増やさないこと．読み戻しは `_result_from_envelope`
（frames は `_decode_frames_v3` で v2 に戻す．risu-core.js の decodeFrame と同じ規則）．

**GUI ネットワークエディタ**（index.html の「EDIT」）は `editNet` を編集して
`/simulate` に POST します．編集モード中は `drawFrame` が `drawEditFrame` に委譲され，
キャンバスの mousedown / hover も `editMode` で分岐します．

**双方向道路**は A→B / B→A の 2 リンクで表現します（UXsim のリンクは一方通行．
描画は円弧で描き分け）．

---

## 3. 設計上の不変条件

各項目は **何を守るか / なぜ / 壊すと何が起きるか / 守っているテスト** の形で書いてあります．

### 3.1 ネットワーク構築は `uxsim_bridge` に一本化する

シナリオ → UXsim World の変換は `uxsim_bridge.build_world` だけが行います．
`server.py` 側に同じ構築処理を持たせないこと．

- **なぜ**: サーバー経路（`run_uxsim`）とオフライン経路（`scripts/run_scenario.py`）が
  同じコードを通らないと，片方だけ直したときに**同じシナリオで結果がずれます**．
- **追加条件**: このモジュールは **uxsim と標準ライブラリしか import しない**．
  FastAPI / pydantic / anthropic を足さないこと（CLI が重い依存なしで動く前提）．
- **シナリオ全体のパラメータ**（`tmax` / `deltan` / `reaction_time` / `random_seed`）は
  `SimulationInput` → `build_world` → `World(...)` と素通しする．**新しく足したら 3 経路
  すべてに通すこと**: `apply_modifications` の `set_params`（`_SCENARIO_PARAM_FIELDS`），
  GUI エディタの送信（index.html の `et-run`），`scripts/run_scenario.py` の `EMIT_TEMPLATE`．
  GUI 送信から `reaction_time` が抜けて，編集のたびに容量の前提が UXsim 既定へ戻る
  不具合があった．`random_seed` を省くと結果は実行ごとに変わる（経路選択ノイズ・合流順）．
- **テスト**: `TestStandalonePipeline`・`TestGuiRerunCarriesScenarioParams`．
  import の純粋性，サーバー経路との統計一致，seed の再現性と全経路での維持を検証．
  CI の `standalone` ジョブも uxsim だけの環境で実行．

### 3.2 LLM ツールの実行は `dispatch_tool_blocks` に一本化する

ストリーミング経路（`chat_claude_stream`）と同期経路（`_chat_claude`）の，
初回ラウンドと追加ラウンドで同じコードを通します．

- **なぜ**: 以前は同じ dispatch が **4 箇所**にコピーされており，実際に
  「同期経路の初回ラウンドだけ未知ツールの分岐が無い」というバグが潜んでいました．
- **仕組み**: 進捗は `("progress", msg)` として yield し，SSE 経路だけが転送，
  同期経路は `collect_tool_results` で捨てる．1 ターン分の状態
  （`sim_id` / `sim_data_cache` / `last_data_sim_id`）は `ToolTurnState` で持ち回る．
- **絶対条件**: **tool_use には必ず 1 対 1 で tool_result を返す**（Anthropic API の要求．
  欠けると 400）．未知のツール名でも結果を積むこと．
- **ツールを追加するとき**: `dispatch_tool_blocks` に分岐を 1 つ足すだけでよい．
  `follow_up` は 2 ラウンド目以降で，進捗の文言と保存メタの `round` が変わる．
- **テスト**: `TestToolDispatch`・`TestChatStreamingPath`．

MCP（`mcp_call_tool`）も同じ dispatcher を通します．会話コンテキストが無いので
`ToolTurnState(body=None, via="mcp")` で呼び，進捗イベントは捨てます．
ツール定義も `CLAUDE_TOOLS` を変換して公開するので，**MCP 専用の定義を書かないこと**
（`TestMcpParity` が同一性を検証）．

フロントも同様に，SSE 経路と JSON 経路の応答反映を `renderAssistantResponse` に
一本化しています（sim バッジ・チャート・トークン使用量の付け方）．

### 3.3 LLM にデータ実体を渡さない

数千〜1 万リンクのネットワークを扱うため，LLM が扱うのは
**ID・差分・要約・パラメトリック命令だけ**にします．

- **既存ネットワークの修正**: `rerun_simulation` の差分命令を使う．
  `run_simulation` で全体を再送しないこと（OSM 由来の大規模網では必ずサイズ超過で失敗する）．
- **格子ネットワーク**: `run_simulation` の `grid` テンプレートと `auto_demands` を使う．
  `expand_run_simulation_args` がサーバー側で nodes/links/demands に展開する．
  LLM に列挙させると 10×10 で出力 2 万トークンを超える．命名規則は tool_result で返す．
- **OD 需要の生成**: `generate_demands` でサーバー側に抽選させる．
  LLM はノード名を知る必要がない．
- **ネットワークの照会**: `get_network_info` は `summary` → 絞り込み（`name_contains` +
  `limit` ≤200 + `offset`）の順で使う．全件取得はできない設計．
- **集計データ**: `get_simulation_data` は `points`（既定 30）・`max_links`（既定 20）で
  量を制御し，速度は 0.1 m/s に丸める．1 回あたり数 KB に収めること．
- **分析用の系列はフレームから作らない**: `run_uxsim` が間引き前の全点・実イベントから
  `vehicle_counts` / `frame_avg_speed` / `speed_histogram` / `trip_series` を作り，画面と
  LLM（`get_simulation_data`）はそれを共有する．フレームは描画専用（時刻・車両とも間引かれる）．
- **台数の単位**: フレームの `ids` は UXsim の**プラトン**（1 個 = `deltan` 台）で，
  描画用にさらに `vehicle_sample_step` 個に 1 個へ間引かれることがある．台数として
  LLM や画面に出す値は `run_uxsim` が**間引き前の全点**から数えた `vehicle_counts`
  （`deltan` 換算済み）を使い，フレームの点数をそのまま台数と呼ばない
  （`network_vehicle_count` が 1/5 になっていた）．古い結果向けの近似は
  `点数 × deltan × vehicle_sample_step`（サーバー・フロントとも同じフォールバック）．
- **チャート**: `{"$data": "network_avg_speed"}` 形式の参照を `extract_charts` →
  `_resolve_chart_refs` がこのターンの集計データ（`sim_data_cache`）で置換する．
  **LLM に配列を書き写させない**（出力トークンは入力の 5 倍単価）．
- **テスト**: `TestScenarioModifications`・`TestNetworkInfo`・
  `TestSimulationDataAggregation`・`TestChartExtraction`．

### 3.4 プロンプトキャッシュを壊さない

- **system プロンプトは不変**（`SYSTEM_PROMPT` そのまま）．sim_id 入りの
  【現在のコンテキスト】は `build_llm_messages` が最後の user メッセージに付ける．
  **system に動的な文字列を足すと，tools 以外のキャッシュが毎ターン無効になる．**
- **キャッシュ境界**（`cache_control`）は API 上限の 4 つ:
  tools 末尾 / system / 履歴の最後の assistant（ターン跨ぎ）/ リクエスト末尾
  （`mark_cache_tail`，同一ターン内の tool ラウンド）．
  末尾の印は `_tail_marked` フラグで管理し，`api_messages` が送信前に剥がす．
  **messages を組み立て直したら必ず `mark_cache_tail` を呼ぶこと．**
- **履歴のトリミング**: `trim_history` が `MAX_HISTORY_CHARS` で古いターンから落とす．
  超過時は予算の半分まで落とす**ヒステリシス**（毎ターン 1 件ずつ落とすと prefix が
  毎回変わってキャッシュが当たらない）．フロントは tool_use / tool_result を履歴に
  残さない（最終テキストのみ）．
- **計測**: 1 ターンの使用量は `UsageTally` が集計し，done イベントの `usage` で
  フロントへ（吹き出し下の `.usage-meta`）．サーバーログにも `usage ...` が出る（logger `risu`）．
- **テスト**: `TestLLMTokenSaving`・`TestConversationContext`．

### 3.5 結果データの表現 — 保存は v2，送出は v3

フレームは列指向 `{ids, xs, ys, vs, alphas, li}`（`li` は `link_names` への index）です．

| | 形式 | 用途 |
|---|---|---|
| `results_store` | **`columnar_v2`**（素の値，numpy 配列のまま） | サーバー内の消費側が前提にしている |
| `/results` の応答 | **`columnar_v3`**（量子化 + 差分符号化） | 転送量とブラウザの parse を減らす |

- **保存側の条件**: 各列は **numpy 配列のまま保持する**（Python float 化しない．メモリ 1/4）．
  フレーム内の ids は昇順（フロントの補間と v3 の差分符号化がこれを前提にしている）．
  消費側（`get_simulation_data`・テスト）は list / ndarray どちらでも動くよう書くこと．
  標準 `json.dumps` に結果 dict を直接渡さない（`envelope_json_bytes` を使う）．
  **結果 dict は保存後に変更しないこと**（圧縮キャッシュが古くなる）．
- **v3 の符号化**（`encode_frames_v3`，`envelope_json_bytes` 内で送出時のみ適用）:
  `ids` = 差分符号化 int32 / `xs`,`ys` = 1 m 丸め int32 / `vs` = 0.1 m/s 単位 int16 /
  `alphas` = 0.001 単位 int16 / `li` = そのまま．
  効果（`scripts\bench.py --sizes 20` で再現できる．エンベロープ全体の値）:
  json 88.0→63.4 MB，gzip 22.2→16.0 MB，直列化 0.37→0.14 秒．
- **v2 を残す理由**: ダウンロード済みの古い JSON を読めるよう，フロントの v2 経路を
  消さないこと．フロントは `r.frame_format` で分岐する．
  **精度を変えるときはサーバーの `encode_frames_v3` とフロントの `isV3` ブランチを必ず同時に直す．**
- **間引き**: 総点数が `MAX_FRAME_POINTS`（既定 300 万，`0` で無効）を超えると
  車両 ID を `vehicle_sample_step` 間隔でサンプリングし，**描画用 frames だけ**を減らす．
  timeline と統計は全点から計算．`get_simulation_data` は台数を step 倍に補正する．
- **テスト**: `TestFrameWireEncodingV3`・`TestPostProcessingPipeline`・
  `TestFrameKeyCompatibility`．

### 3.6 後処理に車両ごとの Python ループを持たない

- `W.analyzer.basic_analysis` は**無効化してある**（od_analysis → floyd_warshall が
  O(ノード数³)．5,000 ノード級 OSM では 1 回 45 秒超）．統計 3 値
  （total / completed / average_travel_time）は `run_uxsim` 内で車両ログから直接計算する．
- **fast path**: cpp バックエンドでは `W._cpp_world.build_all_vehicle_logs_flat_compact()`
  で全車両ログをフラット配列として 1 回で受け取り（`_collect_run_points`），
  `W._skip_log_on_terminate = True` で uxsim 側の車両別 `_log_cache` 構築
  （数万台で数秒）を省略する．リンク別平均速度は (フレーム, リンク) キーの `np.bincount`，
  フレーム分割は `searchsorted`．
  **uxsim 更新時はこの内部 API の互換性を確認すること**（`_LOG_STATE_MAP`，
  `_veh_by_index` の順序 = `VEHICLES` の順序，`offsets`）．失敗すると車両別ログの
  フォールバックに落ちる（動くが遅い）．
  **静かに遅くなるのを防ぐ仕組み**: 使われた経路は結果の `_runtime.fast_path`・
  `RUNTIME_STATUS`（`/healthz`）・実行ログ（`fast_path=on/OFF`）に出る．起動時に
  `startup_selfcheck` が最小シナリオを流して警告し（`RISU_STARTUP_SELFCHECK=0` で無効），
  `test_fast_path_is_active_for_installed_uxsim` が cpp 環境でフォールバックしたら CI を落とす．
- **時刻方向の間引き**（`MAX_FRAMES`，既定 200）は `select_frames` が bincount + LUT で
  O(N) に行い，**列抽出の前に**適用する（np.unique / isin のソートは 5,000 万点で数秒かかった）．
- uxsim は C++ バックエンド（`World(cpp=True)`，1.14 以降）優先．`TypeError` で
  純 Python にフォールバックする．
- **転送**: `envelope_compressed_bytes` が orjson（`OPT_SERIALIZE_NUMPY`）+ 圧縮を
  executor スレッドで 1 回だけ実行し，`results_store[sim_id]["_enc_cache"][方式]` に
  キャッシュする（イベントループをブロックしない）．
  圧縮方式は `negotiate_encoding` が Accept-Encoding で交渉（**zstd 優先** → gzip → 非圧縮）．
  zstd は gzip lv1 比で grid20 相当 16.3MB→5.6MB，ブラウザの fetch+解凍 767ms→120ms．
  `zstandard` 未インストールなら自動で gzip に落ちる（機能差なし）．
  キャッシュは**要求された方式のみ**作る（1 件数十 MB になり得るので先回りしない）．
- **テスト**: `TestPostProcessingPipeline`・`TestResultsEncodingNegotiation`．
  数値の確認は `python scripts\bench.py`．

### 3.7 描画は 60fps の予算（16.7ms）を守る

`drawFrame` は毎フレーム呼ばれます（車両 12,000 台で中央値 0.2ms）．

- **静的レイヤー**: リンク・ノード・ラベルは `_getStaticLayer` がオフスクリーンキャンバスに
  描いてキャッシュする（キー: キャンバスサイズ・カメラ・drawMode・LINK モードならフレーム index）．
  `linkDrawInfo` / `hitTargets` も同時にキャッシュ．多点パスは累積長 `segCum` を持つ．
- **車両**: `_interpolateVehicles` が TypedArray バッファ `_vbuf` に書き込み，
  `_drawVehicleDots` が速度比を 24 段階に量子化して同色をまとめて fill / stroke する．
  trail モードの軌跡線も (色, 位置, 長さ) でグループ化して stroke．
- **統計**: `computeStatsSeries` は各リンク timeline を 1 回だけ走査する
  （O(フレーム数 × リンク数)）．`drawSparkline` はその `avgRatio` を使うので，
  **先に `computeStatsSeries` を呼ぶこと**．
- **フレームが無い時刻の扱い**: フレームは走行車両がいる時刻にしか無い．描画（`_interpolateVehicles`）
  と統計（`statValuesAt`）は同じ `RisuCore.frameWindow` で判定し，連続フレームの間隔は
  サーバーの `frame_interval_s`（DELTAT × 間引き幅）を使う．フレームの並びから推定すると
  フレームが 2 個のケースで空白を通常間隔と誤認する．判定は exact / interp / hold / none．
- **ロジックとグローバルの分離**: DOM・Canvas・グローバル状態に依存しない処理
  （フレーム復号・統計系列・現示の判定・時刻検索）は `static/js/risu-core.js` に置き，
  index.html はグローバルを渡す薄いラッパーにする．**フロントにロジックを足すときは
  risu-core.js に関数を足して `tests/js` に単体テストを書く**（`node --test tests/js/*.test.js`）．
  ブラウザでの結線は `tests/e2e`（Playwright）が見る．
- **信号**: `_drawSignals` が毎フレーム描く（現示はフレーム間でも変わるので静的レイヤーに
  入れない）．流入リンクごとに停止線バー 1 本だけ．
  最短の流入リンクの画面長が `SIGNAL_LOD_PX` 未満なら描かない．
  以前の「流入リンクごとに固定サイズの信号機筐体」は 4 枝交差点で必ず重なった．
  交差点ノードの現示リングと現在位置の印も試したが，回って見える・情報が重複する
  と不評だったので置かない．色は青・赤の 2 色のみ．**現示は必ず `phase_log`（UXsim の
  実測）から取り，`t % cycle` の公称計算と混ぜないこと**．UXsim は deltat 刻みで
  切り替えるため公称サイクルから遅れが累積し，混ぜると境界で誤表示になる
  （旧「残り 3 秒で黄」は 1,200 秒中 320 秒が誤表示だった）．
  流入リンクの終端・接線は `_approachGeom` が `linkDrawInfo` から取る（直線 / 双方向円弧 /
  多点の 3 形式を吸収）．

### 3.8 認証が無い前提を崩さない

このリポジトリは**シングルユーザー版**です．認証・課金コードを追加しないこと
（サーバー版と分離するため）．認証が無いことを前提に，既定値で守っています．

- **待ち受けの既定は `127.0.0.1`**（`RISU_HOST`）．**`0.0.0.0` に戻さないこと．**
  戻すと同一 LAN の誰でも結果を閲覧でき，`/chat` 経由で**サーバー所有者の API キーに
  課金**できてしまいます．ループバック以外を指定した場合は起動時に警告を出します．
- **CORS の既定も localhost のみ**（`RISU_ALLOWED_ORIGINS`）．
- **テスト**: `TestBindDefaults`が既定値を固定．

---

## 4. 変更時のチェックリスト

| やること | 確認すること |
|---|---|
| **uxsim を上げる** | §3.6 の内部 API（`_LOG_STATE_MAP` / `_veh_by_index` / `offsets`）．`pytest` 全件 + `scripts\bench.py` で性能退行がないか |
| **LLM ツールを足す** | `dispatch_tool_blocks` に分岐を 1 つ（§3.2）．`CLAUDE_TOOLS` の定義と `SYSTEM_PROMPT` の指示も更新．tool_result を必ず返す |
| **フレームの精度・形式を変える** | サーバーの `encode_frames_v3` とフロントの `isV3` を**同時に**（§3.5）．v2 読み込み経路は残す |
| **圧縮方式を変える** | `negotiate_encoding` と `_enc_cache` のキー．非圧縮応答に `Vary` を付けない（§5） |
| **SYSTEM_PROMPT を変える** | system に動的な文字列を入れない（§3.4）．トークン量はログの `usage` 行で確認 |
| **型が絡む変更** | `pyright` を通す（CI の typecheck ジョブ）．外部ライブラリの動的属性は `# type: ignore[attr-defined]` を最小限に |
| **ログを出す** | `from .runtime import log` で logger `risu` を使う（`print` は `TestLogging` が弾く）．info = 通常の進行，warning = 劣化して続行，error = 失敗．`RISU_LOG_LEVEL` で制御 |
| **依存を追加する** | `requirements.txt`（上限付き）+ `pyproject.toml`．`requirements.lock.txt` は clean な venv の `pip freeze` で作り直す（手順はファイル冒頭）．ライセンスは THIRD_PARTY_LICENSES.md に追記．CI の `licenses` ジョブが GPL 系を弾く |
| **CI が依存の更新で落ちた** | 週次 / 手動実行と 3.10 / 3.11 のジョブは最新版で走る（`--upgrade`）．3.12 のジョブは lock 固定なので，そこが落ちたら自分の変更．直したら lock を作り直す（手順は lock 冒頭．PyQt5 系と pywin32 は入れない） |
| **`uxsim_bridge.py` を触る** | FastAPI / pydantic / anthropic を import しないこと（§3.1） |
| **シナリオ全体のパラメータを足す** | `SimulationInput` / `SCENARIO_DEFAULTS` / `build_world` / `set_params` / GUI `et-run` / `EMIT_TEMPLATE` の 6 箇所（§3.1） |
| **台数を扱う集計を足す** | `vehicle_counts` を使うか `deltan` を掛ける（§3.3）．フレームの点数はプラトン数 |
| **入力の値域を変える** | `SimulationInput` の `Field(gt=/ge=)`．`TestScenarioValidation` に 1 件足す（deltan=0・負のリンク長・負の需要・時刻逆転を受理していた） |
| **フロントのロジックを足す** | `static/js/risu-core.js` に純粋関数として書き，`tests/js` に単体テスト（§3.7）．index.html には DOM の結線だけ |
| **ファイルを追加する** | §6 の公開対象かどうか |

---

## 5. 既知の落とし穴

一度踏んで直したものです．同じ形の変更をするときは思い出してください．

| 症状 | 原因と対処 |
|---|---|
| `Vary: Accept-Encoding, Accept-Encoding` と重複する | 非圧縮応答には GZipMiddleware が素通し時に `Vary` を足す．**自前で付けないこと**（圧縮済み応答には middleware が触らないので自前で付ける） |
| `--emit` した生成スクリプトが `SyntaxError` | Windows パスの `\U` が unicode エスケープと解釈される．docstring に埋める値は `_doc_safe` を通す |
| フレームのキーが見つからない | `str(round(25.0, 1))` は `"25.0"` になる．キーの文字列化ルールを変えない（`TestFrameKeyCompatibility`） |
| CSV 取込で `float("")` エラー | 空セルの扱い．`_get_float` の既定値経由で読む（`TestCSVParser`） |
| 重複ノード名で UXsim の生エラーが出る | `SimulationInput` の検証で名前つきのメッセージに変換済み（`TestScenarioValidation`） |
| 取込で「Input should be greater than 0（links.N.length）」 | OSM / GMNS / CSV に長さ 0 のリンクがある．取込関数の末尾で `clamp_link_lengths`（最小 1 m）を通す．検証エラーは `_describe_loc` がリンク名で出す |
| 台数が想定の 1/5 に見える | フレームの `ids` はプラトン（`deltan` 台）．`vehicle_counts` を使う（§3.3） |
| 全車両到着後も画面が「到着 45 / 走行中 5」のまま | フレームは走行車両がいる時刻にしか無い．累積台数はフレームから推定せず，実イベントの `trip_series`（時間軸 0〜tmax）を使う |
| 画面と LLM で平均速度が違う / 間引きで変わる | 定義は「走行中全車両の台数重み平均」の 1 つ（`frame_avg_speed`，間引き前）．リンク timeline の単純平均を「平均速度」と呼ばない．分析用の値は `vehicle_sample_step` の前で計算する |
| `signal_group` 省略で現示 0 だけ青になる | UXsim の既定は `[0]`．RISU の意味は「全現示で青」なので `build_world` と `EMIT_TEMPLATE` が展開し，`signals[].groups` は適用後のリストを返す |
| 現示表示が後半ほどずれる | ログ 1 件 = `W.DELTAT` 秒（`deltan × reaction_time`）．`tmax / len(phase_log)` で逆算しない．`signals[].deltat` を送る |
| GUI で容量 0 が消える | 空欄だけが「自動」．`0` は有効値として送る（`v < 0` のときだけ削除） |
| 同じ入力で結果が毎回変わる | `random_seed` 未指定．比較・追試ではシナリオに持たせる（§3.1） |
| GUI で再実行すると容量の前提が変わる | 送信データから `reaction_time` が落ちていた．`et-run` は全体パラメータを引き継ぐ（`TestGuiRerunCarriesScenarioParams`） |
| `pip install` が `No such file or directory` で止まる | Windows の 260 文字パス長制限（`anthropic` の長いファイル名）．浅い場所に clone するか長いパスを有効化 |

`ruff format` は**意図的に CI へ入れていません**．`risu/simulation.py` などの数値処理は
桁を揃えて書いてあり，自動整形すると数千行の差分が出て履歴が読めなくなるためです．
ファイル分割は済んだので，入れるならモジュール単位で段階的に（1 モジュール 1 コミット）．

---

## 6. 公開しないもの

次は `.gitignore` 済みです．ローカルには置いたまま使えますが，
**これらに依存するコードを本体に足さないこと**（公開環境で壊れます）．

| 対象 | 用途 |
|---|---|
| `static/demo/` `test_data/` `scripts/poster/` | 再配布できないデータや，手元の環境に依存する使い捨てスクリプトの置き場 |
| `docs/*`（`docs/images/` を除く） | 公開しない作業文書 |
| `.env` | API キー |

`docs/` は既定で無視し，公開したいものだけ `!docs/<name>` で許可する方式です．
新しく公開したい文書を追加するときは `.gitignore` に明示的に足してください．
