#!/usr/bin/env python3
"""
setup-tasks.py — Register system scheduler entries for med reminders.

Reads meds-state.json and creates one task per dose-time event:
  fire   at dose_time                        → send initial reminder
  check  at dose_time + late_threshold_min   → LATE nudge if unconfirmed
  miss   at dose_time + missed_threshold_min → MISSED if still unconfirmed

Plus global daily tasks:
  digest         at global.digest_time       → daily med summary + deferred reset
  reset-deferred at 00:00                    → midnight deferred → pending

Platform:
  Windows  — Task Scheduler via schtasks (/it = only when logged on)
  Mac/Linux — crontab

Note on interval-frequency meds: only a repeating fire task is registered.
LATE/MISSED escalation applies to time-of-day meds (once_daily, twice_daily,
weekly) only. Interval meds re-fire on schedule; the advisory check skips
confirmed doses.

Re-runnable: previously registered tasks are removed before re-registering.
Run this after any change to meds-state.json (add, edit, remove a med).

Usage:
    python3 setup-tasks.py            # register tasks
    python3 setup-tasks.py --dry-run  # show what would be registered, no changes
    python3 setup-tasks.py --clear    # remove all MedReminder tasks only
"""

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone as tz_utc
from pathlib import Path
from typing import Optional

if sys.version_info < (3, 9):
    sys.exit(f"Python 3.9+ required — found {sys.version.split()[0]}")

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).parent.resolve()
STATE_FILE    = Path(os.environ.get("MEDS_STATE_FILE", SCRIPT_DIR / "meds-state.json"))
DISPATCH_FILE = SCRIPT_DIR / "dispatch.py"
# safe-write.sh kept for users who prefer it; dispatch.py uses safe_write.py directly
REGISTRY_FILE = SCRIPT_DIR / ".registered-tasks.json"

TASK_FOLDER   = "MedReminder"   # Windows Task Scheduler subfolder
CRON_MARKER   = "# MedReminder" # crontab section marker

# ── Import dispatch helpers (same directory) ──────────────────────────────────

sys.path.insert(0, str(SCRIPT_DIR))
try:
    from dispatch import compute_next_due, get_tz, load_state, save_state
except ImportError as e:
    sys.exit(f"Cannot import from dispatch.py: {e}\nEnsure dispatch.py is in the same directory.")

# ── Task model ────────────────────────────────────────────────────────────────

@dataclass
class Task:
    name: str                        # unique short identifier, e.g. "fire_med-001_0"
    label: str                       # human description for dry-run output
    args: list                       # dispatch.py subcommand + positional args
    hhmm: Optional[str]              # HH:MM for daily-at-time trigger; None for interval
    interval_min: Optional[int]      # repeating interval in minutes; None for daily

    @property
    def full_name(self) -> str:
        """Full task name including folder prefix."""
        return f"{TASK_FOLDER}\\{self.name}"

    @property
    def cron_name(self) -> str:
        return f"{self.name}"

# ── Time math ─────────────────────────────────────────────────────────────────

def hhmm_add(hhmm: str, minutes: int) -> str:
    """Add minutes to an HH:MM string, wrapping at midnight."""
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"

def hhmm_to_cron(hhmm: str) -> str:
    """'08:30' → '30 8' (cron minute + hour fields)."""
    h, m = map(int, hhmm.split(":"))
    return f"{m} {h}"

# ── Registry (tracks registered task names for cleanup) ───────────────────────

def load_registry() -> list:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return []

def save_registry(names: list) -> None:
    REGISTRY_FILE.write_text(json.dumps(names, indent=2), encoding="utf-8")

# ── Task builder ──────────────────────────────────────────────────────────────

def build_tasks(state: dict) -> list:
    """Build the full task list from current state."""
    tasks = []
    g = state["global"]

    # ── Global tasks ──────────────────────────────────────────────────────────
    tasks.append(Task(
        name="digest",
        label=f"Daily med digest at {g['digest_time']}",
        args=["digest"],
        hhmm=g["digest_time"],
        interval_min=None,
    ))
    tasks.append(Task(
        name="reset-deferred",
        label="Midnight deferred reset",
        args=["reset-deferred"],
        hhmm="00:00",
        interval_min=None,
    ))

    # ── Per-med tasks ─────────────────────────────────────────────────────────
    for med in state["meds"]:
        if med.get("paused"):
            continue

        mid   = med["id"]
        name  = med["name"]
        sched = med["schedule"]
        freq  = sched["frequency"]
        esc   = med["escalation"]
        late  = esc["late_threshold_minutes"]
        miss  = esc["missed_threshold_minutes"]

        if freq == "as_needed":
            continue

        if freq == "interval":
            interval_min = sched["interval_hours"] * 60
            tasks.append(Task(
                name=f"fire_{mid}_interval",
                label=f"{name}: fire every {sched['interval_hours']}h",
                args=["fire", mid, "0"],
                hhmm=None,
                interval_min=interval_min,
            ))
            # LATE/MISSED escalation not supported for interval meds in v1
            continue

        # Time-of-day meds (once_daily, twice_daily, weekly)
        for i, t in enumerate(sched["times"]):
            check_t = hhmm_add(t, late)
            miss_t  = hhmm_add(t, miss)
            tasks.append(Task(
                name=f"fire_{mid}_{i}",
                label=f"{name}: reminder at {t}",
                args=["fire", mid, str(i)],
                hhmm=t,
                interval_min=None,
            ))
            tasks.append(Task(
                name=f"check_{mid}_{i}",
                label=f"{name}: late check at {check_t} (+{late}m)",
                args=["check", mid, str(i)],
                hhmm=check_t,
                interval_min=None,
            ))
            tasks.append(Task(
                name=f"miss_{mid}_{i}",
                label=f"{name}: missed check at {miss_t} (+{miss}m)",
                args=["miss", mid, str(i)],
                hhmm=miss_t,
                interval_min=None,
            ))

    return tasks

# ── next_due initialisation ───────────────────────────────────────────────────

def init_next_due(state: dict, dry_run: bool) -> bool:
    """
    Set next_due for any med where it is null (fresh install or new med).
    Returns True if state was changed.
    """
    from zoneinfo import ZoneInfo
    tz  = get_tz(state)
    now = datetime.now(tz=tz)
    changed = False

    for med in state["meds"]:
        if med["state"]["next_due"] is None and med["schedule"]["frequency"] != "as_needed":
            nd = compute_next_due(med, now, tz)
            if nd:
                med["state"]["next_due"] = nd.isoformat()
                changed = True
                print(f"  init next_due [{med['id']}] -> {nd.strftime('%Y-%m-%d %H:%M %Z')}")

    if changed and not dry_run:
        save_state(state, dry_run=False)

    return changed

# ── Windows Task Scheduler ────────────────────────────────────────────────────

def _schtasks(*args, dry_run: bool, capture: bool = True):
    cmd = ["schtasks"] + list(args)
    if dry_run:
        print(f"  [DRY RUN] {' '.join(cmd)}")
        return None
    result = subprocess.run(cmd, capture_output=capture, text=True)
    return result

def _tr_command(task: Task) -> str:
    """Build the /tr value: quoted python + dispatch path + args."""
    python = str(Path(sys.executable).resolve())
    dispatch = str(DISPATCH_FILE.resolve())
    args_str = " ".join(task.args)
    return f'"{python}" "{dispatch}" {args_str}'

def clear_windows(dry_run: bool) -> None:
    registered = load_registry()
    if not registered:
        print("No registered tasks found in .registered-tasks.json — nothing to clear.")
        return
    for name in registered:
        full = f"\\{TASK_FOLDER}\\{name}"
        r = _schtasks("/delete", "/tn", full, "/f", dry_run=dry_run)
        if r and r.returncode == 0:
            print(f"  Removed: {full}")
        elif r:
            # Task may not exist — that's fine
            print(f"  (Not found, skipping): {full}")
    if not dry_run:
        save_registry([])
        print("Registry cleared.")

def register_windows(tasks: list, dry_run: bool) -> None:
    names = []
    today = datetime.now().strftime("%m/%d/%Y")

    for task in tasks:
        full_name = f"\\{TASK_FOLDER}\\{task.name}"
        tr = _tr_command(task)
        base_args = [
            "/create", "/f",
            "/tn", full_name,
            "/tr", tr,
            "/it",                   # only when user is logged on
            "/sd", today,            # start today
        ]

        if task.interval_min is not None:
            # Repeating every N minutes
            extra = ["/sc", "MINUTE", "/mo", str(task.interval_min)]
        else:
            # Daily at specific time
            extra = ["/sc", "DAILY", "/st", task.hhmm]

        r = _schtasks(*base_args, *extra, dry_run=dry_run)
        if r and r.returncode != 0:
            print(f"  WARNING: schtasks failed for {task.name}: {r.stderr.strip()}")
        else:
            print(f"  Registered: {full_name}  ({task.label})")
            names.append(task.name)

    if not dry_run:
        save_registry(names)

# ── Unix crontab ──────────────────────────────────────────────────────────────

def _read_crontab() -> list:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout.splitlines()
    if "no crontab for" in r.stderr.lower():
        return []
    sys.exit(f"Error reading crontab: {r.stderr.strip()}")

def _write_crontab(lines: list, dry_run: bool) -> None:
    content = "\n".join(lines) + "\n"
    if dry_run:
        print(f"\n[DRY RUN] New crontab would be:\n{content}")
        return
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)

def _strip_med_reminder_block(lines: list) -> list:
    """Remove the MedReminder block (from marker to blank line after it)."""
    result = []
    in_block = False
    for line in lines:
        if line.strip() == CRON_MARKER:
            in_block = True
            continue
        if in_block:
            # End of block: first non-MedReminder cron line that's non-empty
            if line.startswith("#") or not line.strip():
                continue
            else:
                in_block = False
        result.append(line)
    return result

def clear_unix(dry_run: bool) -> None:
    lines = _read_crontab()
    cleaned = _strip_med_reminder_block(lines)
    if len(cleaned) == len(lines):
        print("No MedReminder block found in crontab.")
        return
    _write_crontab(cleaned, dry_run)
    print("MedReminder crontab block removed.")

def register_unix(tasks: list, dry_run: bool) -> None:
    python = sys.executable
    dispatch = str(DISPATCH_FILE.resolve())

    lines = _read_crontab()
    lines = _strip_med_reminder_block(lines)  # remove old block first

    new_lines = [CRON_MARKER]
    for task in tasks:
        args_str = " ".join(task.args)
        cmd = f'"{python}" "{dispatch}" {args_str}'

        if task.interval_min is not None:
            # Every N minutes: */N * * * *
            entry = f"*/{task.interval_min} * * * * {cmd}"
        else:
            # Daily at HH:MM
            cron_time = hhmm_to_cron(task.hhmm)
            entry = f"{cron_time} * * * {cmd}"

        new_lines.append(f"# {task.label}")
        new_lines.append(entry)
        print(f"  Registered: {task.name}  ({task.label})")

    new_lines.append("")  # trailing blank line
    _write_crontab(lines + new_lines, dry_run)

# ── Summary printer ───────────────────────────────────────────────────────────

def print_summary(tasks: list, state: dict) -> None:
    meds = state["meds"]
    active = [m for m in meds if not m.get("paused")]
    paused = [m for m in meds if m.get("paused")]

    print(f"\nMeds: {len(active)} active, {len(paused)} paused")
    print(f"Tasks to register: {len(tasks)}")
    print(f"  Global: digest + reset-deferred")
    for med in active:
        freq = med["schedule"]["frequency"]
        count = 1 if freq in ("interval", "as_needed") else len(med["schedule"].get("times", [])) * 3
        if freq == "as_needed":
            count = 0
        label = "interval (fire only)" if freq == "interval" else f"{count} tasks"
        print(f"  {med['id']} ({med['name']}): {label}")
    print()

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register med reminder tasks in Task Scheduler or crontab.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be registered without making changes")
    parser.add_argument("--clear", action="store_true",
                        help="Remove all MedReminder tasks and exit")
    args = parser.parse_args()

    if not STATE_FILE.exists():
        sys.exit(
            f"State file not found: {STATE_FILE}\n"
            "Run: cp meds-state.template.json meds-state.json"
        )
    if not DISPATCH_FILE.exists():
        sys.exit(f"dispatch.py not found: {DISPATCH_FILE}")

    is_windows = sys.platform == "win32"
    state = load_state()
    tasks = build_tasks(state)

    if args.clear:
        print(f"Clearing MedReminder tasks ({'dry run' if args.dry_run else 'live'})...")
        if is_windows:
            clear_windows(args.dry_run)
        else:
            clear_unix(args.dry_run)
        return

    print_summary(tasks, state)

    # Initialize next_due for fresh meds (writes state if changed)
    print("Checking next_due initialization...")
    init_next_due(state, args.dry_run)

    # Clear existing tasks before re-registering
    print(f"\nClearing existing MedReminder tasks...")
    if is_windows:
        clear_windows(args.dry_run)
    else:
        clear_unix(args.dry_run)

    print(f"\nRegistering {len(tasks)} tasks ({'dry run' if args.dry_run else 'live'})...")
    if is_windows:
        register_windows(tasks, args.dry_run)
    else:
        register_unix(tasks, args.dry_run)

    if args.dry_run:
        print("\nDry run complete. No changes made.")
    else:
        print(f"\nDone. {len(tasks)} tasks registered.")
        print("Verify in Task Scheduler: start → 'Task Scheduler' → Task Scheduler Library → MedReminder")


if __name__ == "__main__":
    main()
