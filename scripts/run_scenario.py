#!/usr/bin/env python
"""RISU のシナリオを素の UXsim で実行する CLI．

RISU サーバー（FastAPI / LLM）を一切起動せずに，ローカルの Python + UXsim だけで
シミュレーションを回すためのパイプライン．対話で組み立てたシナリオを，論文用の
バッチ実行や独自の後処理につなぐことを想定している．

使い方
------
  # ファイルのシナリオを実行して統計を表示
  python scripts/run_scenario.py scenario.json

  # 起動中の RISU からシナリオを取り出して実行（サーバーは取得にしか使わない）
  python scripts/run_scenario.py --sim-id 6c49d38a

  # UXsim 標準の CSV（車両軌跡・リンク集計）を書き出す
  python scripts/run_scenario.py scenario.json --csv out/

  # RISU にも UXsim にも依存しない，単体で動く Python スクリプトを生成する
  python scripts/run_scenario.py scenario.json --emit run_my_sim.py

入力として受け付ける JSON
------------------------
  * シナリオそのもの        {"nodes": [...], "links": [...], "demands": [...]}
  * RISU のエンベロープ      {"scenario": {...}, "source": {...}}
      GET /results/<id>/scenario，GET /results/<id>，UI の .JSON ダウンロードが
      返す形．いずれも中の "scenario" を自動で取り出す．
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# リポジトリ直下の uxsim_bridge を import できるようにする（scripts/ から実行されるため）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uxsim_bridge import build_world, scenario_from_dict  # noqa: E402

DEFAULT_RISU_URL = "http://localhost:8001"


# ──────────────────────────────────────────────
# シナリオの取得
# ──────────────────────────────────────────────
def load_from_file(path: str) -> dict:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_from_risu(sim_id: str, base_url: str) -> dict:
    """起動中の RISU から軽量エンベロープ（scenario のみ）を取得する．"""
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    url = f"{base_url.rstrip('/')}/results/{sim_id}/scenario"
    try:
        with urlopen(url, timeout=30) as r:  # noqa: S310 (ローカルの自前サーバー)
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 404:
            raise SystemExit(
                f"sim_id '{sim_id}' が見つかりません．RISU の結果ストアは "
                "インメモリなので，サーバーを再起動すると消えます．"
            ) from e
        raise SystemExit(f"RISU からの取得に失敗しました（HTTP {e.code}）: {url}") from e
    except URLError as e:
        raise SystemExit(
            f"RISU に接続できません: {url}\n"
            f"  {e.reason}\n"
            "  サーバーを起動するか，--url で場所を指定してください．"
        ) from e


# ──────────────────────────────────────────────
# 実行
# ──────────────────────────────────────────────
def run(scenario, *, cpp: bool, analysis: bool) -> tuple[object, float]:
    # analyzer の集計を使うには save_mode=1（車両ログの保存）が必要．
    # RISU サーバーは結果を自前で組み立てるので save_mode=0 だが，CLI では素の
    # UXsim として使えることが目的なので，集計を求められたら有効にする．
    W = build_world(
        scenario,
        cpp=cpp,
        disable_basic_analysis=not analysis,
        print_mode=1 if analysis else 0,
        save_mode=1 if analysis else 0,
    )
    t0 = time.perf_counter()
    W.exec_simulation()
    return W, time.perf_counter() - t0


def print_stats(W, scenario, elapsed: float, *, analysis: bool) -> None:
    n_nodes = len(scenario.nodes)
    n_links = len(scenario.links)
    n_dem = len(scenario.demands)
    print(f"\n{'=' * 56}")
    print(f"  {getattr(scenario, 'name', 'sim')}")
    print(f"{'=' * 56}")
    print(f"  ネットワーク : {n_nodes} ノード / {n_links} リンク / {n_dem} 需要")
    print(f"  tmax         : {getattr(scenario, 'tmax', '?')} s   deltan: {getattr(scenario, 'deltan', '?')}"
          f"   reaction_time: {getattr(scenario, 'reaction_time', None) or 'uxsim default'}"
          f"   random_seed: {getattr(scenario, 'random_seed', None)}")
    print(f"  計算時間     : {elapsed:.2f} s")

    if analysis:
        # UXsim 標準の集計（basic_analysis を通した場合のみ意味がある）
        try:
            W.analyzer.print_simple_stats()
        except Exception as e:  # pragma: no cover - uxsim 側の実装依存
            print(f"  （analyzer の統計を取得できませんでした: {e}）")
    else:
        print("  （--analysis を付けると UXsim の集計統計も表示します）")
    print()


def write_csv(W, out_dir: str) -> None:
    """UXsim 標準の CSV 出力（analyzer 経由）．"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    # UXsim は out/ 配下に書く実装なので，カレントを移さず analyzer の API を使う
    written = []
    for meth, fname in (
        ("vehicles_to_pandas", "vehicles.csv"),
        ("link_to_pandas", "links.csv"),
        ("basic_to_pandas", "basic.csv"),
        ("od_to_pandas", "od.csv"),
    ):
        fn = getattr(W.analyzer, meth, None)
        if fn is None:
            continue
        try:
            fn().to_csv(d / fname, index=False, encoding="utf-8-sig")
            written.append(fname)
        except Exception as e:
            print(f"  警告: {fname} を書けませんでした（{e}）")
    print(f"  CSV 出力     : {d}/  ({', '.join(written) if written else 'なし'})")


# ──────────────────────────────────────────────
# 単体スクリプトの生成
# ──────────────────────────────────────────────
EMIT_TEMPLATE = '''#!/usr/bin/env python
"""{name_doc} — RISU が生成した UXsim スクリプト（単体で動く）．

  生成元: {source}
  生成日: {when}

RISU にも uxsim_bridge にも依存しない．uxsim だけあれば動くので，
そのまま編集して実験条件を振ってよい:

    pip install uxsim
    python {filename}
"""

from uxsim import World

# ── シナリオ ──────────────────────────────────
NAME = {name!r}
TMAX = {tmax!r}
DELTAN = {deltan!r}
REACTION_TIME = {reaction_time!r}
RANDOM_SEED = {random_seed!r}      # None なら実行ごとに結果が変わる

# {{"name", "x", "y", "flow_capacity", "signal"}}
NODES = {nodes}

# {{"name", "start", "end", "length", "free_flow_speed", "jam_density",
#   "number_of_lanes", "capacity", "signal_group"}}
LINKS = {links}

# {{"orig", "dest", "t_start", "t_end", "flow"}}
DEMANDS = {demands}


def build():
    kwargs = dict(name=NAME, tmax=TMAX, deltan=DELTAN,
                  print_mode=1, save_mode=1, show_mode=0)
    if REACTION_TIME:
        kwargs["reaction_time"] = float(REACTION_TIME)
    if RANDOM_SEED is not None:
        kwargs["random_seed"] = int(RANDOM_SEED)
    try:
        W = World(**kwargs, cpp=True)   # uxsim >= 1.14 の C++ バックエンド
    except TypeError:
        W = World(**kwargs)

    nodes = {{}}
    for n in NODES:
        extra = {{}}
        if n.get("flow_capacity") is not None:
            extra["flow_capacity"] = n["flow_capacity"]
        if n.get("signal") is not None:
            extra["signal"] = n["signal"]
        nodes[n["name"]] = W.addNode(n["name"], x=n["x"], y=n["y"], **extra)

    # signal_group 省略 = その交差点の全現示で青（常時通行可能）．
    # UXsim の既定 [0] は現示 0 だけ青なので，多現示の信号ノードへ入るリンクは全現示に展開する
    signal_phases = {{n["name"]: len(n["signal"]) for n in NODES
                     if n.get("signal") and len(n["signal"]) > 1}}
    for lk in LINKS:
        extra = {{}}
        if lk.get("signal_group") is not None:
            extra["signal_group"] = lk["signal_group"]
        elif lk["end"] in signal_phases:
            extra["signal_group"] = list(range(signal_phases[lk["end"]]))
        if lk.get("capacity") is not None:
            # RISU の capacity は UXsim の capacity_out（下流端の流出容量）にマップする
            extra["capacity_out"] = lk["capacity"]
        W.addLink(
            lk["name"],
            start_node=nodes[lk["start"]],
            end_node=nodes[lk["end"]],
            length=lk["length"],
            free_flow_speed=lk.get("free_flow_speed", 20.0),
            jam_density=lk.get("jam_density", 0.2),
            number_of_lanes=lk.get("number_of_lanes", 1),
            **extra,
        )

    for d in DEMANDS:
        W.adddemand(orig=nodes[d["orig"]], dest=nodes[d["dest"]],
                    t_start=d["t_start"], t_end=d["t_end"], flow=d["flow"])
    return W


if __name__ == "__main__":
    W = build()
    W.exec_simulation()
    W.analyzer.print_simple_stats()
    # 以降は UXsim の通常の API がそのまま使える:
    #   W.analyzer.macroscopic_fundamental_diagram()
    #   W.analyzer.time_space_diagram_traj_links([["linkA", "linkB"]])
    #   W.analyzer.vehicles_to_pandas().to_csv("vehicles.csv", index=False)
'''


def _doc_safe(value) -> str:
    r"""生成スクリプトの docstring に安全に埋め込める文字列にする．

    Windows のパス（C:\Users\...）をそのまま docstring に入れると，\U が
    unicode エスケープとして解釈されて生成物が SyntaxError になる．
    三重引用符による docstring の打ち切りも防ぐ．
    """
    return str(value).replace("\\", "\\\\").replace('"""', "'''")


def _fmt_list(rows: list[dict], indent: int = 4) -> str:
    """dict のリストを 1 行 1 要素で整形する（生成コードを読めるように）．"""
    if not rows:
        return "[]"
    pad = " " * indent
    body = ",\n".join(pad + json.dumps(r, ensure_ascii=False) for r in rows)
    return "[\n" + body + ",\n]"


def emit_python(raw_scenario: dict, out_path: str, source: str) -> None:
    sc = raw_scenario.get("scenario", raw_scenario) if isinstance(raw_scenario, dict) else raw_scenario
    text = EMIT_TEMPLATE.format(
        name=sc.get("name", "sim"),
        name_doc=_doc_safe(sc.get("name", "sim")),
        tmax=sc.get("tmax", 3600),
        deltan=sc.get("deltan", 5),
        reaction_time=sc.get("reaction_time"),
        random_seed=sc.get("random_seed"),
        nodes=_fmt_list(sc["nodes"]),
        links=_fmt_list(sc["links"]),
        demands=_fmt_list(sc["demands"]),
        source=_doc_safe(source),
        when=time.strftime("%Y-%m-%d %H:%M:%S"),
        filename=_doc_safe(Path(out_path).name),
    )
    Path(out_path).write_text(text, encoding="utf-8")
    n = len(text.encode("utf-8"))
    print(f"  スクリプト生成: {out_path}  ({n / 1024:.1f} KB)")
    print(f"                  python {out_path}  で単体実行できます")


# ──────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_scenario.py",
        description="RISU のシナリオを素の UXsim で実行する（サーバー不要）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("使い方\n------\n")[1].split("入力として")[0],
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("scenario", nargs="?", help="シナリオ JSON のパス（- で標準入力）")
    src.add_argument("--sim-id", help="起動中の RISU から取得する sim_id")

    ap.add_argument("--url", default=DEFAULT_RISU_URL,
                    help=f"RISU の URL（既定: {DEFAULT_RISU_URL}）")
    ap.add_argument("--emit", metavar="OUT.py",
                    help="単体で動く UXsim スクリプトを生成する（実行はしない）")
    ap.add_argument("--csv", metavar="DIR", help="UXsim 標準の CSV を書き出す")
    ap.add_argument("--analysis", action="store_true",
                    help="UXsim の basic_analysis を有効にして集計統計も出す"
                         "（ノード数が多いと重い: 全点対最短路 O(N^3)）")
    ap.add_argument("--no-cpp", action="store_true",
                    help="C++ バックエンドを使わず純 Python で実行する")
    args = ap.parse_args(argv)

    # ── 取得 ──
    if args.sim_id:
        raw = load_from_risu(args.sim_id, args.url)
        source = f"{args.url}/results/{args.sim_id}/scenario"
    else:
        raw = load_from_file(args.scenario)
        source = args.scenario

    # ── 生成のみ ──
    if args.emit:
        emit_python(raw, args.emit, source)
        return 0

    # ── 実行 ──
    try:
        scenario = scenario_from_dict(raw)
    except (ValueError, TypeError) as e:
        raise SystemExit(f"シナリオを読めません: {e}") from e

    # CSV 出力には analyzer が要る
    analysis = args.analysis or bool(args.csv)
    W, elapsed = run(scenario, cpp=not args.no_cpp, analysis=analysis)
    print_stats(W, scenario, elapsed, analysis=analysis)
    if args.csv:
        write_csv(W, args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
