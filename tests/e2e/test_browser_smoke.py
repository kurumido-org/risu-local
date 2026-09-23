"""ブラウザのスモークテスト（Playwright + headless Chromium）．

CI の e2e ジョブで実行する．ローカルでは
    pip install playwright && playwright install chromium
    pytest tests/e2e -q
playwright が無い環境では自動で skip する（通常の `pytest tests/` を重くしない）．

見るのは「実際のブラウザで描画・統計・エディタが壊れていないか」の最小限:
  - ページが JS エラーなしに読める
  - /simulate の結果を loadResult で読み込み，3 つの描画モードで Canvas に何か描かれる
  - 統計パネルの値がサーバーの統計と一致する（終了時刻で 到着 = 全台，走行中 0）
  - エディタが元シナリオの reaction_time / seed を引き継ぐ
細かいロジックは tests/js（node）で検証する．ここは結線の確認．
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    """テスト用に uvicorn をスレッドで起動し，URL を返す．"""
    os.environ["RISU_STARTUP_SELFCHECK"] = "0"
    os.environ.setdefault("LLM_BACKEND", "mock")
    os.chdir(ROOT)   # static/ の相対パスで mount している
    import uvicorn

    import server

    port = _free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started, "uvicorn が起動しない"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=5)


SCENARIO = {
    "name": "e2e_signal", "tmax": 900, "deltan": 5, "reaction_time": 1.5, "random_seed": 7,
    "nodes": [{"name": "W", "x": 0, "y": 0}, {"name": "I", "x": 1000, "y": 0, "signal": [30, 30]},
              {"name": "E", "x": 2000, "y": 0}, {"name": "S", "x": 1000, "y": -1000}],
    "links": [{"name": "W_I", "start": "W", "end": "I", "length": 1000, "signal_group": 0},
              {"name": "S_I", "start": "S", "end": "I", "length": 1000, "signal_group": 1},
              {"name": "I_E", "start": "I", "end": "E", "length": 1000}],
    "demands": [{"orig": "W", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.3},
                {"orig": "S", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.2}],
}


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


@pytest.fixture(scope="module")
def sim(base_url):
    d = _post_json(f"{base_url}/simulate", SCENARIO)
    assert "id" in d, d
    return d


@pytest.fixture(scope="module")
def page(base_url):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
              if m.type == "error" else None)
        pg.goto(base_url, wait_until="load")
        pg.wait_for_function("typeof loadResult === 'function' && typeof RisuCore === 'object'")
        pg._risu_errors = errors  # テストから参照
        yield pg
        browser.close()


def test_page_loads_without_js_errors(page):
    assert page.title().startswith("RISU")
    assert page._risu_errors == [], page._risu_errors


def test_result_loads_and_all_draw_modes_render(page, sim):
    page.evaluate("async (id) => { await loadResult(id); stopPlay(); }", sim["id"])
    page.wait_for_function("frameTimes.length > 0")
    n_frames = page.evaluate("frameTimes.length")
    assert n_frames > 0
    # 3 つの描画モードで Canvas に描画される（背景以外のピクセルがある）
    for mode in ("link", "vehicle", "trail"):
        painted = page.evaluate(
            """(mode) => {
                setDrawMode(mode);
                timeSlider.value = 300; drawFrame();
                const ctx = canvasEl.getContext('2d');
                const d = ctx.getImageData(0, 0, canvasEl.width, canvasEl.height).data;
                let n = 0;
                for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
                return n;
            }""", mode)
        assert painted > 1000, f"{mode} モードで何も描かれていない"
    assert page._risu_errors == [], page._risu_errors


def test_stats_panel_matches_server_stats(page, sim):
    stats = sim["stats"]
    end = page.evaluate(
        """() => {
            timeSlider.value = 1000; drawFrame(); drawAllStatCharts(); updateStatValues();
            const num = id => parseFloat(document.getElementById(id).textContent);
            return { active: num('sv-active'), completed: num('sv-completed'), entered: num('sv-inflow'),
                     speed: document.getElementById('sv-speed').textContent };
        }""")
    assert end["completed"] == stats["completed_trips"]
    assert end["entered"] == stats["total_trips"]
    assert end["active"] == stats["total_trips"] - stats["completed_trips"]
    # 途中の時刻では走行中 > 0（車両が描かれている時刻）
    mid = page.evaluate(
        """() => { timeSlider.value = 200; drawFrame(); updateStatValues();
                   return parseFloat(document.getElementById('sv-active').textContent); }""")
    assert mid > 0


def test_signals_are_drawn_as_stop_bars(page, sim):
    # 信号交差点 I には 2 本の流入リンク → 緑と赤の停止線バーが描かれる
    counts = page.evaluate(
        """() => {
            setDrawMode('vehicle'); timeSlider.value = 100; drawFrame();
            const ctx = canvasEl.getContext('2d');
            const d = ctx.getImageData(0, 0, canvasEl.width, canvasEl.height).data;
            let g = 0, r = 0;
            for (let i = 0; i < d.length; i += 4) {
              const R = d[i], G = d[i+1], B = d[i+2];
              if (Math.abs(R-0x2d)<12 && Math.abs(G-0x9a)<12 && Math.abs(B-0x4a)<12) g++;
              else if (Math.abs(R-0xdc)<12 && Math.abs(G-0x35)<12 && Math.abs(B-0x45)<12) r++;
            }
            return { g, r, signals: signalsData.length };
        }""")
    assert counts["signals"] == 1
    assert counts["g"] > 0 and counts["r"] > 0, counts


def test_editor_carries_scenario_params(page, sim):
    vals = page.evaluate(
        """() => {
            toggleEditMode();
            const v = { rt: document.getElementById('et-rt').value,
                        seed: document.getElementById('et-seed').value,
                        tmax: document.getElementById('et-tmax').value,
                        nodes: editNet.nodes.length, links: editNet.links.length };
            exitEditMode();
            return v;
        }""")
    assert float(vals["rt"]) == SCENARIO["reaction_time"]
    assert int(vals["seed"]) == SCENARIO["random_seed"]
    assert int(vals["tmax"]) == SCENARIO["tmax"]
    assert vals["nodes"] == len(SCENARIO["nodes"]) and vals["links"] == len(SCENARIO["links"])
    assert page._risu_errors == [], page._risu_errors
