"""実行環境の共有情報: .env の読込，バージョン，スレッドプール，uxsim 経路の状態．
他のどのモジュールからも import される（依存を持たない）．
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from dotenv import load_dotenv

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "RISU_SCHEMA_VERSION",
    "RISU_VERSION",
    "RUNTIME_STATUS",
    "executor",
]

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
# .env の読込は，以下の os.environ.get より必ず先に行うこと
load_dotenv()

# ──────────────────────────────────────────────
# 再現性のためのバージョン情報 / DL JSON スキーマ
# ──────────────────────────────────────────────
RISU_SCHEMA_VERSION = "1.0"
RISU_VERSION = "0.1"
try:
    import uxsim as _uxsim_module
    UXSIM_VERSION = getattr(_uxsim_module, "__version__", "unknown")
except Exception:
    UXSIM_VERSION = "unknown"

# 直近のシミュレーションで実際に使われた経路．uxsim の内部 API（§3.6）が使えなくなると
# 動作は止まらず「遅くなる」だけなので，ここに記録して /healthz・起動ログ・テストで見えるようにする．
RUNTIME_STATUS: dict[str, Any] = {
    "uxsim_version": UXSIM_VERSION,
    "backend": None,      # "cpp" / "python"（直近の実行）
    "fast_path": None,    # True = フラット配列の高速経路，False = 車両別ログのフォールバック
    "fast_path_error": None,
}
executor = ThreadPoolExecutor(max_workers=4)
