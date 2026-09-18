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
pytest tests/ -v                     # テスト（136 件）
ruff check .                         # lint（CI の lint ジョブと同一設定）
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
├── server.py             ← FastAPI + UXsim + LLM + MCP（単一ファイル）
├── uxsim_bridge.py       ← シナリオ → UXsim World（RISU 非依存の純粋モジュール）
├── static/
│   ├── index.html        ← UI 本体（チャット + Canvas 可視化 + Chart.js）
│   ├── vendor/           ← marked / DOMPurify（同梱．ライセンス表記を消さないこと）
│   └── sample_risu.csv
├── scripts/
│   ├── run_scenario.py   ← 素の UXsim で実行する CLI（サーバー不要）
│   └── bench.py          ← 後処理・直列化のベンチマーク
├── tests/test_stability.py
├── pyproject.toml        ← ruff / pytest 設定 + パッケージメタデータ
├── requirements.txt      ← 範囲指定（上限付き）
├── requirements.lock.txt ← 検証済みの正確なバージョン（再現用）
└── .env                  ← LLM_BACKEND / ANTHROPIC_API_KEY（.env.example をコピー）
```

### 2.2 データの流れ

```
ブラウザ ──① 指示──→ /chat ──② tool_use──→ _dispatch_tool_blocks
                                                    │
                                            ③ uxsim_bridge.build_world
                                                    │
                                              UXsim（C++ backend）
                                                    │
                                            ④ _run_uxsim の後処理
                                              （frames / GeoJSON / 統計）
                                                    │
                                          results_store[sim_id]（メモリ）
                                                    │
ブラウザ ←─⑥ zstd/gzip─ /results/{id} ←─⑤ columnar_v3 に量子化
```

**要点は「LLM がデータ実体を通らない」こと**（③〜⑤は LLM を経由しない）．
LLM が扱うのは `sim_id`・差分命令・集計値だけです（§3.3）．

### 2.3 モジュールの責務

| 関数 / エンドポイント | 役割 |
|---|---|
| `uxsim_bridge.build_world` | シナリオ → UXsim World．**サーバーと CLI の共通経路**（§3.1） |
| `_run_uxsim(scenario)` | UXsim 実行 → GeoJSON + 個車フレーム + 統計 |
| `_dispatch_tool_blocks` | LLM ツールの実行．**全経路で共通**（§3.2） |
| `_get_simulation_data(sim_id)` | LLM に渡す集計データ（数 KB に制限） |
| `_apply_modifications` | `rerun_simulation` の差分命令をシナリオに適用 |
| `_parse_csv_scenario` / `_gmns_to_scenario` | CSV / GMNS パーサー |
| `_run_osm_import(place)` | OSMnx で道路ネットワーク取得 |
| `_envelope_compressed_bytes` | 結果の直列化 + 圧縮（方式別キャッシュ） |
| `POST /simulate` / `GET /results/{id}` | 直接実行 / 結果取得 |
| `POST /chat` | LLM 対話（claude = SSE ストリーミング / ollama / mock） |
| `GET /mcp` | MCP SSE エンドポイント |

**LLM ツール**: `run_simulation` / `rerun_simulation` / `get_network_info` /
`get_simulation_data` / `import_osm_network`．
チャートは LLM が ````chart```` ブロックで Chart.js 設定を出力し，フロントが描画します．

**結果ストア**は in-memory dict（`results_store`）で，再起動すると消えます．
`MAX_RESULTS`（既定 30）を超えると `_store_sim` が古い順に追い出します
（1 件数十 MB になり得るため）．

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

- **なぜ**: サーバー経路（`_run_uxsim`）とオフライン経路（`scripts/run_scenario.py`）が
  同じコードを通らないと，片方だけ直したときに**同じシナリオで結果がずれます**．
- **追加条件**: このモジュールは **uxsim と標準ライブラリしか import しない**．
  FastAPI / pydantic / anthropic を足さないこと（CLI が重い依存なしで動く前提）．
- **テスト**: `TestStandalonePipeline`（7 件）．import の純粋性と，
  サーバー経路との統計一致を検証．CI の `standalone` ジョブも uxsim だけの環境で実行．

### 3.2 LLM ツールの実行は `_dispatch_tool_blocks` に一本化する

ストリーミング経路（`_chat_claude_stream`）と同期経路（`_chat_claude`）の，
初回ラウンドと追加ラウンドで同じコードを通します．

- **なぜ**: 以前は同じ dispatch が **4 箇所**にコピーされており，実際に
  「同期経路の初回ラウンドだけ未知ツールの分岐が無い」というバグが潜んでいました．
- **仕組み**: 進捗は `("progress", msg)` として yield し，SSE 経路だけが転送，
  同期経路は `_collect_tool_results` で捨てる．1 ターン分の状態
  （`sim_id` / `sim_data_cache` / `last_data_sim_id`）は `_ToolTurnState` で持ち回る．
- **絶対条件**: **tool_use には必ず 1 対 1 で tool_result を返す**（Anthropic API の要求．
  欠けると 400）．未知のツール名でも結果を積むこと．
- **ツールを追加するとき**: `_dispatch_tool_blocks` に分岐を 1 つ足すだけでよい．
  `follow_up` は 2 ラウンド目以降で，進捗の文言と保存メタの `round` が変わる．
- **テスト**: `TestToolDispatch`（8 件）・`TestChatStreamingPath`（4 件）．

フロントも同様に，SSE 経路と JSON 経路の応答反映を `renderAssistantResponse` に
一本化しています（sim バッジ・チャート・トークン使用量の付け方）．

### 3.3 LLM にデータ実体を渡さない

数千〜1 万リンクのネットワークを扱うため，LLM が扱うのは
**ID・差分・要約・パラメトリック命令だけ**にします．

- **既存ネットワークの修正**: `rerun_simulation` の差分命令を使う．
  `run_simulation` で全体を再送しないこと（OSM 由来の大規模網では必ずサイズ超過で失敗する）．
- **格子ネットワーク**: `run_simulation` の `grid` テンプレートと `auto_demands` を使う．
  `_expand_run_simulation_args` がサーバー側で nodes/links/demands に展開する．
  LLM に列挙させると 10×10 で出力 2 万トークンを超える．命名規則は tool_result で返す．
- **OD 需要の生成**: `generate_demands` でサーバー側に抽選させる．
  LLM はノード名を知る必要がない．
- **ネットワークの照会**: `get_network_info` は `summary` → 絞り込み（`name_contains` +
  `limit` ≤200 + `offset`）の順で使う．全件取得はできない設計．
- **集計データ**: `_get_simulation_data` は `points`（既定 30）・`max_links`（既定 20）で
  量を制御し，速度は 0.1 m/s に丸める．1 回あたり数 KB に収めること．
- **チャート**: `{"$data": "network_avg_speed"}` 形式の参照を `_extract_charts` →
  `_resolve_chart_refs` がこのターンの集計データ（`sim_data_cache`）で置換する．
  **LLM に配列を書き写させない**（出力トークンは入力の 5 倍単価）．
- **テスト**: `TestScenarioModifications`（12 件）・`TestNetworkInfo`（4 件）・
  `TestSimulationDataAggregation`（7 件）・`TestChartExtraction`（4 件）．

### 3.4 プロンプトキャッシュを壊さない

- **system プロンプトは不変**（`SYSTEM_PROMPT` そのまま）．sim_id 入りの
  【現在のコンテキスト】は `_build_llm_messages` が最後の user メッセージに付ける．
  **system に動的な文字列を足すと，tools 以外のキャッシュが毎ターン無効になる．**
- **キャッシュ境界**（`cache_control`）は API 上限の 4 つ:
  tools 末尾 / system / 履歴の最後の assistant（ターン跨ぎ）/ リクエスト末尾
  （`_mark_cache_tail`，同一ターン内の tool ラウンド）．
  末尾の印は `_tail_marked` フラグで管理し，`_api_messages` が送信前に剥がす．
  **messages を組み立て直したら必ず `_mark_cache_tail` を呼ぶこと．**
- **履歴のトリミング**: `_trim_history` が `MAX_HISTORY_CHARS` で古いターンから落とす．
  超過時は予算の半分まで落とす**ヒステリシス**（毎ターン 1 件ずつ落とすと prefix が
  毎回変わってキャッシュが当たらない）．フロントは tool_use / tool_result を履歴に
  残さない（最終テキストのみ）．
- **計測**: 1 ターンの使用量は `_UsageTally` が集計し，done イベントの `usage` で
  フロントへ（吹き出し下の `.usage-meta`）．サーバーログにも `[RISU usage]` が出る．
- **テスト**: `TestLLMTokenSaving`（8 件）・`TestConversationContext`（4 件）．

### 3.5 結果データの表現 — 保存は v2，送出は v3

フレームは列指向 `{ids, xs, ys, vs, alphas, li}`（`li` は `link_names` への index）です．

| | 形式 | 用途 |
|---|---|---|
| `results_store` | **`columnar_v2`**（素の値，numpy 配列のまま） | サーバー内の消費側が前提にしている |
| `/results` の応答 | **`columnar_v3`**（量子化 + 差分符号化） | 転送量とブラウザの parse を減らす |

- **保存側の条件**: 各列は **numpy 配列のまま保持する**（Python float 化しない．メモリ 1/4）．
  フレーム内の ids は昇順（フロントの補間と v3 の差分符号化がこれを前提にしている）．
  消費側（`_get_simulation_data`・テスト）は list / ndarray どちらでも動くよう書くこと．
  標準 `json.dumps` に結果 dict を直接渡さない（`_envelope_json_bytes` を使う）．
  **結果 dict は保存後に変更しないこと**（圧縮キャッシュが古くなる）．
- **v3 の符号化**（`_encode_frames_v3`，`_envelope_json_bytes` 内で送出時のみ適用）:
  `ids` = 差分符号化 int32 / `xs`,`ys` = 1 m 丸め int32 / `vs` = 0.1 m/s 単位 int16 /
  `alphas` = 0.001 単位 int16 / `li` = そのまま．
  効果（`scripts\bench.py --sizes 20` で再現できる．エンベロープ全体の値）:
  json 88.0→63.4 MB，gzip 22.2→16.0 MB，直列化 0.37→0.14 秒．
- **v2 を残す理由**: ダウンロード済みの古い JSON を読めるよう，フロントの v2 経路を
  消さないこと．フロントは `r.frame_format` で分岐する．
  **精度を変えるときはサーバーの `_encode_frames_v3` とフロントの `isV3` ブランチを必ず同時に直す．**
- **間引き**: 総点数が `MAX_FRAME_POINTS`（既定 300 万，`0` で無効）を超えると
  車両 ID を `vehicle_sample_step` 間隔でサンプリングし，**描画用 frames だけ**を減らす．
  timeline と統計は全点から計算．`_get_simulation_data` は台数を step 倍に補正する．
- **テスト**: `TestFrameWireEncodingV3`（5 件）・`TestPostProcessingPipeline`（8 件）・
  `TestFrameKeyCompatibility`（3 件）．

### 3.6 後処理に車両ごとの Python ループを持たない

- `W.analyzer.basic_analysis` は**無効化してある**（od_analysis → floyd_warshall が
  O(ノード数³)．5,000 ノード級 OSM では 1 回 45 秒超）．統計 3 値
  （total / completed / average_travel_time）は `_run_uxsim` 内で車両ログから直接計算する．
- **fast path**: cpp バックエンドでは `W._cpp_world.build_all_vehicle_logs_flat_compact()`
  で全車両ログをフラット配列として 1 回で受け取り（`_collect_run_points`），
  `W._skip_log_on_terminate = True` で uxsim 側の車両別 `_log_cache` 構築
  （数万台で数秒）を省略する．リンク別平均速度は (フレーム, リンク) キーの `np.bincount`，
  フレーム分割は `searchsorted`．
  **uxsim 更新時はこの内部 API の互換性を確認すること**（`_LOG_STATE_MAP`，
  `_veh_by_index` の順序 = `VEHICLES` の順序，`offsets`）．失敗すると車両別ログの
  フォールバックに落ちる（動くが遅い）．
- **時刻方向の間引き**（`MAX_FRAMES`，既定 200）は `_select_frames` が bincount + LUT で
  O(N) に行い，**列抽出の前に**適用する（np.unique / isin のソートは 5,000 万点で数秒かかった）．
- uxsim は C++ バックエンド（`World(cpp=True)`，1.14 以降）優先．`TypeError` で
  純 Python にフォールバックする．
- **転送**: `_envelope_compressed_bytes` が orjson（`OPT_SERIALIZE_NUMPY`）+ 圧縮を
  executor スレッドで 1 回だけ実行し，`results_store[sim_id]["_enc_cache"][方式]` に
  キャッシュする（イベントループをブロックしない）．
  圧縮方式は `_negotiate_encoding` が Accept-Encoding で交渉（**zstd 優先** → gzip → 非圧縮）．
  zstd は gzip lv1 比で grid20 相当 16.3MB→5.6MB，ブラウザの fetch+解凍 767ms→120ms．
  `zstandard` 未インストールなら自動で gzip に落ちる（機能差なし）．
  キャッシュは**要求された方式のみ**作る（1 件数十 MB になり得るので先回りしない）．
- **テスト**: `TestPostProcessingPipeline`（8 件）・`TestResultsEncodingNegotiation`（9 件）．
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

### 3.8 認証が無い前提を崩さない

このリポジトリは**シングルユーザー版**です．認証・課金コードを追加しないこと
（サーバー版と分離するため）．認証が無いことを前提に，既定値で守っています．

- **待ち受けの既定は `127.0.0.1`**（`RISU_HOST`）．**`0.0.0.0` に戻さないこと．**
  戻すと同一 LAN の誰でも結果を閲覧でき，`/chat` 経由で**サーバー所有者の API キーに
  課金**できてしまいます．ループバック以外を指定した場合は起動時に警告を出します．
- **CORS の既定も localhost のみ**（`RISU_ALLOWED_ORIGINS`）．
- **テスト**: `TestBindDefaults`（4 件）が既定値を固定．

---

## 4. 変更時のチェックリスト

| やること | 確認すること |
|---|---|
| **uxsim を上げる** | §3.6 の内部 API（`_LOG_STATE_MAP` / `_veh_by_index` / `offsets`）．`pytest` 全件 + `scripts\bench.py` で性能退行がないか |
| **LLM ツールを足す** | `_dispatch_tool_blocks` に分岐を 1 つ（§3.2）．`CLAUDE_TOOLS` の定義と `SYSTEM_PROMPT` の指示も更新．tool_result を必ず返す |
| **フレームの精度・形式を変える** | サーバーの `_encode_frames_v3` とフロントの `isV3` を**同時に**（§3.5）．v2 読み込み経路は残す |
| **圧縮方式を変える** | `_negotiate_encoding` と `_enc_cache` のキー．非圧縮応答に `Vary` を付けない（§5） |
| **SYSTEM_PROMPT を変える** | system に動的な文字列を入れない（§3.4）．トークン量は `[RISU usage]` で確認 |
| **依存を追加する** | `requirements.txt`（上限付き）+ `requirements.lock.txt` + `pyproject.toml`．ライセンスは THIRD_PARTY_LICENSES.md に追記．CI の `licenses` ジョブが GPL 系を弾く |
| **`uxsim_bridge.py` を触る** | FastAPI / pydantic / anthropic を import しないこと（§3.1） |
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
| `pip install` が `No such file or directory` で止まる | Windows の 260 文字パス長制限（`anthropic` の長いファイル名）．浅い場所に clone するか長いパスを有効化 |

`ruff format` は**意図的に CI へ入れていません**．`server.py` の数値処理は桁を揃えて
書いてあり，自動整形すると数千行の差分が出て履歴が読めなくなるためです．
入れるならファイル分割の後に段階的に．

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
