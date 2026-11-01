"""需要の出所（source.demand）が派生・保存・再読込を通じて追えること．テスト設計 R04，T04 / T30．

OSM 取込の自動生成需要は「仮定値」で，LLM が実測と取り違えて説明しないよう毎ターンの
コンテキストに出所を載せる．容量だけ変えた派生結果でも同じ情報が残らなければならない．
F5 は通信なしの合成道路網（tests/fixtures）．
"""

import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import risu.results  # noqa: E402
from risu.results import list_results, results_store  # noqa: E402
from risu.tools import conversation_context_block, derived_demand_provenance, handle_rerun_simulation  # noqa: E402

from fixtures import F5_DEMAND_NOTE, F5_GEOMETRIES, IsolatedStore, store_f5  # noqa: E402
from helpers import chat_body  # noqa: E402


def _rerun(base, mods):
    return asyncio.run(handle_rerun_simulation({"base_sim_id": base, "modifications": mods},
                                               types.SimpleNamespace(messages=[])))


class TestDemandProvenanceSurvivesDerivation:
    """T04 / R04 / L10: 派生結果でも自動生成需要と判別でき，出所への参照経路が残る．"""

    def test_base_result_exposes_assumed_demand(self):
        with IsolatedStore():
            store_f5("f5_base")
            block = conversation_context_block(chat_body("x", last_sim_id="f5_base"))
            assert "需要の出所: OSM 取込時の自動生成（仮定値）" in block
            assert "実測の交通量ではない" in block

    def test_capacity_only_rerun_inherits_the_note(self):
        with IsolatedStore():
            store_f5("f5_t04")
            content, new_id, err = _rerun("f5_t04", [{"action": "update_links", "names": ["AB"], "set": {"capacity": 0.25}}])
            assert not err, content
            src = results_store[new_id]["_meta"]["source"]
            assert src["base_sim_id"] == "f5_t04" and src["base_type"] == "osm"
            dem = src["demand"]
            assert dem["method"] == "osm_boundary_pairs" and dem["inherited_from"] == "f5_t04"
            assert dem["note"].startswith(F5_DEMAND_NOTE) and "需要は変更していない" in dem["note"]
            # LLM への経路: tool_result と毎ターンのコンテキスト
            assert json.loads(content)["demand_assumption"] == dem["note"]
            block = conversation_context_block(chat_body("この結果の需要は実測データですか", last_sim_id=new_id))
            assert "需要の出所: OSM 取込時の自動生成（仮定値）" in block and "f5_t04" in block
            # 元の結果側の出所は変わらない
            assert results_store["f5_t04"]["_meta"]["source"]["demand"]["note"] == F5_DEMAND_NOTE
            # 道路形状（多点）も引き継ぐ
            coords = {f["properties"]["name"]: f["geometry"]["coordinates"]
                      for f in results_store[new_id]["geojson"]["features"]}
            assert coords["AB"] == F5_GEOMETRIES["AB"] and len(coords["BC"]) == 2

    def test_demand_changing_rerun_records_what_changed(self):
        """需要を変更した場合は「元は自動生成，その後に何を変更したか」を記録する．"""
        with IsolatedStore():
            store_f5("f5_t04b")
            content, new_id, err = _rerun("f5_t04b", [
                {"action": "update_demands", "all": True, "scale_flow": 1.5},
                {"action": "update_links", "names": ["BC"], "set": {"free_flow_speed": 10}},
            ])
            assert not err, content
            dem = results_store[new_id]["_meta"]["source"]["demand"]
            assert dem["modified_by"] == ["update_demands"]
            assert "派生時に需要を変更: update_demands" in dem["note"] and dem["note"].startswith(F5_DEMAND_NOTE)
            # 2 段派生しても起点は最初の取込結果
            content2, new2, err2 = _rerun(new_id, [{"action": "update_links", "names": ["AB"], "set": {"capacity": 0.3}}])
            assert not err2
            dem2 = results_store[new2]["_meta"]["source"]["demand"]
            assert dem2["inherited_from"] == "f5_t04b" and dem2["modified_by"] == ["update_demands"]
            assert "自動生成（仮定値）" in dem2["note"]

    def test_results_without_provenance_stay_unmarked(self):
        """自動生成ではない需要（手入力・CSV）には出所の注記を付けない（誤って仮定扱いにしない）．"""
        assert derived_demand_provenance({"source": {"type": "manual"}}, "x", []) is None
        assert derived_demand_provenance(None, "x", []) is None
        d = derived_demand_provenance({"source": {"type": "osm", "demand": {"note": "N", "method": "m"}}}, "b",
                                      [{"action": "remove_nodes", "names": ["D"]}])
        assert d["modified_by"] == ["remove_nodes"] and d["inherited_from"] == "b"


class TestProvenancePersists:
    """T30: 派生結果を保存し，メモリ追出し後 / 別プロセス相当の読み戻しでも条件・親 ID・出所・版を維持．"""

    def test_derived_result_roundtrips_through_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(risu.results, "RESULTS_DIR", str(tmp_path))
        with IsolatedStore():
            store_f5("f5_t30")
            content, new_id, err = _rerun("f5_t30", [{"action": "update_links", "names": ["AB"], "set": {"capacity": 0.25}}])
            assert not err, content
            raw = results_store[new_id]
            # 永続化の完了を待つ（HTTP 応答直後に保存済みと仮定しない）
            for _ in range(200):
                if risu.results.persisted_path(new_id):
                    break
                import time; time.sleep(0.05)
            assert risu.results.persisted_path(new_id), "派生結果が保存されていない"
            before = {"scenario": raw["_scenario"], "stats": raw["stats"], "source": raw["_meta"]["source"],
                      "uxsim_version": raw["_meta"]["uxsim_version"]}
            # メモリから消して読み戻す
            dict.pop(results_store, new_id)
            assert not dict.__contains__(results_store, new_id) and new_id in results_store
            loaded = results_store[new_id]
            assert loaded["_scenario"] == before["scenario"]
            assert loaded["stats"] == before["stats"]
            src = loaded["_meta"]["source"]
            assert src["base_sim_id"] == "f5_t30" and src["base_type"] == "osm"
            assert src["demand"]["note"] == before["source"]["demand"]["note"]
            assert src["demand"]["inherited_from"] == "f5_t30"
            assert loaded["_meta"]["uxsim_version"] == before["uxsim_version"]
            # 一覧（サイドカー）にも出所が残る
            row = next(r for r in list_results(50) if r["sim_id"] == new_id)
            assert row["source"]["base_sim_id"] == "f5_t30" and "仮定値" in row["source"]["demand"]["note"]
            # 読み戻した結果からもコンテキストに出所が載る
            block = conversation_context_block(chat_body("x", last_sim_id=new_id))
            assert "需要の出所: OSM 取込時の自動生成（仮定値）" in block


class TestEnvelopeKeepsRoadGeometry:
    """U03 の裏付け: 保存形式（エンベロープ）は多点の道路形状を同梱し，読み戻し・再アップロードで直線に戻らない．

    [修正履歴] build_envelope は scenario に link_geometries を載せず，/upload は scenario.link_geometries しか
    見なかったため，OSM 取込結果を保存して読み戻すと道路が直線になった．
    """

    def test_envelope_carries_geometries_and_upload_restores_them(self):
        from fastapi.testclient import TestClient

        import risu.api
        from risu.results import build_envelope, link_geometries_of
        with IsolatedStore():
            store_f5("f5_geo")
            env = build_envelope("f5_geo")
            assert env["scenario"]["link_geometries"] == F5_GEOMETRIES
            assert build_envelope("f5_geo", include_result=False)["scenario"]["link_geometries"] == F5_GEOMETRIES
            assert link_geometries_of(results_store["f5_geo"]) == F5_GEOMETRIES
            # 正規化シナリオ自体には混ざらない（直線だけの結果には載せない）
            assert "link_geometries" not in results_store["f5_geo"]["_scenario"]
            from risu.results import envelope_json_bytes
            client = TestClient(risu.api.app)
            for name, payload in (("full.json", envelope_json_bytes("f5_geo")),
                                  ("light.json", json.dumps(build_envelope("f5_geo", include_result=False)).encode("utf-8"))):
                r = client.post("/upload", files={"files": (name, payload, "application/json")}, data={"tmax": "2400"})
                assert r.status_code == 200, r.text
                new = results_store[r.json()["id"]]
                assert link_geometries_of(new) == F5_GEOMETRIES, name
                assert "link_geometries" not in new["_scenario"]
