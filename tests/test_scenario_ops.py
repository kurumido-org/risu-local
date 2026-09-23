"""差分命令・grid・OD 生成（risu.scenario_ops）．"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.results import results_store  # noqa: E402
from risu.schema import SimulationInput  # noqa: E402
from risu.simulation import _run_uxsim  # noqa: E402


# ============================================================
# 2z. シナリオパッチエンジン（rerun_simulation）
# ============================================================

class TestScenarioModifications:
    """
    _apply_modifications: 大規模ネットワークを LLM に往復させないための
    差分命令エンジン．保存済みシナリオに小さなパッチを適用する．
    """

    def _base(self):
        return {
            "name": "base", "tmax": 1000, "deltan": 5,
            "nodes": [
                {"name": "A", "x": 0, "y": 0},
                {"name": "B", "x": 1000, "y": 0},
                {"name": "C", "x": 2000, "y": 0},
            ],
            "links": [
                {"name": "r1", "start": "A", "end": "B", "length": 1000,
                 "free_flow_speed": 20, "number_of_lanes": 1},
                {"name": "r2", "start": "B", "end": "C", "length": 1000,
                 "free_flow_speed": 20, "number_of_lanes": 1},
            ],
            "demands": [
                {"orig": "A", "dest": "C", "t_start": 0, "t_end": 500, "flow": 0.4},
            ],
        }

    def test_update_links_by_name(self):
        from risu.scenario_ops import _apply_modifications
        sc, applied = _apply_modifications(self._base(), [
            {"action": "update_links", "names": ["r1"], "set": {"capacity": 0.3}},
        ])
        assert sc["links"][0]["capacity"] == 0.3
        assert "capacity" not in sc["links"][1]
        assert len(applied) == 1

    def test_update_links_all(self):
        from risu.scenario_ops import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "update_links", "all": True, "set": {"free_flow_speed": 10}},
        ])
        assert all(l["free_flow_speed"] == 10 for l in sc["links"])

    def test_unknown_link_name_raises(self):
        from risu.scenario_ops import _apply_modifications
        with pytest.raises(ValueError, match="r99"):
            _apply_modifications(self._base(), [
                {"action": "update_links", "names": ["r99"], "set": {"capacity": 1}},
            ])

    def test_update_demands_scale(self):
        from risu.scenario_ops import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "update_demands", "all": True, "scale_flow": 1.5},
        ])
        assert sc["demands"][0]["flow"] == 0.6

    def test_remove_nodes_cascades(self):
        from risu.scenario_ops import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "remove_nodes", "names": ["B"]},
        ])
        assert len(sc["nodes"]) == 2
        assert len(sc["links"]) == 0  # r1, r2 とも B に接続
        assert len(sc["demands"]) == 1  # A→C は残る

    def test_add_and_signal(self):
        from risu.scenario_ops import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "update_nodes", "names": ["B"], "set": {"signal": [30, 30]}},
            {"action": "update_links", "names": ["r1"], "set": {"signal_group": 0}},
            {"action": "add_demand", "demand": {"orig": "C", "dest": "A",
                                                "t_start": 0, "t_end": 500, "flow": 0.2}},
        ])
        assert sc["nodes"][1]["signal"] == [30, 30]
        assert sc["links"][0]["signal_group"] == 0
        assert len(sc["demands"]) == 2

    def test_base_scenario_not_mutated(self):
        from risu.scenario_ops import _apply_modifications
        base = self._base()
        _apply_modifications(base, [
            {"action": "update_links", "all": True, "set": {"capacity": 0.1}},
        ])
        assert "capacity" not in base["links"][0]

    def test_rerun_handler_end_to_end(self):
        """保存済みシナリオ → パッチ → 再実行 → 新 sim_id"""
        import asyncio
        import types
        import json as _json
        from risu.results import _store_sim
        from risu.tools import _handle_rerun_simulation

        base_result = _run_uxsim(SimulationInput(**self._base()))
        _store_sim("rerun_base", base_result)

        body = types.SimpleNamespace(messages=[])
        content, new_id, is_err = asyncio.run(_handle_rerun_simulation({
            "base_sim_id": "rerun_base",
            "modifications": [
                {"action": "update_links", "names": ["r1"], "set": {"capacity": 0.15}},
            ],
        }, body))
        assert not is_err, content
        assert new_id in results_store
        payload = _json.loads(content)
        assert payload["base_sim_id"] == "rerun_base"
        assert payload["applied"]
        # 新シナリオに capacity が反映され，渋滞で旅行時間が悪化している
        new_sc = results_store[new_id]["_scenario"]
        assert new_sc["links"][0]["capacity"] == 0.15
        assert payload["average_travel_time_s"] > base_result["stats"]["average_travel_time_s"]

    def test_rerun_handler_bad_sim_id(self):
        import asyncio
        import types
        from risu.tools import _handle_rerun_simulation
        content, new_id, is_err = asyncio.run(_handle_rerun_simulation(
            {"base_sim_id": "no_such_id", "modifications": []},
            types.SimpleNamespace(messages=[])))
        assert is_err and new_id is None

    def test_generate_demands_random(self):
        """ノード名を知らなくてもサーバー側でランダム OD を生成できる"""
        from risu.scenario_ops import _apply_modifications
        sc, applied = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "random",
             "n_pairs": 5, "flow_per_pair": 0.1, "seed": 42, "clear_existing": True},
        ])
        assert len(sc["demands"]) == 5
        node_names = {n["name"] for n in sc["nodes"]}
        for d in sc["demands"]:
            assert d["orig"] in node_names and d["dest"] in node_names
            assert d["orig"] != d["dest"]
            assert d["flow"] == 0.1
        # seed 固定で再現性がある
        sc2, _ = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "random",
             "n_pairs": 5, "flow_per_pair": 0.1, "seed": 42, "clear_existing": True},
        ])
        assert sc["demands"] == sc2["demands"]

    def test_generate_demands_flow_total(self):
        from risu.scenario_ops import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "random",
             "n_pairs": 4, "flow_total": 1.0, "clear_existing": True},
        ])
        assert all(d["flow"] == 0.25 for d in sc["demands"])

    def test_generate_demands_boundary(self):
        from risu.scenario_ops import _apply_modifications
        sc, _ = _apply_modifications(self._base(), [
            {"action": "generate_demands", "strategy": "boundary", "clear_existing": True},
        ])
        assert len(sc["demands"]) >= 2  # 周縁ノード全ペア

    def test_set_params_sets_seed_and_reaction_time(self):
        from risu.scenario_ops import _apply_modifications
        sc, applied = _apply_modifications(self._base(), [
            {"action": "set_params", "random_seed": 42, "reaction_time": 1.7},
        ])
        assert sc["random_seed"] == 42 and sc["reaction_time"] == 1.7
        assert sc["tmax"] == 1000  # 触っていない値はそのまま
        assert "set_params" in applied[0]
        # None で既定に戻す
        sc2, _ = _apply_modifications(sc, [{"action": "set_params", "random_seed": None}])
        assert "random_seed" not in sc2 and sc2["reaction_time"] == 1.7

    def test_set_params_rejects_unknown_field(self):
        from risu.scenario_ops import _apply_modifications
        with pytest.raises(ValueError, match="set_params"):
            _apply_modifications(self._base(), [{"action": "set_params", "foo": 1}])
        with pytest.raises(ValueError, match="set_params"):
            _apply_modifications(self._base(), [{"action": "set_params"}])

    def test_seed_survives_rerun_derivation(self):
        """base に seed があれば，別の差分だけ当てた派生シナリオにも同じ seed が残る．"""
        from risu.scenario_ops import _apply_modifications
        base = self._base(); base["random_seed"] = 7; base["reaction_time"] = 1.5
        sc, _ = _apply_modifications(base, [
            {"action": "update_links", "names": ["r1"], "set": {"capacity": 0.3}},
        ])
        assert sc["random_seed"] == 7 and sc["reaction_time"] == 1.5
        si = SimulationInput(**sc)
        assert si.random_seed == 7
