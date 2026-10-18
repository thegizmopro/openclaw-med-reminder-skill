#!/usr/bin/env python3
"""
doctor.py — health check for the med reminder skill.

Verifies the pieces that fail silently at 3am:
  - state file exists and passes validation
  - timezone database is importable (Windows needs `pip install tzdata`)
  - a message sender is wired up (send-message.sh or MEDS_SEND_CMD)
  - the tick task is registered and no legacy per-dose job survives
    (issue #1 §5 — legacy jobs would double-fire; the tick owns all logic)

Usage:
    python doctor.py               # run all checks, exit 0 healthy / 1 on FAIL
    python doctor.py --send-test   # also pipe one test message through the sender
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit(f"Python 3.9+ required — found {sys.version.split()[0]}")

# Console output includes unicode symbols that cp1252 consoles can't encode
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = Path(os.environ.get("MEDS_STATE_FILE", SCRIPT_DIR / "meds-state.json"))

sys.path.insert(0, str(SCRIPT_DIR))
import safe_write
from dispatch import _bash_executable


def load_setup_tasks():
    """Import setup-tasks.py (hyphenated filename) by path."""
    spec = importlib.util.spec_from_file_location(
        "setup_tasks", SCRIPT_DIR / "setup-tasks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(cmd, **kw):
    """Single seam for OS scheduler commands — tests mock this, never the OS."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


results = []  # (level, message) — level: ok | warn | fail

def ok(msg):   results.append(("ok", msg))
def warn(msg): results.append(("warn", msg))
def fail(msg): results.append(("fail", msg))


# ── Checks ─────────────────────────────────────────────────────────────────────

def check_runtime() -> None:
    ok(f"Python {sys.version.split()[0]}")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        ok("timezone database importable")
    except Exception:
        fail("timezone database missing — on Windows run: pip install tzdata")


def check_state() -> dict:
    if not STATE_FILE.exists():
        fail(f"state file not found: {STATE_FILE} "
             "(run: cp meds-state.template.json meds-state.json)")
        return {}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except ValueError as e:
        fail(f"state file is not valid JSON: {e}")
        return {}
    try:
        safe_write.validate(state)
        ok(f"state file valid ({len(state['meds'])} med(s): {STATE_FILE})")
    except ValueError as e:
        fail(f"state file invalid: {e}")
        return {}

    setup = load_setup_tasks()
    quiet = state["global"]["quiet_hours"]
    for med in state["meds"]:
        sched = med["schedule"]
        if sched["frequency"] not in ("interval", "as_needed"):
            for t in sched.get("times", []):
                if setup.in_quiet_window(t, quiet):
                    warn(f"{med['name']}: dose at {t} is inside quiet hours "
                         f"({quiet['start']}-{quiet['end']}) — it never reminds")
    return state


def check_sender() -> bool:
    send_helper = SCRIPT_DIR / "send-message.sh"
    send_cmd = os.environ.get("MEDS_SEND_CMD", "")
    if send_helper.exists():
        bash = _bash_executable()
        if Path(bash).exists() or bash == "bash":
            ok(f"sender: send-message.sh (via {bash})")
        else:
            fail(f"send-message.sh exists but bash not found at '{bash}' — "
                 "install Git or point MEDS_SEND_CMD at a non-bash sender")
        if send_cmd:
            warn("both MEDS_SEND_CMD and send-message.sh set — "
                 "send-message.sh is ignored in this shell")
        return True
    if send_cmd:
        warn("MEDS_SEND_CMD is set but send-message.sh is missing — scheduled "
             "tasks don't inherit env vars, so they can't send. Create "
             "send-message.sh (it can just exec your command).")
        return True
    fail("no message sender configured — create send-message.sh or set MEDS_SEND_CMD")
    return False


def check_scheduler() -> None:
    """Tick registered + no legacy job left over (issue #1 §5)."""
    setup = load_setup_tasks()
    h = setup.state_hash(STATE_FILE)
    reg = setup.load_registry(h)
    scheduler = reg.get("scheduler")
    if not scheduler:
        scheduler = ("windows" if sys.platform == "win32"
                     else "launchd" if sys.platform == "darwin"
                     else "cron")

    if scheduler == "windows":
        if setup.verify_windows(h, run=_run):
            ok(f"tick task registered ({setup.tick_task_full_name(h)})")
        else:
            fail("tick task missing — run: python setup-tasks.py")
        names = setup.list_medreminder_tasks(run=_run)
        if names is None:
            warn("could not enumerate \\MedReminder\\ — check Task Scheduler manually")
        else:
            legacy = [n for n in names if n != setup.tick_task_full_name(h)]
            if legacy:
                fail(f"{len(legacy)} legacy task(s) present "
                     f"({', '.join(sorted(legacy)[:3])}) — re-run: python setup-tasks.py")
            else:
                ok("no legacy scheduled tasks")
    else:
        if scheduler == "launchd":
            if setup.verify_launchd(h, run=_run):
                ok(f"launchd tick registered ({setup.launchd_label(h)})")
            else:
                fail("launchd tick missing — run: python setup-tasks.py")
        elif scheduler == "systemd":
            if setup.verify_systemd(h, run=_run):
                ok(f"systemd tick timer enabled ({setup.systemd_unit(h)}.timer)")
            else:
                fail("systemd tick timer missing — "
                     "run: python setup-tasks.py --scheduler systemd")
        else:
            if setup.verify_cron(h, run=_run):
                ok("crontab tick block present")
            else:
                fail("crontab tick block missing — run: python setup-tasks.py")

        # legacy jobs were crontab per-dose entries on every Unix flavor
        lines = setup.read_crontab(run=_run)
        if lines is None:
            warn("crontab unreadable — check legacy entries manually")
        else:
            legacy = setup.find_legacy_cron(lines, STATE_FILE, h)
            if legacy:
                fail(f"{len(legacy)} legacy crontab line(s) present — "
                     "re-run: python setup-tasks.py")
            else:
                ok("no legacy crontab lines")


def send_test() -> None:
    from dispatch import send_message
    print("\nSending test message through the configured sender...")
    try:
        send_message("[med reminder] doctor test — ignore this message", dry_run=False)
        ok("test message sent — check your channel")
    except SystemExit:
        fail("test send failed — see dispatch.log for the sender's error output")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Med reminder health check.")
    parser.add_argument("--send-test", action="store_true",
                        help="also pipe one test message through the sender")
    args = parser.parse_args()

    print(f"med-reminder doctor — state: {STATE_FILE}\n")
    check_runtime()
    state = check_state()
    sender_ok = check_sender()
    check_scheduler()
    if args.send_test and sender_ok:
        send_test()

    symbols = {"ok": "[ ok ]", "warn": "[WARN]", "fail": "[FAIL]"}
    for level, msg in results:
        print(f"{symbols[level]} {msg}")
    counts = {lv: sum(1 for l, _ in results if l == lv) for lv in ("ok", "warn", "fail")}
    print(f"\n{counts['ok']} ok, {counts['warn']} warning(s), {counts['fail']} failure(s)")
    sys.exit(1 if counts["fail"] else 0)


if __name__ == "__main__":
    main()
