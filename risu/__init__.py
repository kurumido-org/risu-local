"""RISU サーバー本体（パッケージ）．

モジュール構成は CLAUDE.md §2.1 を参照．`risu.runtime` が .env を読むので，
どのモジュールより先に import されるようここで読み込む．
"""

from . import runtime as _runtime  # noqa: F401  (.env を最初に読む)
