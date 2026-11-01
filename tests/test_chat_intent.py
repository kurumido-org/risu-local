"""チャットの実行意図（risu.llm の強制再試行分岐・失敗の終端・ラウンド上限）．テスト設計 R01 / R02 / R06，T01 / T02 / T06 / T24．

実 LLM は呼ばない．偽クライアント（helpers.RecordingFakeAnthropic）が要求とツール履歴を記録し，
LLM の「成功しました」という文ではなく results_store の実データで判定する．
"""

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import risu.llm  # noqa: E402
import risu.simulation  # noqa: E402
from risu.results import results_store  # noqa: E402

from fixtures import F1, F1_TIGHT, IsolatedStore, changed_paths, frozen_copy, store_result  # noqa: E402
from helpers import chat_body, collect_sse, text_msg, tool_msg  # noqa: E402

L05 = "ABの容量だけ900台/時にして再実行してください．他の条件は維持してください．"
L07 = "交通シミュレーションのリンク容量の意味だけ説明してください．今回はシミュレーションを実行しないでください．"


def _done(events):
    kinds = [e["type"] for e in events]
    assert kinds.count("done") + kinds.count("error") == 1, kinds
    return events[-1]


class TestExplicitNoRun:
    """T01 / R01: 説明だけの依頼で実行しない．"""

    def test_explanation_only_request_is_not_forced_into_a_tool_call(self):
        with IsolatedStore() as iso:
            store_result("s0_t01", F1)
            before = frozen_copy("s0_t01")
            events, fake = collect_sse(chat_body(L07, last_sim_id="s0_t01"),
                                       [text_msg("リンク容量とは，単位時間に通過できる最大台数です．")])
            done = _done(events)
            assert done["type"] == "done" and done["sim_id"] is None
            assert "最大台数" in done["content"]
            # 強制再試行（tool_choice 付きの create）が発生していない
            assert fake.forced_tool_choices() == []
            assert [k for k, _ in fake.calls] == ["stream"]
            # 実行関数は呼ばれず，新規 ID も無く，基準も不変（件数ではなく内容で判定）
            assert fake.tool_uses() == [] and iso.new_ids() == []
            assert frozen_copy("s0_t01") == before

    @pytest.mark.parametrize("text", [
        "渋滞の仕組みだけ説明して．実行しないで",
        "この交通シミュレーションの結果の意味を説明だけしてください",
        "再計算は不要です．ネットワークの道路構成を教えて",
        "Explain the bottleneck without running the simulation",
    ])
    def test_no_run_phrases_are_recognized(self, text):
        assert risu.llm.explicit_no_run(text)

    @pytest.mark.parametrize("text", [
        "渋滞をシミュレーションして", "5x5 の格子ネットワークを作って実行して", "容量を半分にして再実行",
    ])
    def test_run_requests_are_not_mistaken_for_no_run(self, text):
        assert not risu.llm.explicit_no_run(text)

    def test_without_the_no_run_phrase_the_retry_branch_fires(self):
        """対照: 同じ「文章だけ」の初回応答でも，実行を求める文なら再試行する（分岐が生きている確認）．"""
        with IsolatedStore() as iso:
            scenario = {**F1, "name": "retry_target"}
            events, fake = collect_sse(chat_body("渋滞のシミュレーションを実行して"), [
                text_msg("シミュレーションを実行します．"),             # 1 回目: 文章だけ
                tool_msg("run_simulation", scenario, "tu_r"),         # 再試行（create）: ツール呼び出し
                text_msg("完了しました．"),
            ])
            done = _done(events)
            assert fake.forced_tool_choices() == [{"type": "any"}]
            assert done["type"] == "done" and done["sim_id"] in results_store
            assert iso.new_ids() == [done["sim_id"]]


class TestModifyRequestKeepsBase:
    """T02 / R02: 既存シナリオの修正依頼で，基準を保持したまま差分実行する（別網の新規実行にしない）．"""

    def _mods(self):
        return [{"action": "update_links", "names": ["AB"], "set": {"capacity": 0.25}}]

    def test_normal_tool_response_reruns_from_the_shown_result(self):
        with IsolatedStore() as iso:
            store_result("s0_t02a", F1)
            before = frozen_copy("s0_t02a")
            events, fake = collect_sse(chat_body(L05, last_sim_id="s0_t02a"), [
                tool_msg("rerun_simulation", {"base_sim_id": "s0_t02a", "modifications": self._mods()}, "tu_m"),
                text_msg("ABの容量を 0.25 台/秒にして再実行しました．"),
            ])
            done = _done(events)
            new_id = done["sim_id"]
            assert new_id and new_id != "s0_t02a" and iso.new_ids() == [new_id]
            new = results_store[new_id]
            assert new["_meta"]["source"]["base_sim_id"] == "s0_t02a"
            # 差分は AB.capacity の 1 項目だけ．基準シナリオの深いコピーも変化しない
            assert set(changed_paths(before["_scenario"], new["_scenario"])) == {"links.AB.capacity"}
            assert new["_scenario"]["links"][0]["capacity"] == 0.25
            assert frozen_copy("s0_t02a") == before
            # tool_result は正しい基準 ID と適用内容を LLM に返している
            (name, args, content, is_err), = fake.tool_uses()
            assert name == "rerun_simulation" and not is_err
            assert json.loads(content)["base_sim_id"] == "s0_t02a"

    def test_text_only_first_response_is_retried_with_rerun_hint_not_forced_run_simulation(self):
        """初回が「変更します」だけ → 再試行．強制するのは run_simulation ではなく，表示中 ID の
        rerun_simulation を案内した tool_choice=any．"""
        with IsolatedStore() as iso:
            store_result("s0_t02b", F1)
            before = frozen_copy("s0_t02b")
            events, fake = collect_sse(chat_body(L05, last_sim_id="s0_t02b"), [
                text_msg("ABの容量を変更します．"),
                tool_msg("rerun_simulation", {"base_sim_id": "s0_t02b", "modifications": self._mods()}, "tu_m2"),
                text_msg("再実行しました．"),
            ])
            done = _done(events)
            assert fake.forced_tool_choices() == [{"type": "any"}], "run_simulation を強制してはいけない"
            retry_kw = fake.calls[1][1]
            hint = retry_kw["messages"][-1]["content"][-1]["text"]
            assert "rerun_simulation" in hint and "s0_t02b" in hint
            new = results_store[done["sim_id"]]
            assert new["_meta"]["source"]["base_sim_id"] == "s0_t02b"
            assert set(changed_paths(before["_scenario"], new["_scenario"])) == {"links.AB.capacity"}
            assert frozen_copy("s0_t02b") == before and len(iso.new_ids()) == 1

    def test_retry_that_still_returns_text_ends_as_incomplete_text_not_fake_success(self):
        with IsolatedStore() as iso:
            store_result("s0_t02c", F1)
            events, fake = collect_sse(chat_body(L05, last_sim_id="s0_t02c"), [
                text_msg("変更します．"),
                text_msg("申し訳ありません，実行できませんでした（未完了）．"),   # 再試行も文章だけ
            ])
            done = _done(events)
            assert done["type"] == "done" and done["sim_id"] is None
            assert iso.new_ids() == [] and fake.tool_uses() == []
            assert "変更します" in done["content"]

    def test_wrong_base_id_or_bad_value_keeps_the_base(self):
        """誤った ID・未知フィールド・不正値のどれでも元データは保持し，新規 ID は生じない．"""
        with IsolatedStore() as iso:
            store_result("s0_t02d", F1)
            before = frozen_copy("s0_t02d")
            cases = [
                {"base_sim_id": "no_such_sim", "modifications": self._mods()},
                {"base_sim_id": "s0_t02d", "modifications": [{"action": "update_links", "names": ["AB"], "set": {"colour": 1}}]},
                {"base_sim_id": "s0_t02d", "modifications": [{"action": "update_links", "names": ["AB"], "set": {"capacity": -1}}]},
                {"base_sim_id": "s0_t02d", "modifications": [{"action": "update_links", "names": ["AB"], "set": {"length": "NaN"}}]},
            ]
            for args in cases:
                events, fake = collect_sse(chat_body(L05, last_sim_id="s0_t02d"), [
                    tool_msg("rerun_simulation", args, "tu_x"), text_msg("失敗しました．")])
                done = _done(events)
                assert done["type"] == "done" and done["sim_id"] is None, args
                (_n, _a, content, is_err), = fake.tool_uses()
                assert is_err and content, args
            assert iso.new_ids() == [] and frozen_copy("s0_t02d") == before


class TestFailuresAndPartialCompletion:
    """T06 / R06: 失敗と部分完了を正しく返す．成功を捏造せず，終端を通知し，次の正しい要求で回復する．"""

    @staticmethod
    def _resp(status):
        return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.test/v1/messages"))

    def _errors(self):
        import anthropic
        return [
            (anthropic.AuthenticationError(message="bad key", response=self._resp(401), body=None), "接続できません"),
            (anthropic.RateLimitError(message="rl", response=self._resp(429), body=None), "混雑"),
            (anthropic.APIStatusError("overloaded", response=self._resp(529), body=None), "過負荷"),
            (anthropic.APIStatusError("server", response=self._resp(503), body=None), "エラー"),
            (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.test")), "接続できません"),
        ]

    def test_llm_failures_end_with_an_error_event_and_leave_results_intact(self):
        with IsolatedStore() as iso:
            store_result("s0_t06", F1)
            before = frozen_copy("s0_t06")
            for exc, needle in self._errors():
                events, fake = collect_sse(chat_body("渋滞を見せて", last_sim_id="s0_t06"), [exc])
                assert events[-1]["type"] == "error", (type(exc).__name__, events[-1])
                assert needle in events[-1]["message"]
                assert [e["type"] for e in events].count("done") == 0
            # 途中（ツール実行後）の失敗でも成功を捏造しない
            import anthropic
            events, fake = collect_sse(chat_body("渋滞を見せて", last_sim_id="s0_t06"), [
                tool_msg("get_network_info", {"sim_id": "s0_t06"}, "tu_i"),
                anthropic.RateLimitError(message="rl", response=self._resp(429), body=None),
            ])
            assert events[-1]["type"] == "error"
            assert iso.new_ids() == [] and frozen_copy("s0_t06") == before
            # 次の正しい要求で回復する（同じ会話・同じストア）
            events, fake = collect_sse(chat_body("道路構成を教えて", last_sim_id="s0_t06"), [
                tool_msg("get_network_info", {"sim_id": "s0_t06"}, "tu_j"), text_msg("3 ノード 2 リンクです．")])
            assert events[-1]["type"] == "done" and events[-1]["content"].startswith("3 ノード")

    def test_uxsim_timeout_is_reported_as_tool_error_not_success(self, monkeypatch):
        with IsolatedStore() as iso:
            monkeypatch.setattr(risu.simulation, "UXSIM_TIMEOUT_SEC", 1e-6)
            events, fake = collect_sse(chat_body("渋滞をシミュレーションして"), [
                tool_msg("run_simulation", {**F1, "name": "timeout_case"}, "tu_t"),
                text_msg("時間内に計算が終わりませんでした．結果はありません．"),
            ])
            done = _done(events)
            assert done["type"] == "done" and done["sim_id"] is None
            (name, _a, content, is_err), = fake.tool_uses()
            assert name == "run_simulation" and is_err and "タイムアウト" in content
            assert iso.new_ids() == []

    def test_broken_tool_arguments_do_not_create_results(self):
        with IsolatedStore() as iso:
            bad = {**F1, "links": [{"name": "AB", "start": "A", "end": "ZZ", "length": 3000}]}
            events, fake = collect_sse(chat_body("道路を作って"), [
                tool_msg("run_simulation", bad, "tu_b"), text_msg("入力に誤りがありました．")])
            done = _done(events)
            assert done["sim_id"] is None and iso.new_ids() == []
            (_n, _a, content, is_err), = fake.tool_uses()
            assert is_err and "ZZ" in content

    def test_round_limit_is_reported_as_incomplete(self, monkeypatch):
        """T24: フェイクが上限までツール要求を続ける．上限を超えず，未実行の要求を完了と説明しない．"""
        with IsolatedStore() as iso:
            store_result("s0_t24", F1)
            monkeypatch.setattr(risu.llm, "MAX_TOOL_ROUNDS", 1)
            info = {"sim_id": "s0_t24", "include": "summary"}
            events, fake = collect_sse(chat_body("道路構成を調べて", last_sim_id="s0_t24"), [
                tool_msg("get_network_info", info, "tu_1"),
                tool_msg("get_network_info", info, "tu_2", text="続けて調べます．"),
                tool_msg("rerun_simulation", {"base_sim_id": "s0_t24", "modifications": []}, "tu_3",
                         text="では再実行します．"),   # 上限到達後に残る要求 → 実行されない
            ])
            done = _done(events)
            assert done["type"] == "done"
            assert done.get("incomplete") is True
            assert "未完了" in done["content"] and "rerun_simulation" in done["content"]
            # API 呼び出しは 1 + 1 + MAX_TOOL_ROUNDS 回で止まる．残った rerun は実行されず新規 ID も無い
            assert len(fake.calls) == 3
            assert [n for n, *_ in fake.tool_uses()] == ["get_network_info", "get_network_info"]
            assert iso.new_ids() == [] and done["sim_id"] is None

    def test_pending_note_names_the_tools(self):
        import types
        note = risu.llm.pending_tools_note([types.SimpleNamespace(name="run_simulation"),
                                            types.SimpleNamespace(name="compare_simulations")])
        assert "run_simulation, compare_simulations" in note and "未完了" in note


class TestWrongBaseIdIsNotSubstituted:
    """T13: 未知・失効 ID を基準に指定したとき，最新の有効結果で代用しない．"""

    def test_unknown_base_reports_the_id_and_creates_nothing(self):
        import asyncio
        import types

        from risu.tools import handle_rerun_simulation
        with IsolatedStore() as iso:
            store_result("valid_t13", F1_TIGHT)
            content, new_id, is_err = asyncio.run(handle_rerun_simulation(
                {"base_sim_id": "expired_t13", "modifications": []}, types.SimpleNamespace(messages=[])))
            assert is_err and new_id is None and "expired_t13" in content
            assert iso.new_ids() == []
