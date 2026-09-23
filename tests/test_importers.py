"""CSV / GMNS 取込（risu.importers）．"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risu.importers import _parse_csv_scenario  # noqa: E402


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
        result = _parse_csv_scenario(csv)
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
        result = _parse_csv_scenario(csv)
        assert result["format"] == "node_csv"
        names = [n["name"] for n in result["nodes"]]
        assert names == ["1", "2", "3"], f"node_id が識別子になるべき: {names}"

    def test_link_csv_prefers_link_id_over_name(self):
        csv = (
            "link_id,name,from_node_id,to_node_id,length\n"
            "10,環状線,1,2,800\n"
            "11,環状線,2,3,800\n"
        )
        result = _parse_csv_scenario(csv)
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
        result = _parse_csv_scenario(csv)
        assert result["format"] == "risu_csv"
        # 空の free_flow_speed はデフォルト値 20 になる
        assert result["links"][0]["free_flow_speed"] == 20

    def test_gmns_node_csv(self):
        """GMNS node.csv 形式"""
        csv = "node_id,x_coord,y_coord,zone_id\n1,0,0,1\n2,5000,0,2\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] in ("gmns_node", "node_csv")
        assert len(result["nodes"]) == 2

    def test_gmns_link_csv(self):
        """GMNS link.csv 形式"""
        csv = "link_id,from_node_id,to_node_id,length,free_speed,lanes\n1,1,2,5000,60,2\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] in ("gmns_link", "link_csv")
        assert len(result["links"]) == 1

    def test_gmns_demand_csv(self):
        """GMNS demand.csv 形式"""
        csv = "o_zone_id,d_zone_id,volume\n1,2,500\n2,1,300\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] in ("gmns_demand", "demand_csv")
        assert len(result["demands"]) == 2

    def test_flexible_node_csv(self):
        """柔軟なカラム名のノード CSV"""
        csv = "name,lon,lat\nA,139.7,35.6\nB,139.71,35.61\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] == "node_csv"
        assert len(result["nodes"]) == 2

    def test_flexible_link_csv(self):
        """柔軟なカラム名のリンク CSV"""
        csv = "id,from,to,distance,speed_limit\n1,A,B,5000,60\n2,B,C,3000,40\n"
        result = _parse_csv_scenario(csv)
        assert result["format"] == "link_csv"
        assert len(result["links"]) == 2
        assert result["links"][0]["start"] == "A"
        assert result["links"][0]["end"] == "B"

    def test_unknown_csv_raises(self):
        """認識できない CSV はエラー"""
        csv = "col_a,col_b\n1,2\n"
        with pytest.raises(ValueError, match="CSV 形式を認識できません"):
            _parse_csv_scenario(csv)
