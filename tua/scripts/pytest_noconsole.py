#!/usr/bin/env python3
"""Run pytest from a process without a Windows console (IDE tool windows, Git Bash, CI runners).

`minisweagent.agents.interactive` builds a prompt_toolkit PromptSession at import time, which on Windows requires a
real console. This wrapper gives prompt_toolkit a dummy output when no console is available, then runs pytest with
the given arguments:

    python tua/scripts/pytest_noconsole.py tests/agents/test_tua.py -q
"""

import sys

import prompt_toolkit.application.current as current
import prompt_toolkit.output.defaults as defaults
import pytest
from prompt_toolkit.output import DummyOutput

_real_create_output = defaults.create_output


def _safe_create_output(*args, **kwargs):
    try:
        return _real_create_output(*args, **kwargs)
    except Exception:
        return DummyOutput()


defaults.create_output = _safe_create_output
current.create_output = _safe_create_output

if __name__ == "__main__":
    sys.exit(pytest.main(sys.argv[1:]))
