#!/usr/bin/env python3
"""Build (and optionally run) the `harbor run` command for one experiment configuration on the fixed subset.

    python tua/scripts/run_config.py A3-qwen --tasks-dir ../TUA-Bench/tasks --wheel tua/dist/mini_swe_agent-*.whl
    python tua/scripts/run_config.py B0-qwen --tasks-dir ../TUA-Bench/tasks            # unmodified harness (PyPI 2.4.6)
    python tua/scripts/run_config.py H-qwen  --tasks-dir ... --attempts 3 --concurrency 2 --dry-run

B* configurations use Harbor's stock `mini-swe-agent` agent pinned to PyPI 2.4.6; all others use this fork
(`tua.harbor_agent:MiniSweAgentFork`) installed from the wheel you built with `tua/scripts/build_wheel.sh`.
Results go to <jobs-dir>/<config>/ which is what tua/scripts/summarize.py reads.
"""

import argparse
import glob
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # .../tua
REPO = ROOT.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="configuration name, e.g. B0-qwen, A3-qwen, H-dsflash")
    ap.add_argument("--tasks-dir", required=True, help="TUA-Bench/tasks directory")
    ap.add_argument("--subset", default=str(ROOT / "configs" / "subset-40.json"))
    ap.add_argument("--jobs-dir", default="jobs")
    ap.add_argument("--wheel", default=None, help="fork wheel (default: newest tua/dist/*.whl)")
    ap.add_argument("--attempts", type=int, default=1, help="repetitions per task (harbor --n-attempts)")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these task names (debugging)")
    ap.add_argument("--harbor", default="harbor", help="harbor executable (e.g. 'uv run harbor' inside TUA-Bench)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    config_path = ROOT / "configs" / f"{a.config}.yaml"
    if not config_path.exists():
        sys.exit(f"no such config: {config_path} (run tua/scripts/gen_configs.py)")
    tasks = a.only or json.loads(Path(a.subset).read_text())["tasks"]
    model = next(
        line.split("model_name:")[1].strip() for line in config_path.read_text().splitlines() if "model_name:" in line
    )

    cmd = [*shlex.split(a.harbor), "run", "-p", a.tasks_dir]
    for t in tasks:
        cmd += ["-i", t]
    if a.config.startswith("B"):
        cmd += ["-a", "mini-swe-agent", "--ak", "version=2.4.6"]
    else:
        wheel = a.wheel or max(glob.glob(str(ROOT / "dist" / "*.whl")), default=None, key=os.path.getmtime)
        if not wheel:
            sys.exit("no wheel found; run tua/scripts/build_wheel.sh or pass --wheel")
        cmd += [
            "--agent-import-path",
            "tua.harbor_agent:MiniSweAgentFork",
            "--ak",
            f"fork_wheel={Path(wheel).resolve()}",
        ]
    cmd += [
        "--ak",
        f"config_file={config_path}",
        "-m",
        model,
        "-e",
        "docker",
        "-n",
        str(a.concurrency),
        "-k",
        str(a.attempts),
    ]
    cmd += ["-o", str(Path(a.jobs_dir) / a.config), "--yes"]

    env = os.environ | {
        "PYTHONPATH": str(REPO) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    }
    print("PYTHONPATH=" + shlex.quote(env["PYTHONPATH"]), shlex.join(cmd))
    if not a.dry_run:
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("OPENROUTER_API_KEY is not set")
        sys.exit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    main()
