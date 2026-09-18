#!/usr/bin/env python
"""RISU の後処理・直列化パイプラインのベンチマーク．

CLAUDE.md の「パフォーマンス上の前提」を壊していないか確認するためのもの．
格子ネットワークを合成して実行し，各段階の所要時間とペイロードサイズを出す．

    python scripts/bench.py                # 既定: 10x10, 20x20, 40x40
    python scripts/bench.py --sizes 20     # 20x20 だけ
    python scripts/bench.py --sizes 20 --profile   # cProfile 付き

出力の読み方:
    exec      UXsim 本体の計算時間（RISU の管轄外）
    post      _run_uxsim の後処理（フレーム収集・GeoJSON 生成）
    json      /results のエンベロープ直列化（orjson，columnar_v3 への量子化を含む）
    gz        gzip 圧縮（RESULTS_GZIP_LEVEL）
    simdata   _get_simulation_data（LLM へ渡す集計）

json / gz のサイズはブラウザが受け取る量そのもの．ここが増えると
JSON.parse とメモリが効いてくるので，大きく変わったら原因を確認すること．
"""

from __future__ import annotations

import argparse
import io
import os
import random
import sys
import time
from pathlib import Path

# リポジトリ直下を import パスに入れる（どこから起動しても動くように）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LLM_BACKEND", "mock")

import server  # noqa: E402


def grid_scenario(n: int, tmax: int = 3600, demands: int = 200, seed: int = 0) -> dict:
    """n×n の格子ネットワーク（全リンク双方向）とランダム OD を作る．"""
    rng = random.Random(seed)
    nodes = [
        {"name": f"N{i}_{j}", "x": i * 500.0, "y": j * 500.0}
        for i in range(n)
        for j in range(n)
    ]
    links = []

    def add(a: str, b: str) -> None:
        links.append({
            "name": f"{a}-{b}", "start": a, "end": b, "length": 500,
            "free_flow_speed": 20, "jam_density": 0.2, "number_of_lanes": 1,
        })

    for i in range(n):
        for j in range(n):
            a = f"N{i}_{j}"
            if i + 1 < n:
                b = f"N{i + 1}_{j}"
                add(a, b)
                add(b, a)
            if j + 1 < n:
                b = f"N{i}_{j + 1}"
                add(a, b)
                add(b, a)

    names = [x["name"] for x in nodes]
    dems = []
    for _ in range(demands):
        o, d = rng.sample(names, 2)
        dems.append({"orig": o, "dest": d, "t_start": 0, "t_end": tmax * 0.6, "flow": 0.3})

    return {"name": f"bench_{n}x{n}", "tmax": tmax, "deltan": 5,
            "nodes": nodes, "links": links, "demands": dems}


def run_one(label: str, scenario: dict, *, profile: bool = False) -> None:
    sim_input = server._scenario_to_input(scenario)

    t0 = time.perf_counter()
    if profile:
        import cProfile
        pr = cProfile.Profile()
        pr.enable()
    result = server._run_uxsim(sim_input)
    if profile:
        pr.disable()
    t1 = time.perf_counter()

    sim_id = f"bench_{label}"
    server._store_sim(sim_id, result)
    try:
        t2 = time.perf_counter()
        payload = server._envelope_json_bytes(sim_id)
        t3 = time.perf_counter()
        gz = server._envelope_gzip_bytes(sim_id)
        t4 = time.perf_counter()
        server._get_simulation_data(sim_id)
        t5 = time.perf_counter()

        frames = result["frames"]
        points = sum(len(f["ids"]) for f in frames.values())
        stats = result["stats"]
        print(
            f"[{label}] nodes={len(scenario['nodes'])} links={len(scenario['links'])} "
            f"frames={len(frames)} veh_pts={points:,}\n"
            f"         exec={stats['simulation_time_s']}s post={stats['post_processing_s']}s "
            f"run_total={t1 - t0:.2f}s\n"
            f"         json={t3 - t2:.3f}s ({len(payload) / 1e6:.1f}MB) "
            f"gz={t4 - t3:.3f}s ({len(gz) / 1e6:.1f}MB) "
            f"simdata={t5 - t4:.3f}s  →  {len(payload) / max(1, points):.1f} bytes/点"
        )
    finally:
        server.results_store.pop(sim_id, None)

    if profile:
        import pstats
        buf = io.StringIO()
        pstats.Stats(pr, stream=buf).sort_stats("tottime").print_stats(20)
        print(buf.getvalue())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RISU 後処理パイプラインのベンチマーク")
    ap.add_argument("--sizes", type=int, nargs="+", default=[10, 20, 40],
                    help="格子の一辺（既定: 10 20 40）")
    ap.add_argument("--tmax", type=int, default=3600)
    ap.add_argument("--profile", action="store_true", help="cProfile の結果も出す")
    args = ap.parse_args(argv)

    # 需要数は規模に応じて増やす（小さい網に大量の OD を流しても飽和するだけ）
    demands_for = {10: 100, 20: 400, 40: 1500}

    for n in args.sizes:
        sc = grid_scenario(n, tmax=args.tmax, demands=demands_for.get(n, n * n // 2))
        run_one(f"grid{n}", sc, profile=args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
