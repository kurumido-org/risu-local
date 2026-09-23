"""入力スキーマ（pydantic）: シナリオ・チャットのリクエスト．値域と参照整合性の検証はここ．
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "ChatInput",
    "ChatMessage",
    "SimulationInput",
    "scenario_to_input",
]


# ──────────────────────────────────────────────
# Pydantic スキーマ
# ──────────────────────────────────────────────
class NodeInput(BaseModel):
    name: str
    x: float
    y: float
    flow_capacity: float | None = Field(default=None, ge=0)
    signal: list[float] | None = None  # 信号現示の青時間リスト（秒）．例: [60,60] → 2現示各60秒

class LinkInput(BaseModel):
    name: str
    start: str
    end: str
    length: float = Field(gt=0)
    free_flow_speed: float = Field(default=20.0, gt=0)
    jam_density: float = Field(default=0.2, gt=0)
    number_of_lanes: int = Field(default=1, ge=1)
    capacity: float | None = Field(default=None, ge=0)  # リンク容量（台/s，リンク全体）．UXsim の capacity_out にマップ．
                                   # None なら FD（速度・密度・車線数）由来の容量のまま
    # この進入リンクが青になる信号現示番号（0 始まり）．複数の現示で青なら list．
    # None（省略）は「その交差点の全現示で青 = 常時通行可能」．UXsim の既定 [0]（現示 0 だけ青）
    # とは違うので，uxsim_bridge.build_world と run_scenario.py の EMIT_TEMPLATE が展開する．
    signal_group: int | list[int] | None = None

class DemandInput(BaseModel):
    orig: str
    dest: str
    t_start: float = Field(ge=0)
    t_end: float = Field(ge=0)
    flow: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_interval(self):
        if self.t_end <= self.t_start:
            raise ValueError(
                f"需要 {self.orig}→{self.dest} の t_end ({self.t_end}) は "
                f"t_start ({self.t_start}) より大きい必要があります")
        return self

class SimulationInput(BaseModel):
    name: str = "sim"
    tmax: int = Field(default=3600, gt=0)
    # 車両集計単位（プラトンサイズ）．UXsim 内部の「1 車両」が deltan 台を表す．
    # フレームの ids の個数はプラトン数なので，台数として扱う箇所では deltan を掛けること
    # （run_uxsim の vehicle_counts / trip_stats / フロントの ACTIVE VEHICLES）．
    deltan: int = Field(default=5, ge=1)
    # 車頭時間（反応時間）秒．UXsim 既定 1.0 → 1 車線容量 ≈ 2,770 台/時（ffs 60km/h, kjam 0.2）．
    # 高速道路の実勢（1,800〜2,000 台/時/車線）に合わせるなら 1.5〜1.7．None なら UXsim 既定．
    reaction_time: float | None = Field(default=None, gt=0)
    # UXsim World の乱数シード（経路選択のノイズ・合流の優先順位に効く）．
    # None なら実行ごとに結果が変わる．条件比較・追試ではシナリオに持たせて全経路で維持する
    # （保存・rerun_simulation・GUI 再実行・scripts/run_scenario.py --emit）．
    random_seed: int | None = None
    nodes: list[NodeInput]
    links: list[LinkInput]
    demands: list[DemandInput]

    @model_validator(mode="after")
    def _normalize_and_validate(self):
        """重複・参照切れを UXsim に渡す前に検出する．

        - 完全一致の重複ノード/リンク（全属性が同じ）は黙って統合
          （「リンクごとにノード行を繰り返す」形式の CSV 等でよくあるため）
        - 同名で属性が異なる場合は，どの名前が問題かを列挙してエラー
        - リンク・需要が存在しないノードを参照している場合もエラー
          （UXsim の KeyError より分かりやすいメッセージにする）
        """
        # ── ノード重複 ──
        seen_nodes: dict[str, NodeInput] = {}
        uniq_nodes = []
        conflicts = []
        for n in self.nodes:
            prev = seen_nodes.get(n.name)
            if prev is None:
                seen_nodes[n.name] = n
                uniq_nodes.append(n)
            elif prev.model_dump() != n.model_dump():
                conflicts.append(n.name)
        if conflicts:
            raise ValueError(
                f"ノード名が重複しています（属性が異なるため自動統合できません）: "
                f"{sorted(set(conflicts))[:10]}．名前を一意にしてください．")
        self.nodes = uniq_nodes

        # ── リンク重複 ──
        seen_links: dict[str, LinkInput] = {}
        uniq_links = []
        conflicts = []
        for lk in self.links:
            prev = seen_links.get(lk.name)
            if prev is None:
                seen_links[lk.name] = lk
                uniq_links.append(lk)
            elif prev.model_dump() != lk.model_dump():
                conflicts.append(lk.name)
        if conflicts:
            raise ValueError(
                f"リンク名が重複しています（属性が異なるため自動統合できません）: "
                f"{sorted(set(conflicts))[:10]}．名前を一意にしてください．")
        self.links = uniq_links

        # ── 参照整合性 ──
        node_names = set(seen_nodes)
        missing = sorted({e for lk in self.links for e in (lk.start, lk.end)
                          if e not in node_names})
        if missing:
            raise ValueError(
                f"リンクが存在しないノードを参照しています: {missing[:10]}．"
                f"ノード定義を追加するか，リンクの start/end を修正してください．")
        missing_d = sorted({e for d in self.demands for e in (d.orig, d.dest)
                            if e not in node_names})
        if missing_d:
            raise ValueError(
                f"需要が存在しないノードを参照しています: {missing_d[:10]}．")
        return self

def scenario_to_input(scenario: dict) -> SimulationInput:
    """dict → SimulationInput．Pydantic 検証エラーを 422 の平易なメッセージに変換する．

    （変換しないと global handler が 500「予期しないエラー」にしてしまい，
    重複ノード名など修正可能な問題がユーザーに伝わらない）
    """
    from pydantic import ValidationError
    try:
        return SimulationInput(**scenario)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            m = str(err.get("msg", ""))
            if m.startswith("Value error, "):
                m = m[len("Value error, "):]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msgs.append(f"{m}" + (f"（{loc}）" if loc else ""))
        raise HTTPException(422, detail="シナリオが不正です: " + " / ".join(msgs[:3]))


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatInput(BaseModel):
    messages: list[ChatMessage]
    # この会話でフロントに表示中のシミュレーションID．
    # LLM へのコンテキスト注入はこの sim だけを対象にする
    # （別会話・アップロード等で作られたグローバル最新の sim を流用しない）．
    last_sim_id: str | None = None
