"""Cost-aware harness variant for TUA-Bench (PE6203 project 5).

`TuaAgent` extends `InteractiveAgent` with five independent switches. With every switch off it behaves
exactly like the default agent, so ablations isolate one component at a time:

- C1 `budget_notices`: budget line after every observation + one wrap-up notice near the limits.
- C2 `stall_detection`: hints on repeated commands / identical outputs / command timeouts.
- C3 `verify_gate`: the first submission is intercepted; an in-container check of the produced files is returned.
- C4 `observation_hygiene`: noise removal and line-based truncation of new observations; old observations are
  shortened in the view sent to the model (the saved trajectory keeps the full text).
- C5 is a prompt-only change and lives in the config files (`minisweagent/config/tua/`).
"""

import time
from collections import deque
from typing import Any

from minisweagent.agents.interactive import InteractiveAgent, InteractiveAgentConfig
from minisweagent.agents.utils import tua_utils as U
from minisweagent.exceptions import Submitted

MARKER_PATH = "/tmp/.tua_task_start"


class TuaAgentConfig(InteractiveAgentConfig):
    budget_notices: bool = False
    budget_wrapup_fraction: float = 0.8
    """Emit the wrap-up notice once this fraction of the step or wall-time limit is used."""
    stall_detection: bool = False
    stall_window: int = 6
    stall_repeat_threshold: int = 3
    verify_gate: bool = False
    verify_gate_max: int = 1
    """How many submissions are intercepted before one is accepted."""
    verify_gate_timeout: int = 90
    observation_hygiene: bool = False
    hygiene_head_lines: int = 80
    hygiene_tail_lines: int = 40
    hygiene_max_chars: int = 8000
    compact_keep_last: int = 8
    """Observations older than this many are shortened in the model's view (0 disables compaction)."""
    compact_head_chars: int = 400


class _CompactingModel:
    """Wraps a model so that `query()` sees a compacted message history. Everything else is delegated."""

    def __init__(self, model, keep_last: int, head_chars: int):
        self._model, self._keep_last, self._head_chars = model, keep_last, head_chars

    def query(self, messages: list[dict], **kwargs) -> dict:
        return self._model.query(U.compact_messages(messages, self._keep_last, self._head_chars), **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)


class TuaAgent(InteractiveAgent):
    def __init__(self, *args, config_class=TuaAgentConfig, **kwargs):
        super().__init__(*args, config_class=config_class, **kwargs)
        if self.config.observation_hygiene and self.config.compact_keep_last > 0:
            self.model = _CompactingModel(self.model, self.config.compact_keep_last, self.config.compact_head_chars)
        self._cmd_history: deque[str] = deque(maxlen=self.config.stall_window)
        self._out_history: deque[str] = deque(maxlen=self.config.stall_window)
        self._gates_used = 0
        self._wrapup_sent = False

    # --- C3 needs a start marker so the check can list files changed during the task

    def run(self, task: str = "", **kwargs) -> dict:
        if self.config.verify_gate:
            self.env.execute({"command": f"touch {MARKER_PATH} 2>/dev/null; true"})
        return super().run(task, **kwargs)

    # --- C1: wrap-up notice is added right before the model is queried

    def step(self) -> list[dict]:
        if self.config.budget_notices and not self._wrapup_sent and (detail := self._budget_exhaustion()):
            self._wrapup_sent = True
            self.add_messages(
                {
                    "role": "user",
                    "content": U.WRAPUP_NOTICE.format(detail=detail),
                    "extra": {"harness": "budget_wrapup"},
                }
            )
        return super().step()

    def _budget_exhaustion(self) -> str:
        elapsed, f = time.time() - self._start_time, self.config.budget_wrapup_fraction
        if 0 < self.config.wall_time_limit_seconds and elapsed >= f * self.config.wall_time_limit_seconds:
            return f"about {U.fmt_duration(self.config.wall_time_limit_seconds - elapsed)} of wall-clock time remain"
        if 0 < self.config.step_limit and self.n_calls >= f * self.config.step_limit:
            return f"only {self.config.step_limit - self.n_calls} steps remain"
        return ""

    # --- Observation pipeline: C4 cleans outputs, C1/C2 annotate the rendered messages, C3 intercepts submission

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        outputs: list[dict] = []
        try:
            self._ask_confirmation_or_interrupt([a["command"] for a in actions])
            for action in actions:
                outputs.append(self.env.execute(action))
        except Submitted as e:
            self._check_for_new_task_or_submit(e)
        finally:
            if self.config.observation_hygiene:
                outputs = [self._clean_output(o) for o in outputs]
            observations = self.model.format_observation_messages(message, outputs, self.get_template_vars())
            if observations and (notes := self._notes(actions, outputs)):
                self._append_text(observations[-1], "\n\n<harness_note>\n" + "\n\n".join(notes) + "\n</harness_note>")
            result = self.add_messages(*observations)
        return result

    def _clean_output(self, output: dict) -> dict:
        c = self.config
        text = U.truncate_lines(
            U.denoise_output(output.get("output", "")), c.hygiene_head_lines, c.hygiene_tail_lines, c.hygiene_max_chars
        )
        return {**output, "output": text}

    def _notes(self, actions: list[dict], outputs: list[dict]) -> list[str]:
        notes = []
        if self.config.stall_detection:
            notes += self._stall_notes(actions, outputs)
        if self.config.budget_notices:
            notes.append(
                U.budget_line(
                    self.n_calls,
                    self.config.step_limit,
                    time.time() - self._start_time,
                    self.config.wall_time_limit_seconds,
                )
            )
        return notes

    def _stall_notes(self, actions: list[dict], outputs: list[dict]) -> list[str]:
        notes, c = [], self.config
        for action, output in zip(actions, outputs):
            cmd = U.normalize_command(action["command"])
            if U.is_timeout(output):
                notes.append(U.TIMEOUT_HINT)
            if (n := list(self._cmd_history).count(cmd) + 1) >= c.stall_repeat_threshold:
                notes.append(U.REPEAT_COMMAND_HINT.format(n=n, window=c.stall_window))
            fp = U.output_fingerprint(output.get("output", ""))
            if (
                len(output.get("output", "")) > 20
                and (m := list(self._out_history).count(fp) + 1) >= c.stall_repeat_threshold
            ):
                notes.append(U.REPEAT_OUTPUT_HINT.format(n=m))
            self._cmd_history.append(cmd)
            self._out_history.append(fp)
        return list(dict.fromkeys(notes))

    @staticmethod
    def _append_text(message: dict, text: str) -> None:
        if isinstance(message.get("content"), str):
            message["content"] += text
        elif isinstance(message.get("content"), list):
            message["content"].append({"type": "text", "text": text})

    # --- C3: verification gate

    def _check_for_new_task_or_submit(self, e: Submitted) -> None:
        if self.config.verify_gate and self._gates_used < self.config.verify_gate_max:
            self._gates_used += 1
            report = self.env.execute(
                {
                    "command": U.build_check_command(
                        MARKER_PATH, U.extract_paths(str(self.extra_template_vars.get("task", "")))
                    )
                },
                timeout=self.config.verify_gate_timeout,
            )
            self._interrupt(
                U.render_gate_message(report.get("output", ""), self.config.verify_gate_max - self._gates_used),
                itype="VerificationGate",
            )
        super()._check_for_new_task_or_submit(e)
