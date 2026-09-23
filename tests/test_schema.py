"""入力スキーマ（risu.schema）: 重複・参照切れ・値域の検証．"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.schema import SimulationInput  # noqa: E402


# ============================================================
# 2y. シナリオ検証（重複・参照切れ）
# ============================================================

class TestScenarioValidation:
    """
    [修正履歴] 重複ノード名が UXsim の生エラー
    「Node name X already used by another node」のまま 400 で返り，
    どこを直せばいいか分からなかった．SimulationInput で事前検証する．
    """

    def _nodes(self):
        return [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1000, "y": 0}]

    def test_exact_duplicate_nodes_merged(self):
        """完全一致の重複ノードは黙って統合される（CSV でよくある形式）"""
        si = SimulationInput(
            name="t", tmax=600, deltan=5,
            nodes=self._nodes() + [{"name": "A", "x": 0, "y": 0}],
            links=[{"name": "r", "start": "A", "end": "B", "length": 1000}],
            demands=[],
        )
        assert len(si.nodes) == 2

    def test_conflicting_duplicate_nodes_rejected(self):
        """同名で座標が異なるノードは名前を列挙してエラー"""
        with pytest.raises(ValueError, match="東京高速道路-IN"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=[{"name": "東京高速道路-IN", "x": 0, "y": 0},
                       {"name": "東京高速道路-IN", "x": 500, "y": 0}],
                links=[], demands=[],
            )

    def test_exact_duplicate_links_merged(self):
        si = SimulationInput(
            name="t", tmax=600, deltan=5,
            nodes=self._nodes(),
            links=[{"name": "r", "start": "A", "end": "B", "length": 1000},
                   {"name": "r", "start": "A", "end": "B", "length": 1000}],
            demands=[],
        )
        assert len(si.links) == 1

    def test_conflicting_duplicate_links_rejected(self):
        with pytest.raises(ValueError, match="リンク名が重複"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=self._nodes(),
                links=[{"name": "r", "start": "A", "end": "B", "length": 1000},
                       {"name": "r", "start": "B", "end": "A", "length": 1000}],
                demands=[],
            )

    def test_link_referencing_missing_node_rejected(self):
        with pytest.raises(ValueError, match="存在しないノード"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=self._nodes(),
                links=[{"name": "r", "start": "A", "end": "X", "length": 1000}],
                demands=[],
            )

    def test_demand_referencing_missing_node_rejected(self):
        with pytest.raises(ValueError, match="需要が存在しないノード"):
            SimulationInput(
                name="t", tmax=600, deltan=5,
                nodes=self._nodes(),
                links=[{"name": "r", "start": "A", "end": "B", "length": 1000}],
                demands=[{"orig": "A", "dest": "Z", "t_start": 0, "t_end": 100, "flow": 0.1}],
            )

    # ── 値域の検証（deltan=0 / 負のリンク長 / 負の需要 / 時刻逆転 を受理していた） ──
    def _valid(self, **over):
        base = dict(
            name="t", tmax=600, deltan=5,
            nodes=self._nodes(),
            links=[{"name": "r", "start": "A", "end": "B", "length": 1000}],
            demands=[{"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.1}],
        )
        base.update(over)
        return base

    def test_valid_baseline_accepted(self):
        si = SimulationInput(**self._valid())
        assert si.random_seed is None and si.reaction_time is None

    @pytest.mark.parametrize("over", [
        {"deltan": 0},
        {"deltan": -1},
        {"tmax": 0},
        {"reaction_time": 0},
        {"reaction_time": -1.0},
    ])
    def test_bad_scenario_params_rejected(self, over):
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(**over))

    @pytest.mark.parametrize("link_over", [
        {"length": -100}, {"length": 0},
        {"free_flow_speed": 0}, {"jam_density": -0.1},
        {"number_of_lanes": 0}, {"capacity": -1},
    ])
    def test_bad_link_values_rejected(self, link_over):
        lk = {"name": "r", "start": "A", "end": "B", "length": 1000}
        lk.update(link_over)
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(links=[lk]))

    @pytest.mark.parametrize("d_over", [
        {"flow": -0.1},
        {"t_start": -10},
        {"t_start": 100, "t_end": 50},   # 逆転
        {"t_start": 100, "t_end": 100},  # 長さゼロ
    ])
    def test_bad_demand_values_rejected(self, d_over):
        d = {"orig": "A", "dest": "B", "t_start": 0, "t_end": 100, "flow": 0.1}
        d.update(d_over)
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(demands=[d]))

    def test_node_negative_capacity_rejected(self):
        nodes = self._nodes()
        nodes[1]["flow_capacity"] = -0.5
        with pytest.raises(ValueError):
            SimulationInput(**self._valid(nodes=nodes))

    def test_validation_error_reaches_client_as_422(self):
        """/simulate 経由でも 500 ではなく，どの項目かが分かる 422 になる．"""
        from fastapi.testclient import TestClient
        from risu.api import app
        client = TestClient(app)
        r = client.post("/simulate", json=self._valid(deltan=0))
        assert r.status_code == 422
        # /simulate は FastAPI 標準の 422（loc 付きリスト）．フロントは msg を連結して表示する
        assert "deltan" in json.dumps(r.json()["detail"])
