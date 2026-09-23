"""RISU シナリオ → UXsim World の変換（RISU 本体に依存しない純粋モジュール）．

このモジュールは **uxsim と標準ライブラリだけ** に依存する．FastAPI / pydantic /
anthropic を import しないので，サーバーを起動せずに素の Python から使える:

    from uxsim_bridge import scenario_from_dict, build_world
    W = build_world(scenario_from_dict(json.load(open("scenario.json"))))
    W.exec_simulation()

server.py の `_run_uxsim` も同じ `build_world` を使う（ネットワーク構築ロジックを
二重に持つと，片方だけ直して挙動がずれるため）．

シナリオの形式は RISU の SimulationInput と同じ:

    {
      "name": "sim", "tmax": 3600, "deltan": 5, "reaction_time": null, "random_seed": null,
      "nodes":   [{"name","x","y","flow_capacity"?,"signal"?}, ...],
      "links":   [{"name","start","end","length",
                   "free_flow_speed"?,"jam_density"?,"number_of_lanes"?,
                   "capacity"?,"signal_group"?}, ...],
      "demands": [{"orig","dest","t_start","t_end","flow"}, ...]
    }
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

__all__ = [
    "SCENARIO_DEFAULTS",
    "NODE_DEFAULTS",
    "LINK_DEFAULTS",
    "scenario_from_dict",
    "build_world",
]

# SimulationInput / NodeInput / LinkInput の既定値（server.py の pydantic モデルと一致させること）
SCENARIO_DEFAULTS: dict[str, Any] = {
    "name": "sim",
    "tmax": 3600,
    "deltan": 5,
    "reaction_time": None,
    "random_seed": None,
}
NODE_DEFAULTS: dict[str, Any] = {
    "flow_capacity": None,
    "signal": None,
}
LINK_DEFAULTS: dict[str, Any] = {
    "free_flow_speed": 20.0,
    "jam_density": 0.2,
    "number_of_lanes": 1,
    "capacity": None,
    "signal_group": None,
}


def _ns(d: dict, defaults: dict) -> SimpleNamespace:
    """dict を属性アクセス可能なオブジェクトにする（欠けたキーは既定値で埋める）．"""
    merged = dict(defaults)
    merged.update(d)
    return SimpleNamespace(**merged)


def scenario_from_dict(data: dict) -> SimpleNamespace:
    """プレーンな dict を build_world が受け取れる形に正規化する．

    `{"scenario": {...}}` というエンベロープ（RISU の /results/<id>/scenario や
    ダウンロードした JSON の形）で渡された場合は中身を取り出す．

    pydantic を使わないので，ここでの検証は「必須キーがあるか」だけに留める．
    厳密な検証（重複ノード名・参照切れ）が要るときは RISU 本体を通すこと．
    """
    if not isinstance(data, dict):
        raise TypeError("シナリオは dict である必要があります")
    # エンベロープなら中身へ降りる（result 付き / scenario のみ のどちらでも動く）
    if "scenario" in data and isinstance(data["scenario"], dict):
        data = data["scenario"]

    for key in ("nodes", "links", "demands"):
        if key not in data:
            raise ValueError(f"シナリオに '{key}' がありません")

    sc = _ns({k: v for k, v in data.items() if k not in ("nodes", "links", "demands")},
             SCENARIO_DEFAULTS)
    sc.nodes = [_ns(n, NODE_DEFAULTS) for n in data["nodes"]]
    sc.links = [_ns(lk, LINK_DEFAULTS) for lk in data["links"]]
    sc.demands = [SimpleNamespace(**d) for d in data["demands"]]
    return sc


def build_world(scenario, *, cpp: bool = True, disable_basic_analysis: bool = False,
                print_mode: int = 0, save_mode: int = 0, show_mode: int = 0):
    """シナリオから UXsim の World を組み立てて返す（exec_simulation は呼ばない）．

    scenario: 属性アクセスできるオブジェクト．RISU の SimulationInput でも，
              scenario_from_dict() が返す SimpleNamespace でもよい．
    cpp:      uxsim >= 1.14 の C++ バックエンドを使う．未対応版では自動で純 Python に落ちる．
    disable_basic_analysis:
              True にすると analyzer.basic_analysis を無効化する．basic_analysis は
              内部で od_analysis → floyd_warshall（全点対最短路, O(ノード数^3)）を実行し，
              5000 ノード級では 1 回数十秒かかる．RISU サーバーはこれを使わないので True．
              素の UXsim として使う場合，W.analyzer の各種集計が必要なら False のままにする．
    print_mode / save_mode / show_mode:
              UXsim World にそのまま渡す．RISU サーバーは結果を自前で組み立てるので
              全て 0（出力なし・ログ保存なし・描画なし）．
              **W.analyzer の集計（print_simple_stats, vehicles_to_pandas 等）を使うなら
              save_mode=1 が必要**で，統計を標準出力に出すなら print_mode=1 も要る．
    """
    from uxsim import World

    world_kwargs = dict(
        name=getattr(scenario, "name", "sim"),
        tmax=getattr(scenario, "tmax", 3600),
        deltan=getattr(scenario, "deltan", 5),
        print_mode=print_mode,
        save_mode=save_mode,
        show_mode=show_mode,
    )
    reaction_time = getattr(scenario, "reaction_time", None)
    if reaction_time:
        world_kwargs["reaction_time"] = float(reaction_time)
    # 乱数シード（経路選択ノイズ・合流順に効く）．None なら UXsim が毎回別の乱数列を使う．
    random_seed = getattr(scenario, "random_seed", None)
    if random_seed is not None:
        world_kwargs["random_seed"] = int(random_seed)

    try:
        W = World(**world_kwargs, cpp=cpp)
    except TypeError:
        # uxsim < 1.14: cpp キーワード自体が無い
        W = World(**world_kwargs)

    node_map = {}
    for n in scenario.nodes:
        kwargs = {}
        if n.flow_capacity is not None:
            kwargs["flow_capacity"] = n.flow_capacity
        if n.signal is not None:
            kwargs["signal"] = n.signal
        node_map[n.name] = W.addNode(n.name, x=n.x, y=n.y, **kwargs)

    link_map = {}
    for lk in scenario.links:
        link_kwargs = {}
        if lk.signal_group is not None:
            link_kwargs["signal_group"] = lk.signal_group
        if lk.capacity is not None:
            # 明示容量: 下流端の流出容量として与える（渋滞の待ち行列が
            # このリンク上に物理的に形成される，標準的なボトルネック表現）
            link_kwargs["capacity_out"] = lk.capacity
        link_map[lk.name] = W.addLink(
            lk.name,
            start_node=node_map[lk.start],
            end_node=node_map[lk.end],
            length=lk.length,
            free_flow_speed=lk.free_flow_speed,
            jam_density=lk.jam_density,
            number_of_lanes=lk.number_of_lanes,
            **link_kwargs,
        )

    for d in scenario.demands:
        W.adddemand(
            orig=node_map[d.orig],
            dest=node_map[d.dest],
            t_start=d.t_start,
            t_end=d.t_end,
            flow=d.flow,
        )

    if disable_basic_analysis:
        _disable_basic_analysis(W)

    # 呼び出し側が名前からオブジェクトを引けるように残しておく
    W._risu_node_map = node_map
    W._risu_link_map = link_map
    return W


def _disable_basic_analysis(W) -> None:
    """analyzer.basic_analysis を無効化する．

    cpp backend は exec_simulation 終了時（simulation_terminated）に自動で
    basic_analysis を呼ぶ．analyzer は exec 中に生成されるので直接は差し替えられず，
    生成フック `_setup_analyzer` をラップして先に潰す．
    """
    if hasattr(W, "_setup_analyzer"):
        _orig = W._setup_analyzer

        def _setup_analyzer_no_basic(*a, **k):
            r = _orig(*a, **k)
            try:
                W.analyzer.basic_analysis = lambda *a_, **k_: None
            except Exception:
                pass  # 失敗しても遅くなるだけで結果は変わらない
            return r

        W._setup_analyzer = _setup_analyzer_no_basic
