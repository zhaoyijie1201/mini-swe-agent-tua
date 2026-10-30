import json
from pathlib import Path

import pytest
import yaml
from jinja2 import StrictUndefined, Template

from minisweagent.agents import get_agent
from minisweagent.agents.interactive import InteractiveAgent
from minisweagent.agents.tua import TuaAgent
from minisweagent.config import builtin_config_dir
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

CONFIG_DIR = Path(__file__).resolve().parents[2] / "tua" / "configs"
CONFIGS = sorted(CONFIG_DIR.glob("*.yaml"))


def _agent_for(path: Path):
    config = yaml.safe_load(path.read_text())
    outputs = [
        make_toolcall_output(
            None,
            [
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": cmd})},
                }
            ],
            [{"command": cmd, "tool_call_id": f"c{i}"}],
        )
        for i, cmd in enumerate(
            ["echo probe", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
        )
    ]
    env_config = {k: v for k, v in config["environment"].items() if k != "environment_class"}
    agent_config = config["agent"] | {
        "mode": "yolo",
        "confirm_exit": False,
        "cost_limit": 0,
    }  # what Harbor's CLI flags set
    return config, get_agent(
        DeterministicToolcallModel(outputs=outputs),
        LocalEnvironment(**env_config),
        agent_config,
        default_type="interactive",
    )


@pytest.mark.parametrize("path", CONFIGS, ids=[p.stem for p in CONFIGS])
def test_generated_config_runs_end_to_end(path: Path):
    config, agent = _agent_for(path)
    assert agent.run("Write the answer to /tmp/answer.txt")["exit_status"] == "Submitted"
    assert "/tmp/answer.txt" in agent.messages[1]["content"]  # task rendered into the instance template
    if path.stem.startswith("B"):
        assert type(agent) is InteractiveAgent and "agent_class" not in config["agent"]
    else:
        assert type(agent) is TuaAgent
    assert (
        config["model"]["model_name"].startswith("openrouter/")
        and config["model"]["model_kwargs"]["extra_body"]["provider"]["allow_fallbacks"] is False
    )


def test_baseline_config_equals_default_mini_yaml():
    mini = yaml.safe_load((builtin_config_dir / "mini.yaml").read_text())
    base = yaml.safe_load((builtin_config_dir / "tua" / "base.yaml").read_text())
    assert base == mini
    b0 = yaml.safe_load((CONFIG_DIR / "B0-qwen.yaml").read_text())
    assert b0["agent"] == mini["agent"] and b0["environment"] == mini["environment"]
    assert {k: v for k, v in b0["model"].items() if k not in ("model_name", "model_kwargs")} == {
        k: v for k, v in mini["model"].items() if k != "model_kwargs"
    }


def test_switch_matrix():
    on = {p.stem: {k for k, v in yaml.safe_load(p.read_text())["agent"].items() if v is True} for p in CONFIGS}
    assert on["A1-qwen"] == {"budget_notices"} and on["A2-qwen"] == {"stall_detection"}
    assert on["A3-qwen"] == {"verify_gate"} and on["A4-qwen"] == {"observation_hygiene"} and on["A5-qwen"] == set()
    assert (
        on["H-qwen"] == on["H-dsflash"] == {"budget_notices", "stall_detection", "verify_gate", "observation_hygiene"}
    )
    assert on["A0-qwen"] == on["B0-qwen"] == set()
    assert (
        yaml.safe_load((CONFIG_DIR / "A5-qwen.yaml").read_text())["agent"]["system_template"]
        != yaml.safe_load((CONFIG_DIR / "A0-qwen.yaml").read_text())["agent"]["system_template"]
    )


def test_c2_format_error_template_renders_both_branches():
    template = yaml.safe_load((CONFIG_DIR / "A2-qwen.yaml").read_text())["model"]["format_error_template"]
    cut_off = Template(template, undefined=StrictUndefined).render(
        error="e", actions=[], has_tool_calls=False, finish_reason="length"
    )
    no_call = Template(template, undefined=StrictUndefined).render(
        error="No tool calls found", actions=[], has_tool_calls=False, finish_reason="stop"
    )
    assert "output token limit" in cut_off and "submit command" not in cut_off
    assert (
        "No tool calls found" in no_call
        and "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in no_call
        and "Nobody can answer questions" in no_call
    )
