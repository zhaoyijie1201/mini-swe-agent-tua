#!/usr/bin/env python3
"""Draw the fixed task subset: stratified by category, only tasks that fit a small machine.

    python tua/scripts/make_subset.py --tasks-dir /path/to/TUA-Bench/tasks [--n 40] [--seed 6203] [--max-cpus 2]

Writes tua/configs/subset-40.json with the task names, the rule and the TUA-Bench commit so every configuration
uses exactly the same tasks.
"""

import argparse
import json
import random
import re
import subprocess
from collections import defaultdict
from pathlib import Path


def read_task(toml_path: Path) -> dict:
    text = toml_path.read_text()

    def grab(key, default):
        return (re.search(rf"^{key}\s*=\s*\"?([^\"\n]+)\"?", text, re.M) or [None, default])[1]

    return {
        "name": toml_path.parent.name,
        "category": grab("category", "unknown"),
        "cpus": int(float(grab("cpus", "1"))),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=6203)
    ap.add_argument("--max-cpus", type=int, default=2)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "configs" / "subset-40.json"))
    a = ap.parse_args()

    tasks_dir = Path(a.tasks_dir)
    pool = [t for t in map(read_task, sorted(tasks_dir.glob("*/task.toml"))) if t["cpus"] <= a.max_cpus]
    by_cat = defaultdict(list)
    for t in pool:
        by_cat[t["category"]].append(t["name"])

    # proportional allocation with largest remainder, at least one task per category where possible
    quotas = {c: a.n * len(v) / len(pool) for c, v in by_cat.items()}
    alloc = {c: int(q) for c, q in quotas.items()}
    for c in sorted(quotas, key=lambda c: quotas[c] - alloc[c], reverse=True):
        if sum(alloc.values()) >= a.n:
            break
        alloc[c] += 1
    rng = random.Random(a.seed)
    chosen = sorted(name for c, k in alloc.items() for name in rng.sample(sorted(by_cat[c]), min(k, len(by_cat[c]))))

    commit = (
        subprocess.run(
            ["git", "-C", str(tasks_dir), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        or None
    )
    out = {
        "benchmark": "TUA-Bench",
        "tua_bench_commit": commit,
        "seed": a.seed,
        "rule": f"tasks with cpus <= {a.max_cpus} ({len(pool)} of {len(list(tasks_dir.glob('*/task.toml')))}), stratified by category with proportional quotas (largest remainder), random.Random(seed).sample within category",
        "quota": {c: alloc[c] for c in sorted(alloc) if alloc[c]},
        "n": len(chosen),
        "tasks": chosen,
    }
    Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {a.out}: {len(chosen)} tasks from {len(pool)} eligible")
    for c in sorted(alloc):
        print(f"  {c:24s} {alloc[c]:2d} / {len(by_cat[c])}")


if __name__ == "__main__":
    main()
