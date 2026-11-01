"""参照先・差分・数値根拠の整合（テスト設計 R03 / R05，T03 / T05 / T08〜T11 / T14 / T19 / T21 / T22）．

「どの結果を基準にしたか」「何だけが変わったか」「図と本文がどの結果の値か」を，
LLM の文章ではなく results_store の正規化シナリオと実データで判定する．
"""

import asyncio
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import risu.llm  # noqa: E402
from risu.aggregate import compare_simulations, get_simulation_data  # noqa: E402
from risu.results import results_store  # noqa: E402
from risu.scenario_ops import apply_modifications  # noqa: E402
from risu.tools import ToolTurnState, dispatch_tool_blocks, handle_rerun_simulation  # noqa: E402

from fixtures import (  # noqa: E402
    F1, F1_SHIFT, F4_SMALL, F6_COMPLETE_TIMES, F6_PARTIAL_TIMES, IsolatedStore,
    changed_paths, demand_multiset, demand_vehicles, f1, f6_complete, f6_partial, frozen_copy,
    store_result, store_synthetic, synthetic_result,
)
from helpers import chat_body  # noqa: E402


def _rerun(base, mods, **extra):
    body = types.SimpleNamespace(messages=[])
    return asyncio.run(handle_rerun_simulation({"base_sim_id": base, "modifications": mods, **extra}, body))


class TestBaseReference:
    """T03 / R03: S0 → S1 の後に「元の S0 から」と明示された変更は S0 を基準にする．"""

    def test_explicit_base_is_used_not_the_latest(self):
        with IsolatedStore():
            store_result("s0_t03", F1)
            content, s1, err = _rerun("s0_t03", [{"action": "update_links", "names": ["AB"], "set": {"capacity": 0.25}}])
            assert not err and s1
            # 最新は S1（容量 0.25）だが，明示された基準 S0 から需要だけ 1.2 倍にする
            content, c, err = _rerun("s0_t03", [{"action": "update_demands", "all": True, "scale_flow": 1.2}])
            assert not err and c
            sc_c = results_store[c]["_scenario"]
            assert sc_c["links"][0]["capacity"] == 0.5, "S1 の容量変更を混ぜてはいけない"
            assert sc_c["demands"][0]["flow"] == pytest.approx(0.72)
            assert results_store[c]["_meta"]["source"]["base_sim_id"] == "s0_t03"
            assert set(changed_paths(results_store["s0_t03"]["_scenario"], sc_c)) == {"demands"}
            assert sc_c["random_seed"] == F1["random_seed"]
            # 比較の向き（b − a）と差分の中身
            cmp = compare_simulations("s0_t03", c)
            assert cmp["a"] == "s0_t03" and cmp["b"] == c
            assert cmp["scenario_diff"]["demands"]["total_vehicles"] == [360.0, 432.0]
            assert cmp["scenario_diff"]["links"]["n_changed"] == 0
            assert cmp["same_random_seed"] is True


class TestChartReferences:
    """T05 / T22 / R05: 図の各配列は，その sim_id の時刻と値の組に対応する．"""

    def _same_axis_pair(self):
        times = [0, 300, 600, 900, 1200]
        a = synthetic_result(scenario=F1, times=times, speeds=[20, 15, 10, 12, 18], counts=[0, 50, 100, 60, 0],
                             stats={"total_trips": 360, "completed_trips": 360, "average_travel_time_s": 300.0})
        b = synthetic_result(scenario=f1(capacity=0.25), times=times, speeds=[20, 8, 4, 5, 9],
                             counts=[0, 80, 160, 150, 90],
                             stats={"total_trips": 360, "completed_trips": 200, "average_travel_time_s": 500.0})
        return a, b

    def test_each_dataset_uses_its_own_sim(self):
        with IsolatedStore():
            a, b = self._same_axis_pair()
            store_synthetic("chart_a", a); store_synthetic("chart_b", b)
            text = ('比較です．\n```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},'
                    '"datasets":[{"label":"A","data":{"$data":"network_avg_speed"}},'
                    '{"label":"B","data":{"$data":"network_avg_speed","sim_id":"chart_b"}}]}}\n```')
            charts, clean = risu.llm.extract_charts(text, {}, "chart_a")
            assert len(charts) == 1 and "注記" not in clean
            d = charts[0]["data"]
            sa, sb = get_simulation_data("chart_a"), get_simulation_data("chart_b")
            assert d["labels"] == sa["time_labels"] == sb["time_labels"]
            assert d["datasets"][0]["data"] == sa["network_avg_speed"] == [20, 15, 10, 12, 18]
            assert d["datasets"][1]["data"] == sb["network_avg_speed"] == [20, 8, 4, 5, 9]
            assert d["datasets"][0]["data"] != d["datasets"][1]["data"]
            # 時間軸が同じなら整列は起きない
            assert "risu" not in ((charts[0].get("options") or {}).get("plugins") or {})

    def test_different_time_axes_are_aligned_explicitly(self):
        """T22: S0 の時刻と S1 の時刻が違うとき，S0 のラベルに S1 の配列を無条件で重ねない．"""
        with IsolatedStore():
            store_synthetic("chart_c", f6_complete()); store_synthetic("chart_p", f6_partial())
            text = ('```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},'
                    '"datasets":[{"label":"完了","data":{"$data":"network_avg_speed"}},'
                    '{"label":"一部","data":{"$data":"network_avg_speed","sim_id":"chart_p"}},'
                    '{"label":"AB 一部","data":{"$data":"link_speeds.AB","sim_id":"chart_p"}}]},'
                    '"options":{"plugins":{"legend":{"display":true}}}}\n```')
            charts, _clean = risu.llm.extract_charts(text, {}, "chart_c")
            assert len(charts) == 1
            c = charts[0]
            union = sorted(set(F6_COMPLETE_TIMES) | set(F6_PARTIAL_TIMES))
            assert c["data"]["labels"] == union
            assert c["options"]["spanGaps"] is True
            assert c["options"]["plugins"]["risu"] == {"alignedTimeAxis": True, "sims": ["chart_c", "chart_p"]}
            assert c["options"]["plugins"]["legend"] == {"display": True}   # 既存の options は保持
            sc, sp = get_simulation_data("chart_c"), get_simulation_data("chart_p")
            ds = c["data"]["datasets"]
            # 各点の (時刻, 値) の組が，その sim 自身のものと一致．他方の時刻には null
            for i, (sd, path) in enumerate([(sc, "network_avg_speed"), (sp, "network_avg_speed"), (sp, "link_speeds")]):
                own = dict(zip(sd["time_labels"], sd[path] if path != "link_speeds" else sd["link_speeds"]["AB"]))
                for t, v in zip(union, ds[i]["data"]):
                    assert v == own.get(t), (i, t, v, own.get(t))
            assert ds[1]["data"][union.index(200)] is None and ds[0]["data"][union.index(1800)] is None

    def test_wrong_mapping_is_detectable(self):
        """§16.4: 検証器が誤りを見つけること．S0 の時刻に S1 の速度を無理に対応付けた chart は不一致になる．"""
        with IsolatedStore():
            store_synthetic("chart_c2", f6_complete()); store_synthetic("chart_p2", f6_partial())
            sc, sp = get_simulation_data("chart_c2"), get_simulation_data("chart_p2")
            wrong = {"labels": sc["time_labels"], "data": sp["network_avg_speed"]}   # 長さ 7 と 5
            assert len(wrong["labels"]) != len(wrong["data"])
            own = dict(zip(sp["time_labels"], sp["network_avg_speed"]))
            mismatched = [t for t, v in zip(wrong["labels"], wrong["data"]) if own.get(t) != v]
            assert mismatched, "誤対応が検出されない"

    def test_invalid_references_are_not_drawn_as_zero_traffic(self):
        """T21 / R05: 壊れた JSON・取得できない sim_id・存在しないパス・空系列・大小文字の fence．"""
        with IsolatedStore():
            store_synthetic("chart_ok", f6_complete())
            cases = {
                "broken_json": "```chart\n{not json}\n```",
                "bad_sim": '```Chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},"datasets":[{"label":"x","data":{"$data":"network_avg_speed","sim_id":"gone"}}]}}\n```',
                "bad_path": '```chartjs\n{"type":"bar","data":{"labels":{"$data":"time_labels"},"datasets":[{"label":"y","data":{"$data":"nothing.here"}}]}}\n```',
                "bad_labels": '```chart\n{"type":"line","data":{"labels":{"$data":"no_such_axis"},"datasets":[{"label":"z","data":{"$data":"network_avg_speed"}}]}}\n```',
                "not_object": "```chart\n[1,2,3]\n```",
            }
            for name, block in cases.items():
                charts, clean = risu.llm.extract_charts("本文．\n" + block, {}, "chart_ok")
                assert charts == [], name
                assert clean.startswith("本文．") and "注記" in clean and "```" not in clean, (name, clean)
            # 1 つのデータセットだけ壊れていれば，それを外して残りを描く
            text = ('```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},"datasets":['
                    '{"label":"ok","data":{"$data":"network_avg_speed"}},'
                    '{"label":"bad","data":{"$data":"link_speeds.ZZ"}}]}}\n```')
            charts, clean = risu.llm.extract_charts(text, {}, "chart_ok")
            assert len(charts) == 1 and [d["label"] for d in charts[0]["data"]["datasets"]] == ["ok"]
            assert "link_speeds.ZZ" in clean and "bad" in clean
            # 非整数の時刻キーでも整列が壊れない（labels が float の sim）
            odd = synthetic_result(scenario=F1, times=[0, 150.5, 301], speeds=[20, 10, 5], counts=[0, 10, 20],
                                   stats={"total_trips": 30, "completed_trips": 30, "average_travel_time_s": 200.0})
            store_synthetic("chart_odd", odd)
            text = ('```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},"datasets":['
                    '{"label":"a","data":{"$data":"network_avg_speed"}},'
                    '{"label":"o","data":{"$data":"network_avg_speed","sim_id":"chart_odd"}}]}}\n```')
            charts, _ = risu.llm.extract_charts(text, {}, "chart_ok")
            labels = charts[0]["data"]["labels"]
            assert 150 in labels or 150.5 in labels or 151 in labels   # 丸めキーが残る
            assert len(labels) == len(set(labels))

    def test_text_numbers_match_the_referenced_results(self):
        """本文の平均旅行時間・完了率・差分は同じデータと一致する（F6: 400 秒 / 250 秒，100% / 50%）．"""
        with IsolatedStore():
            store_synthetic("num_c", f6_complete()); store_synthetic("num_p", f6_partial())
            cmp = compare_simulations("num_c", "num_p")
            st = cmp["stats"]
            assert st["average_travel_time_s"] == {"a": 400.0, "b": 250.0, "diff": -150.0}
            assert st["completed_trips"] == {"a": 360, "b": 180, "diff": -180}
            rate = {k: st["completed_trips"][k] / st["total_trips"][k] for k in ("a", "b")}
            assert rate == {"a": 1.0, "b": 0.5}
            # 完了 0 の平均は None → 差も None（0 秒にしない）
            z = synthetic_result(scenario=F1, times=[0, 600], speeds=[0, 0], counts=[0, 360],
                                 stats={"total_trips": 360, "completed_trips": 0, "average_travel_time_s": None})
            store_synthetic("num_z", z)
            cz = compare_simulations("num_c", "num_z")
            assert cz["stats"]["average_travel_time_s"] == {"a": 400.0, "b": None, "diff": None}
            assert get_simulation_data("num_z")["stats"]["average_travel_time_s"] is None


class TestModificationSemantics:
    """T08〜T11: 差分命令の全体失敗・順序・需要の時間シフト・負時刻．"""

    def test_second_invalid_modification_fails_the_whole_rerun(self):
        """T08: 1 件目が有効でも 2 件目が未知リンク / 不正値なら新しい結果を保存せず，基準は不変．"""
        with IsolatedStore() as iso:
            store_result("s0_t08", F1)
            before = frozen_copy("s0_t08")
            for second in ({"action": "update_links", "names": ["ZZ"], "set": {"capacity": 0.3}},
                           {"action": "update_links", "names": ["BC"], "set": {"capacity": "fast"}},
                           {"action": "set_params", "deltan": 0},
                           {"action": "no_such_action"}):
                content, new_id, err = _rerun("s0_t08", [
                    {"action": "update_links", "names": ["AB"], "set": {"capacity": 0.25}}, second])
                assert err and new_id is None, (second, content)
            assert iso.new_ids() == [] and frozen_copy("s0_t08") == before

    def test_demand_set_then_scale_in_order(self):
        """T09: flow を 0.2 に設定 → 1.5 倍 の 2 命令で最終 0.3（順序の逆転・片方だけの適用を検出）．"""
        sc, applied = apply_modifications(F1, [
            {"action": "update_demands", "orig": "A", "dest": "C", "set": {"flow": 0.2}},
            {"action": "update_demands", "orig": "A", "dest": "C", "scale_flow": 1.5},
        ])
        assert sc["demands"][0]["flow"] == pytest.approx(0.3) and len(applied) == 2
        sc2, _ = apply_modifications(F1, [
            {"action": "update_demands", "orig": "A", "dest": "C", "scale_flow": 1.5},
            {"action": "update_demands", "orig": "A", "dest": "C", "set": {"flow": 0.2}},
        ])
        assert sc2["demands"][0]["flow"] == pytest.approx(0.2)   # 逆順なら set が勝つ

    def test_shift_demands_keeps_total_vehicles(self):
        """T10 / L12: 600〜1200 秒の需要の 30% を 600 秒前倒し．元区間 0.42，前倒し区間 0.18，総需要 360 台．"""
        with IsolatedStore():
            store_result("s0_t10", F1_SHIFT)
            before = frozen_copy("s0_t10")
            content, new_id, err = _rerun("s0_t10", [
                {"action": "shift_demands", "t_from": 600, "t_to": 1200, "fraction": 0.3, "shift_s": -600}])
            assert not err, content
            sc = results_store[new_id]["_scenario"]
            rows = demand_multiset(sc)
            assert rows == {("A", "C", 600.0, 1200.0, 0.42): 1, ("A", "C", 0.0, 600.0, 0.18): 1}
            assert demand_vehicles(sc) == pytest.approx(360.0, abs=1e-9)
            assert demand_vehicles(before["_scenario"]) == pytest.approx(360.0, abs=1e-9)
            assert set(changed_paths(before["_scenario"], sc)) == {"demands"}
            assert frozen_copy("s0_t10") == before
            # 検証器の確認（§16.4）: flow の和が同じでも区間長が変われば総台数は違う
            wrong = {**sc, "demands": [{"orig": "A", "dest": "C", "t_start": 600, "t_end": 1200, "flow": 0.42},
                                       {"orig": "A", "dest": "C", "t_start": 0, "t_end": 300, "flow": 0.18}]}
            assert sum(d["flow"] for d in wrong["demands"]) == pytest.approx(0.6)
            assert demand_vehicles(wrong) != pytest.approx(360.0)

    def test_shift_into_negative_time_is_rejected(self):
        """T11: シフト後が負時刻になる需要はエラー．基準の flow・時刻を含めて不変，新規 ID なし．"""
        with IsolatedStore() as iso:
            store_result("s0_t11", F1)   # 需要 0〜600
            before = frozen_copy("s0_t11")
            content, new_id, err = _rerun("s0_t11", [
                {"action": "shift_demands", "t_from": 0, "t_to": 600, "fraction": 0.3, "shift_s": -600}])
            assert err and new_id is None and "負" in content
            assert iso.new_ids() == [] and frozen_copy("s0_t11") == before
            assert results_store["s0_t11"]["_scenario"]["demands"][0]["flow"] == 0.6

    def test_update_demands_rejects_no_op_and_unknown_keys(self):
        """[評価 L22 で発見] update_demands に set も scale_flow も無い，または filter / remove のような未知のキーが
        あると，以前は「全需要に何も設定しない」で成功扱いになり，削除したつもりの需要が残った．"""
        with pytest.raises(ValueError, match="set.*scale_flow"):
            apply_modifications(F1, [{"action": "update_demands", "orig": "A", "dest": "C"}])
        with pytest.raises(ValueError, match="未対応のキー.*remove_demands"):
            apply_modifications(F1, [{"action": "update_demands", "filter": {"orig": "A", "dest": "C"}, "remove": True}])
        with pytest.raises(ValueError, match="未対応のキー"):
            apply_modifications(F1, [{"action": "remove_demands", "filter": {"orig": "A", "dest": "C"}}])
        sc, _ = apply_modifications(F1, [{"action": "remove_demands", "orig": "A", "dest": "C"}])
        assert sc["demands"] == []

    def test_remove_corner_node_cascades(self):
        """T22 / L22: 格子の隅 n0_0 を削除 → 8 ノード・20 有向リンク．対象需要だけ削除．"""
        sc, applied = apply_modifications(F4_SMALL, [{"action": "remove_nodes", "names": ["n0_0"]}])
        assert len(sc["nodes"]) == 8 and len(sc["links"]) == 20
        assert all("n0_0" not in (lk["start"], lk["end"]) for lk in sc["links"])
        assert [(d["orig"], d["dest"]) for d in sc["demands"]] == [("n1_0", "n1_2")]
        assert "接続リンク 4 本" in applied[0] and "需要 1 件" in applied[0]


class TestDemandComparison:
    """T19: 需要の比較は多重集合．同じ需要行が 2 本ある結果と 1 本の結果で総需要 720 → 360 台．"""

    def test_duplicate_demand_rows_are_counted(self):
        with IsolatedStore():
            dup = {**F1, "demands": [dict(F1["demands"][0]), dict(F1["demands"][0])]}
            store_result("dup_t19", dup)
            store_result("one_t19", F1)
            cmp = compare_simulations("dup_t19", "one_t19")
            d = cmp["scenario_diff"]["demands"]
            assert d["n_rows"] == [2, 1] and d["n_removed"] == 1 and d["n_added"] == 0
            assert d["total_vehicles"] == [720.0, 360.0]
            assert d["total_flow"] == [1.2, 0.6]
            # 検証器の確認（§16.4）: 集合化して 1 本消すと差が見えない
            as_set = {tuple(x.items()) for x in dup["demands"]}
            assert len(as_set) == 1
            assert demand_multiset(dup) != demand_multiset(F1)


class TestConversationIsolation:
    """T14 / T21: 会話ごとの状態と，長い履歴のトリミング後の基準．"""

    @staticmethod
    def _tb(name, args, tid):
        return types.SimpleNamespace(name=name, input=args, id=tid, type="tool_use")

    def test_two_conversations_interleaved_keep_their_own_ids(self):
        """T14: 同じアプリで会話 A・B のツール処理を交互に進めても，sim_id と集計キャッシュが混ざらない．"""
        with IsolatedStore() as iso:
            store_result("s0_t14", F1)
            state_a = ToolTurnState(chat_body("A", last_sim_id=None))
            state_b = ToolTurnState(chat_body("B", last_sim_id="s0_t14"))

            async def run(state, blocks):
                out = []
                async for kind, payload in dispatch_tool_blocks(blocks, state):
                    if kind == "results":
                        out = payload
                return out

            async def both():
                return await asyncio.gather(
                    run(state_a, [self._tb("run_simulation", {"grid": {"nx": 3, "spacing": 300},
                                                              "auto_demands": {"strategy": "boundary", "flow_per_pair": 0.05},
                                                              "tmax": 600, "random_seed": 1}, "a1"),
                                  self._tb("get_simulation_data", {"sim_id": ""}, "a2")]),
                    run(state_b, [self._tb("rerun_simulation", {"base_sim_id": "s0_t14", "modifications": [
                                      {"action": "update_links", "names": ["AB"], "set": {"capacity": 0.25}}]}, "b1"),
                                  self._tb("get_simulation_data", {"sim_id": ""}, "b2")]),
                )
            res_a, res_b = asyncio.run(both())
            assert state_a.sim_id and state_b.sim_id and state_a.sim_id != state_b.sim_id
            assert set(iso.new_ids()) == {state_a.sim_id, state_b.sim_id}
            # 空の sim_id は「この会話がこのターンで作った結果」に解決され，他会話の結果にならない
            assert state_a.last_data_sim_id == state_a.sim_id and set(state_a.sim_data_cache) == {state_a.sim_id}
            assert state_b.last_data_sim_id == state_b.sim_id and set(state_b.sim_data_cache) == {state_b.sim_id}
            assert json.loads(res_a[1]["content"])["sim_id"] == state_a.sim_id
            assert json.loads(res_b[1]["content"])["sim_id"] == state_b.sim_id
            assert results_store[state_b.sim_id]["_meta"]["source"]["base_sim_id"] == "s0_t14"
            assert results_store[state_a.sim_id]["_meta"]["source"].get("base_sim_id") is None

    def test_trimmed_history_still_uses_the_shown_result(self, monkeypatch):
        """T21 / L21: 予算超の履歴に別結果が混ざっていても，基準は表示中 ID（last_sim_id）．"""
        with IsolatedStore():
            store_result("shown_t21", F1)
            store_result("other_t21", f1(capacity=0.25))
            monkeypatch.setattr(risu.llm, "MAX_HISTORY_CHARS", 2000)
            history = []
            for i in range(30):
                history.append(("user", f"質問 {i} " + "あ" * 120))
                history.append(("assistant", f"結果 other_t21 を作りました（{i}）" + "い" * 120))
            body = chat_body("いま表示している結果のAB容量だけ900台/時にして，再実行してください．",
                             history=history, last_sim_id="shown_t21")
            msgs = risu.llm.build_llm_messages(body)
            assert len(msgs) < len(body.messages)                # トリミングされている
            ctx = msgs[-1]["content"][-1]["text"]
            assert 'rerun_simulation(base_sim_id="shown_t21")' in ctx
            assert "other_t21" not in ctx
            first = msgs[0]["content"]
            assert "省略" in (first if isinstance(first, str) else first[0]["text"])
