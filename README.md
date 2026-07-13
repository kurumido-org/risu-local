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

```
単純なボトルネック道路を作って渋滞をシミュレーションして
3×3 のグリッドネットワークを作って
渋谷駅周辺を OSM から取得してシミュレーションして
結果を速度グラフで見せて
```

## API エンドポイント

| Method | Path | 説明 |
|--------|------|------|
| POST | `/simulate` | シミュレーション直接実行 |
| GET  | `/results/{id}` | 結果取得（GeoJSON + フレーム + 統計） |
| POST | `/chat` | LLM チャット（ツール自動呼び出し、SSE ストリーミング） |
| POST | `/upload` | CSV / JSON ファイルからシミュレーション実行 |
| POST | `/import/osm` | OpenStreetMap インポート |
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

## テスト

```powershell
pip install pytest
pytest tests/ -v
```

## ライセンス / 謝辞

- 交通流計算エンジン: [UXsim](https://github.com/toruseo/UXsim) (MIT License)
- 地理データ取得: [OSMnx](https://github.com/gboeing/osmnx) / OpenStreetMap contributors
- チャート描画: [Chart.js](https://www.chartjs.org/)
