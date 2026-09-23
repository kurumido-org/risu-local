"""フロントのソース検査: エディタの引き継ぎ，risu-core.js の利用（ロジック自体は tests/js，結線は tests/e2e）．"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))



class TestGuiRerunCarriesScenarioParams:
    """GUI エディタからの再実行が reaction_time / random_seed を落とさないことを，
    index.html のソース上で固定する（ブラウザなしで検証できる範囲）．

    [修正履歴] 編集後の POST /simulate に reaction_time が無く，元シナリオで 1.7 を
    指定していても UXsim 既定 1.0 に戻り，1 車線容量が約 1,846 → 2,880 台/時に変わっていた．
    """

    @pytest.fixture(scope="class")
    def html(self):
        """index.html と，そこから読み込む static/js/*.js を読み込み順に連結したもの．"""
        static = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        with open(os.path.join(static, "index.html"), encoding="utf-8") as f:
            page = f.read()
        from risu.api import UI_SCRIPTS
        parts = [page]
        for src in UI_SCRIPTS:
            with open(os.path.join(static, "js", src), encoding="utf-8") as f:
                parts.append(f.read())
        return "\n".join(parts)

    def _run_handler(self, html):
        i = html.index("document.getElementById('et-run').addEventListener")
        j = html.index("// ── 再生制御 ──", i)
        return html[i:j]

    def test_submit_includes_reaction_time_and_seed(self, html):
        h = self._run_handler(html)
        assert "scenario.reaction_time = " in h
        assert "scenario.random_seed = " in h
        assert "deltan:" in h

    def test_edit_mode_initializes_from_scenario(self, html):
        i = html.index("function toggleEditMode()")
        block = html[i:i + 2000]
        assert "src.reaction_time" in block and "src.random_seed" in block
        assert 'id="et-rt"' in html and 'id="et-seed"' in html

    def test_capacity_zero_is_kept(self, html):
        """[修正履歴] 容量欄に 0 を入れると capacity が削除され，容量制約が外れていた．"""
        i = html.index("field === 'capacity'")
        block = html[i:i + 500]
        assert "v < 0) delete lk.capacity" in block
        assert "v <= 0" not in block

    def test_stats_and_phase_logic_live_in_risu_core(self, html):
        """統計・現示・フレーム復号のロジックは static/js/risu-core.js にあり，
        index.html はグローバルを渡すだけ（ロジックの検証は node --test tests/js）．"""
        i = html.index("function computeStatsSeries()")
        block = html[i:html.index("function currentTimeSec()", i)]
        assert "RisuCore.computeStats(" in block and "tripSeries" in block and "frameAvgSpeed" in block
        j = html.index("function updateStatValues()")
        assert "RisuCore.statValuesAt(" in html[j:j + 1200]
        k = html.index("function currentPhaseIdx(sig, t)")
        assert "RisuCore.phaseIndexAt(" in html[k:k + 400]
        assert "RisuCore.decodeFrames(" in html
        assert '<script src="/js/risu.bundle.js"></script>' in html

    def test_active_vehicles_uses_real_counts(self, html):
        # 実台数への換算は risu-core.js の computeStats（tests/js で検証）．
        # index.html 側はサーバーの vehicle_counts / vehicle_sample_step を渡していること
        assert "vehicleCounts = Array.isArray(r.vehicle_counts)" in html
        assert "r.vehicle_sample_step" in html


class TestJsBundle:
    """UI の JS はソース分割のまま 1 本で配信する（読み込み時間の維持）．"""

    def test_bundle_contains_every_ui_script_in_order(self):
        from risu.api import UI_SCRIPTS, build_js_bundle
        static_js = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js")
        assert set(os.listdir(static_js)) == set(UI_SCRIPTS), "static/js のファイルは UI_SCRIPTS に列挙する"
        assert UI_SCRIPTS[0] == "risu-core.js"
        body, etag = build_js_bundle(static_js)
        text = body.decode("utf-8")
        pos = [text.index(f"// ---- {n} ----") for n in UI_SCRIPTS]
        assert pos == sorted(pos)
        assert etag.startswith('"') and build_js_bundle(static_js)[1] == etag

    def test_bundle_endpoint_serves_and_revalidates(self):
        from fastapi.testclient import TestClient
        import risu.api
        c = TestClient(risu.api.app)
        r = c.get("/js/risu.bundle.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert "RisuCore" in r.text and "function computeStatsSeries" in r.text
        assert r.headers.get("cache-control") == "no-cache"
        r2 = c.get("/js/risu.bundle.js", headers={"If-None-Match": r.headers["etag"]})
        assert r2.status_code == 304
