#!/usr/bin/env python3
"""Apply two evaluation fixes to an installed Harbor 0.6.3 (run inside the Python env that has harbor).

1. Run the `chmod 777` of the log directories as root and include the artifacts dir. TUA-Bench images default to
   a non-root user, so the stock call fails silently and the verifier cannot write its reward.
2. Delete `/usr/local/bin/chrome_state.py` after the container's own `task` mode has run. Its `solved` mode jumps
   straight to the answer page (it exists for the official oracle), and the agent can read and execute it.

Both issues were first reported by the PE6203 group-2 project (Lapis0x0/PE6203-cost-aware-harness); this script
re-implements the same fixes. Neither changes a task or a verifier. Idempotent; keeps a `.orig` backup.

    python tua/scripts/patch_harbor.py          # patch
    python tua/scripts/patch_harbor.py --check  # report only
"""

import importlib.util
import sys
from pathlib import Path

OLD = """            await self.exec(
                f"chmod 777 {self._env_paths.agent_dir} {self._env_paths.verifier_dir}"
            )
"""
NEW = """            await self.exec(
                f"chmod 777 {self._env_paths.agent_dir} {self._env_paths.verifier_dir} {self._env_paths.artifacts_dir}",
                user="root",  # tua-harness patch 1: TUA images default to USER agent; chmod must run as root
            )
            # tua-harness patch 2: remove the oracle helper whose `solved` mode leaks the answer page.
            # Wait until its `task` mode (run by the entrypoint at startup) has finished, then delete it as root.
            await self.exec(
                "if [ -f /usr/local/bin/chrome_state.py ]; then "
                "for i in $(seq 1 240); do [ -f /tmp/chrome_state.log ] && ! pgrep -f '[c]hrome_state.py task' >/dev/null 2>&1 && break; sleep 0.5; done; "
                "sleep 2; rm -f /usr/local/bin/chrome_state.py; fi",
                user="root",
            )
"""


def main() -> None:
    spec = importlib.util.find_spec("harbor")
    if spec is None or not spec.submodule_search_locations:
        sys.exit("harbor is not installed in this Python environment")
    target = Path(list(spec.submodule_search_locations)[0]) / "environments" / "docker" / "docker.py"
    text = target.read_text()
    if "tua-harness patch" in text:
        print(f"already patched: {target}")
        return
    if OLD not in text:
        sys.exit(f"unexpected docker.py content (not Harbor 0.6.3?): {target}")
    if "--check" in sys.argv:
        print(f"not patched yet: {target}")
        return
    target.with_suffix(".py.orig").write_text(text)
    target.write_text(text.replace(OLD, NEW))
    print(f"patched: {target} (backup: {target.with_suffix('.py.orig')})")


if __name__ == "__main__":
    main()
