"""Pure helper functions for the TUA harness switches (see `minisweagent.agents.tua`).

Everything here is side-effect free and unit-testable without containers or model calls.
"""

import hashlib
import re
import shlex

# ---------------------------------------------------------------------------
# C4: observation hygiene
# ---------------------------------------------------------------------------

# Generic terminal/GUI noise that never carries task information. Matched per line, case-sensitive.
NOISE_PATTERNS: list[str] = [
    r"dbus\[\d+\]|dbus-daemon|Failed to connect to the bus|DBUS_SESSION_BUS_ADDRESS",
    r"Gtk-(WARNING|Message|CRITICAL)|GLib-GIO-(WARNING|CRITICAL)|GLib-GObject-",
    r"Fontconfig (warning|error)|Pango-WARNING",
    r"libEGL warning|MESA(-LOADER)?:|libGL error|glx: failed",
    r"XDG_RUNTIME_DIR|Warning: Ignoring XDG_SESSION_TYPE",
    r"^\[\d+:\d+:\d{4}/\d{6}\.\d+:(INFO|WARNING|ERROR|VERBOSE\d?):",  # chromium log lines
    r"QStandardPaths:|QXcbConnection:|qt\.qpa\.",
    r"javaldx: Could not find|Warning: failed to launch javaldx",
    r"^\s*\(process:\d+\):",
    r"ALSA lib|snd_pcm_open|jack server is not running",
    r"Xlib:\s+extension .* missing",
    r"^\s*WARNING: (Kernel|The sandbox)",
    r"gpu_process_host|viz_main_impl|sandbox_linux|zygote_host_impl",
]
_NOISE_RE = re.compile("|".join(f"(?:{p})" for p in NOISE_PATTERNS))


def denoise_output(text: str) -> str:
    """Drop known noise lines and fold consecutive duplicate lines.

    Returns the text unchanged when nothing matches, so prompt caching is unaffected for clean outputs.
    """
    if not text:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if _NOISE_RE.search(line):
            dropped += 1
            continue
        kept.append(line)
    folded: list[str] = []
    i = 0
    while i < len(kept):
        j = i
        while j + 1 < len(kept) and kept[j + 1] == kept[i] and kept[i].strip():
            j += 1
        folded.append(kept[i])
        if j > i:
            folded.append(f"[harness: previous line repeated {j - i} more times]")
        i = j + 1
    if dropped == 0 and len(folded) == len(kept):
        return text
    out = "\n".join(folded)
    if dropped:
        out += f"\n[harness: {dropped} noise lines (dbus/GTK/fontconfig/chromium logs) removed]"
    if text.endswith("\n"):
        out += "\n"
    return out


def truncate_lines(text: str, head_lines: int, tail_lines: int, max_chars: int) -> str:
    """Line-based head/tail truncation with a hard character cap."""
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) > head_lines + tail_lines:
        elided = len(lines) - head_lines - tail_lines
        text = (
            "".join(lines[:head_lines])
            + f"\n[harness: {elided} lines elided; use head/tail/grep/sed -n to look at a smaller part]\n\n"
            + "".join(lines[-tail_lines:])
        )
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + f"\n[harness: {len(text) - max_chars} characters elided]\n" + text[-half:]
    return text


def compact_messages(messages: list[dict], keep_last: int, head_chars: int) -> list[dict]:
    """Return a copy of ``messages`` where observations older than the last ``keep_last`` are shortened.

    The stored trajectory is never modified; only the view sent to the model is. Observations are
    recognised by ``extra.raw_output`` (set by every observation formatter in mini-swe-agent).
    """
    obs_idx = [i for i, m in enumerate(messages) if isinstance(m.get("extra"), dict) and "raw_output" in m["extra"]]
    to_compact = set(obs_idx[:-keep_last]) if keep_last > 0 else set(obs_idx)
    if not to_compact:
        return messages
    out = []
    for i, m in enumerate(messages):
        if i in to_compact and isinstance(m.get("content"), str) and len(m["content"]) > head_chars + 200:
            m = dict(m)
            n_more = len(m["content"]) - head_chars
            m["content"] = (
                m["content"][:head_chars]
                + f"\n[harness: {n_more} more characters of this earlier observation were trimmed to save context; "
                "re-run the command if you need them]"
            )
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# C2: stall detection
# ---------------------------------------------------------------------------


def normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


def output_fingerprint(output: str) -> str:
    return hashlib.sha1(output[:4000].encode("utf-8", errors="replace")).hexdigest()


def is_timeout(output: dict) -> bool:
    info = f"{output.get('exception_info', '')} {output.get('extra', {}).get('exception_type', '')}"
    return "timed out" in info.lower() or "timeoutexpired" in info.lower()


TIMEOUT_HINT = (
    "The command hit the harness time limit for a single step and was killed. Do not simply re-run it. "
    "Options: (a) run it in the background and poll: `nohup CMD > /tmp/out.log 2>&1 &` then `tail /tmp/out.log`; "
    "(b) wrap it with an explicit `timeout N CMD`; (c) split the work into smaller steps; "
    "(d) if it is a GUI program, start it in the background and interact with it afterwards."
)

REPEAT_COMMAND_HINT = (
    "You have run the same command {n} times in your last {window} steps without making progress. "
    "Repeating it will give the same result. Step back: re-read the task statement, check the assumption that failed "
    "(file path, tool name, option syntax, program state), look at the documentation (`--help`, `man`), "
    "or try a different approach."
)

REPEAT_OUTPUT_HINT = (
    "The last {n} commands produced identical output. The environment is not changing; "
    "re-check whether your commands are actually taking effect (correct file, correct process, correct working directory) "
    "before continuing."
)


# ---------------------------------------------------------------------------
# C3: pre-submission verification
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(r"(?<![\w./\-~])(/(?:[\w.\-@+]+/)*[\w.\-@+]+)")
_OFFICE_EXT = (".xlsx", ".xlsm", ".docx", ".pptx", ".odt", ".ods", ".odp")


def extract_paths(task_text: str, limit: int = 20) -> list[str]:
    """Absolute paths mentioned in the task statement, de-duplicated, order preserved."""
    seen: list[str] = []
    for m in _PATH_RE.finditer(task_text or ""):
        p = m.group(1).rstrip(".,;:'\"`)")
        if p in ("/", "/app", "/tmp", "/home", "/root", "/usr", "/etc", "/var", "/dev", "/opt", "/bin", "/proc"):
            continue
        if p.startswith(("/dev/", "/proc/", "/usr/bin/", "/bin/", "/etc/apt")):
            continue
        if p not in seen:
            seen.append(p)
        if len(seen) >= limit:
            break
    return seen


# Runs inside the task container. Pure stdlib; degrades gracefully when python3 is missing.
_CHECK_PY = r"""
import os, sys, zipfile, re
import xml.etree.ElementTree as ET
marker = sys.argv[1]
paths = sys.argv[2:]
def human(n):
    for u in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"
print("## Paths mentioned in the task statement")
if not paths:
    print("(none detected)")
office = []
for p in paths:
    if os.path.isdir(p):
        n = sum(len(f) for _, _, f in os.walk(p))
        print(f"- {p}: directory, {n} files")
    elif os.path.isfile(p):
        sz = os.path.getsize(p)
        flag = "  <-- EMPTY FILE" if sz == 0 else ""
        print(f"- {p}: exists, {human(sz)}{flag}")
        if p.lower().endswith(%OFFICE%): office.append(p)
    else:
        print(f"- {p}: MISSING (does not exist). If the task expects this as an output, it has not been produced.")
print()
print("## Files created or modified since the task started (excluding caches/config dirs; max 40)")
skip = ("/.cache/", "/.config/", "/.local/", "/.mozilla/", "/.thunderbird/", "/__pycache__/", "/.git/", "/node_modules/", "/.npm/", "/.ipython/", "/snap/")
mod = []
try:
    t0 = os.path.getmtime(marker)
except OSError:
    t0 = None
roots = [r for r in ("/app", "/home", "/root", "/output", "/data", "/logs/artifacts") if os.path.isdir(r)]
if t0 is not None:
    for root in roots:
        for d, dirs, files in os.walk(root):
            if any(s.rstrip("/") in d.split("/") for s in (".cache", ".config", ".local", ".mozilla", ".thunderbird", "__pycache__", ".git", "node_modules", ".npm")):
                dirs[:] = []
                continue
            for f in files:
                fp = os.path.join(d, f)
                try:
                    if os.path.getmtime(fp) >= t0 - 1 and not any(s in fp for s in skip):
                        mod.append(fp)
                except OSError:
                    pass
            if len(mod) > 400: break
    mod = sorted(set(mod))
    for fp in mod[:40]:
        try: print(f"- {fp} ({human(os.path.getsize(fp))})")
        except OSError: print(f"- {fp}")
    if len(mod) > 40: print(f"- ... and {len(mod)-40} more")
    if not mod: print("(no files changed under " + ", ".join(roots) + ") <-- if the task asked you to produce or edit a file, nothing was written")
    for fp in mod:
        if fp.lower().endswith(%OFFICE%) and fp not in office: office.append(fp)
else:
    print("(start marker missing; skipped)")
print()
print("## Office/zip-based documents")
if not office: print("(none)")
for p in office:
    probs = []
    try:
        with zipfile.ZipFile(p) as z:
            bad = z.testzip()
            if bad: probs.append(f"corrupt member {bad}")
            names = z.namelist()
            if "[Content_Types].xml" not in names and "mimetype" not in names:
                probs.append("missing [Content_Types].xml / mimetype")
            for n in names:
                if n.endswith(".xml") or n.endswith(".rels"):
                    try: ET.fromstring(z.read(n))
                    except ET.ParseError as e: probs.append(f"malformed XML in {n}: {e}")
            if p.lower().endswith((".xlsx", ".xlsm")):
                nf = nv = 0
                for n in names:
                    if n.startswith("xl/worksheets/") and n.endswith(".xml"):
                        data = z.read(n).decode("utf-8", "replace")
                        for c in re.finditer(r"<c\b[^>]*>(.*?)</c>", data, re.S):
                            body = c.group(1)
                            if "<f" in body:
                                nf += 1
                                if "<v>" not in body: nv += 1
                if nf:
                    msg = f"{nf} formula cells, {nv} WITHOUT cached values"
                    if nv: msg += " <-- a grader reading values (openpyxl data_only / pandas) will see empty cells; recalculate and save with LibreOffice (e.g. `soffice --headless --convert-to xlsx --outdir OUT FILE`) or write the literal values"
                    probs.append(msg)
    except zipfile.BadZipFile:
        probs.append("NOT A VALID ZIP/OFFICE FILE (corrupt or wrong format)")
    except Exception as e:
        probs.append(f"check error: {e}")
    print(f"- {p}: " + ("OK (opens, XML well-formed)" if not probs else "; ".join(probs)))
print()
print("Note: these checks only test that files exist and open. They do NOT verify that the content, values, formatting or application state match the task statement.")
""".replace("%OFFICE%", repr(_OFFICE_EXT))


def build_check_command(marker_path: str, paths: list[str]) -> str:
    """Shell command that prints the verification report (uses python3 when available)."""
    args = " ".join(shlex.quote(p) for p in [marker_path, *paths])
    script = shlex.quote(_CHECK_PY)
    return (
        "if command -v python3 >/dev/null 2>&1; then "
        f"python3 -I -c {script} {args}; "
        "else echo 'python3 not available in this environment; file checks skipped'; "
        f'for p in {" ".join(shlex.quote(p) for p in paths)}; do if [ -e "$p" ]; then echo "- $p: exists"; else echo "- $p: MISSING"; fi; done; fi'
    )


VERIFY_GATE_TEMPLATE = """Your submission was NOT accepted yet. Before finishing, the harness ran an independent check of the files you produced:

<verification_report>
{report}
</verification_report>

Now, in separate steps:
1. Re-read the task statement at the top of this conversation and list every concrete requirement: output path and file name, file format, exact values/settings/text, final application or browser state.
2. Verify each requirement with a command that inspects the actual artifact or state (re-open the file you produced and print the relevant cells/text/settings; check the process or window state), not your memory of what you did.
3. Fix whatever is missing or wrong. Make sure outputs are at the exact paths requested.
4. Then run the submit command again. This check will not be repeated{repeat_note}.
"""


def render_gate_message(report: str, gates_left: int) -> str:
    note = "" if gates_left <= 0 else f" more than {gates_left} time(s)"
    return VERIFY_GATE_TEMPLATE.format(report=report.strip() or "(empty report)", repeat_note=note)


# ---------------------------------------------------------------------------
# C1: budget notices
# ---------------------------------------------------------------------------


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s"


def budget_line(n_calls: int, step_limit: int, elapsed: float, wall_limit: int) -> str:
    parts = [f"step {n_calls}" + (f"/{step_limit}" if step_limit > 0 else "")]
    parts.append(f"elapsed {fmt_duration(elapsed)}")
    if wall_limit > 0:
        parts.append(f"time left ~{fmt_duration(wall_limit - elapsed)}")
    if step_limit > 0:
        parts.append(f"steps left {max(0, step_limit - n_calls)}")
    return "[budget] " + " | ".join(parts)


WRAPUP_NOTICE = (
    "Budget notice from the harness: {detail}. Wrap up now. Make sure every output the task asked for is saved at the "
    "exact path requested and in the requested format, do a final quick check of the artifact, then submit. "
    "Do not start new explorations."
)
