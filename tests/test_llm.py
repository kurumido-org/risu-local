"""LLM 連携（risu.llm / risu.prompts）: ツール定義，チャート抽出，トークン節約，ストリーミング経路，会話コンテキスト．"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.results import results_store  # noqa: E402
from risu.schema import SimulationInput  # noqa: E402
from risu.simulation import _run_uxsim  # noqa: E402

from helpers import (  # noqa: E402
    BOTTLENECK_SCENARIO, GRID_BIDIRECTIONAL_SCENARIO,
    _FakeAnthropic, _FakeBlock, _FakeMessage,
)

class TestConversationContext:
    """/chat のコンテキスト注入は会話に紐づく sim（last_sim_id）だけを使うこと．

    グローバル最新（results_store の末尾）を注入すると，別会話や
    アップロードで作られた無関係なシナリオを LLM が流用してしまう．
    """

    @pytest.fixture()
    def stored_sim(self):
        sim_id = "ctx_test"
        results_store[sim_id] = {
            "geojson": {"features": [{"properties": {"name": "L1"}},
                                     {"properties": {"name": "L2"}}]},
            "_scenario": {"nodes": [{"name": "A"}, {"name": "B"}],
                          "links": [{"name": "L1"}, {"name": "L2"}],
                          "demands": [], "tmax": 600},
        }
        yield sim_id
        results_store.pop(sim_id, None)

    def _body(self, last_sim_id):
        from risu.schema import ChatInput
        return ChatInput(messages=[{"role": "user", "content": "hi"}],
                         last_sim_id=last_sim_id)

    def test_no_last_sim_id_injects_nothing(self, stored_sim):
        # results_store に sim があっても，会話が指定しなければ注入しない
        from risu.tools import _conversation_context_block
        assert _conversation_context_block(self._body(None)) == ""

    def test_valid_last_sim_id_injects_that_sim(self, stored_sim):
        from risu.tools import _conversation_context_block
        block = _conversation_context_block(self._body(stored_sim))
        assert stored_sim in block
        assert f'rerun_simulation(base_sim_id="{stored_sim}")' in block
        assert "2 ノード / 2 リンク" in block

    def test_unknown_last_sim_id_injects_nothing(self, stored_sim):
        # サーバー再起動などで sim が消えた場合は注入しない
        from risu.tools import _conversation_context_block
        assert _conversation_context_block(self._body("gone123")) == ""

    def test_conversation_sim_id_helper(self, stored_sim):
        from risu.tools import _conversation_sim_id
        assert _conversation_sim_id(self._body(stored_sim)) == stored_sim
        assert _conversation_sim_id(self._body(None)) is None
        assert _conversation_sim_id(self._body("  ")) is None


# ============================================================
# 6. LLM ツール定義の整合性テスト
# ============================================================

class TestToolDefinitions:
    """LLM ツール定義がサーバー実装と整合していることを検証"""

    def test_claude_tools_defined(self):
        from risu.prompts import CLAUDE_TOOLS
        tool_names = [t["name"] for t in CLAUDE_TOOLS]
        assert "run_simulation" in tool_names
        assert "get_simulation_data" in tool_names

    def test_run_simulation_schema(self):
        from risu.prompts import CLAUDE_TOOLS
        tool = next(t for t in CLAUDE_TOOLS if t["name"] == "run_simulation")
        schema = tool["input_schema"]
        assert "nodes" in schema["properties"]
        assert "links" in schema["properties"]
        assert "demands" in schema["properties"]

    def test_get_simulation_data_schema(self):
        from risu.prompts import CLAUDE_TOOLS
        tool = next(t for t in CLAUDE_TOOLS if t["name"] == "get_simulation_data")
        schema = tool["input_schema"]
        assert "sim_id" in schema["properties"]

    def test_system_prompt_contains_risu(self):
        """
        [修正履歴] AI の一人称を RISU に変更した．
        """
        from risu.prompts import SYSTEM_PROMPT
        assert "RISU" in SYSTEM_PROMPT
        assert "一人称" in SYSTEM_PROMPT or "RISU" in SYSTEM_PROMPT

    def test_system_prompt_chart_instructions(self):
        """
        [修正履歴] チャート生成の指示がシステムプロンプトに含まれる．
        """
        from risu.prompts import SYSTEM_PROMPT
        assert "chart" in SYSTEM_PROMPT.lower() or "チャート" in SYSTEM_PROMPT or "グラフ" in SYSTEM_PROMPT


# ============================================================
# 8. チャートブロック抽出テスト
# ============================================================

class TestChartExtraction:
    """
    LLM レスポンスからの ```chart ブロック抽出をテスト．
    """

    def test_single_chart_extraction(self):
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = 'テキスト\n```chart\n{"type":"line","data":{"labels":[1,2],"datasets":[]}}\n```\n続き'
        matches = chart_pattern.findall(text)
        assert len(matches) == 1
        parsed = json.loads(matches[0])
        assert parsed["type"] == "line"

    def test_multiple_chart_extraction(self):
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = (
            '説明\n```chart\n{"type":"line","data":{"labels":[],"datasets":[]}}\n```\n'
            '別の説明\n```chart\n{"type":"bar","data":{"labels":[],"datasets":[]}}\n```\n'
        )
        matches = chart_pattern.findall(text)
        assert len(matches) == 2
        assert json.loads(matches[0])["type"] == "line"
        assert json.loads(matches[1])["type"] == "bar"

    def test_chart_block_removal(self):
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = '前文\n```chart\n{"type":"line"}\n```\n後文'
        clean = chart_pattern.sub('', text).strip()
        assert "chart" not in clean
        assert "前文" in clean
        assert "後文" in clean

    def test_invalid_json_ignored(self):
        chart_pattern = re.compile(r'```chart\s*\n(.*?)\n```', re.DOTALL)
        text = '```chart\nnot valid json\n```'
        matches = chart_pattern.findall(text)
        charts = []
        for m in matches:
            try:
                charts.append(json.loads(m))
            except json.JSONDecodeError:
                pass
        assert len(charts) == 0


# ============================================================
# 10. LLM とのやり取りのトークン節約
# ============================================================

class TestLLMTokenSaving:
    """
    [修正履歴] 動的シミュレーションでは毎ターン rerun が走り，sim_id 入りの
    【現在のコンテキスト】を system に足していたため system 以降のキャッシュが毎回無効化，
    さらに会話履歴にはキャッシュ境界が無く tool ラウンドごとに全額再課金されていた．
    - system は不変，動的コンテキストは最後の user メッセージの追加ブロック
    - 履歴の最後の assistant と リクエスト末尾に cache_control
    - 履歴は文字数予算でトリミング（ヒステリシス付き）
    - グリッド網は grid テンプレートでサーバー展開（LLM の出力トークン削減）
    - チャートは {"$data": ...} 参照で配列の書き写しを不要に
    """

    def _body(self, msgs, last_sim_id=None):
        from risu.schema import ChatInput
        return ChatInput(messages=msgs, last_sim_id=last_sim_id)

    def test_system_prompt_stays_static_and_context_goes_to_last_user(self):
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        sid = "tok_ctx"
        results_store[sid] = {
            "geojson": {"features": [{"properties": {"name": "L1"}}]},
            "_scenario": {"nodes": [{"name": "A"}], "links": [{"name": "L1"}], "demands": [], "tmax": 600},
        }
        try:
            body = self._body([
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ], last_sim_id=sid)
            msgs = risu.llm._build_llm_messages(body)
            assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
            # 履歴の最後の assistant にキャッシュ境界
            a1 = msgs[1]["content"]
            assert isinstance(a1, list) and a1[0]["text"] == "a1" and "cache_control" in a1[0]
            # 最後の user: 本文ブロック + コンテキストブロック（末尾に cache_control）
            u2 = msgs[2]["content"]
            assert u2[0]["text"] == "u2" and "cache_control" not in u2[0]
            assert sid in u2[1]["text"] and "cache_control" in u2[1]
            # 送信用には内部フラグが残らない
            api = risu.llm._api_messages(msgs)
            assert all("_tail_marked" not in b for m in api for b in m["content"])
            # コンテキストが無い場合は本文ブロックだけ（末尾にキャッシュ境界）
            msgs2 = risu.llm._build_llm_messages(self._body([{"role": "user", "content": "hi"}]))
            assert len(msgs2[0]["content"]) == 1 and "cache_control" in msgs2[0]["content"][0]
        finally:
            results_store.pop(sid, None)

    def test_tail_cache_mark_moves_with_rounds(self):
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        msgs = risu.llm._build_llm_messages(self._body([{"role": "user", "content": "hi"}]))
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "calling"}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]})
        risu.llm._mark_cache_tail(msgs)
        # 末尾の印は tool_result に移り，以前の末尾（user "hi"）からは外れる
        assert "cache_control" in msgs[-1]["content"][-1]
        assert "cache_control" not in msgs[0]["content"][0]

    def test_trim_history_hysteresis_and_first_role(self, monkeypatch):
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        monkeypatch.setattr(risu.llm, "MAX_HISTORY_CHARS", 1000)
        msgs = []
        for i in range(20):
            msgs.append({"role": "user", "content": f"u{i} " + "x" * 100})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 100})
        msgs.append({"role": "user", "content": "last"})
        kept = risu.llm._trim_history(msgs)
        assert kept[0]["role"] == "user"
        assert kept[-1]["content"] == "last"
        assert "省略" in kept[0]["content"]
        total = sum(len(m["content"]) for m in kept)
        assert total <= 1000 // 2 + 200  # 予算の半分まで落とす（先頭の注記分は許容）
        # 予算内なら手を付けない
        small = msgs[-3:]
        assert risu.llm._trim_history(small) is small

    def test_grid_template_and_auto_demands(self):
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        args = {"grid": {"nx": 4, "ny": 3, "spacing": 250, "free_flow_speed": 15},
                "auto_demands": {"strategy": "random", "n_pairs": 5, "flow_per_pair": 0.1, "seed": 1},
                "tmax": 1200}
        scenario, info = risu.scenario_ops._expand_run_simulation_args(args)
        assert len(scenario["nodes"]) == 12
        # 双方向: 横 (3×3) + 縦 (4×2) = 17 本 × 2
        assert len(scenario["links"]) == 34
        assert all(l["length"] == 250 and l["free_flow_speed"] == 15 for l in scenario["links"])
        assert len(scenario["demands"]) == 5
        assert info["grid"]["nx"] == 4 and "n{i}_{j}" in info["grid"]["node_naming"]
        assert info["auto_demands"]["generated"] == 5
        # SimulationInput として妥当で，実行できる
        r = _run_uxsim(SimulationInput(**scenario))
        assert r["stats"]["total_trips"] > 0
        # 片方向グリッド
        one, _ = risu.scenario_ops._expand_run_simulation_args(
            {"grid": {"nx": 3, "bidirectional": False}, "demands": [
                {"orig": "n0_0", "dest": "n2_2", "t_start": 0, "t_end": 100, "flow": 0.2}]})
        assert len(one["links"]) == 12
        # nodes/links も demands も無ければエラー
        with pytest.raises(ValueError):
            risu.scenario_ops._expand_run_simulation_args({"nodes": [], "links": [], "demands": []})
        with pytest.raises(ValueError):
            risu.scenario_ops._expand_run_simulation_args({"grid": {"nx": 3}})

    def test_generate_demands_shared_with_rerun(self):
        from risu.scenario_ops import _apply_modifications
        sc = {"nodes": [{"name": f"n{i}", "x": i * 100, "y": 0} for i in range(6)],
              "links": [{"name": f"l{i}", "start": f"n{i}", "end": f"n{i+1}", "length": 100} for i in range(5)],
              "demands": [], "tmax": 600}
        out, applied = _apply_modifications(sc, [
            {"action": "generate_demands", "strategy": "random", "n_pairs": 4, "seed": 7, "flow_total": 0.8}])
        assert len(out["demands"]) == 4 and all(d["flow"] == 0.2 for d in out["demands"])
        with pytest.raises(ValueError):
            _apply_modifications(sc, [{"action": "generate_demands", "strategy": "nope"}])

    def test_chart_data_refs_resolved(self):
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        sid = "tok_chart"
        risu.results._store_sim(sid, _run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            cache = {}
            text = ('結果です．\n```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},'
                    '"datasets":[{"label":"v","data":{"$data":"network_avg_speed"}},'
                    '{"label":"r1","data":{"$data":"link_speeds.r1","sim_id":"%s"}},'
                    '{"label":"missing","data":{"$data":"nope.x"}}]}}\n```\n以上．' % sid)
            charts, clean = risu.llm._extract_charts(text, cache, sid)
            assert clean == "結果です．\n\n以上．".replace("\n\n", "\n\n") or "chart" not in clean
            assert len(charts) == 1
            d = charts[0]["data"]
            sd = risu.aggregate._get_simulation_data(sid)
            assert d["labels"] == sd["time_labels"]
            assert d["datasets"][0]["data"] == sd["network_avg_speed"]
            assert d["datasets"][1]["data"] == sd["link_speeds"]["r1"]
            assert d["datasets"][2]["data"] == []  # 未解決は空配列
            assert sid in cache  # 未取得なら _get_simulation_data で補う
        finally:
            results_store.pop(sid, None)

    def test_simulation_data_is_compact(self):
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        sid = "tok_compact"
        risu.results._store_sim(sid, _run_uxsim(GRID_BIDIRECTIONAL_SCENARIO), {"type": "manual"})
        try:
            sd = risu.aggregate._get_simulation_data(sid)
            assert len(sd["time_labels"]) <= 30
            assert len(sd["link_speeds"]) <= 20
            sd2 = risu.aggregate._get_simulation_data(sid, points=10, max_links=0)
            assert len(sd2["time_labels"]) <= 10 and sd2["link_speeds"] == {}
            assert len(json.dumps(sd)) < 8000
        finally:
            results_store.pop(sid, None)

    def test_stream_chat_end_to_end_with_fake_client(self, monkeypatch):
        """偽 Anthropic クライアントで /chat ストリーミングの流れを検証:
        grid テンプレートで実行 → get_simulation_data → $data 参照チャート → usage 付き done．
        各リクエストの messages にキャッシュ境界が正しく付くことも確認．"""
        import anthropic
        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation
        from types import SimpleNamespace as NS

        captured = []

        class FakeStream:
            def __init__(self, response):
                self._r = response
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def __iter__(self):
                for b in self._r.content:
                    if b.type == "text":
                        yield NS(type="content_block_delta", delta=NS(type="text_delta", text=b.text))
                    else:
                        yield NS(type="content_block_start", index=0,
                                 content_block=NS(type="tool_use", id=b.id, name=b.name))
            def get_final_message(self):
                return self._r

        usage = NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50,
                   cache_creation_input_tokens=10)
        responses = [
            NS(stop_reason="tool_use", usage=usage, content=[
                NS(type="text", text="RISUが実行します"),
                NS(type="tool_use", id="t1", name="run_simulation",
                   input={"grid": {"nx": 3, "spacing": 300},
                          "auto_demands": {"strategy": "boundary"}, "tmax": 600}),
            ]),
            NS(stop_reason="tool_use", usage=usage, content=[
                NS(type="tool_use", id="t2", name="get_simulation_data", input={"sim_id": ""}),
            ]),
            NS(stop_reason="end_turn", usage=usage, content=[
                NS(type="text", text='完了．\n```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},'
                                     '"datasets":[{"data":{"$data":"network_avg_speed"}}]}}\n```'),
            ]),
        ]

        class FakeMessages:
            def stream(self, **kw):
                captured.append(kw)
                return FakeStream(responses[len(captured) - 1])
            def create(self, **kw):
                raise AssertionError("create は呼ばれないはず")

        class FakeClient:
            def __init__(self, *a, **kw):
                self.messages = FakeMessages()

        monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
        monkeypatch.setattr(risu.llm, "ANTHROPIC_API_KEY", "dummy")

        body = risu.schema.ChatInput(messages=[
            {"role": "user", "content": "前の話"}, {"role": "assistant", "content": "前の返事"},
            {"role": "user", "content": "3x3 グリッドで実行してグラフも"}])

        import asyncio as _aio
        async def run():
            resp = await risu.llm._chat_claude_stream(body)
            events = []
            async for chunk in resp.body_iterator:
                for line in chunk.split("\n\n"):
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            return events
        events = _aio.run(run())
        done = [e for e in events if e["type"] == "done"][0]
        try:
            assert done["sim_id"] in results_store
            assert done["usage"]["calls"] == 3 and done["usage"]["output_tokens"] == 60
            assert done["usage"]["cache_read_tokens"] == 150
            assert len(done["charts"]) == 1
            sd = risu.aggregate._get_simulation_data(done["sim_id"])
            assert done["charts"][0]["data"]["labels"] == sd["time_labels"]
            assert "```" not in done["content"]
            # ─ リクエスト構造 ─
            assert len(captured) == 3
            for kw in captured:
                assert kw["system"][0]["text"] == risu.prompts.SYSTEM_PROMPT  # system は不変
                assert "cache_control" in kw["system"][0]
                assert "cache_control" in kw["tools"][-1]
                msgs = kw["messages"]
                # 末尾メッセージの最後のブロックにキャッシュ境界，内部フラグは無い
                last_blocks = msgs[-1]["content"]
                assert "cache_control" in last_blocks[-1] and "_tail_marked" not in last_blocks[-1]
                # 履歴の assistant にもキャッシュ境界
                assert "cache_control" in msgs[1]["content"][0]
                n_marks = sum(1 for m in msgs for b in m["content"]
                              if isinstance(b, dict) and "cache_control" in b)
                assert n_marks == 2, f"messages 内の breakpoint は 2 つ（履歴 assistant + 末尾）: {n_marks}"
            # 2 回目以降は tool_result が末尾
            assert captured[1]["messages"][-1]["content"][-1]["type"] == "tool_result"
            # grid 展開結果（命名規則）が tool_result で LLM に伝わる
            tr1 = json.loads(captured[1]["messages"][-1]["content"][-1]["content"])
            assert tr1["network"]["nodes"] == 9 and "n{i}_{j}" in tr1["grid"]["node_naming"]
        finally:
            results_store.pop(done["sim_id"], None)


class TestChatStreamingPath:
    """_chat_claude_stream を SSE ごと通して検証する．

    共通化した _dispatch_tool_blocks をストリーミング経路が正しく配線できているか
    （state 経由の sim_id 引き回し，進捗イベントの転送，done イベントの中身）を見る．
    実 API は呼ばない．
    """

    @staticmethod
    def _collect_sse(body, script):
        """スタブ化したクライアントで event_generator を回し，SSE イベントを集める．"""
        import asyncio
        import sys

        import risu.aggregate
        import risu.api
        import risu.llm
        import risu.mcp_server
        import risu.prompts
        import risu.results
        import risu.runtime
        import risu.scenario_ops
        import risu.schema
        import risu.simulation

        anthropic_mod = sys.modules.get("anthropic")
        if anthropic_mod is None:
            import anthropic as anthropic_mod  # noqa: F811

        orig = anthropic_mod.Anthropic
        anthropic_mod.Anthropic = lambda **kw: _FakeAnthropic(script)
        try:
            async def go():
                resp = await risu.llm._chat_claude_stream(body)
                chunks = []
                async for c in resp.body_iterator:
                    chunks.append(c if isinstance(c, str) else c.decode("utf-8"))
                return "".join(chunks)

            raw = asyncio.run(go())
        finally:
            anthropic_mod.Anthropic = orig

        events = []
        for part in raw.split("\n\n"):
            part = part.strip()
            if part.startswith("data: "):
                events.append(json.loads(part[6:]))
        return events

    @staticmethod
    def _body(text="テスト"):
        from risu.schema import ChatInput
        return ChatInput(messages=[{"role": "user", "content": text}], last_sim_id=None)

    def test_text_only_response_streams_and_finishes(self):
        """ツールを呼ばない応答: text_delta が流れ，done で content が返る．"""
        script = [_FakeMessage([_FakeBlock("text", text="こんにちは．RISUです．")], "end_turn")]
        events = self._collect_sse(self._body("こんにちは"), script)

        kinds = [e["type"] for e in events]
        assert "stream_start" in kinds
        assert "done" in kinds
        deltas = "".join(e["text"] for e in events if e["type"] == "text_delta")
        assert deltas == "こんにちは．RISUです．"
        done = next(e for e in events if e["type"] == "done")
        assert done["content"] == "こんにちは．RISUです．"
        assert done["sim_id"] is None
        assert "usage" in done

    def test_tool_round_sets_sim_id_and_emits_progress(self):
        """run_simulation を経由すると done に sim_id が乗り，進捗が流れること．

        共通 dispatch が state.sim_id に書き，ストリーミング側がそれを読む配線の確認．
        """
        scenario = {
            "name": "sse_test", "tmax": 600, "deltan": 5,
            "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}],
            "links": [{"name": "AB", "start": "A", "end": "B", "length": 1000}],
            "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 300, "flow": 0.4}],
        }
        script = [
            # 1回目: tool_use
            _FakeMessage([_FakeBlock("tool_use", name="run_simulation",
                                     input=scenario, id="tu_a")], "tool_use"),
            # ツール結果を受けた最終回答（ストリーミング）
            _FakeMessage([_FakeBlock("text", text="シミュレーションが完了しました．")], "end_turn"),
        ]
        events = self._collect_sse(self._body("道路を作って"), script)
        done = next(e for e in events if e["type"] == "done")
        try:
            assert done["sim_id"], "done に sim_id が乗っていない"
            assert done["sim_id"] in results_store
            assert done["content"] == "シミュレーションが完了しました．"
            progress = [e["message"] for e in events if e["type"] == "progress"]
            assert any("UXsim" in m for m in progress), progress
        finally:
            results_store.pop(done.get("sim_id"), None)

    def test_unknown_tool_does_not_break_the_stream(self):
        """未知ツールでも tool_result が返り，ストリームが done まで到達する．"""
        script = [
            _FakeMessage([_FakeBlock("tool_use", name="bogus_tool",
                                     input={}, id="tu_b")], "tool_use"),
            _FakeMessage([_FakeBlock("text", text="対応していない操作でした．")], "end_turn"),
        ]
        events = self._collect_sse(self._body("なにか"), script)
        assert [e["type"] for e in events].count("done") == 1
        done = next(e for e in events if e["type"] == "done")
        assert done["sim_id"] is None
        assert done["content"] == "対応していない操作でした．"

    def test_api_error_becomes_error_event_not_crash(self):
        """スタブを尽きさせて例外を起こし，error イベントで終わることを確認．"""
        events = self._collect_sse(self._body("x"), [])
        assert events and events[-1]["type"] == "error"
        assert "message" in events[-1]
