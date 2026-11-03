"""利用者の操作シナリオ U01〜U12 をブラウザで通す（Playwright + headless Chromium）．

LLM は呼ばない．uvicorn を同一プロセスのスレッドで起動するので，`anthropic.Anthropic` を
台本つきの偽クライアント（tests/helpers.RecordingFakeAnthropic）に差し替え，`risu.api.LLM_BACKEND`
を "claude" にすると /chat が SSE ストリーミング経路（chat_claude_stream → dispatch_tool_blocks）
を実際に通る．ツール実行（run_simulation / rerun_simulation / get_simulation_data）は本物の
UXsim で走る．

各試験は独立: page フィクスチャが毎回ページを読み直して localStorage を消し，偽クライアントの
台本を空にし，試験中に results_store へ増えた結果を終了時に取り除く（IsolatedStore）．

  U01 チャット → 進捗 → 結果表示 → チャート
  U02 ファイル添付（/upload 経由）→ エディタで容量を 0 に → 再実行（全体パラメータの引継ぎ）
  U03 保存（.json+result）→ NEW → 履歴 / アップロードから復元 → ID の取り違えなし
  U04 応答中に STOP → 再送
  U05 応答中に NEW → 遅れて届く旧会話のイベントが新会話を汚さない → 履歴から別結果へ
  U06 需要の空白時間・全車両到着後の表示（走行中 0・到着の累積は保持）
  U07 SSE が 7 バイト単位で分割されても（UTF-8 の途中・イベント境界）正しく復元される
  U08 壊れた chart・名前や本文の HTML が実行されない（DOMPurify）・注記が出る
  U09 時間軸の異なる 2 結果の比較チャート（和集合の軸）→ 別結果へ切替えてもチャートは不変
  U10 履歴から前の結果を再表示 → 修正依頼の last_sim_id / base_sim_id がその ID
  U11 LINK / VEHICLE / TRAIL 切替と現示境界の停止線色（phase_log 準拠）
  U12 壊れたファイル → LLM 接続失敗 → 正しい入力で復帰，既存結果も見られる
"""
from __future__ import annotations

import copy
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from helpers import (  # noqa: E402
    RecordingFakeAnthropic, _FakeBlock, _FakeMessage, _FakeStream, _RecordingMessages,
    text_msg, tool_msg, types_ns,
)
from fixtures import (  # noqa: E402
    F1, F1_NO_JAM, F1_TIGHT, F5_DEMAND_NOTE, F5_GEOMETRIES, IsolatedStore, changed_paths, f1,
    store_f5, store_result,
)
from risu.results import results_store  # noqa: E402


# ──────────────────────────────────────────────
# 偽 Anthropic クライアント（ゆっくり流れるストリームも作れる）
# ──────────────────────────────────────────────
class _SlowStream(_FakeStream):
    """イベントごとに delay 秒待つストリーム（STOP / NEW を応答の途中で押す試験用）．

    chat_claude_stream は SDK のストリームを同期的に回すので，この sleep はサーバーの
    イベントループを塞ぐ．実 SDK でも各イベントの受信待ちは同じ性質なので，試験としては
    「応答の途中」を作る手段として十分．短く保つこと．
    """

    def __init__(self, message, events, delay):
        super().__init__(message, events)
        self._delay = delay

    def __iter__(self):
        for e in self._events:
            time.sleep(self._delay)
            yield e


class _E2EMessages(_RecordingMessages):
    def stream(self, **kwargs):
        msg = self._next("stream", kwargs)
        events = [types_ns(type="content_block_delta", delta=types_ns(type="text_delta", text=b.text))
                  for b in msg.content if b.type == "text"]
        delay = getattr(msg, "delay", 0)
        return _SlowStream(msg, events, delay) if delay else _FakeStream(msg, events)


FAKE = RecordingFakeAnthropic([])
FAKE.messages = _E2EMessages([])


def slow_text(parts: list[str], delay: float = 0.2) -> _FakeMessage:
    """複数の text_delta に分かれて，1 つずつ delay 秒おきに届く最終回答．"""
    m = _FakeMessage([_FakeBlock("text", text=p) for p in parts], "end_turn")
    m.delay = delay
    return m


def multi_text(parts: list[str]) -> _FakeMessage:
    """複数の text_delta に分かれて届く（遅延なし）最終回答．"""
    return _FakeMessage([_FakeBlock("text", text=p) for p in parts], "end_turn")


def run_sim_msg(scenario: dict, tid: str = "tu_run") -> _FakeMessage:
    return tool_msg("run_simulation", copy.deepcopy(scenario), tid=tid)


# ──────────────────────────────────────────────
# サーバー / ブラウザ
# ──────────────────────────────────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    """偽 Anthropic を差し込んだ状態で uvicorn をスレッド起動し，URL を返す．"""
    os.environ["RISU_STARTUP_SELFCHECK"] = "0"
    os.chdir(ROOT)   # static/ の相対パスで mount している
    import anthropic
    import uvicorn

    import risu.api
    import risu.llm

    orig_client, orig_backend, orig_key = anthropic.Anthropic, risu.api.LLM_BACKEND, risu.llm.ANTHROPIC_API_KEY
    anthropic.Anthropic = lambda **kw: FAKE
    risu.api.LLM_BACKEND = "claude"          # api.py は値で import しているのでこちらを差し替える
    risu.llm.ANTHROPIC_API_KEY = "dummy"     # 空だと実行前に弾かれる

    port = _free_port()
    config = uvicorn.Config(risu.api.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started, "uvicorn が起動しない"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        th.join(timeout=5)
        anthropic.Anthropic, risu.api.LLM_BACKEND, risu.llm.ANTHROPIC_API_KEY = orig_client, orig_backend, orig_key


@pytest.fixture(scope="module")
def _page(base_url):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
              if m.type == "error" else None)
        pg.on("dialog", lambda d: d.accept())   # NEW の confirm() は常に OK
        pg._risu_errors = errors
        yield pg
        browser.close()


@pytest.fixture
def page(_page, base_url):
    """試験ごとに: ページ再読込・localStorage 消去・台本リセット・results_store の隔離．"""
    FAKE.messages._script[:] = []
    FAKE.messages.calls.clear()
    with IsolatedStore() as iso:
        _page.goto(base_url, wait_until="load")
        _page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        _page.wait_for_function("typeof loadResult === 'function' && typeof RisuCore === 'object'")
        _page._risu_errors[:] = []
        _page._iso = iso
        yield _page
        # 進行中の fetch を切ってから次の試験へ（遅いストリームが残っていても台本を取り違えない）
        _page.goto("about:blank")
        for _ in range(100):
            if not FAKE.messages._script:
                break
            time.sleep(0.1)


# ──────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────
def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _sse_events(text: str) -> list[dict]:
    out = []
    for part in text.split("\n\n"):
        part = part.strip()
        if part.startswith("data: "):
            out.append(json.loads(part[6:]))
    return out


def _install_progress_probe(page):
    """#ai-status の文言の変化を window.__progress に記録する（進捗は一瞬で消えるため）．"""
    page.evaluate("""() => {
        window.__progress = [];
        const el = document.querySelector('#ai-status .status-msg');
        // 1 回の同期ループで何度も書き換わると callback は最後の状態しか見られないので，記録ごとに拾う
        new MutationObserver((records) => {
          for (const r of records) for (const n of r.addedNodes) {
            const t = n.textContent;
            if (t && window.__progress[window.__progress.length - 1] !== t) window.__progress.push(t);
          }
        }).observe(el, { childList: true, characterData: true, subtree: true });
    }""")


def _send(page, text: str):
    """プロンプトを送って /chat の応答（ストリーム完了後の全文）を返す．"""
    page.fill("#user-input", text)
    with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=90000) as ri:
        page.click("#send-btn")
    return ri.value


def _done_event(resp) -> dict:
    evs = _sse_events(resp.text())
    done = [e for e in evs if e.get("type") == "done"]
    assert done, [e.get("type") for e in evs]
    return done[-1]


def _wait_idle(page, timeout: int = 90000):
    """送信ボタンが戻り，ステータスバーが消えるまで（端末が操作可能な状態）．"""
    page.wait_for_function("!document.getElementById('send-btn').disabled", timeout=timeout)
    page.wait_for_function("!document.getElementById('ai-status').classList.contains('visible')", timeout=timeout)


def _wait_result(page, sim_id: str, timeout: int = 60000):
    """loadResult(sim_id) が終わって統計系列まで揃うのを待ち，自動再生を止める．"""
    page.wait_for_function("(id) => lastSimId === id && frameTimes.length > 0 && !!statsSeries",
                           arg=sim_id, timeout=timeout)
    page.wait_for_timeout(150)      # loadResult は 100ms 後に startPlay する
    page.evaluate("stopPlay()")


def _operable(page) -> dict:
    return page.evaluate("""() => ({
        sendDisabled: document.getElementById('send-btn').disabled,
        inputDisabled: document.getElementById('user-input').disabled,
        statusVisible: document.getElementById('ai-status').classList.contains('visible'),
        label: document.getElementById('sim-id-label').textContent,
        lastSimId: lastSimId,
    })""")


def _system_messages(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('#messages .msg.system .body')].map(e => e.textContent)")


def _last_assistant_raw(page):
    return page.evaluate("""() => {
        const els = [...document.querySelectorAll('#messages .msg.assistant .body')];
        return els.length ? (els[els.length - 1]._raw ?? null) : null;
    }""")


def _click_server_history(page, sim_id: str):
    """履歴ドロワーの「サーバーの結果」から sim_id の行をクリックする（loadResult + 閉じる）．"""
    if page.evaluate("document.getElementById('history-drawer').classList.contains('visible')"):
        page.click("#history-btn")
    page.click("#history-btn")
    sel = f"#history-list .hist-section ~ .hist-item:has-text('{sim_id}')"
    page.wait_for_selector(sel, timeout=30000)
    page.click(sel)


def _stat_values(page) -> dict:
    return page.evaluate("""() => {
        const num = id => parseFloat(document.getElementById(id).textContent);
        return { active: num('sv-active'), completed: num('sv-completed'), entered: num('sv-inflow'),
                 label: document.getElementById('sim-id-label').textContent };
    }""")


def _set_time(page, t: float):
    page.evaluate("(t) => { timeSlider.value = Math.round(t / simTmax * 1000); drawFrame(); "
                  "drawAllStatCharts(); updateStatValues(); }", t)


def _stop_bar_colors(page) -> dict:
    """流入リンクごとの停止線バーの色（'green' / 'red' / None）と，画面全体の緑・赤ピクセル数．

    バーの位置は render.js の _drawSignals と同じ（終端から進行方向に backOff 戻った点）．
    その周囲 ±5px で SIGNAL_COLORS に近い画素を数え，多い方の色を採る．
    """
    return page.evaluate("""() => {
        const dpr = Math.min(1.5, window.devicePixelRatio || 1);
        const ctx = canvasEl.getContext('2d');
        const d = ctx.getImageData(0, 0, canvasEl.width, canvasEl.height).data;
        const W = canvasEl.width;
        const isG = (i) => Math.abs(d[i]-0x2d)<12 && Math.abs(d[i+1]-0x9a)<12 && Math.abs(d[i+2]-0x4a)<12;
        const isR = (i) => Math.abs(d[i]-0xdc)<12 && Math.abs(d[i+1]-0x35)<12 && Math.abs(d[i+2]-0x45)<12;
        let g = 0, r = 0;
        for (let i = 0; i < d.length; i += 4) { if (isG(i)) g++; else if (isR(i)) r++; }
        const dense = (geoData.features || []).length > 30;
        const backOff = dense ? 7 : 11;
        const bars = {};
        for (const [name] of Object.entries(signalsData[0].groups || {})) {
          const info = _staticLayer.linkDrawInfo[name];
          const a = _approachGeom(info);
          const cx = a.endX - a.tx * backOff, cy = a.endY - a.ty * backOff;
          let gg = 0, rr = 0;
          for (let dy = -5; dy <= 5; dy++) for (let dx = -5; dx <= 5; dx++) {
            const x = Math.round((cx + dx) * dpr), y = Math.round((cy + dy) * dpr);
            const i = (y * W + x) * 4;
            if (isG(i)) gg++; else if (isR(i)) rr++;
          }
          bars[name] = gg > rr ? 'green' : (rr > gg ? 'red' : null);
        }
        return { g, r, bars };
    }""")


def _chart_summary(page, idx: int = 0) -> dict:
    return page.evaluate("""(i) => {
        const c = window._chartInstances[i];
        return { labels: c.data.labels,
                 datasets: c.data.datasets.map(d => ({ label: d.label, data: d.data })),
                 risu: (c.options.plugins || {}).risu || null };
    }""", idx)


def _page_errors(page, ignore: tuple[str, ...] = ()) -> list[str]:
    return [e for e in page._risu_errors if not any(s in e for s in ignore)]


CHART_AVG_SPEED = ('```chart\n{"type": "line", "data": {"labels": {"$data": "time_labels"}, '
                   '"datasets": [{"label": "平均速度 m/s", "data": {"$data": "network_avg_speed"}}]}}\n```')

# 需要が 2 回に分かれる（間に空白時間がある）シナリオ．burst 1 = 0.5 台/s × 10 s = 5 台
GAP_SCENARIO = {
    "name": "e2e_gap", "tmax": 1000, "deltan": 5, "random_seed": 1,
    "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 100, "y": 0}],
    "links": [{"name": "AB", "start": "A", "end": "B", "length": 100}],
    "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 10, "flow": 0.5},
                {"orig": "A", "dest": "B", "t_start": 700, "t_end": 710, "flow": 0.5}],
}

# 現示 0 = 西 1 本，現示 1 = 南北 2 本（緑 / 赤の本数が現示で変わるので色の対応を数で確かめられる）
SIGNAL_ASYM = {
    "name": "e2e_signal_asym", "tmax": 600, "deltan": 5, "reaction_time": 1.5, "random_seed": 7,
    "nodes": [{"name": "W", "x": -1000, "y": 0}, {"name": "I", "x": 0, "y": 0, "signal": [30, 30]},
              {"name": "E", "x": 1000, "y": 0}, {"name": "S", "x": 0, "y": -1000}, {"name": "N", "x": 0, "y": 1000}],
    "links": [{"name": "W_I", "start": "W", "end": "I", "length": 1000, "signal_group": 0},
              {"name": "S_I", "start": "S", "end": "I", "length": 1000, "signal_group": 1},
              {"name": "N_I", "start": "N", "end": "I", "length": 1000, "signal_group": 1},
              {"name": "I_E", "start": "I", "end": "E", "length": 1000}],
    "demands": [{"orig": "W", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.2},
                {"orig": "S", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.1},
                {"orig": "N", "dest": "E", "t_start": 0, "t_end": 300, "flow": 0.1}],
}


# ══════════════════════════════════════════════
# U01 チャット → 進捗 → 結果 → チャート
# ══════════════════════════════════════════════
def test_u01_chat_progress_result_and_chart(page):
    FAKE.messages._script[:] = [
        run_sim_msg(F1),
        text_msg("ボトルネック AB で渋滞が発生しました．\n\n" + CHART_AVG_SPEED),
    ]
    _install_progress_probe(page)
    resp = _send(page, "ボトルネック道路を作ってシミュレーションして")
    done = _done_event(resp)
    assert done["sim_id"] and done.get("charts") and len(done["charts"]) == 1
    _wait_idle(page)
    _wait_result(page, done["sim_id"])

    # 進捗が表示された（Step 1/3 → Step 2/3 → Step 3/3）
    progress = page.evaluate("window.__progress")
    assert any("Step 1/3" in p for p in progress) and any("Step 2/3" in p for p in progress), progress
    assert any("Step 3/3" in p for p in progress), progress

    # 表示中の ID = done.sim_id = results_store に入った ID
    assert page._iso.new_ids() == [done["sim_id"]]
    st = _operable(page)
    assert st["label"] == done["sim_id"] and st["lastSimId"] == done["sim_id"]
    badge = page.text_content("#messages .msg.assistant .sim-badge")
    assert done["sim_id"] in badge

    # チャートが吹き出し内に描かれ，参照が実データで埋まっている
    assert page.locator("#messages .msg.assistant .chart-wrap canvas").count() == 1
    ch = _chart_summary(page, 0)
    assert len(ch["labels"]) > 2 and len(ch["datasets"][0]["data"]) == len(ch["labels"])
    assert all(isinstance(v, (int, float)) for v in ch["datasets"][0]["data"])
    assert "```chart" not in _last_assistant_raw(page)

    # 端末は操作可能な状態に戻る
    assert st["sendDisabled"] is False and st["inputDisabled"] is False and st["statusVisible"] is False
    assert _page_errors(page) == [], page._risu_errors


def test_u01_map_shows_before_the_explanation_finishes(page):
    """結果ができた時点（sim_ready）で地図を出し，説明の生成完了（done）を待たない．done で読み直さない．

    [修正履歴] 地図は done を受けてから読み込んでいたので，計算が 3〜4 秒で終わっても
    LLM が説明を書く 8 秒ほど地図が出なかった．"""
    FAKE.messages._script[:] = [
        run_sim_msg(F1),
        slow_text([f"説明{i}．" for i in range(12)], delay=0.3),
    ]
    gets: list[str] = []
    page.on("request", lambda r: gets.append(r.url) if "/results/" in r.url else None)
    page.fill("#user-input", "ボトルネック道路を作ってシミュレーションして")
    page.click("#send-btn")
    page.wait_for_function("lastSimId && frameTimes.length > 0", timeout=30000)
    shown = page.evaluate("lastSimId")
    # 地図が出た時点では，まだ説明を生成中（送信ボタンが戻っていない）
    assert page.evaluate("document.getElementById('send-btn').disabled") is True
    _wait_idle(page)
    _wait_result(page, shown)
    assert page.text_content("#sim-id-label") == shown
    assert shown in page.text_content("#messages .msg.assistant .sim-badge")
    assert len([u for u in gets if u.endswith(f"/results/{shown}")]) == 1    # done で読み直さない
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U02 添付 → 表示 → エディタで容量を変更 → 再実行
# ══════════════════════════════════════════════
def test_u02_attach_file_edit_capacity_and_rerun(page):
    sc = f1(reaction_time=1.7, random_seed=13, deltan=5, name="attach_src")
    # 20KB を超える添付は UI が /upload に直接送る（LLM を通さない）．JSON の末尾の空白は無害
    payload = json.dumps(sc).encode("utf-8") + b" " * 21000
    FAKE.messages._script[:] = [text_msg("添付を読み込みました"), text_msg("添付を読み込みました")]
    page.set_input_files("#file-input", files=[{"name": "attach.json", "mimeType": "application/json",
                                                "buffer": payload}])
    assert page.locator("#file-preview .file-chip").count() == 1
    _send(page, "これを表示して")     # 添付のシステム注記に「実行」が含まれるので強制再試行が 1 回入る
    _wait_idle(page)
    page.wait_for_function("lastSimId !== null && frameTimes.length > 0 && !!statsSeries", timeout=60000)
    page.wait_for_timeout(150); page.evaluate("stopPlay()")
    src_id = page.evaluate("lastSimId")
    assert any(f"sim_id: {src_id}" in m for m in _system_messages(page))
    assert results_store[src_id]["_meta"]["source"]["type"] == "json"
    base = results_store[src_id]["_scenario"]
    assert base["reaction_time"] == 1.7 and base["random_seed"] == 13 and base["deltan"] == 5

    # EDIT → リンク AB をクリックで選択 → 容量を 0 に
    page.click("#edit-btn")
    page.wait_for_function("editMode === true && editNet && editNet.links.length === 2")
    assert page.input_value("#et-rt") == "1.7" and page.input_value("#et-seed") == "13"
    pt = page.evaluate("""() => {
        const T = editTransform();
        const byName = {}; for (const n of editNet.nodes) byName[n.name] = n;
        const lk = editNet.links.find(l => l.name === 'AB');
        const a = byName[lk.start], b = byName[lk.end];
        const r = canvasEl.getBoundingClientRect();
        return { x: r.left + (T.px(a.x) + T.px(b.x)) / 2, y: r.top + (T.py(a.y) + T.py(b.y)) / 2 };
    }""")
    page.mouse.click(pt["x"], pt["y"])
    page.wait_for_selector("#edit-props input[data-ep='capacity']", state="visible")
    assert page.text_content("#edit-props .ep-title").startswith("LINK A → B")
    assert page.input_value("#edit-props input[data-ep='capacity']") == "0.5"
    page.fill("#edit-props input[data-ep='capacity']", "0")
    page.dispatch_event("#edit-props input[data-ep='capacity']", "change")
    assert page.evaluate("editNet.links.find(l => l.name === 'AB').capacity") == 0

    # 実行 → 新しい結果
    page.click("#et-run")
    page.wait_for_function("(old) => lastSimId !== null && lastSimId !== old && !editMode && !!statsSeries",
                           arg=src_id, timeout=60000)
    page.wait_for_timeout(150); page.evaluate("stopPlay()")
    new_id = page.evaluate("lastSimId")
    assert new_id in results_store and new_id != src_id
    new_sc = results_store[new_id]["_scenario"]
    diff = changed_paths(base, new_sc)
    assert set(diff) <= {"name", "links.AB.capacity"}, diff
    assert new_sc["links"][0]["name"] == "AB" and new_sc["links"][0]["capacity"] == 0   # 0 は削除されない
    assert new_sc["reaction_time"] == 1.7 and new_sc["random_seed"] == 13 and new_sc["deltan"] == 5
    assert [n["name"] for n in new_sc["nodes"]] == ["A", "B", "C"]
    assert len(new_sc["demands"]) == 1
    assert page.text_content("#sim-id-label") == new_id
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U03 保存 → NEW → 復元 → 照合
# ══════════════════════════════════════════════
def _assert_f5_restored(page, src_id: str, shown_id: str):
    st = page.evaluate("""() => ({
        sc: { tmax: scenarioData.tmax, deltan: scenarioData.deltan, rt: scenarioData.reaction_time,
              seed: scenarioData.random_seed, links: scenarioData.links.length, nodes: scenarioData.nodes.length,
              demands: scenarioData.demands.length },
        stats: simStats, label: document.getElementById('sim-id-label').textContent, lastSimId,
        ab: geoData.features.find(f => f.properties.name === 'AB').geometry.coordinates,
        paramsTitle: document.getElementById('scn-params-label').title,
    })""")
    src = results_store[src_id]
    assert st["label"] == shown_id and st["lastSimId"] == shown_id
    assert st["sc"] == {"tmax": 2400, "deltan": 1, "rt": 1.0, "seed": 42, "links": 6, "nodes": 4, "demands": 3}
    for k in ("total_trips", "completed_trips", "average_travel_time_s"):
        assert st["stats"][k] == src["stats"][k], k
    return st


def test_u03_save_new_restore_from_history(page, base_url, tmp_path):
    src_id = "e2e_u03_src"
    store_f5(src_id)
    page.evaluate("async (id) => { await loadResult(id); }", src_id)
    _wait_result(page, src_id)

    # .json+result をダウンロード（/results/{id} をそのまま保存）
    with page.expect_download(timeout=60000) as dl:
        page.click("#dl-btn")
    path = tmp_path / dl.value.suggested_filename
    dl.value.save_as(str(path))
    with open(path, encoding="utf-8") as f:
        env = json.load(f)
    assert env["sim_id"] == src_id and env["scenario"]["random_seed"] == 42
    assert env["source"]["demand"]["note"] == F5_DEMAND_NOTE
    assert env["result"]["stats"]["total_trips"] == results_store[src_id]["stats"]["total_trips"]

    # 新しい会話 → 表示は空に戻る
    page.click("#new-chat-btn")
    assert _operable(page)["label"] == "—" and page.evaluate("lastSimId") is None

    # 履歴（サーバーの結果）から復元 → シナリオ・統計・多点形状・出所が揃う
    _click_server_history(page, src_id)
    _wait_result(page, src_id)
    st = _assert_f5_restored(page, src_id, src_id)
    assert st["ab"] == F5_GEOMETRIES["AB"]
    assert F5_DEMAND_NOTE in st["paramsTitle"]
    assert _page_errors(page) == [], page._risu_errors


def test_u03_restore_from_uploaded_envelope(page, base_url, tmp_path):
    """保存した .json+result を添付で読み戻す: パラメータ・統計・出所（imported_from）が復元され，
    表示 ID は新しい結果（元の ID と取り違えない）．"""
    src_id = "e2e_u03_up"
    store_f5(src_id)
    page.evaluate("async (id) => { await loadResult(id); }", src_id)
    _wait_result(page, src_id)
    with page.expect_download(timeout=60000) as dl:
        page.click("#dl-btn")
    path = tmp_path / "saved.json"
    dl.value.save_as(str(path))
    page.click("#new-chat-btn")

    FAKE.messages._script[:] = [text_msg("読み戻しました"), text_msg("読み戻しました")]
    page.set_input_files("#file-input", files=[str(path)])
    _send(page, "これを読み戻して")
    _wait_idle(page)
    page.wait_for_function("(old) => lastSimId !== null && lastSimId !== old && !!statsSeries", arg=src_id,
                           timeout=60000)
    page.wait_for_timeout(150); page.evaluate("stopPlay()")
    new_id = page.evaluate("lastSimId")
    assert new_id != src_id and new_id in page._iso.new_ids()
    _assert_f5_restored(page, src_id, new_id)
    meta = results_store[new_id]["_meta"]["source"]
    assert meta["type"] == "json" and meta["imported_from_sim_id"] == src_id
    assert _page_errors(page) == [], page._risu_errors


def test_u03_uploaded_envelope_keeps_link_geometry(page, base_url, tmp_path):
    src_id = "e2e_u03_geom"
    store_f5(src_id)
    page.evaluate("async (id) => { await loadResult(id); }", src_id)
    _wait_result(page, src_id)
    with page.expect_download(timeout=60000) as dl:
        page.click("#dl-btn")
    path = tmp_path / "saved.json"
    dl.value.save_as(str(path))
    page.click("#new-chat-btn")
    FAKE.messages._script[:] = [text_msg("読み戻しました"), text_msg("読み戻しました")]
    page.set_input_files("#file-input", files=[str(path)])
    _send(page, "これを読み戻して")
    _wait_idle(page)
    page.wait_for_function("(old) => lastSimId !== null && lastSimId !== old && !!statsSeries", arg=src_id,
                           timeout=60000)
    ab = page.evaluate("geoData.features.find(f => f.properties.name === 'AB').geometry.coordinates")
    assert ab == F5_GEOMETRIES["AB"]


# ══════════════════════════════════════════════
# U04 応答中に STOP → 再送
# ══════════════════════════════════════════════
def test_u04_stop_during_response_then_resend(page):
    FAKE.messages._script[:] = [
        slow_text(["ゆっくり", "した", "返事", "です", "．", "まだ", "続き", "ます"], delay=0.25),
        run_sim_msg(F1_NO_JAM),
        text_msg("実行しました．"),
    ]
    page.fill("#user-input", "こんにちは")      # 強制再試行の語を含めない（台本を増やさない）
    page.click("#send-btn")
    page.wait_for_selector("#messages .msg.assistant", timeout=30000)   # ストリーム開始
    page.click("#cancel-btn")
    page.wait_for_function(
        "[...document.querySelectorAll('#messages .msg.system .body')].some(e => e.textContent.includes('中止'))",
        timeout=10000)
    st = _operable(page)
    assert st["sendDisabled"] is False and st["inputDisabled"] is False and st["statusVisible"] is False
    assert st["lastSimId"] is None

    resp = _send(page, "ボトルネックを作って")
    done = _done_event(resp)
    assert done["sim_id"]
    _wait_idle(page)
    _wait_result(page, done["sim_id"])
    assert _last_assistant_raw(page) == "実行しました．"
    assert page.text_content("#sim-id-label") == done["sim_id"]
    # 中止した応答の断片は残っていてもよいが，最後の吹き出し・表示 ID を上書きしない
    assert page.evaluate("lastSimId") == done["sim_id"]
    assert FAKE.messages._script == []
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U05 応答中に NEW → 旧会話の遅延イベント → 履歴から別結果へ
# ══════════════════════════════════════════════
def test_u05_new_during_response_keeps_new_conversation_clean(page):
    other = "e2e_u05_other"
    store_result(other, F1_TIGHT)
    FAKE.messages._script[:] = [
        run_sim_msg(F1_NO_JAM),
        slow_text(["結果", "の", "説明", "を", "ゆっくり", "書き", "ます", "．"], delay=0.25),
    ]
    page.fill("#user-input", "ボトルネックを作って")
    page.click("#send-btn")
    page.wait_for_selector("#messages .msg.assistant", timeout=60000)   # 最終回答のストリーム中
    page.click("#new-chat-btn")        # confirm は自動 OK
    assert page.evaluate("lastSimId") is None and page.evaluate("chatHistory.length") == 0

    # 旧会話の応答が最後まで届く（送信ボタンが戻る）のを待つ
    page.wait_for_function("!document.getElementById('send-btn').disabled", timeout=60000)
    page.wait_for_timeout(800)
    old_ids = page._iso.new_ids()
    assert len(old_ids) == 1
    st = _operable(page)
    assert st["lastSimId"] is None and st["label"] == "—", st
    assert page.locator("#messages .msg.assistant").count() == 0
    assert page.evaluate("chatHistory.length") == 0

    _click_server_history(page, other)
    _wait_result(page, other)
    assert page.text_content("#sim-id-label") == other


# ══════════════════════════════════════════════
# U06 需要の空白時間・全車両到着後
# ══════════════════════════════════════════════
def test_u06_demand_gap_and_after_arrivals(page, base_url):
    sid = _post_json(f"{base_url}/simulate", GAP_SCENARIO)["id"]
    page.evaluate("async (id) => { await loadResult(id); }", sid)
    _wait_result(page, sid)
    ts = results_store[sid]["trip_series"]

    def expect_at(t):
        i = max(k for k, tt in enumerate(ts["t"]) if tt <= t)
        return ts["entered"][i], ts["completed"][i]

    e1, c1 = expect_at(500)
    assert e1 == 5 and c1 == 5                   # burst 1 = 5 台，空白時間までに全て到着
    for t, (exp_e, exp_c) in ((500, (e1, c1)), (1000, expect_at(1000))):
        _set_time(page, t)
        v = page.evaluate("""() => {
            const t = currentTimeSec();
            return { active: parseFloat(document.getElementById('sv-active').textContent),
                     completed: parseFloat(document.getElementById('sv-completed').textContent),
                     entered: parseFloat(document.getElementById('sv-inflow').textContent),
                     mode: RisuCore.frameWindow(frameTimes, t, frameIntervalS).mode };
        }""")
        assert v["mode"] == "none", (t, v)
        assert v["active"] == 0 and v["completed"] == exp_c and v["entered"] == exp_e, (t, v)
    # 全車両到着後は到着 = 流入 = 総台数
    assert expect_at(1000) == (10, 10)
    assert page.evaluate("statsSeries.totalTrips") == 10
    # 描画された車両数 0（背景の静的レイヤー以外の車両ドットが無い）: 空白時間は interp/hold ではなく none
    assert page.evaluate("RisuCore.frameWindow(frameTimes, 12, frameIntervalS).mode") == "hold"
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U07 SSE の任意位置分割（UTF-8 の途中・イベント境界）
# ══════════════════════════════════════════════
def test_u07_sse_split_at_arbitrary_bytes(page, monkeypatch):
    import risu.api
    from fastapi.responses import StreamingResponse

    orig = risu.api.chat_claude_stream

    async def rechunked(body):
        resp = await orig(body)
        inner = resp.body_iterator

        async def gen():
            async for c in inner:
                b = c.encode("utf-8") if isinstance(c, str) else c
                for i in range(0, len(b), 7):     # 7 バイト刻み: 日本語（3 バイト）とイベント境界を必ず跨ぐ
                    yield b[i:i + 7]
        return StreamingResponse(gen(), media_type="text/event-stream")

    monkeypatch.setattr(risu.api, "chat_claude_stream", rechunked)
    parts = ["渋滞は 600 秒", "から始まり，平均速度は ", "12.3 m/s まで低下", "しました．\n\n**要点**\n",
             "- ボトルネックは AB（容量 0.5 台/秒）\n", "- 到着 360 台，平均旅行時間は自由流の 2 倍超\n",
             "記号も混ぜる: 「」・→・≒・😀"]
    FAKE.messages._script[:] = [run_sim_msg(F1), multi_text(parts)]
    _install_progress_probe(page)
    resp = _send(page, "ボトルネック道路を作ってシミュレーションして")
    done = _done_event(resp)
    assert done["sim_id"]
    _wait_idle(page)
    _wait_result(page, done["sim_id"])
    expected = "".join(parts).strip()
    assert _last_assistant_raw(page) == expected
    assert done["content"] == expected
    progress = page.evaluate("window.__progress")
    assert any("Step 2/3" in p for p in progress) and any("Step 3/3" in p for p in progress), progress
    assert page.text_content("#sim-id-label") == done["sim_id"]
    assert page._iso.new_ids() == [done["sim_id"]]
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U08 壊れた chart・HTML 入りの名前と本文
# ══════════════════════════════════════════════
def test_u08_invalid_chart_and_html_are_rendered_safely(page):
    xss_link = "<img src=x onerror=window.__xss=1>"
    sc = copy.deepcopy(F1)
    sc["links"][0]["name"] = xss_link
    answer = ("結果です．<script>window.__xss2=1</script> リンク " + xss_link +
              " がボトルネックです．\n\n```chart\n{\"type\": \"line\", \"data\": {broken\n```\n")
    FAKE.messages._script[:] = [run_sim_msg(sc), text_msg(answer)]
    resp = _send(page, "ボトルネックを作って")
    done = _done_event(resp)
    assert done["sim_id"] and not done.get("charts")
    _wait_idle(page)
    _wait_result(page, done["sim_id"])

    # 名前はそのまま保存されている（サーバーは名前を書き換えない）
    assert results_store[done["sim_id"]]["link_names"][0] == xss_link
    st = page.evaluate("""() => {
        const els = [...document.querySelectorAll('#messages .msg.assistant .body')];
        const body = els[els.length - 1];
        return { xss: window.__xss, xss2: window.__xss2, html: body.innerHTML, text: body.textContent,
                 raw: body._raw, onerror: !!body.querySelector('[onerror]'), script: !!body.querySelector('script') };
    }""")
    assert st["xss"] is None and st["xss2"] is None
    # DOM として script 要素・onerror 属性が存在しない（名前は文字として見えてよい）
    assert st["onerror"] is False and st["script"] is False
    assert "がボトルネックです" in st["text"]
    assert "window.__xss2" not in st["text"]           # script の中身ごと除去される
    assert "注記" in st["raw"] and "グラフ設定の解析に失敗" in st["raw"]
    assert "注記" in st["text"] and "```chart" not in st["raw"]
    assert page.locator("#messages .chart-wrap").count() == 0
    # img の src=x は 404 になるだけ（リソース読込失敗はスクリプト実行ではない）
    assert _page_errors(page, ignore=("Failed to load resource",)) == [], page._risu_errors


# ══════════════════════════════════════════════
# U09 時間軸の異なる A/B 比較チャート → 別結果へ切替
# ══════════════════════════════════════════════
def test_u09_ab_comparison_chart_then_switch_result(page):
    from risu.aggregate import get_simulation_data
    a, b = "e2e_u09_a", "e2e_u09_b"
    store_result(a, f1(capacity=None, flow=0.3, t_end=300, tmax=600, name="short"))
    store_result(b, F1)
    da, db = get_simulation_data(a, points=30, max_links=20), get_simulation_data(b, points=30, max_links=20)
    assert list(da["time_labels"]) != list(db["time_labels"])   # 前提: 時間軸が違う

    _click_server_history(page, a)
    _wait_result(page, a)
    chart = json.dumps({"type": "line",
                        "data": {"labels": {"$data": "time_labels", "sim_id": a},
                                 "datasets": [{"label": "A", "data": {"$data": "network_avg_speed", "sim_id": a}},
                                              {"label": "B", "data": {"$data": "network_avg_speed", "sim_id": b}}]}},
                       ensure_ascii=False)
    FAKE.messages._script[:] = [
        _FakeMessage([_FakeBlock("tool_use", name="get_simulation_data", input={"sim_id": a}, id="tu_a"),
                      _FakeBlock("tool_use", name="get_simulation_data", input={"sim_id": b}, id="tu_b")],
                     "tool_use"),
        text_msg("A と B の平均速度を比べます．\n\n```chart\n" + chart + "\n```"),
    ]
    resp = _send(page, f"{a} と {b} を比較して")
    done = _done_event(resp)
    assert done["sim_id"] is None and len(done["charts"]) == 1
    _wait_idle(page)
    page.wait_for_selector("#messages .chart-wrap canvas")

    union = sorted({float(t) for t in da["time_labels"]} | {float(t) for t in db["time_labels"]})
    exp_labels = [int(t) if t.is_integer() else t for t in union]
    exp_a = {float(t): v for t, v in zip(da["time_labels"], da["network_avg_speed"])}
    exp_b = {float(t): v for t, v in zip(db["time_labels"], db["network_avg_speed"])}
    ch = _chart_summary(page, 0)
    assert ch["labels"] == exp_labels
    assert ch["risu"] == {"alignedTimeAxis": True, "sims": sorted([a, b])}
    for ds, exp in ((ch["datasets"][0], exp_a), (ch["datasets"][1], exp_b)):
        assert len(ds["data"]) == len(union)
        for t, v in zip(union, ds["data"]):
            if t in exp:
                assert v == pytest.approx(exp[t]), (ds["label"], t)
            else:
                assert v is None, (ds["label"], t)
    assert [None in ds["data"] for ds in ch["datasets"]].count(True) >= 1   # どちらかは自分に無い時刻が null
    assert page.text_content("#sim-id-label") == a

    # 別の結果へ切替えても，チャートは元の 2 sim のまま
    _click_server_history(page, b)
    _wait_result(page, b)
    assert page.text_content("#sim-id-label") == b
    assert _chart_summary(page, 0) == ch
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U10 履歴から前の結果を再表示 → 修正依頼
# ══════════════════════════════════════════════
def test_u10_redisplay_previous_result_and_modify(page):
    x, y = "e2e_u10_x", "e2e_u10_y"
    store_result(x, F1_NO_JAM)
    store_result(y, F1_TIGHT)
    _click_server_history(page, y)
    _wait_result(page, y)
    _click_server_history(page, x)          # 前の結果を再表示
    _wait_result(page, x)

    seen: dict = {}

    def rerun(kw):
        blocks = kw["messages"][-1]["content"]
        txt = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        m = re.search(r"この会話のシミュレーションID: (\S+)", txt)
        seen["ctx_id"] = m.group(1) if m else None
        return tool_msg("rerun_simulation", {"base_sim_id": seen["ctx_id"],
                                             "modifications": [{"action": "update_links", "names": ["AB"],
                                                                "set": {"capacity": 0.25}}]}, tid="tu_rr")

    FAKE.messages._script[:] = [rerun, text_msg("容量を 0.25 に下げて再実行しました．")]
    bodies: list[dict] = []

    def capture(route):
        bodies.append(json.loads(route.request.post_data))
        route.continue_()

    page.route("**/chat", capture)
    try:
        resp = _send(page, "AB の容量を 0.25 に下げて再実行して")
    finally:
        page.unroute("**/chat")
    done = _done_event(resp)
    _wait_idle(page)
    _wait_result(page, done["sim_id"])

    assert bodies and bodies[-1]["last_sim_id"] == x
    assert seen["ctx_id"] == x
    new_id = done["sim_id"]
    assert new_id not in (x, y)
    meta = results_store[new_id]["_meta"]["source"]
    assert meta["tool"] == "rerun_simulation" and meta["base_sim_id"] == x
    assert results_store[new_id]["_scenario"]["links"][0]["capacity"] == 0.25
    assert set(changed_paths(results_store[x]["_scenario"], results_store[new_id]["_scenario"])) == {"links.AB.capacity"}
    assert page.text_content("#sim-id-label") == new_id
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U11 描画モード切替と現示境界
# ══════════════════════════════════════════════
def test_u11_draw_modes_and_signal_boundary(page, base_url):
    d = _post_json(f"{base_url}/simulate", SIGNAL_ASYM)
    sid = d["id"]
    page.evaluate("async (id) => { await loadResult(id); }", sid)
    _wait_result(page, sid)

    # モードを切替えても表示 ID と統計は変わらない
    _set_time(page, 1000)   # 終了時刻
    ref = _stat_values(page)
    assert ref["label"] == sid and ref["completed"] == d["stats"]["completed_trips"]
    for mode in ("link", "vehicle", "trail", "link"):
        page.evaluate("(m) => { setDrawMode(m); drawAllStatCharts(); updateStatValues(); }", mode)
        assert page.evaluate("drawMode") == mode
        assert _stat_values(page) == ref, mode

    # phase_log の最後の切替点をまたぐ 2 時刻（車両はもう居ないので停止線バーだけが色を持つ）
    b = page.evaluate("""() => {
        const sig = signalsData[0]; const log = sig.phase_log; const dt = sig.deltat;
        let k = -1;
        for (let i = log.length - 1; i > 0; i--) if (log[i] !== log[i - 1]) { k = i; break; }
        const pick = (idx) => {
          let v = Math.round(((idx + 0.5) * dt) / simTmax * 1000);
          for (let s = -3; s <= 3; s++) {
            const t = simTmax * (v + s) / 1000;
            if (Math.floor(t / dt) === idx) return { v: v + s, t, phase: RisuCore.phaseIndexAt(sig, t, simTmax) };
          }
          return null;
        };
        return { k, dt, log_len: log.length, before: pick(k - 1), after: pick(k), phases: sig.phases,
                 groups: sig.groups };
    }""")
    assert b["k"] > 0 and b["before"] and b["after"], b
    assert b["before"]["phase"] != b["after"]["phase"]
    assert b["before"]["phase"] == b["after"]["phase"] ^ 1    # 2 現示
    assert b["before"]["t"] > 400                              # 需要は 300 s まで．車両は到着済み
    page.evaluate("setDrawMode('vehicle')")
    assert b["groups"] == {"W_I": [0], "S_I": [1], "N_I": [1]}
    px = {}
    for key in ("before", "after"):
        page.evaluate("(v) => { timeSlider.value = v; drawFrame(); }", b[key]["v"])
        px[key] = _stop_bar_colors(page)
        assert px[key]["g"] > 0 and px[key]["r"] > 0, (key, px[key])
        # 各流入リンクの停止線は，phase_log の現示にその group が含まれれば緑，でなければ赤
        phase = b[key]["phase"]
        expect = {name: ("green" if phase in grp else "red") for name, grp in b["groups"].items()}
        assert px[key]["bars"] == expect, (key, phase, px[key])
    # 境界をまたいで色が入れ替わる（現示 0 = 西 1 本が緑，現示 1 = 南北 2 本が緑）
    assert px["before"]["bars"] != px["after"]["bars"]
    ph0, ph1 = ("before", "after") if b["before"]["phase"] == 0 else ("after", "before")
    assert px[ph1]["g"] > px[ph0]["g"] and px[ph0]["r"] > px[ph1]["r"], px
    assert _page_errors(page) == [], page._risu_errors


# ══════════════════════════════════════════════
# U12 壊れたファイル → 接続失敗 → 正しい入力で復帰
# ══════════════════════════════════════════════
def test_u12_bad_file_then_network_error_then_recovery(page):
    import anthropic
    import httpx
    keep = "e2e_u12_keep"
    store_result(keep, F1_NO_JAM)

    # (1) 壊れた JSON（20KB 超 → /upload）→ 422 の平易なメッセージ
    bad = b'{"nodes": [' + b" " * 21000 + b"BROKEN"
    page.set_input_files("#file-input", files=[{"name": "bad.json", "mimeType": "application/json", "buffer": bad}])
    page.click("#send-btn")
    page.wait_for_function(
        "[...document.querySelectorAll('#messages .msg.system .body')].some(e => e.textContent.includes('解析できません'))",
        timeout=30000)
    _wait_idle(page)
    msgs = _system_messages(page)
    assert any("bad.json" in m and "エラー" in m for m in msgs), msgs
    assert page._iso.new_ids() == []
    assert FAKE.messages.calls == []        # LLM には届いていない

    # (2) LLM への接続失敗 → エラーイベント → 読める失敗文
    FAKE.messages._script[:] = [anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))]
    resp = _send(page, "こんにちは")
    evs = _sse_events(resp.text())
    assert [e["type"] for e in evs][-1] == "error" and "接続できません" in evs[-1]["message"]
    _wait_idle(page)
    assert any("接続できません" in m for m in _system_messages(page))
    st = _operable(page)
    assert st["sendDisabled"] is False and st["inputDisabled"] is False

    # (3) 正しい入力 → 結果が出る
    FAKE.messages._script[:] = [run_sim_msg(F1_NO_JAM), text_msg("できました．")]
    done = _done_event(_send(page, "ボトルネックを作って"))
    _wait_idle(page)
    _wait_result(page, done["sim_id"])
    assert page.text_content("#sim-id-label") == done["sim_id"]
    assert _last_assistant_raw(page) == "できました．"

    # 既存の結果も引き続き見られる
    _click_server_history(page, keep)
    _wait_result(page, keep)
    assert page.text_content("#sim-id-label") == keep
    assert done["sim_id"] in results_store and keep in results_store
    # /upload の 422 はブラウザが「Failed to load resource」を console に出す（想定どおりの失敗）
    assert _page_errors(page, ignore=("Failed to load resource",)) == [], page._risu_errors
