# コントリビューションガイド

**Issue でのご報告・ご質問を歓迎します．** 小さな報告でも助かります．

少人数で開発しているため，Pull Request は確認にお時間をいただくことがあります．
不具合や要望は，まず Issue でお知らせいただけると対応しやすいです．

## まず知っておいてほしいこと

RISU は **v0.1.0 のベータ**です．検証の範囲を正確に書くと，次の状態です．

- サーバー側は **Windows・Ubuntu × Python 3.10〜3.13** で CI のテストが通っています
- **ブラウザ UI を実際に操作して確認したのは Windows 11 / Chrome だけ**です．
  CI にブラウザテストは含まれていません
- macOS はサポート対象外です

そのため，**Linux や Chrome 以外のブラウザで使った報告がいちばん価値があります**．
結果がどちらでも Issue で教えていただけると助かります．

## 開発環境

```powershell
python -m venv .venv
.venv\Scripts\activate            # Linux: source .venv/bin/activate

pip install -r requirements-dev.txt   # 実行時依存 + pytest / ruff
copy .env.example .env                # LLM_BACKEND=mock なら API キー不要
python server.py                      # http://localhost:8001
```

セットアップの詳細とつまずきやすい点は [README](README.md#2-セットアップ) にあります．

## 変更前に通すもの

CI（`.github/workflows/ci.yml`）と同じ内容をローカルで実行できます．

```powershell
pytest tests/ -v        # テスト（136 件）
ruff check .            # lint（pyproject.toml の設定）
python scripts\bench.py # 性能に関わる変更をしたとき
```

`ruff format` は**意図的に使っていません**．`risu/simulation.py` などの数値処理は桁を揃えて
書いてあり，自動整形すると数千行の差分が出て履歴が読めなくなるためです．

## コードを変更する場合

フォークして手元で改造する場合も，Pull Request を送る場合も共通です．

**[CLAUDE.md](CLAUDE.md) の「設計上の不変条件」に目を通してください．**
知らずに触ると性能・コスト・互換性が静かに壊れる箇所をまとめてあります．
とくに次は，テストが通っていても壊れたことに気づきにくい部分です．

| 変更するもの | 読む節 |
|---|---|
| ネットワーク構築まわり | §3.1（`uxsim_bridge` に一本化） |
| LLM ツールの追加・変更 | §3.2（`_dispatch_tool_blocks`）・§3.3（データを LLM に渡さない） |
| プロンプト・会話履歴 | §3.4（キャッシュを壊さない） |
| 結果データの形式・圧縮 | §3.5・§3.6 |
| 描画（`drawFrame` 周辺） | §3.7（60fps の予算） |
| 待ち受け・CORS | §3.8（**認証が無い前提**） |

[§4 の逆引き表](CLAUDE.md#4-変更時のチェックリスト)から該当箇所に飛べます．

## Issue の書き方

**バグ報告**には次を含めてください．環境差の切り分けに必要です．

- OS とバージョン（例: Windows 11 / Ubuntu 24.04）
- Python のバージョン（`python --version`）
- `LLM_BACKEND`（`mock` / `ollama` / `claude`）
- 再現手順と，**エラーの全文**（省略せずに）
- `pip list` の該当部分（`uxsim` / `fastapi` / `numpy` のバージョン）

`LLM_BACKEND=mock` でも再現するかを確かめていただけると，LLM 側の問題か
RISU 側の問題かが切り分けられます．

**機能の提案**は，やりたいことと現状の回避策を書いていただければ十分です．

## 方針

- **このリポジトリはシングルユーザー版です．** 認証・課金・マルチユーザー機能は
  追加しません（別リポジトリで管理しています）
- **交通工学の妥当性を優先します．** 便利でも，シミュレーションとして誤った結果を
  出しやすい機能は入れません
- **LLM にネットワーク実体を渡さない設計を崩しません**（CLAUDE.md §3.3）．
  大規模ネットワークを扱える理由がここにあります

## ライセンス

Pull Request を送った時点で，その内容が本プロジェクトのライセンス
（[MIT](LICENSE)）で配布されることに同意したものとみなします．
