"""CSV / GMNS 取込（risu.importers）．"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.importers import parse_csv_scenario  # noqa: E402


# ============================================================
# 3. CSV パーサーテスト
# ============================================================

class TestCSVParser:
    """
    [修正履歴] 空セルで float("") エラーが発生した．
    _f() / _i() ヘルパーで対処済み．
    """

    def test_risu_csv_basic(self):
        """RISU CSV 形式の基本パース"""
        csv = (
            "type,name,x,y,start,end,length,free_flow_speed,number_of_lanes,orig,dest,t_start,t_end,flow\n"
            "node,A,0,0,,,,,,,,,,\n"
            "node,B,5000,0,,,,,,,,,,\n"
            "link,r1,,,A,B,5000,20,1,,,,,\n"
            "demand,,,,,,,,,A,B,0,600,0.5\n"
        )
        result = parse_csv_scenario(csv)
        assert result["format"] == "risu_csv"
        assert len(result["nodes"]) == 2
        assert len(result["links"]) == 1
        assert len(result["demands"]) == 1

    def test_node_csv_prefers_node_id_over_name(self):
        """
        [修正履歴] node_id（主キー）と name（表示ラベル，重複可）の両方を持つ
        GMNS 風データで，name を識別子に選んで「ノード名が重複」エラーになった．
        ID 系カラムを優先する．
        """
        csv = (
            "node_id,name,x_coord,y_coord\n"
            "1,東京高速道路-IN,0,0\n"
            "2,東京高速道路-IN,500,0\n"
            "3,東京高速道路-OUT,1000,0\n"
        )
        result = parse_csv_scenario(csv)
        assert result["format"] == "node_csv"
        names = [n["name"] for n in result["nodes"]]
        assert names == ["1", "2", "3"], f"node_id が識別子になるべき: {names}"

    def test_link_csv_prefers_link_id_over_name(self):
        csv = (
            "link_id,name,from_node_id,to_node_id,length\n"
            "10,環状線,1,2,800\n"
            "11,環状線,2,3,800\n"
        )
        result = parse_csv_scenario(csv)
        assert result["format"] == "link_csv"
        names = [l["name"] for l in result["links"]]
        assert names == ["10", "11"], f"link_id が識別子になるべき: {names}"
        assert result["links"][0]["start"] == "1"

    def test_risu_csv_empty_cells(self):
        """
        空セルが含まれる RISU CSV でエラーにならない．
        [修正履歴] float("") で ValueError が発生した．
        """
        csv = (
            "type,name,x,y,start,end,length,free_flow_speed,number_of_lanes,orig,dest,t_start,t_end,flow\n"
            "node,start,0,0,,,,,,,,,,\n"
            "node,goal,5000,0,,,,,,,,,,\n"
            "link,road,,,start,goal,5000,,,,,,,,\n"
            "demand,,,,,,,,,start,goal,0,600,0.8\n"
        )
        result = parse_csv_scenario(csv)
        assert result["format"] == "risu_csv"
        # 空の free_flow_speed はデフォルト値 20 になる
        assert result["links"][0]["free_flow_speed"] == 20

    def test_gmns_node_csv(self):
        """GMNS node.csv 形式"""
        csv = "node_id,x_coord,y_coord,zone_id\n1,0,0,1\n2,5000,0,2\n"
        result = parse_csv_scenario(csv)
        assert result["format"] in ("gmns_node", "node_csv")
        assert len(result["nodes"]) == 2

    def test_gmns_link_csv(self):
        """GMNS link.csv 形式"""
        csv = "link_id,from_node_id,to_node_id,length,free_speed,lanes\n1,1,2,5000,60,2\n"
        result = parse_csv_scenario(csv)
        assert result["format"] in ("gmns_link", "link_csv")
        assert len(result["links"]) == 1

    def test_gmns_demand_csv(self):
        """GMNS demand.csv 形式"""
        csv = "o_zone_id,d_zone_id,volume\n1,2,500\n2,1,300\n"
        result = parse_csv_scenario(csv)
        assert result["format"] in ("gmns_demand", "demand_csv")
        assert len(result["demands"]) == 2

    def test_flexible_node_csv(self):
        """柔軟なカラム名のノード CSV"""
        csv = "name,lon,lat\nA,139.7,35.6\nB,139.71,35.61\n"
        result = parse_csv_scenario(csv)
        assert result["format"] == "node_csv"
        assert len(result["nodes"]) == 2

    def test_flexible_link_csv(self):
        """柔軟なカラム名のリンク CSV"""
        csv = "id,from,to,distance,speed_limit\n1,A,B,5000,60\n2,B,C,3000,40\n"
        result = parse_csv_scenario(csv)
        assert result["format"] == "link_csv"
        assert len(result["links"]) == 2
        assert result["links"][0]["start"] == "A"
        assert result["links"][0]["end"] == "B"

    def test_unknown_csv_raises(self):
        """認識できない CSV はエラー"""
        csv = "col_a,col_b\n1,2\n"
        with pytest.raises(ValueError, match="CSV 形式を認識できません"):
            parse_csv_scenario(csv)


class TestZeroLengthLinks:
    """[修正履歴] 取込データの長さ 0 のリンクが SimulationInput の検証（length > 0）で弾かれ，
    「Input should be greater than 0（links.9.length）」で取込全体が失敗した．
    取込側で最小長（1 m）に補正して通し，補正したことをログと OSM の要約に残す．"""

    def test_clamp_helper(self):
        from risu.importers import MIN_LINK_LENGTH_M, clamp_link_lengths
        links = [{"name": "a", "length": 0}, {"name": "b", "length": -3}, {"name": "c"},
                 {"name": "d", "length": "x"}, {"name": "e", "length": 250.5}]
        fixed = clamp_link_lengths(links)
        assert fixed == ["a", "b", "c", "d"]
        assert all(lk["length"] == MIN_LINK_LENGTH_M for lk in links[:4])
        assert links[4]["length"] == 250.5
        assert clamp_link_lengths([]) == []

    def test_csv_with_zero_length_link_is_accepted(self):
        from risu.importers import parse_csv_scenario
        from risu.schema import SimulationInput
        csv = (
            "type,name,x,y,start,end,length,free_flow_speed,number_of_lanes,orig,dest,t_start,t_end,flow\n"
            "node,A,0,0,,,,,,,,,,\nnode,B,0,0,,,,,,,,,,\nnode,C,1000,0,,,,,,,,,,\n"
            "link,AB,,,A,B,0,20,1,,,,,\nlink,BC,,,B,C,1000,20,1,,,,,\n"
            "demand,,,,,,,,,A,C,0,100,0.3\n"
        )
        sc = parse_csv_scenario(csv)
        assert {lk["name"]: lk["length"] for lk in sc["links"]} == {"AB": 1.0, "BC": 1000.0}
        SimulationInput(**{k: v for k, v in sc.items() if k != "format"})   # 検証を通る

    def test_validation_error_names_the_link(self):
        from fastapi import HTTPException
        from risu.schema import scenario_to_input
        sc = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 1, "y": 0}],
              "links": [{"name": "ok", "start": "A", "end": "B", "length": 5},
                        {"name": "bad_link", "start": "A", "end": "B", "length": 0}],
              "demands": [{"orig": "A", "dest": "B", "t_start": 0, "t_end": 10, "flow": -1}]}
        with pytest.raises(HTTPException) as ei:
            scenario_to_input(sc)
        detail = ei.value.detail
        assert "リンク 'bad_link' の length" in detail
        assert "需要 'A→B' の flow" in detail
        assert "links.1.length" not in detail
