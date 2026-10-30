#!/usr/bin/env python3
"""Summarise Harbor runs: success rate, tokens and dollars per configuration from the raw logs.

    python tua/scripts/summarize.py jobs/ [--prices tua/pricing/prices.json] [--subset tua/configs/subset-40.json] [--plot]

Layout expected: jobs/<config-name>/.../result.json with a sibling agent/mini-swe-agent.trajectory.json
(what `harbor run -o jobs/<config-name>` produces). Several runs of one configuration can live in the same
folder; the per-task mean is used, so repetitions are averaged. Cost = tokens x the fixed price table
(never the provider's reported dollars), split into agent inference and harness overhead (text the harness injected).
"""

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

HARNESS_MARKERS = ("<harness_note>", "<verification_report>", "Budget notice from the harness")


def model_price(prices: dict, model_name: str) -> dict:
    key = model_name.removeprefix("openrouter/")
    if key not in prices["models"]:
        raise SystemExit(f"model {model_name} not in price table; add it to the prices file")
    return prices["models"][key]


def trajectory_stats(traj: dict, price: dict) -> dict:
    s = {
        "prompt": 0,
        "cached": 0,
        "completion": 0,
        "steps": 0,
        "harness_chars": 0,
        "exit_status": traj.get("info", {}).get("exit_status", ""),
    }
    for m in traj.get("messages", []):
        usage = ((m.get("extra") or {}).get("response") or {}).get("usage") or {}
        if usage:
            s["steps"] += 1
            s["prompt"] += usage.get("prompt_tokens") or 0
            s["cached"] += (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            s["completion"] += usage.get("completion_tokens") or 0
        content = m.get("content") if isinstance(m.get("content"), str) else ""
        if (m.get("extra") or {}).get("harness") or any(k in content for k in HARNESS_MARKERS):
            s["harness_chars"] += (
                len(content.split("<harness_note>")[-1]) if "<harness_note>" in content else len(content)
            )
    uncached = s["prompt"] - s["cached"]
    s["cost"] = (
        uncached * price["input"] + s["cached"] * price["cache_read"] + s["completion"] * price["output"]
    ) / 1e6
    # injected text is sent once at input price, then re-sent (mostly cached) every later step; 4 chars/token
    s["harness_cost"] = (
        s["harness_chars"]
        / 4
        * (price["input"] + price["cache_read"] * max(0, s["steps"] - 1) / max(1, s["steps"]))
        / 1e6
    )
    return s


def load_runs(jobs_dir: Path, prices: dict) -> dict[str, dict[str, list[dict]]]:
    runs: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for result_path in sorted(jobs_dir.glob("*/**/result.json")):
        config = result_path.relative_to(jobs_dir).parts[0]
        res = json.loads(result_path.read_text())
        rewards = (res.get("verifier_result") or {}).get("rewards") or {}
        reward = float(rewards.get("reward", next(iter(rewards.values()), 0.0))) if rewards else 0.0
        traj_path = result_path.parent / "agent" / "mini-swe-agent.trajectory.json"
        model_name = ((res.get("agent_info") or {}).get("model_info") or {}).get("name") or ""
        row = {
            "task": res["task_name"].split("/")[-1],
            "reward": reward,
            "exception": (res.get("exception_info") or {}).get("exception_type"),
        }
        if traj_path.exists():
            traj = json.loads(traj_path.read_text())
            model_name = traj.get("info", {}).get("config", {}).get("model", {}).get("model_name") or model_name
            row |= trajectory_stats(traj, model_price(prices, model_name))
        else:
            row |= {
                "prompt": 0,
                "cached": 0,
                "completion": 0,
                "steps": 0,
                "cost": 0.0,
                "harness_cost": 0.0,
                "exit_status": "no-trajectory",
            }
        row["model"] = model_name
        runs[config][row["task"]].append(row)
    return runs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs_dir")
    ap.add_argument("--prices", default=str(Path(__file__).resolve().parent.parent / "pricing" / "prices.json"))
    ap.add_argument("--subset", default=str(Path(__file__).resolve().parent.parent / "configs" / "subset-40.json"))
    ap.add_argument("--out", default=None, help="output directory (default: <jobs_dir>/summary)")
    ap.add_argument("--plot", action="store_true", help="also write pareto.png (needs matplotlib)")
    a = ap.parse_args()

    jobs_dir = Path(a.jobs_dir)
    out_dir = Path(a.out or jobs_dir / "summary")
    out_dir.mkdir(parents=True, exist_ok=True)
    prices = json.loads(Path(a.prices).read_text())
    subset = set(json.loads(Path(a.subset).read_text())["tasks"]) if Path(a.subset).exists() else None
    runs = load_runs(jobs_dir, prices)

    summary = []
    with (out_dir / "per_task.csv").open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "config",
                "task",
                "run",
                "reward",
                "steps",
                "prompt",
                "cached",
                "completion",
                "cost",
                "harness_cost",
                "exit_status",
                "exception",
                "model",
            ],
        )
        w.writeheader()
        for config, tasks in runs.items():
            if subset:
                missing = subset - set(tasks)
                if missing:
                    print(f"[warn] {config}: {len(missing)} subset tasks have no result yet")
            rows = []
            for task, reps in sorted(tasks.items()):
                if subset and task not in subset:
                    continue
                for i, r in enumerate(reps):
                    w.writerow(
                        {
                            "config": config,
                            "run": i,
                            **{k: r.get(k) for k in w.fieldnames if k not in ("config", "run")},
                        }
                    )
                rows.append(
                    {
                        k: statistics.mean(r[k] for r in reps)
                        for k in ("reward", "steps", "prompt", "cached", "completion", "cost", "harness_cost")
                    }
                    | {"n_runs": len(reps)}
                )
            if not rows:
                continue
            n = len(rows)
            summary.append(
                {
                    "config": config,
                    "model": next(iter(tasks.values()))[0]["model"],
                    "n_tasks": n,
                    "runs_per_task": statistics.mean(r["n_runs"] for r in rows),
                    "success_rate": statistics.mean(r["reward"] for r in rows),
                    "cost_per_run": sum(r["cost"] for r in rows),
                    "harness_overhead": sum(r["harness_cost"] for r in rows),
                    "steps_per_run": sum(r["steps"] for r in rows),
                    "prompt_tokens": sum(r["prompt"] for r in rows),
                    "cached_tokens": sum(r["cached"] for r in rows),
                    "completion_tokens": sum(r["completion"] for r in rows),
                }
            )

    summary.sort(key=lambda s: s["cost_per_run"])
    best = -1.0
    for s in summary:
        s["pareto"] = s["success_rate"] > best
        best = max(best, s["success_rate"])
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    lines = [
        "| config | model | tasks | runs/task | success | $/run | harness $ | steps/run | prompt tok | cached tok | completion tok | Pareto |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        lines.append(
            f"| {s['config']} | {s['model']} | {s['n_tasks']} | {s['runs_per_task']:.1f} | {s['success_rate']:.1%} | {s['cost_per_run']:.3f} | {s['harness_overhead']:.3f} | {s['steps_per_run']:.0f} | {s['prompt_tokens']:,.0f} | {s['cached_tokens']:,.0f} | {s['completion_tokens']:,.0f} | {'*' if s['pareto'] else ''} |"
        )
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

    if a.plot and summary:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(7, 4.5))
        for s in summary:
            ax.scatter(s["cost_per_run"], s["success_rate"], s=60, color="tab:red" if s["pareto"] else "tab:gray")
            ax.annotate(
                s["config"],
                (s["cost_per_run"], s["success_rate"]),
                textcoords="offset points",
                xytext=(5, 5),
                fontsize=8,
            )
        front = [s for s in summary if s["pareto"]]
        ax.plot([s["cost_per_run"] for s in front], [s["success_rate"] for s in front], color="tab:red", lw=1)
        ax.set_xlabel("USD per run (fixed price table)")
        ax.set_ylabel("success rate")
        ax.set_title("TUA-Bench subset: accuracy vs cost (red = Pareto frontier)")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / "pareto.png", dpi=150)
        print("wrote", out_dir / "pareto.png")


if __name__ == "__main__":
    main()
