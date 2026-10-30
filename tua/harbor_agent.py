"""Harbor agent that installs THIS fork of mini-swe-agent inside the task container.

Usage (Harbor 0.6.3, `tua/` on PYTHONPATH):

    harbor run ... --agent-import-path tua.harbor_agent:MiniSweAgentFork \
        --ak fork_wheel=/abs/path/mini_swe_agent-2.4.6-py3-none-any.whl \
        --ak config_file=tua/configs/A3-qwen.yaml

Either `fork_wheel` (a wheel built from this repository, uploaded into the container) or
`fork_url` [+ `fork_ref`] (a git URL the container can reach) selects what gets installed.
All other kwargs (`config_file`, `reasoning_effort`, `version`, ...) behave as in Harbor's stock adapter.
"""

from pathlib import Path

from harbor.agents.installed.mini_swe_agent import MiniSweAgent
from harbor.environments.base import BaseEnvironment

_UPLOAD_DIR = "/tmp/mswea-fork"


class MiniSweAgentFork(MiniSweAgent):
    def __init__(
        self,
        fork_wheel: str | None = None,
        fork_url: str | None = None,
        fork_ref: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if not fork_wheel and not fork_url:
            raise ValueError("MiniSweAgentFork needs --ak fork_wheel=<path> or --ak fork_url=<git url>")
        self._fork_wheel = Path(fork_wheel).expanduser().resolve() if fork_wheel else None
        self._fork_url, self._fork_ref = fork_url, fork_ref

    @staticmethod
    def name() -> str:
        return "mini-swe-agent-tua"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                "if command -v apt-get &>/dev/null; then apt-get update && apt-get install -y curl build-essential git;"
                " elif command -v apk &>/dev/null; then apk add --no-cache curl bash build-base git python3 py3-pip;"
                " elif command -v yum &>/dev/null; then yum install -y curl git gcc make;"
                " elif command -v dnf &>/dev/null; then dnf install -y curl git gcc make; fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        if self._fork_wheel:
            await self.exec_as_root(environment, command=f"mkdir -p {_UPLOAD_DIR} && chmod 777 {_UPLOAD_DIR}")
            await environment.upload_file(self._fork_wheel, f"{_UPLOAD_DIR}/{self._fork_wheel.name}")
            spec = f"{_UPLOAD_DIR}/{self._fork_wheel.name}"
        else:
            spec = f"git+{self._fork_url}" + (f"@{self._fork_ref}" if self._fork_ref else "")
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh && "
                'if ! grep -q \'export PATH="$HOME/.local/bin:$PATH"\' "$HOME/.bashrc" 2>/dev/null; then'
                '  echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$HOME/.bashrc"; fi && '
                'source "$HOME/.local/bin/env" && '
                f"uv tool install --python 3.12 '{spec}' && "
                "mini-swe-agent --help"
            ),
        )
