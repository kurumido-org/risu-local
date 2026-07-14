# RISU — Real-time Interactive Simulator for Urban mobility

交通流シミュレーター [UXsim](https://github.com/toruseo/UXsim) をブラウザ上で
LLM と対話しながら操作できるローカルアプリケーションです。

- 自然言語でシナリオを記述 → LLM がシミュレーションを設計・実行 → 結果をブラウザで可視化
- 3 種類の描画モード（リンク交通状態 / 個車ドット / 軌跡トレイル）
- LLM が自由にグラフを生成してチャット内に表示（Chart.js）
- CSV / JSON / GMNS / OpenStreetMap からのデータインポート
- MCP エンドポイント搭載（Claude Code 等の MCP クライアントから直接操作可能）

> RISU is a browser-based chat interface for the mesoscopic traffic simulator
> **UXsim**. Describe a scenario in natural language, and the LLM designs,
> runs, and visualizes the simulation — all locally on your machine with
> your own API key.

## セットアップ

### 1. 依存関係のインストール

```powershell
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

### 2. API キーの設定

`.env.example` をコピーして `.env` を作成し、自分の API キーを記入します。

```powershell
copy .env.example .env
```

```ini
LLM_BACKEND=claude              # claude / ollama / mock
ANTHROPIC_API_KEY=sk-ant-...    # https://console.anthropic.com で取得
```

Claude API を使わない場合は、ローカル LLM（Ollama）も利用できます:

```ini
LLM_BACKEND=ollama
```

別ターミナルで `ollama serve` を起動しておいてください
（既定モデル: `qwen2.5:3b`。`server.py` の `OLLAMA_MODEL` で変更可）。

API キーなしで動作を確認したい場合は `LLM_BACKEND=mock` を指定します。

### 3. 起動

```powershell
python server.py
```

ブラウザで http://localhost:8001 を開きます。右上のドットが緑になれば接続成功です。

## 使い方

チャットに自然言語で入力すると、LLM がシナリオを構築して UXsim を実行し、
結果をネットワーク図とグラフで表示します。

### GUI ネットワークエディタ

右パネル上部の **EDIT** ボタンで編集モードに入り、ネットワークを GUI で構築・編集できます：

- **選択**: クリックで選択、ドラッグでノード移動（接続リンクの延長は自動再計算）
- **ノード / リンク**: クリックで追加（リンクは連続作成可、双方向チェックで逆方向も同時作成）
- **削除**: クリックで削除（Del キーでも可）
- プロパティパネルで名前・座標・流出容量・信号（ノード）、延長・自由流速度・車線数・**容量（台/s）**・信号 group（リンク）を編集
- 「需要」ボタンで OD 需要を編集、「▶ 実行」でそのままシミュレーション

編集対象は現在表示中のシナリオ（チャットや OSM で作ったネットワークの手直しも可能）、
何も表示していなければ白紙から作成します。

```
単純なボトルネック道路を作って渋滞をシミュレーションして
3×3 のグリッドネットワークを作って
渋谷駅周辺を OSM から取得してシミュレーションして
新宿駅から半径2kmの幹線道路だけでシミュレーションして
結果を速度グラフで見せて
```

OSM インポートは取得する道路の種類を選べます（チャットで「主要道路だけ」「幹線道路で」
のように指定するか、API の `road_types` パラメータで指定）:

| 値 | 内容 | 用途 |
|---|---|---|
| `major` | 高速道路・国道級のみ | 広域（半径 2km 以上）でも高速 |
| `arterial` | 幹線道路まで | 都市スケールの標準 |
| `drive` | 一般車道（デフォルト） | 住宅街の道路含む、サービス道路除外 |
| `all` | 全車道 | 駐車場内通路まで含む。狭い範囲向け |

## API エンドポイント

| Method | Path | 説明 |
|--------|------|------|
| POST | `/simulate` | シミュレーション直接実行 |
| GET  | `/results/{id}` | 結果取得（GeoJSON + フレーム + 統計） |
| POST | `/chat` | LLM チャット（ツール自動呼び出し、SSE ストリーミング） |
| POST | `/upload` | CSV / JSON ファイルからシミュレーション実行 |
| POST | `/import/osm` | OpenStreetMap インポート（`road_types`: major / arterial / drive / all） |
| GET  | `/gmns/datasets` | GMNS Plus データセット一覧 |
| POST | `/gmns/import` | GMNS データセットインポート |
| GET  | `/mcp` | MCP SSE エンドポイント |
| GET  | `/docs` | Swagger UI |

## 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `LLM_BACKEND` | `claude` | `claude` / `ollama` / `mock` |
| `ANTHROPIC_API_KEY` | — | Claude バックエンド時に必須 |
| `RISU_MAX_NODES` | `50000` | ノード数上限 |
| `RISU_MAX_LINKS` | `100000` | リンク数上限 |
| `RISU_MAX_DEMANDS` | `10000` | 需要数上限 |
| `RISU_MAX_TMAX` | `86400` | シミュレーション時間上限（秒） |
| `RISU_UXSIM_TIMEOUT` | `120` | UXsim 実行タイムアウト（秒） |
| `RISU_MAX_TOOL_ROUNDS` | `3` | LLM tool_use ループ最大回数 |
| `RISU_MAX_UPLOAD_BYTES` | `10485760` | アップロード上限（バイト） |

## 高速化（推奨）

UXsim 1.14 系（beta）は C++ バックエンドを搭載しており、シミュレーション実行が
純 Python 実装の 10〜20 倍高速になります。RISU はどちらでも動作しますが、
大きなネットワークを扱う場合はインストールを推奨します：

```powershell
pip install --pre "uxsim>=1.14.0b7"
```

そのほかの高速化はデフォルトで有効です：

- 結果 API（`/results/{id}`）は orjson + gzip 圧縮で配信（転送量 ~96% 削減）
- シミュレーション後処理は numpy でベクトル化済み
- OSM インポートは `./cache` にキャッシュされ、同じ地名の 2 回目以降は高速

## テスト

```powershell
pip install pytest
pytest tests/ -v
```

## ライセンス / 謝辞

- 交通流計算エンジン: [UXsim](https://github.com/toruseo/UXsim) (MIT License)
- 地理データ取得: [OSMnx](https://github.com/gboeing/osmnx) / OpenStreetMap contributors
- チャート描画: [Chart.js](https://www.chartjs.org/)
