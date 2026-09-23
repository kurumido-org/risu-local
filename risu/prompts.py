"""LLM に渡す固定文字列: SYSTEM_PROMPT，CLAUDE_TOOLS（ツール定義），mock 用シナリオ．
system に動的な文字列を足さないこと（CLAUDE.md §3.4）．
"""

from __future__ import annotations

# モジュール外から使う名前（他モジュール・server.py・scripts・tests）．これ以外は内部実装．
__all__ = [
    "CLAUDE_TOOLS",
    "MOCK_SCENARIOS",
    "SYSTEM_PROMPT",
    "mock_llm_response",
]

# ---- Chat エンドポイント ----
# LLM_BACKEND 環境変数で切り替え: "mock" / "claude" / "ollama"

SYSTEM_PROMPT = """私はRISUです．交通流シミュレーター UXsim を対話的に操作するアシスタントです．
一人称は常に「RISU」を使います．

【最重要ルール】
ユーザーがシミュレーションの実行を求めている場合（ネットワーク作成，渋滞シミュレーション，交通シナリオなど），
「実行します」「作成します」と言うだけでなく，必ずその場で run_simulation ツールを呼び出してください．
テキストだけで応答してツール呼び出しを省略することは絶対にしないでください．

【ツール呼び出しの指針】
- ノード座標の単位はメートル（例: x=0, y=0 ～ x=5000, y=5000）
- flow の単位は台/秒（例: 0.5 = 1秒に0.5台）
- 知らないネットワーク名を求められた場合でも，妥当な仮定でシナリオを構築してツールを呼び出すこと

【リンクの方向ルール（必須）】
- すべての link は有向リンク（start → end の一方向）として扱われる
- 双方向道路は必ず "2 本の並行有向リンク" として表現すること
- 推奨: A→B と B→A を両方明示的に作成する
- 一方通行を表現したい場合のみ片方向のリンクだけを作る

【容量・ボトルネックの表現】
- リンクの容量を明示したい場合は link の capacity（台/秒，リンク全体）を指定する
  例: {"name": "r1", "start": "A", "end": "B", "length": 2000, "capacity": 0.5}
- capacity 指定時は下流端の流出容量として作用し，渋滞の待ち行列はそのリンク上に形成される
- 【注意】リンクの終点ノードがそのまま目的地（demand の dest）の場合，capacity は作用しない
  （車両は境界を通過せず到着・消滅する）．ボトルネックを見せたい場合は
  その下流にもう 1 本リンクを置き，目的地を先に延ばすこと
- ユーザーが「容量 1800 台/時」のように台/時で言った場合は 3600 で割って台/秒に変換する（1800台/時 = 0.5台/秒）
- ノードの flow_capacity は「交差点の処理能力」を表す（全流入リンク合計の流出容量）．
  特定の道路のボトルネックは link capacity，交差点のボトルネックは node flow_capacity を使い分ける

【信号制御】
UXsim は交差点ノードに信号制御を設定できる．2 つのパラメータで記述する:

■ ノード側: signal パラメータ（各現示の青時間リスト）
  - signal: [60, 60]  → 2現示，各60秒青 → サイクル長120秒
  - signal: [30, 10, 50, 5]  → 4現示，サイクル長95秒
  - signal を省略 or null → 信号なし（常時通行可能）

■ リンク側: signal_group パラメータ（どの現示で青になるか）
  - signal_group: 0  → signal[0] の現示で通行可能
  - signal_group: 1  → signal[1] の現示で通行可能
  - signal_group を省略 → 信号に関係なく常時通行可能（退出リンクはこれ）

■ 使い方のルール
  - signal はノード（交差点）に設定する．signal_group は進入リンクに設定する
  - 退出リンク（交差点→外部）には signal_group を付けない（常時通行可能）
  - 同じ signal_group の進入リンクは同じ現示で同時に青になる
  - 典型例: 東西方向 signal_group=0，南北方向 signal_group=1

■ 4枝交差点の例（2現示，東西青/南北青）
  nodes:
    {"name": "I", "x": 0, "y": 0, "signal": [60, 60]}  ← サイクル120秒
  links (進入リンクのみ signal_group を設定):
    {"name": "EI", "start": "E", "end": "I", "signal_group": 0}  ← phase 0 で青
    {"name": "WI", "start": "W", "end": "I", "signal_group": 0}  ← phase 0 で青
    {"name": "SI", "start": "S", "end": "I", "signal_group": 1}  ← phase 1 で青
    {"name": "NI", "start": "N", "end": "I", "signal_group": 1}  ← phase 1 で青
    {"name": "IE", "start": "I", "end": "E"}  ← 退出: signal_group なし
    {"name": "IW", "start": "I", "end": "W"}  ← 退出: signal_group なし

■ ユーザーが「信号をつけて」「信号制御して」と言った場合
  - まず交差点ノードに signal パラメータを追加する
  - 進入リンクに signal_group を割り当てる（対向方向は同じ group）
  - 青時間はユーザーの指示に従う．指示がなければ均等（例: [60, 60]）にする

【既存ネットワークの修正・再実行（最重要ルール）】
- 直前のシミュレーション（sim_id は【現在のコンテキスト】に記載）のネットワークを
  修正して再実行する場合は，必ず rerun_simulation を使う
- rerun_simulation はサーバーに保存されたシナリオへ「差分命令」だけを適用する．
  ネットワーク全体（nodes/links）を run_simulation で再送してはいけない．
  特に OSM 取込・ファイルアップロード由来の大規模ネットワークでは，
  再送するとサイズ超過で必ず失敗する
- run_simulation を使うのは「ゼロから新しいネットワークを設計する」ときだけ
- 格子状（グリッド）ネットワークは nodes/links を列挙せず run_simulation の grid テンプレート
  {"grid":{"nx":5,"ny":5,"spacing":500}} を使う（サーバーが展開する．ノード名 n{i}_{j}，
  リンク名 n0_0-n1_0 形式）．需要も auto_demands（random / boundary）で生成できる．
  例: run_simulation({"grid":{"nx":5,"ny":5,"spacing":500},"auto_demands":{"strategy":"boundary"},"tmax":3600})
- シナリオ比較（容量変更前後など）も rerun_simulation を複数回呼べばよい．
  各実行の sim_id が返るので，get_simulation_data でそれぞれの結果を取得して比較する
- 過去の結果（「前回の」「保存してある」）を参照するときは list_simulations で一覧を取り，sim_id を確かめる
- シミュレーションは確率的（経路選択ノイズ・合流順）で，同じ入力でも実行ごとに結果が変わる．
  条件比較や追試では random_seed を固定する（run_simulation の random_seed，または
  rerun_simulation の {"action":"set_params","random_seed":42}）．seed は保存シナリオに残るので，
  同じ base から派生させる限り以降の rerun でも同じ seed が使われる．
  差の解釈では「シード違いによる揺らぎ」の可能性も述べる
- 台数の単位: 統計と network_vehicle_count は実台数（deltan 換算済み）．
  「1 プラトン = deltan 台」なので，フレームの点数をそのまま台数と呼ばないこと
- ネットワークの中身（ノード名・リンク名・構造）が必要なときは get_network_info で照会する:
  - まず include="summary" で規模・座標範囲・次数上位ノード・名前のサンプルを把握
  - 特定の名前が必要なら include="nodes"/"links" + name_contains / limit / offset で絞り込む
  - 全件を取得しようとしないこと（上限 200 件/回．要約と絞り込みで足りるはず）
- 「ランダムに OD を作って」「需要を自動生成して」と言われたら，ノード名を調べる必要はない．
  rerun_simulation の generate_demands アクションでサーバー側に生成させる:
  {"action":"generate_demands","strategy":"random","n_pairs":10,"flow_per_pair":0.2,"clear_existing":true}
  周縁ノード間の現実的な通過交通なら strategy="boundary" を使う
- 「時差出勤」「ピークを分散」と言われたら rerun_simulation の shift_demands アクションを使う:
  {"action":"shift_demands","t_from":3600,"t_to":10800,"fraction":0.3,"shift_s":-3600}
  （時刻はシミュレーション開始からの秒．前倒しと後ろ倒しに分けるなら 2 つ並べる）

【OSM（OpenStreetMap）連携】
- ユーザーが実在の地名・場所・駅名・ランドマーク等を言及した場合，import_osm_network ツールを使う
  例: 「東京駅周辺」「渋谷の道路」「大阪城公園あたり」「新宿駅」
- import_osm_network は地名を自動でジオコーディングし，道路ネットワークをダウンロードする
- distance_m パラメータで範囲を制御する（デフォルト500m）．ユーザーの要望に応じて調整する
  - 「広い範囲」→ 1000〜2000m，「狭い範囲」「駅前だけ」→ 200〜300m
- road_types パラメータで取得する道路の種類を制御する:
  - major: 高速道路・国道級のみ（都市間・広域シミュレーション向け）
  - arterial: 幹線道路まで（都市スケールの標準）
  - drive: 一般車道（デフォルト．住宅街の道路含む，サービス道路除外）
  - all: 全車道（駐車場内通路等も含む．最も細かいが最も重い）
- 【重要】distance_m が 1000 以上のときは road_types を "arterial" か "major" にすること．
  細街路込みで広範囲を取得するとノード数が数千を超え，計算がタイムアウトする．
  ユーザーが「主要道路」「幹線道路」「大きい道路だけ」と言った場合も major / arterial を使う
- 取得後は自動的にダミー需要でシミュレーションが実行される
- ユーザーが範囲や需要を調整したい場合は対話的にヒアリングしてよい
- 結果は地図として表示される

【グラフ機能】
- ユーザーがグラフ・チャート・分析・可視化を求めた場合，get_simulation_data ツールを呼んでデータを取得する
- データを受け取ったら，ユーザーの要望に合った Chart.js 設定を ```chart ... ``` コードブロックで出力する
- Chart.js設定は完全なJSON: {type, data: {labels, datasets}, options} 形式
- 【必須】get_simulation_data の配列は書き写さず参照で指定する（出力トークン節約）:
  {"$data":"time_labels"} / {"$data":"network_avg_speed"} / {"$data":"network_vehicle_count"} / {"$data":"network_completed_count"} /
  {"$data":"link_speeds.<リンク名>"} / {"$data":"speed_histogram.labels"} / {"$data":"speed_histogram.counts"}
  複数の sim を比較するときは {"$data":"network_avg_speed","sim_id":"xxxxxxxx"} と sim_id を付ける
- 例: ```chart\n{"type":"line","data":{"labels":{"$data":"time_labels"},"datasets":[{"label":"平均速度","data":{"$data":"network_avg_speed"},"borderColor":"#0d9668"}]}}\n```
- 差分・比率など加工した値が必要なときだけ数値を直接書く
- どんな種類のグラフでも自由に作成できる（折れ線，棒，散布図，レーダー等）
- 複数のグラフを返す場合は複数の ```chart ブロックを使う
- データの加工・計算・フィルタリングは自由に行ってよい（平均，差分，比率，累積など）

【ファイル添付】
- ユーザーがCSV/JSONファイルを添付した場合，メッセージ内にパース結果が含まれる
- RISU CSV形式のパース結果にはnodes/links/demandsがJSON形式で含まれるので，そのまま run_simulation に渡すこと
- ユーザーのメッセージも確認し，パラメータ変更の要望があれば反映すること

【結果の説明】
結果を説明する際は，渋滞箇所・平均旅行時間・完了率などを分かりやすく日本語で解説してください．
常に「RISUが〜しました」のように一人称で話してください．"""

# ── モック用シナリオ定義 ──
MOCK_SCENARIOS = {
    "bottleneck": {
        "scenario": {
            "name": "bottleneck",
            "tmax": 2000,
            "deltan": 5,
            "nodes": [
                {"name": "start", "x": 0, "y": 0},
                {"name": "neck",  "x": 5000, "y": 0, "flow_capacity": 0.4},
                {"name": "goal",  "x": 7500, "y": 0},
            ],
            "links": [
                {"name": "road1", "start": "start", "end": "neck",  "length": 5000},
                {"name": "road2", "start": "neck",  "end": "goal",  "length": 2500, "free_flow_speed": 10},
            ],
            "demands": [
                {"orig": "start", "dest": "goal", "t_start": 0, "t_end": 600, "flow": 0.8},
            ],
        },
        "description": (
            "ボトルネック道路のシミュレーションを実行しました．\n\n"
            "【シナリオ】\n"
            "・start → neck → goal の3ノード直線道路\n"
            "・neck ノードの流出容量を 0.4 台/秒に制限（ボトルネック）\n"
            "・road2 の自由流速度を 10 m/s に低下\n"
            "・0〜600秒に 0.8 台/秒の需要を投入\n\n"
            "【結果の見方】\n"
            "・タイムスライダーを動かすと，時間経過に伴う速度変化が確認できます\n"
            "・緑=自由流（スムーズ），赤=渋滞を示します\n"
            "・ボトルネック手前（road1）で渋滞が発生し，速度が低下している様子が観察できます"
        ),
    },
    "grid": {
        "scenario": {
            "name": "grid_3x3",
            "tmax": 3000,
            "deltan": 5,
            "nodes": [
                {"name": "n00", "x": 0,    "y": 0},
                {"name": "n10", "x": 2000, "y": 0},
                {"name": "n20", "x": 4000, "y": 0},
                {"name": "n01", "x": 0,    "y": 2000},
                {"name": "n11", "x": 2000, "y": 2000},
                {"name": "n21", "x": 4000, "y": 2000},
                {"name": "n02", "x": 0,    "y": 4000},
                {"name": "n12", "x": 2000, "y": 4000},
                {"name": "n22", "x": 4000, "y": 4000},
            ],
            "links": [
                # 水平方向（→）
                {"name": "h00",  "start": "n00", "end": "n10", "length": 2000},
                {"name": "h10",  "start": "n10", "end": "n20", "length": 2000},
                {"name": "h01",  "start": "n01", "end": "n11", "length": 2000},
                {"name": "h11",  "start": "n11", "end": "n21", "length": 2000},
                {"name": "h02",  "start": "n02", "end": "n12", "length": 2000},
                {"name": "h12",  "start": "n12", "end": "n22", "length": 2000},
                # 水平方向（←）
                {"name": "h00r", "start": "n10", "end": "n00", "length": 2000},
                {"name": "h10r", "start": "n20", "end": "n10", "length": 2000},
                {"name": "h01r", "start": "n11", "end": "n01", "length": 2000},
                {"name": "h11r", "start": "n21", "end": "n11", "length": 2000},
                {"name": "h02r", "start": "n12", "end": "n02", "length": 2000},
                {"name": "h12r", "start": "n22", "end": "n12", "length": 2000},
                # 垂直方向（↑）
                {"name": "v00",  "start": "n00", "end": "n01", "length": 2000},
                {"name": "v10",  "start": "n10", "end": "n11", "length": 2000},
                {"name": "v20",  "start": "n20", "end": "n21", "length": 2000},
                {"name": "v01",  "start": "n01", "end": "n02", "length": 2000},
                {"name": "v11",  "start": "n11", "end": "n12", "length": 2000},
                {"name": "v21",  "start": "n21", "end": "n22", "length": 2000},
                # 垂直方向（↓）
                {"name": "v00r", "start": "n01", "end": "n00", "length": 2000},
                {"name": "v10r", "start": "n11", "end": "n10", "length": 2000},
                {"name": "v20r", "start": "n21", "end": "n20", "length": 2000},
                {"name": "v01r", "start": "n02", "end": "n01", "length": 2000},
                {"name": "v11r", "start": "n12", "end": "n11", "length": 2000},
                {"name": "v21r", "start": "n22", "end": "n21", "length": 2000},
            ],
            "demands": [
                {"orig": "n00", "dest": "n22", "t_start": 0, "t_end": 800, "flow": 0.5},
                {"orig": "n02", "dest": "n20", "t_start": 0, "t_end": 800, "flow": 0.3},
                {"orig": "n20", "dest": "n02", "t_start": 200, "t_end": 600, "flow": 0.4},
            ],
        },
        "description": (
            "3×3 グリッドネットワークのシミュレーションを実行しました．\n\n"
            "【シナリオ】\n"
            "・9ノード（3×3格子），24リンク（全道路双方向）のグリッド道路網\n"
            "・3つの OD 需要: 左下→右上，左上→右下，右下→左上\n"
            "・交差点（n11）付近で交通が集中\n\n"
            "【結果の見方】\n"
            "・双方向リンクは円弧で表示されます（進行方向の右側に膨らむ）\n"
            "・中央の交差点付近のリンクが赤くなり，混雑が確認できます\n"
            "・タイムスライダーで需要投入前後の変化を観察してください"
        ),
    },
    "default": {
        "scenario": {
            "name": "simple_road",
            "tmax": 1500,
            "deltan": 5,
            "nodes": [
                {"name": "A", "x": 0, "y": 0},
                {"name": "B", "x": 3000, "y": 1000},
                {"name": "C", "x": 6000, "y": 0},
            ],
            "links": [
                {"name": "AB", "start": "A", "end": "B", "length": 3200},
                {"name": "BC", "start": "B", "end": "C", "length": 3200},
            ],
            "demands": [
                {"orig": "A", "dest": "C", "t_start": 0, "t_end": 500, "flow": 0.6},
            ],
        },
        "description": (
            "シミュレーションを実行しました．\n\n"
            "【シナリオ】\n"
            "・A → B → C の3ノード道路\n"
            "・0〜500秒に 0.6 台/秒の需要\n\n"
            "【結果の見方】\n"
            "・タイムスライダーで速度の時間変化を確認できます\n"
            "・緑=スムーズ，赤=渋滞です"
        ),
    },
}

def mock_llm_response(user_text: str) -> dict:
    """キーワードマッチでシナリオ選択 or テキスト応答を返す"""
    text = user_text.lower()

    # シミュレーション不要な質問
    greetings = ["hello", "こんにちは", "はじめ", "ありがとう", "thanks"]
    if any(g in text for g in greetings):
        return {"content": "こんにちは！交通流シミュレーター UXsim のアシスタントです．\n\nシミュレーションしたいシナリオを入力してください．例えば：\n・「ボトルネック道路を作って」\n・「3×3 グリッドネットワーク」\n・「渋滞をシミュレーションして」\n\nお気軽にどうぞ！", "scenario_key": None}

    info_keywords = ["教えて", "説明", "とは", "仕組み", "条件"]
    if any(k in text for k in info_keywords) and not any(k in text for k in ["作って", "シミュレ", "実行"]):
        return {"content": "交通流の渋滞は，道路の容量を超える交通需要が発生したときに起こります．\n\n主な要因：\n・ボトルネック（車線減少，合流部）\n・交通需要の集中（ラッシュアワー）\n・信号制御の不適切な設定\n\n実際にシミュレーションで確認してみましょう！\n「ボトルネック道路を作って」と入力してみてください．", "scenario_key": None}

    # シナリオ選択
    if any(k in text for k in ["ボトルネック", "bottleneck", "単純", "渋滞"]):
        return {"content": None, "scenario_key": "bottleneck"}
    if any(k in text for k in ["グリッド", "grid", "格子", "3×3", "3x3"]):
        return {"content": None, "scenario_key": "grid"}

    # デフォルト: シミュレーション実行
    return {"content": None, "scenario_key": "default"}


# ── Claude ツール定義 ──
CLAUDE_TOOLS = [
    {
        "name": "run_simulation",
        "description": (
            "UXsim 交通流シミュレーションを実行する．"
            "ノード（交差点），リンク（道路），需要（交通量）を指定する．"
            "座標の単位はメートル，flow の単位は台/秒．"
            "格子状ネットワークは nodes/links を列挙せず grid テンプレートで指定すること"
            "（サーバー側で展開．ノード名は n{i}_{j}，リンク名は n0_0-n1_0 形式で返る）．"
            "OD をランダム/周縁に自動生成したいときは auto_demands を使う（demands は省略可）．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":    {"type": "string", "description": "シミュレーション名"},
                "tmax":    {"type": "integer", "description": "シミュレーション終了時刻（秒）．デフォルト2000"},
                "deltan":  {"type": "integer", "description": "車両集計単位（デフォルト5）"},
                "reaction_time": {"type": "number", "description": "車頭時間（秒）．省略で UXsim 既定 1.0（1 車線 ≈ 2,770 台/時）．実勢容量 1,800〜2,000 台/時/車線なら 1.5〜1.7"},
                "random_seed": {"type": "integer", "description": "シミュレーション本体の乱数シード．条件比較・追試で結果を固定したいときに指定（省略で毎回変わる）"},
                "grid": {
                    "type": "object",
                    "description": "格子ネットワークをサーバー側で生成する（nodes/links の列挙不要）",
                    "properties": {
                        "nx": {"type": "integer", "description": "横方向のノード数"},
                        "ny": {"type": "integer", "description": "縦方向のノード数（省略で nx）"},
                        "spacing": {"type": "number", "description": "ノード間隔＝リンク長（m）．デフォルト500"},
                        "bidirectional": {"type": "boolean", "description": "双方向道路にする（デフォルト true）"},
                        "free_flow_speed": {"type": "number"},
                        "number_of_lanes": {"type": "integer"},
                        "capacity": {"type": "number"},
                        "name_prefix": {"type": "string", "description": "ノード名の接頭辞（デフォルト n）"},
                    },
                    "required": ["nx"],
                },
                "auto_demands": {
                    "type": "object",
                    "description": "OD 需要の自動生成．strategy=random（ランダムなノードペア）/ boundary（周縁ノード全ペア）",
                    "properties": {
                        "strategy": {"type": "string", "enum": ["random", "boundary"]},
                        "n_pairs": {"type": "integer", "description": "random のペア数（デフォルト10）"},
                        "flow_per_pair": {"type": "number", "description": "ペアあたり流率（台/秒）"},
                        "flow_total": {"type": "number", "description": "合計流率（台/秒，flow_per_pair の代わり）"},
                        "seed": {"type": "integer"},
                        "t_start": {"type": "number"},
                        "t_end": {"type": "number", "description": "デフォルト tmax の半分"},
                    },
                },
                "nodes": {
                    "type": "array",
                    "description": "交差点リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "x":    {"type": "number", "description": "X座標（メートル）"},
                            "y":    {"type": "number", "description": "Y座標（メートル）"},
                            "flow_capacity": {"type": "number", "description": "ノード流出容量（台/秒）．省略で無制限"},
                            "signal": {
                                "type": "array",
                                "items": {"type": "number"},
                                "description": "信号現示の青時間リスト（秒）．例: [60,60]→2現示各60秒．省略=信号なし",
                            },
                        },
                        "required": ["name", "x", "y"],
                    },
                },
                "links": {
                    "type": "array",
                    "description": "道路リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":             {"type": "string"},
                            "start":            {"type": "string", "description": "始点ノード名"},
                            "end":              {"type": "string", "description": "終点ノード名"},
                            "length":           {"type": "number", "description": "道路長（メートル）"},
                            "free_flow_speed":  {"type": "number", "description": "自由流速度（m/s）．デフォルト20"},
                            "jam_density":      {"type": "number", "description": "渋滞密度（台/m）．デフォルト0.2"},
                            "number_of_lanes":  {"type": "integer", "description": "車線数．デフォルト1"},
                            "capacity":         {"type": "number", "description": "リンク容量（台/秒，リンク全体）．ボトルネックの明示表現に使う（例: 0.5）．省略時は速度・密度・車線数から決まる容量"},
                            "signal_group":     {"type": "integer", "description": "この進入リンクが青になる信号現示番号（0始まり）．退出リンクには不要．省略=その交差点の全現示で青（常時通行可能）"},
                        },
                        "required": ["name", "start", "end", "length"],
                    },
                },
                "demands": {
                    "type": "array",
                    "description": "交通需要リスト",
                    "items": {
                        "type": "object",
                        "properties": {
                            "orig":    {"type": "string", "description": "出発ノード名"},
                            "dest":    {"type": "string", "description": "到着ノード名"},
                            "t_start": {"type": "number", "description": "需要開始時刻（秒）"},
                            "t_end":   {"type": "number", "description": "需要終了時刻（秒）"},
                            "flow":    {"type": "number", "description": "交通量（台/秒）"},
                        },
                        "required": ["orig", "dest", "t_start", "t_end", "flow"],
                    },
                },
            },
            "required": [],
        },
    },
    {
        "name": "rerun_simulation",
        "description": (
            "保存済みシミュレーション（base_sim_id）のネットワークを起点に，"
            "小さな修正（modifications）を適用して再実行する．"
            "OSM 取込やファイルアップロードで作られた既存ネットワークの調整・比較は"
            "【必ず】このツールを使うこと．ネットワーク全体を run_simulation で"
            "再送してはいけない（大規模ネットワークではサイズ超過になる）．"
            "modifications の例:\n"
            '・リンク容量変更: {"action":"update_links","names":["r1"],"set":{"capacity":0.5}}\n'
            '・全リンク速度変更: {"action":"update_links","all":true,"set":{"free_flow_speed":15}}\n'
            '・部分一致: {"action":"update_links","name_contains":"link_1","set":{"number_of_lanes":2}}\n'
            '・信号設置: {"action":"update_nodes","names":["I1"],"set":{"signal":[60,60]}}\n'
            '・需要 1.5 倍: {"action":"update_demands","all":true,"scale_flow":1.5}\n'
            '・需要追加: {"action":"add_demand","demand":{"orig":"A","dest":"B","t_start":0,"t_end":1800,"flow":0.3}}\n'
            '・リンク削除（通行止め）: {"action":"remove_links","names":["r2"]}\n'
            '・ノード/リンク追加: {"action":"add_node","node":{...}} / {"action":"add_link","link":{...}}\n'
            '・時間変更: {"action":"set_tmax","tmax":7200}\n'
            '・全体パラメータ: {"action":"set_params","reaction_time":1.7,"random_seed":42}\n'
            '  （tmax / deltan / reaction_time / random_seed．シードは保存シナリオに残るので，'
            '同条件比較では base と同じ seed のまま差分だけ当てればよい）\n'
            '・OD 自動生成: {"action":"generate_demands","strategy":"random","n_pairs":10,'
            '"flow_per_pair":0.2,"clear_existing":true}\n'
            '  （strategy: random=ランダムなノードペア / boundary=ネットワーク周縁の全ペア．'
            'seed で再現可，flow_total で合計流率指定も可．ノード名を知らなくても使える）\n'
            '・需要の時間シフト（時差出勤）: {"action":"shift_demands","t_from":3600,"t_to":10800,'
            '"fraction":0.3,"shift_s":-3600}\n'
            '  （時間帯 [t_from,t_to) の需要の fraction を shift_s 秒ずらす．前倒しは負，後ろ倒しは正．'
            '2 方向に分けるなら 2 回指定）\n'
            "modifications: [] で無修正の再実行（tmax だけ変える等）も可能．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "base_sim_id": {"type": "string", "description": "起点となるシミュレーション ID"},
                "modifications": {
                    "type": "array",
                    "description": "修正命令の配列（description の例を参照）．各要素は action フィールドを持つ",
                    "items": {"type": "object"},
                },
                "tmax": {"type": "integer", "description": "シミュレーション時間の上書き（秒，省略可）"},
                "name": {"type": "string", "description": "新しいシミュレーション名（省略可）"},
            },
            "required": ["base_sim_id", "modifications"],
        },
    },
    {
        "name": "get_network_info",
        "description": (
            "保存済みシミュレーションのネットワーク構造（ノード・リンク・需要）を照会する．"
            "大規模ネットワークはチャットに全体が渡らないため，ノード名やリンク名が"
            "必要な操作（OD 設定・信号設置・特定リンクの修正など）の前に，"
            "このツールで必要な分だけ調べる．"
            "include='summary' で規模・座標範囲・次数上位ノード・名前のサンプルを取得．"
            "include='nodes'/'links'/'demands' で一覧（limit/offset でページング，"
            "name_contains で絞り込み）．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sim_id": {"type": "string", "description": "シミュレーション ID"},
                "include": {"type": "string", "enum": ["summary", "nodes", "links", "demands"],
                            "description": "取得内容．デフォルト summary"},
                "name_contains": {"type": "string", "description": "名前の部分一致フィルタ（nodes/links 用）"},
                "limit": {"type": "integer", "description": "最大件数（デフォルト 50，上限 200）"},
                "offset": {"type": "integer", "description": "ページングオフセット"},
            },
            "required": ["sim_id"],
        },
    },
    {
        "name": "get_simulation_data",
        "description": (
            "シミュレーション結果の集計データを取得する．"
            "ユーザーがグラフ・チャート・分析を求めた場合に呼び出す．"
            "返されるデータ: time_labels, network_avg_speed（走行中車両の台数重み平均）, "
            "network_vehicle_count, network_entered_count, network_completed_count, "
            "link_speeds(混雑度上位リンク別), speed_histogram, stats．"
            "チャートでは配列を書き写さず {\"$data\":\"network_avg_speed\"} のような参照を使う"
            "（サーバーが実データに置換する）．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sim_id": {"type": "string", "description": "シミュレーションID"},
                "points": {"type": "integer", "description": "時系列のサンプル点数（デフォルト 30，上限 60）"},
                "max_links": {"type": "integer", "description": "link_speeds に含めるリンク数（デフォルト 20，上限 50）．リンク別の分析が不要なら 0"},
            },
            "required": ["sim_id"],
        },
    },
    {
        "name": "list_simulations",
        "description": (
            "サーバーにあるシミュレーション結果の一覧（sim_id・名前・作成日時・出所・規模・統計）を新しい順に返す．"
            "「前回の結果」「さっきのシミュレーション」「保存してある結果」など過去の結果を参照するとき，"
            "compare_simulations や rerun_simulation に渡す sim_id を探すときに使う．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "件数（デフォルト 20，上限 100）"},
            },
        },
    },
    {
        "name": "import_osm_network",
        "description": (
            "OpenStreetMap から実在の道路ネットワークをダウンロードしてシミュレーションを実行する．"
            "地名・ランドマーク名を指定すると，自動でジオコーディングし，周辺の道路を取得する．"
            "例: '東京駅', 'Shibuya Station', '大阪城公園', 'Times Square, New York' など．"
            "取得後，自動的にダミー需要を設定してシミュレーションを実行する．"
            "結果はブラウザ上に地図として表示される．"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "地名・ランドマーク名・住所（日本語・英語どちらも可）",
                },
                "distance_m": {
                    "type": "integer",
                    "description": "中心からの取得半径（メートル）．デフォルト500．大きいほど広い範囲だが処理に時間がかかる．100〜2000が推奨．",
                },
                "road_types": {
                    "type": "string",
                    "enum": ["major", "arterial", "drive", "all"],
                    "description": (
                        "取得する道路の種類．"
                        "major=高速道路・国道級のみ / arterial=幹線道路まで / "
                        "drive=一般車道（デフォルト，住宅街の道路含む） / "
                        "all=サービス道路・駐車場内通路含む全車道．"
                        "半径 1000m 以上では major か arterial を推奨"
                        "（ノード数が減り計算が大幅に速くなる）．"
                    ),
                },
                "tmax": {
                    "type": "integer",
                    "description": "シミュレーション時間（秒）．デフォルト3600",
                },
            },
            "required": ["place"],
        },
    }
]
