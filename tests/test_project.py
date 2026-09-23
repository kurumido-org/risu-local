"""リポジトリ全体の前提: ライセンス衛生，パッケージ構成のガード．"""

import os
import re
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))



# ============================================================
# ライセンス衛生（MIT で公開できる状態を保つ）
# ============================================================

class TestLicenseHygiene:
    """RISU を MIT で配布できる前提を壊さないためのテスト．

    uxsim は PyQt5 (GPL v3) を必須依存として宣言しているため，pip install すると
    環境には入る．RISU 自身がそれを import しない限り，MIT 配布の妨げにはならない．
    「いつのまにか import されるようになっていた」を検知するのがここの目的．
    詳細は THIRD_PARTY_LICENSES.md を参照．
    """

    GPL_MODULES = ("PyQt5", "PyQt6", "PySide2", "PySide6")

    def test_vendored_scripts_keep_license_headers_and_no_cdn(self):
        """同梱 JS（marked / DOMPurify / Chart.js）はライセンスヘッダを保持し，
        index.html は CDN から script を読まない（オフラインで完全に動く前提）．"""
        root = os.path.dirname(os.path.dirname(__file__))
        vendor = os.path.join(root, "static", "vendor")
        expected = {"marked.umd.min.js", "purify.min.js", "chart.umd.min.js"}
        assert expected <= set(os.listdir(vendor))
        # 同梱ファイルは THIRD_PARTY_LICENSES.md に必ず載せる（配布物なので表示義務がある）．
        # ヘッダの形式は配布元（jsDelivr のバナー等）に依存するので，ここでは表の記載を検証する
        with open(os.path.join(root, "THIRD_PARTY_LICENSES.md"), encoding="utf-8") as f:
            licenses = f.read()
        for name in expected:
            assert f"static/vendor/{name}" in licenses, f"{name} が THIRD_PARTY_LICENSES.md に無い"
        with open(os.path.join(root, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
        assert srcs and all(s.startswith("/vendor/") or s.startswith("/js/") for s in srcs), srcs

    def test_risu_does_not_import_qt(self):
        """server.py を読み込み，シミュレーションを流しても Qt を import しない．"""
        import subprocess

        code = f'''
import sys, os
os.environ["LLM_BACKEND"] = "mock"
import risu.aggregate, risu.api, risu.llm, risu.mcp_server, risu.prompts, risu.results, risu.runtime, risu.scenario_ops, risu.schema, risu.simulation
sc = risu.schema.SimulationInput(
    name="lic", tmax=300, deltan=5,
    nodes=[{{"name": "A", "x": 0, "y": 0}}, {{"name": "B", "x": 500, "y": 0}}],
    links=[{{"name": "AB", "start": "A", "end": "B", "length": 500}}],
    demands=[{{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.3}}],
)
res = risu.simulation.run_uxsim(sc)
risu.results.store_sim("lic", res)
risu.results.envelope_json_bytes("lic")
risu.aggregate.get_simulation_data("lic")
qt = [m for m in sys.modules if m.split(".")[0] in {self.GPL_MODULES!r}]
print("QT:" + ",".join(sorted(qt)))
'''
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        line = next(ln for ln in r.stdout.splitlines() if ln.startswith("QT:"))
        loaded = [m for m in line[3:].split(",") if m]
        assert not loaded, (
            f"GPL ライセンスの Qt モジュールが読み込まれた: {loaded}．"
            "MIT 配布の前提が崩れるので，依存の追加を見直すこと"
        )

    def test_risu_runs_without_qt_installed(self):
        """PyQt5 が入っていない環境でも全経路が動くこと．

        利用者が `pip uninstall PyQt5` しても RISU が壊れない，という
        THIRD_PARTY_LICENSES.md の記述を裏付ける．
        """
        import subprocess

        code = '''
import sys, os

class _Block:
    BAD = ("PyQt5", "PyQt6", "PySide2", "PySide6")
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BAD:
            raise ImportError(name + " は未インストールという想定")
        return None

sys.meta_path.insert(0, _Block())
os.environ["LLM_BACKEND"] = "mock"
import risu.aggregate, risu.api, risu.llm, risu.mcp_server, risu.prompts, risu.results, risu.runtime, risu.scenario_ops, risu.schema, risu.simulation
sc = risu.schema.SimulationInput(
    name="noqt", tmax=300, deltan=5,
    nodes=[{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 500, "y": 0}],
    links=[{"name": "AB", "start": "A", "end": "B", "length": 500}],
    demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.3}],
)
res = risu.simulation.run_uxsim(sc)
risu.results.store_sim("noqt", res)
assert b'"columnar_v3"' in risu.results.envelope_json_bytes("noqt")
assert risu.aggregate.get_simulation_data("noqt")

from uxsim_bridge import build_world, scenario_from_dict
W = build_world(scenario_from_dict({
    "name": "b", "tmax": 200, "deltan": 5,
    "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 500, "y": 0}],
    "links": [{"name": "AB", "start": "A", "end": "B", "length": 500}],
    "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.3}],
}), disable_basic_analysis=True)
W.exec_simulation()
print("OK")
'''
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "OK" in r.stdout

    def test_bundled_vendor_files_keep_license_headers(self):
        """同梱している marked / DOMPurify のライセンス表記が消えていないこと．

        MIT / Apache-2.0 いずれも著作権表示の保持が条件なので，
        ミニファイ済みファイルの先頭コメントを削ってはいけない．
        """
        root = os.path.dirname(os.path.dirname(__file__))
        vendor = os.path.join(root, "static", "vendor")
        expected = {
            "marked.umd.min.js": ("marked", "license", "Do NOT use SRI"),
            "purify.min.js": ("DOMPurify", "license"),
        }
        for fname, needles in expected.items():
            path = os.path.join(vendor, fname)
            assert os.path.exists(path), f"{fname} が無い"
            head = open(path, encoding="utf-8").read(600)
            assert any(n.lower() in head.lower() for n in needles), (
                f"{fname} の先頭にライセンス表記が見当たらない")

    def test_third_party_licenses_doc_exists(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for fname in ("LICENSE", "THIRD_PARTY_LICENSES.md"):
            path = os.path.join(root, fname)
            assert os.path.exists(path), f"{fname} が無い"
        text = open(os.path.join(root, "LICENSE"), encoding="utf-8").read()
        assert "MIT License" in text


# ============================================================
# パッケージ構成のガード（単一ファイルに戻さない）
# ============================================================

class TestPackageLayout:
    """server.py は起動処理だけ，本体は risu/ の役割別モジュール（CLAUDE.md §2.1）．
    1 ファイルに機能が集まり直すのを CI で止める．"""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    MAX_MODULE_LINES = 1000

    def test_entry_file_has_no_logic(self):
        import ast
        src = open(os.path.join(self.ROOT, "server.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert defs == [], "server.py に関数/クラスを足さない．risu/ の該当モジュールへ"
        assert src.count("\n") < 40

    def test_modules_stay_focused(self):
        pkg = os.path.join(self.ROOT, "risu")
        expected = {"runtime", "schema", "simulation", "results", "aggregate", "scenario_ops",
                    "importers", "prompts", "tools", "llm", "mcp_server", "api"}
        found = {f[:-3] for f in os.listdir(pkg) if f.endswith(".py") and f != "__init__.py"}
        assert expected <= found, expected - found
        for name in found:
            with open(os.path.join(pkg, name + ".py"), encoding="utf-8") as f:
                n = sum(1 for _ in f)
            assert n <= self.MAX_MODULE_LINES, f"risu/{name}.py が {n} 行．分割を検討（上限 {self.MAX_MODULE_LINES}）"

    def test_package_import_graph_is_acyclic(self):
        """モジュール間の import は一方向（runtime/schema → simulation/results → … → api）．"""
        import ast
        pkg = os.path.join(self.ROOT, "risu")
        edges = {}
        for f in os.listdir(pkg):
            if not f.endswith(".py") or f == "__init__.py":
                continue
            tree = ast.parse(open(os.path.join(pkg, f), encoding="utf-8").read())
            deps = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module:
                    deps.add(n.module)
                elif isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("risu."):
                    deps.add(n.module.split(".", 1)[1])
            edges[f[:-3]] = deps
        state = {}
        def visit(m, stack):
            if state.get(m) == 1:
                raise AssertionError("import cycle: " + " -> ".join(stack + [m]))
            if state.get(m) == 2:
                return
            state[m] = 1
            for d in edges.get(m, ()):
                visit(d, stack + [m])
            state[m] = 2
        for m in edges:
            visit(m, [])
        assert edges["runtime"] == set() and edges["schema"] == set()
        assert "api" not in {d for m, ds in edges.items() if m != "api" for d in ds}


class TestLogging:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    """サーバーは print ではなく logger "risu" を使う（uvicorn と同じ出力先，レベル制御，caplog で検証可）．"""

    def test_no_print_in_package(self):
        import ast
        pkg = os.path.join(self.ROOT, "risu")
        offenders = []
        for f in sorted(os.listdir(pkg)):
            if not f.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(pkg, f), encoding="utf-8").read())
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print":
                    offenders.append(f"{f}:{n.lineno}")
        assert offenders == [], f"print() は使わず risu.runtime.log を使う: {offenders}"

    def test_simulation_logs_through_risu_logger(self, caplog):
        import logging

        from helpers import BOTTLENECK_SCENARIO
        from risu.simulation import run_uxsim
        with caplog.at_level(logging.INFO, logger="risu"):
            run_uxsim(BOTTLENECK_SCENARIO)
        msgs = [r.getMessage() for r in caplog.records if r.name == "risu"]
        assert any("Simulation done" in m for m in msgs), msgs

    def test_log_level_from_env(self, monkeypatch):
        import logging

        import risu.runtime
        monkeypatch.setenv("RISU_LOG_LEVEL", "DEBUG")
        try:
            risu.runtime.configure_logging()
            assert risu.runtime.log.level == logging.DEBUG
        finally:
            monkeypatch.delenv("RISU_LOG_LEVEL", raising=False)
            risu.runtime.configure_logging()
            assert risu.runtime.log.level == logging.INFO
