import json
import sys

import pytest

from minisweagent.agents.interactive import InteractiveAgent
from minisweagent.agents.tua import TuaAgent
from minisweagent.agents.utils import tua_utils as U
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
BASE = {
    "system_template": "sys",
    "instance_template": "task: {{task}}",
    "mode": "yolo",
    "confirm_exit": False,
    "cost_limit": 0,
}


def tc(command: str, i: int) -> dict:
    return make_toolcall_output(
        f"step {i}",
        [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": command})},
            }
        ],
        [{"command": command, "tool_call_id": f"call_{i}"}],
    )


def model(commands: list[str]) -> DeterministicToolcallModel:
    return DeterministicToolcallModel(outputs=[tc(c, i) for i, c in enumerate(commands)])


def strip(messages: list[dict]) -> list[dict]:
    return [
        {k: v for k, v in m.items() if k != "extra"}
        | {"extra": {k: v for k, v in m.get("extra", {}).items() if k != "timestamp"}}
        for m in messages
    ]


def observations(agent) -> list[str]:
    return [m["content"] for m in agent.messages if m.get("role") == "tool"]


def test_all_switches_off_matches_interactive_agent():
    commands = ["echo hello", "echo again", SUBMIT]
    ref = InteractiveAgent(model(commands), LocalEnvironment(), **BASE)
    tua = TuaAgent(model(commands), LocalEnvironment(), **BASE)
    assert ref.run("do it")["exit_status"] == "Submitted"
    assert tua.run("do it")["exit_status"] == "Submitted"
    assert strip(tua.messages) == strip(ref.messages)
    assert tua.model.__class__ is DeterministicToolcallModel  # no compaction wrapper when hygiene is off


def test_budget_line_and_single_wrapup_notice():
    agent = TuaAgent(
        model(["echo a", "echo b", "echo c", SUBMIT]),
        LocalEnvironment(),
        **BASE,
        budget_notices=True,
        step_limit=6,
        budget_wrapup_fraction=0.5,
    )
    assert agent.run("t")["exit_status"] == "Submitted"
    obs = observations(agent)
    assert "[budget] step 1/6" in obs[0] and "steps left 5" in obs[0]
    notices = [m for m in agent.messages if m.get("extra", {}).get("harness") == "budget_wrapup"]
    assert len(notices) == 1 and "only 3 steps remain" in notices[0]["content"]
    assert agent.messages.index(notices[0]) > agent.messages.index(
        [m for m in agent.messages if m["role"] == "tool"][2]
    )


def test_stall_detection_flags_repeated_commands_and_outputs():
    cmd = "echo this output is long enough to count"
    agent = TuaAgent(model([cmd, cmd, cmd, "echo x", SUBMIT]), LocalEnvironment(), **BASE, stall_detection=True)
    agent.run("t")
    obs = observations(agent)
    assert "<harness_note>" not in obs[0] and "<harness_note>" not in obs[1]
    assert "same command 3 times" in obs[2] and "identical output" in obs[2]
    assert "<harness_note>" not in obs[3]


@pytest.mark.skipif(sys.platform == "win32", reason="process-group timeout handling differs on Windows")
def test_timeout_hint():
    agent = TuaAgent(model(["sleep 3", SUBMIT]), LocalEnvironment(timeout=1), **BASE, stall_detection=True)
    agent.run("t")
    assert "time limit for a single step" in observations(agent)[0]


def test_verify_gate_intercepts_first_submission_only():
    agent = TuaAgent(model(["echo hi", SUBMIT, SUBMIT]), LocalEnvironment(), **BASE, verify_gate=True)
    assert agent.run("write /tmp/tua_out.txt and /app/result.xlsx")["exit_status"] == "Submitted"
    gates = [m for m in agent.messages if m.get("extra", {}).get("interrupt_type") == "VerificationGate"]
    assert (
        len(gates) == 1
        and "<verification_report>" in gates[0]["content"]
        and "will not be repeated." in gates[0]["content"]
    )
    assert agent.n_calls == 3


def test_extract_paths():
    text = "Save to `/app/artifacts/out.txt`. Inputs: /app/input/images/1-162hrh2ax2.tif, /home/user/Desktop/a b.docx (see /tmp). Not /usr/bin/python3."
    assert U.extract_paths(text) == [
        "/app/artifacts/out.txt",
        "/app/input/images/1-162hrh2ax2.tif",
        "/home/user/Desktop/a",
    ]


def test_denoise_and_truncate():
    noisy = "real line\n(process:12): Gtk-WARNING **: cannot open display\nsame\nsame\nsame\n[1234:1234:1030/120000.123456:ERROR:gpu_init.cc(1)] x\nend\n"
    cleaned = U.denoise_output(noisy)
    assert cleaned.splitlines()[:3] == ["real line", "same", "[harness: previous line repeated 2 more times]"]
    assert "Gtk-WARNING" not in cleaned and "2 noise lines" in cleaned
    assert U.denoise_output("clean\noutput\n") == "clean\noutput\n"
    long = "\n".join(f"line {i}" for i in range(200))
    out = U.truncate_lines(long, 10, 5, 100_000)
    assert out.startswith("line 0\n") and out.endswith("line 199") and "185 lines elided" in out
    assert len(U.truncate_lines("x" * 20_000, 1000, 1000, 1000)) < 1200


def test_compaction_only_affects_model_view():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(4):
        msgs += [
            {"role": "assistant", "content": f"a{i}"},
            {"role": "tool", "content": f"obs{i} " + "y" * 1000, "extra": {"raw_output": "x"}},
        ]
    out = U.compact_messages(msgs, keep_last=2, head_chars=20)
    assert [len(m["content"]) > 900 for m in out if m["role"] == "tool"] == [False, False, True, True]
    assert "trimmed to save context" in out[3]["content"] and msgs[3]["content"].startswith("obs0 yyyy")

    class Recorder(DeterministicToolcallModel):
        def query(self, messages, **kwargs):
            self.seen = messages
            return super().query(messages, **kwargs)

    long_cmd = """python3 -c "print('z'*3000)" || python -c "print('z'*3000)" """
    agent = TuaAgent(
        Recorder(outputs=[tc(long_cmd, i) for i in range(3)] + [tc(SUBMIT, 3)]),
        LocalEnvironment(),
        **BASE,
        observation_hygiene=True,
        compact_keep_last=1,
        compact_head_chars=100,
    )
    agent.run("t")
    tool_views = [m["content"] for m in agent.model.seen if m.get("role") == "tool"]
    assert len(tool_views) == 3 and all("trimmed" in v for v in tool_views[:2]) and "trimmed" not in tool_views[2]
    assert all(len(v) > 3000 for v in observations(agent)[:3])  # stored trajectory intact
