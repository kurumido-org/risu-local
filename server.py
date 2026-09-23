"""RISU の起動ファイル．

    python server.py          # http://localhost:8001

本体は risu/ パッケージ（構成は CLAUDE.md §2.1）．ここには起動処理だけを置き，
関数を足さないこと（テストがこのファイルの行数を固定している）．
"""

from risu.api import RISU_HOST, RISU_PORT, RISU_RELOAD, app  # noqa: F401  (uvicorn の "server:app")

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
