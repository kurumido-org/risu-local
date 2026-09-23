"""ツール実行（risu.tools / risu.mcp_server）: dispatcher，ネットワーク照会，MCP の同一性．"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.results import results_store  # noqa: E402
from risu.schema import SimulationInput  # noqa: E402
from risu.simulation import run_uxsim  # noqa: E402

from helpers import (  # noqa: E402
    BOTTLENECK_SCENARIO,
)

class TestNetworkInfo:
    """get_network_info: LLM がネットワークを必要な分だけ照会するツール"""

    @pytest.fixture(scope="class")
    def sim_id(self):
        scenario = SimulationInput(
            name="info_test", tmax=600, deltan=5,
            nodes=[{"name": f"n{i}", "x": i * 100, "y": 0} for i in range(80)],
            links=[{"name": f"L{i}", "start": f"n{i}", "end": f"n{i+1}", "length": 100}
                   for i in range(79)],
            demands=[{"orig": "n0", "dest": "n79", "t_start": 0, "t_end": 300, "flow": 0.3}],
        )
        result = run_uxsim(scenario)
        results_store["info_test"] = result
        return "info_test"

    def test_summary(self, sim_id):
        from risu.tools import handle_get_network_info
        content, is_err = handle_get_network_info({"sim_id": sim_id})
        assert not is_err
        d = json.loads(content)
        assert d["total"] == {"nodes": 80, "links": 79, "demands": 1}
        assert d["bbox"]["x_max"] == 7900
        assert len(d["sample_node_names"]) == 20

    def test_nodes_paging_and_filter(self, sim_id):
        from risu.tools import handle_get_network_info
        content, _ = handle_get_network_info(
            {"sim_id": sim_id, "include": "nodes", "limit": 10, "offset": 5})
        d = json.loads(content)
        assert len(d["nodes"]) == 10
        assert d["nodes"][0]["name"] == "n5"
        assert "note" in d  # 途中までの表示であることが明示される
        content, _ = handle_get_network_info(
            {"sim_id": sim_id, "include": "nodes", "name_contains": "n7"})
        d = json.loads(content)
        # n7, n70..n79 の 11 件
        assert d["matched"] == 11

    def test_limit_cap(self, sim_id):
        from risu.tools import handle_get_network_info
        content, _ = handle_get_network_info(
            {"sim_id": sim_id, "include": "nodes", "limit": 9999})
        d = json.loads(content)
        assert len(d["nodes"]) <= 200

    def test_bad_sim_id(self):
        from risu.tools import handle_get_network_info
        content, is_err = handle_get_network_info({"sim_id": "nope"})
        assert is_err


# ============================================================
# ツール実行の共通ディスパッチ（dispatch_tool_blocks）
# ============================================================

class TestToolDispatch:
    """ストリーミング経路と同期経路が通る共通 dispatch のテスト．

    以前は初回ラウンド・追加ラウンド × 2 経路の計 4 箇所に同じ dispatch が
    コピーされており，片方だけ直すと挙動がずれる状態だった．共通化したので
    「両経路が同じ結果を返すこと」をここで固定する．
    """

    @staticmethod
    def _tb(name, args, tid="tu_1"):
        """anthropic の tool_use ブロック相当のダミー．"""
        import types
        return types.SimpleNamespace(name=name, input=args, id=tid, type="tool_use")

    @staticmethod
    def _body(messages=None, last_sim_id=None):
        from risu.schema import ChatInput
        return ChatInput(
            messages=messages or [{"role": "user", "content": "テスト"}],
            last_sim_id=last_sim_id,
        )

    def _run(self, blocks, *, follow_up=False, body=None):
        """dispatch を回して (progress メッセージ列, tool_results, state) を返す．"""
        import asyncio
        from risu.tools import ToolTurnState, dispatch_tool_blocks

        state = ToolTurnState(body or self._body())
        progress, results = [], []

        async def go():
            async for kind, payload in dispatch_tool_blocks(blocks, state, follow_up=follow_up):
                if kind == "progress":
                    progress.append(payload)
                else:
                    results.append(payload)

        asyncio.run(go())
        assert len(results) == 1, "results は最後に 1 回だけ yield されるべき"
        return progress, results[0], state

    # ── API 不変条件 ──────────────────────────────

    def test_every_tool_use_gets_exactly_one_result(self):
        """tool_use には必ず 1 対 1 で tool_result を返す（Anthropic API の要求）．

        欠けると API が 400 を返すので，未知のツール名でも結果を積む必要がある．
        共通化前は同期経路の初回ラウンドにこの else 分岐が無く，
        未知ツールを呼ばれるとリクエストが壊れる状態だった．
        """
        blocks = [
            self._tb("get_network_info", {"sim_id": "nope"}, "a"),
            self._tb("get_simulation_data", {"sim_id": "nope"}, "b"),
            self._tb("totally_unknown_tool", {}, "c"),
        ]
        _, results, _ = self._run(blocks)
        assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
        assert all(r["type"] == "tool_result" for r in results)
        assert all(isinstance(r["content"], str) for r in results)

    def test_unknown_tool_is_reported_not_dropped(self):
        _, results, _ = self._run([self._tb("no_such_tool", {}, "x")])
        assert len(results) == 1
        assert "未知のツール" in results[0]["content"]
        assert "no_such_tool" in results[0]["content"]

    # ── 実際のツール ──────────────────────────────

    def test_run_simulation_populates_sim_id(self):
        blocks = [self._tb("run_simulation", {
            "name": "dispatch_test", "tmax": 600, "deltan": 5,
            "nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}],
            "links": [{"name": "AB", "start": "A", "end": "B", "length": 1000}],
            "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 300, "flow": 0.4}],
        }, "r1")]
        _, results, state = self._run(blocks)
        try:
            assert state.sim_id, "run_simulation 後に sim_id が入っていない"
            assert state.sim_id in results_store
            assert not results[0].get("is_error"), results[0]["content"]
        finally:
            results_store.pop(state.sim_id, None)

    def test_get_simulation_data_fills_cache_for_chart_refs(self):
        """get_simulation_data の結果が $data 解決用キャッシュに入ること．"""
        from risu.results import store_sim
        sid = "dispatch_data_test"
        store_sim(sid, run_uxsim(BOTTLENECK_SCENARIO), {"type": "manual"})
        try:
            _, results, state = self._run(
                [self._tb("get_simulation_data", {"sim_id": sid}, "d1")])
            assert state.last_data_sim_id == sid
            assert sid in state.sim_data_cache
            assert "network_avg_speed" in results[0]["content"]
        finally:
            results_store.pop(sid, None)

    def test_get_simulation_data_missing_is_not_an_exception(self):
        _, results, state = self._run(
            [self._tb("get_simulation_data", {"sim_id": "does_not_exist"}, "d2")])
        assert "データが見つかりません" in results[0]["content"]
        assert state.last_data_sim_id is None
        assert state.sim_data_cache == {}

    # ── 進捗イベント ──────────────────────────────

    def test_progress_messages_differ_between_rounds(self):
        """初回ラウンドは Step 表記，追加ラウンドは別文言（SSE の見た目を保つ）．"""
        blocks = [self._tb("get_simulation_data", {"sim_id": "x"}, "p1")]
        first, _, _ = self._run(blocks, follow_up=False)
        later, _, _ = self._run(blocks, follow_up=True)
        assert first == ["データを集計中..."]
        assert later == ["チャートデータを取得中..."]

    def test_sync_path_helper_drops_progress(self):
        """collect_tool_results は進捗を捨てて results だけ返す．"""
        import asyncio
        from risu.tools import ToolTurnState, collect_tool_results

        state = ToolTurnState(self._body())
        blocks = [self._tb("get_simulation_data", {"sim_id": "x"}, "s1")]
        results = asyncio.run(collect_tool_results(blocks, state))
        assert isinstance(results, list) and len(results) == 1
        assert results[0]["tool_use_id"] == "s1"

    def test_both_paths_produce_identical_results(self):
        """同じ入力なら，進捗を拾う経路（SSE）と捨てる経路（同期）で
        tool_result が完全に一致すること．共通化の目的そのもの．"""
        import asyncio
        from risu.tools import ToolTurnState, collect_tool_results

        blocks = [
            self._tb("get_network_info", {"sim_id": "nope"}, "a"),
            self._tb("get_simulation_data", {"sim_id": "nope"}, "b"),
            self._tb("unknown", {}, "c"),
        ]
        _, streamed, _ = self._run(blocks)
        sync = asyncio.run(collect_tool_results(blocks, ToolTurnState(self._body())))
        assert streamed == sync


# ============================================================
# MCP はチャットと同じツール定義・同じ dispatcher を通る
# ============================================================

class TestMcpParity:
    """[修正履歴] MCP は run_simulation / get_result の 2 つだけを別定義していて，
    差分再実行・ネットワーク照会・集計データ・OSM 取込が MCP から使えなかった．
    いまは CLAUDE_TOOLS をそのまま公開し，dispatch_tool_blocks を共有する（§3.2）．
    """

    def test_mcp_exposes_every_chat_tool_with_same_schema(self):
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
        tools = {t.name: t for t in risu.mcp_server.mcp_tools()}
        for t in risu.prompts.CLAUDE_TOOLS:
            assert t["name"] in tools, f"MCP に {t['name']} が無い"
            assert tools[t["name"]].inputSchema == t["input_schema"]
            assert tools[t["name"]].description == t["description"]
        assert "get_result" in tools   # 互換ツール

    def test_mcp_roundtrip_through_shared_dispatcher(self):
        import asyncio
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

        async def go():
            created = []
            try:
                r = await risu.mcp_server.mcp_call_tool("run_simulation", {
                    "grid": {"nx": 3, "spacing": 500},
                    "auto_demands": {"strategy": "boundary", "flow_per_pair": 0.1},
                    "tmax": 600, "random_seed": 1,
                })
                d = json.loads(r); sid = d["sim_id"]; created.append(sid)
                assert risu.results.results_store[sid]["_meta"]["source"]["via"] == "mcp"

                r = await risu.mcp_server.mcp_call_tool("get_simulation_data", {"sim_id": sid, "points": 10})
                assert "network_avg_speed" in json.loads(r)

                r = await risu.mcp_server.mcp_call_tool("get_network_info", {"sim_id": sid, "include": "summary"})
                assert "error" not in r.lower()[:40]

                r = await risu.mcp_server.mcp_call_tool("rerun_simulation", {
                    "base_sim_id": sid,
                    "modifications": [{"action": "set_params", "random_seed": 2}],
                })
                d2 = json.loads(r); created.append(d2["sim_id"])
                assert d2["sim_id"] != sid
                assert risu.results.results_store[d2["sim_id"]]["_scenario"]["random_seed"] == 2

                r = await risu.mcp_server.mcp_call_tool("get_result", {"simulation_id": sid})
                assert "total_trips" in r
                r = await risu.mcp_server.mcp_call_tool("no_such_tool", {})
                assert "未知のツール" in r
                r = await risu.mcp_server.mcp_call_tool("get_result", {"simulation_id": "missing"})
                assert "見つかりません" in r
            finally:
                for s in created:
                    risu.results.results_store.pop(s, None)

        asyncio.run(go())
