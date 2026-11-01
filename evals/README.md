# 実 LLM 評価（テスト設計 §4 / §8 / §15）

`evals/cases/*.json` が 1 タスク = 1 ファイル（L01〜L24）．`scripts/evaluate_chat.py` が
事前状態を作り，実 LLM（`.env` の `LLM_BACKEND=claude` / `ANTHROPIC_API_KEY`）で会話を回し，
ツール履歴・保存結果・応答を照合して `evals/runs/<日時>/` に記録する．

```powershell
.venv\Scripts\activate
python scripts\evaluate_chat.py --cases L01-L12 --trials 3          # 初回 36 会話
python scripts\evaluate_chat.py --cases L01-L24 --trials 3          # 拡張 72 会話
python scripts\evaluate_chat.py --cases L05,L07 --trials 1 --dry-run # 事前状態と判定器の確認（LLM を呼ばない）
python scripts\evaluate_chat.py --selfcheck                          # 判定器が誤りを検出することの確認（§16.4）
```

- 実行順序は seed でランダム化．会話ごとに results_store を空にして独立した事前状態を作る．
- 判定は「実行 / 条件 / 数値 / 説明」の 4 種（§15.3）．説明の判定は項目別に人が行う．
  自動照合できない項目は `null`（人の確認待ち）で，未確認のまま合格にしない．
- 記録: `summary.json` / `summary.md`（タスク別 x/3，失敗分類，費用・時間）と 1 試行ごとの JSON（§16.3）．
- API キーは記録しない．
- 公開する 24 タスクは開発・回帰用．論文の最終評価には同じ分類の未使用課題を別に確保する（§10）．

## ケースファイルの形式

```json
{
  "id": "L05",
  "title": "容量だけ変更して再実行",
  "setup": "s0",                 // scripts/evaluate_chat.py の SETUPS に定義（事前状態と表示中 ID）
  "preamble": false,             // 新規作成の共通メッセージを前置きするか
  "turns": ["ABの容量だけ900台/時にして再実行してください．他の条件は維持してください．"],
  "checks": "L05",               // CHECKS の判定器（省略時は id）
  "human": ["回答中の数値は新結果または明示した比較結果と一致する"]   // 人が判定する項目
}
```

`${S0}` などのプレースホルダーは，セットアップで得た実 ID に置換してから送る．
