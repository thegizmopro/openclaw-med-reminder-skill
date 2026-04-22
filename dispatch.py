#!/usr/bin/env python3
"""
dispatch.py — Med reminder dispatch engine (Python 3.9+, stdlib only)

Called by Task Scheduler / crontab with a mode subcommand. Advisory scheduling:
each mode checks current state first and exits silently if the dose was already
confirmed or deferred — no cancellation of registered tasks needed.

Subcommands:
    fire  <med-id> <dose-index>   Send initial dose reminder (at dose time)
    check <med-id> <dose-index>   Send LATE nudge if unconfirmed
    miss  <med-id> <dose-index>   Log as MISSED if still unconfirmed
    digest                         Send daily med summary, reset deferred
    reset-deferred                 Midnight reset: deferred → pending

Flags:
    --dry-run    Print without sending or writing state

Environment:
    MEDS_STATE_FILE   Path to meds-state.json   (default: same dir as script)
    MEDS_LOG_FILE     Path to dispatch.log       (default: same dir as script)
    MEDS_SEND_CMD     Shell command to send a message (receives text on stdin)
                      Falls back to send-message.sh in the skill directory.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone as tz_fixed
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── Python version guard ───────────────────────────────────────────────────────

if sys.version_info < (3, 9):
    sys.exit(f"Python 3.9+ required — found {sys.version.split()[0]}")

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE  = Path(os.environ.get("MEDS_STATE_FILE", SCRIPT_DIR / "meds-state.json"))
LOG_FILE    = Path(os.environ.get("MEDS_LOG_FILE",   SCRIPT_DIR / "dispatch.log"))
SEND_CMD    = os.environ.get("MEDS_SEND_CMD", "")
HISTORY_MAX = 30

sys.path.insert(0, str(SCRIPT_DIR))
from safe_write import safe_write as _safe_write_fn

log = logging.getLogger("dispatch")

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(dry_run: bool) -> None:
    level = logging.DEBUG if dry_run else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [dispatch] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

# ── State I/O ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        sys.exit(
            f"State file not found: {STATE_FILE}\n"
            "Run: cp meds-state.template.json meds-state.json"
        )
    with STATE_FILE.open(encoding="utf-8") as f:
        return json.load(f)

def save_state(state: dict, dry_run: bool) -> None:
    if dry_run:
        payload = json.dumps(state, indent=2)
        log.info("[DRY RUN] Would write state (first 300 chars):\n%s", payload[:300])
        return
    try:
        _safe_write_fn(state)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"safe_write failed: {e}")
    log.debug("State saved OK")

# ── Messaging ─────────────────────────────────────────────────────────────────

SEND_RETRIES = 3
SEND_RETRY_DELAY = 5  # seconds between attempts


def send_message(text: str, dry_run: bool) -> None:
    if dry_run:
        print(f"\n{'-' * 56}\n[DRY RUN] Message:\n{text}\n{'-' * 56}\n")
        return

    send_helper = SCRIPT_DIR / "send-message.sh"
    cmd = SEND_CMD or (f"bash {send_helper}" if send_helper.exists() else "")

    if not cmd:
        sys.exit(
            "No message sender configured.\n"
            "Set MEDS_SEND_CMD env var or create send-message.sh in the skill directory.\n"
            "send-message.sh receives the message text on stdin."
        )

    last_err = ""
    for attempt in range(1, SEND_RETRIES + 1):
        result = subprocess.run(cmd, shell=True, input=text, capture_output=True, text=True)
        if result.returncode == 0:
            log.info("Message sent (%d chars, attempt %d)", len(text), attempt)
            return
        last_err = result.stderr.strip()
        log.warning("Send attempt %d/%d failed (exit %d): %s",
                    attempt, SEND_RETRIES, result.returncode, last_err)
        if attempt < SEND_RETRIES:
            time.sleep(SEND_RETRY_DELAY)

    log.error("Send failed after %d attempts: %s", SEND_RETRIES, last_err)
    sys.exit(1)

# ── Timezone ──────────────────────────────────────────────────────────────────

def get_tz(state: dict) -> ZoneInfo:
    name = state["global"]["timezone"]
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        hint = (
            "\nOn Windows, run: pip install tzdata"
            if sys.platform == "win32" else ""
        )
        sys.exit(
            f"Unknown timezone '{name}'.{hint}\n"
            "Use an IANA name like 'America/Los_Angeles' or 'America/Chicago'.\n"
            "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
        )

# ── Time helpers ──────────────────────────────────────────────────────────────

def parse_hhmm(hhmm: str, tz: ZoneInfo, base: datetime) -> datetime:
    """Return base's calendar date at HH:MM in tz (timezone-aware)."""
    h, m = map(int, hhmm.split(":"))
    local = base.astimezone(tz)
    return local.replace(hour=h, minute=m, second=0, microsecond=0)

def most_recent_dose_time(times: list, dose_index: int, tz: ZoneInfo, now: datetime) -> datetime:
    """Most recent past occurrence of times[dose_index] relative to now."""
    t = parse_hhmm(times[dose_index], tz, now)
    if t > now:
        t -= timedelta(days=1)
    return t

def in_quiet_hours(now: datetime, quiet: dict, tz: ZoneInfo) -> bool:
    """True if now is inside the quiet window. Handles midnight-spanning windows."""
    start = parse_hhmm(quiet["start"], tz, now)
    end   = parse_hhmm(quiet["end"],   tz, now)
    local = now.astimezone(tz)
    if start <= end:
        return start <= local < end   # same-day window
    return local >= start or local < end  # midnight-spanning (e.g. 22:00–07:00)

def fmt_12h(hhmm: str) -> str:
    """'08:00' → '8:00am', '20:00' → '8:00pm'"""
    h, m = map(int, hhmm.split(":"))
    suffix = "am" if h < 12 else "pm"
    return f"{h % 12 or 12}:{m:02d}{suffix}"

def now_utc() -> datetime:
    return datetime.now(tz=tz_fixed.utc)

def now_iso() -> str:
    return now_utc().isoformat()

# ── Next-due calculation ──────────────────────────────────────────────────────

def compute_next_due(med: dict, after: datetime, tz: ZoneInfo) -> Optional[datetime]:
    """
    Next scheduled dose after `after`. Returns None for as_needed.

    Frequency handling:
      once_daily  — next occurrence of times[0]
      twice_daily — soonest of times[0] or times[1]
      interval    — after + interval_hours
      weekly      — next occurrence of times[0] at least 6 days from after
      as_needed   — None
    """
    sched = med["schedule"]
    freq  = sched["frequency"]

    if freq == "as_needed":
        return None

    if freq == "interval":
        return after + timedelta(hours=sched["interval_hours"])

    # Time-of-day based frequencies
    times = sched["times"]
    candidates = []
    for t in times:
        candidate = parse_hhmm(t, tz, after)
        if candidate <= after:
            candidate += timedelta(days=1)
        candidates.append(candidate)

    if freq == "weekly":
        # Advance soonest candidate until it's at least 6 days out
        base = min(candidates)
        while (base - after) < timedelta(days=6):
            base += timedelta(days=7)
        return base

    # once_daily or twice_daily
    return min(candidates)

# ── Template expansion ────────────────────────────────────────────────────────

def expand(template: str, med: dict) -> str:
    return (
        template
        .replace("{name}", med["name"])
        .replace("{dose}", med["dose"])
        .replace("{unit}", med["unit"])
    )

# ── History ───────────────────────────────────────────────────────────────────

def append_history(
    med: dict,
    event: str,
    dose_taken: Optional[str] = None,
    notes: str = "",
) -> None:
    entry: dict = {
        "timestamp": now_iso(),
        "event": event,
        "dose_prescribed": f"{med['dose']}{med['unit']}",
        "dose_taken": dose_taken,
    }
    if notes:
        entry["notes"] = notes
    hist = med["state"]["history"]
    hist.append(entry)
    if len(hist) > HISTORY_MAX:
        med["state"]["history"] = hist[-HISTORY_MAX:]

# ── Advisory skip logic ───────────────────────────────────────────────────────

def already_confirmed(med: dict, dose_time: datetime) -> bool:
    """True if last_taken >= dose_time (user already confirmed this dose)."""
    last = med["state"].get("last_taken")
    if not last:
        return False
    return datetime.fromisoformat(last) >= dose_time

def skip_if_needed(state: dict, med: dict, dose_time: datetime, mode: str) -> bool:
    """Return True and log reason if this event should be skipped."""
    if state["global"]["paused"]:
        log.info("SKIP [%s] %s — global pause active", mode, med["id"])
        return True
    if med.get("paused"):
        log.info("SKIP [%s] %s — med paused", mode, med["id"])
        return True
    if med["state"]["status"] == "deferred":
        log.info("SKIP [%s] %s — med deferred until next digest", mode, med["id"])
        return True
    if already_confirmed(med, dose_time):
        log.info("SKIP [%s] %s — already confirmed (last_taken >= dose_time)", mode, med["id"])
        return True
    return False

# ── Med lookup ────────────────────────────────────────────────────────────────

def find_med(state: dict, med_id: str) -> dict:
    for med in state["meds"]:
        if med["id"] == med_id:
            return med
    known = [m["id"] for m in state["meds"]]
    sys.exit(f"Med '{med_id}' not found. Known IDs: {known}")

def dose_time_for(med: dict, dose_index: int, tz: ZoneInfo, now: datetime) -> datetime:
    """Reference dose time for advisory checks. Interval meds use next_due."""
    if med["schedule"]["frequency"] == "interval":
        nd = med["state"].get("next_due")
        return datetime.fromisoformat(nd) if nd else now
    return most_recent_dose_time(med["schedule"]["times"], dose_index, tz, now)

# ── Mode handlers ─────────────────────────────────────────────────────────────

def handle_fire(med_id: str, dose_index: int, dry_run: bool) -> None:
    """
    Fire at the scheduled dose time. Send the initial reminder.
    Advisory: exits silently if dose already confirmed or deferred.
    """
    state = load_state()
    tz    = get_tz(state)
    now   = datetime.now(tz=tz)
    med   = find_med(state, med_id)
    dose_time = dose_time_for(med, dose_index, tz, now)

    if in_quiet_hours(now, state["global"]["quiet_hours"], tz):
        log.info("SKIP [fire] %s — quiet hours", med_id)
        return

    if skip_if_needed(state, med, dose_time, "fire"):
        return

    msg = f"Reminder: {med['name']} {med['dose']}{med['unit']}"
    if med["schedule"].get("with_food"):
        msg += " — take with food"
    if med["schedule"].get("notes"):
        msg += f"\n{med['schedule']['notes']}"

    send_message(msg, dry_run)

    med["state"]["status"]       = "reminded"
    med["state"]["last_reminded"] = now.isoformat()
    save_state(state, dry_run)
    log.info("FIRE [%s] reminder sent at %s", med_id, now.isoformat())


def handle_check(med_id: str, dose_index: int, dry_run: bool) -> None:
    """
    LATE check at dose_time + late_threshold. Send nudge if not yet confirmed.
    Advisory: exits silently if confirmed since the reminder was sent.
    """
    state = load_state()
    tz    = get_tz(state)
    now   = datetime.now(tz=tz)
    med   = find_med(state, med_id)
    dose_time = dose_time_for(med, dose_index, tz, now)

    if in_quiet_hours(now, state["global"]["quiet_hours"], tz):
        log.info("SKIP [check] %s — quiet hours", med_id)
        return

    if skip_if_needed(state, med, dose_time, "check"):
        return

    send_message(expand(med["escalation"]["late_message"], med), dry_run)

    med["state"]["status"]       = "late"
    med["state"]["last_reminded"] = now.isoformat()
    append_history(med, "late_nudge_sent")
    save_state(state, dry_run)
    log.info("CHECK [%s] late nudge sent at %s", med_id, now.isoformat())


def handle_miss(med_id: str, dose_index: int, dry_run: bool) -> None:
    """
    MISSED at dose_time + missed_threshold. Log as missed, advance next_due.
    Advisory: exits silently if confirmed.
    """
    state = load_state()
    tz    = get_tz(state)
    now   = datetime.now(tz=tz)
    med   = find_med(state, med_id)
    dose_time = dose_time_for(med, dose_index, tz, now)

    if skip_if_needed(state, med, dose_time, "miss"):
        return

    send_message(expand(med["escalation"]["missed_message"], med), dry_run)

    med["state"]["status"]       = "missed"
    med["state"]["missed_count"] += 1
    append_history(med, "missed")

    # Advance next_due past the missed dose so the schedule stays correct
    nd = compute_next_due(med, dose_time, tz)
    med["state"]["next_due"] = nd.isoformat() if nd else None

    save_state(state, dry_run)
    log.info(
        "MISS [%s] logged at %s — missed_count=%d, next_due=%s",
        med_id, now.isoformat(), med["state"]["missed_count"], med["state"]["next_due"],
    )


def handle_digest(dry_run: bool) -> None:
    """
    Send the daily med summary. Reset all deferred meds to pending.
    Skips if global pause is active.
    """
    state = load_state()
    tz    = get_tz(state)
    now   = datetime.now(tz=tz)

    if state["global"]["paused"]:
        log.info("SKIP [digest] — global pause active")
        return

    # Reset deferred meds before building the message
    reset_count = sum(
        1 for med in state["meds"]
        if med["state"]["status"] == "deferred"
        and not (med.get("paused") or state["global"]["paused"])
    )
    for med in state["meds"]:
        if med["state"]["status"] == "deferred":
            med["state"]["status"] = "pending"
    if reset_count:
        log.info("DIGEST reset %d deferred med(s) to pending", reset_count)

    active = [m for m in state["meds"] if not m.get("paused")]

    if not active:
        log.info("DIGEST — no active meds, skipping message")
        if reset_count:
            save_state(state, dry_run)
        return

    # Build digest message
    date_str = now.strftime("%a %b ") + str(now.day)  # "Mon Apr 21" (cross-platform)
    lines = [f"Meds for {date_str}:"]

    for i, med in enumerate(active, 1):
        sched = med["schedule"]
        freq  = sched["frequency"]

        if freq == "interval":
            time_str = f"every {sched['interval_hours']}h"
        elif freq == "as_needed":
            time_str = "as needed"
        else:
            time_str = ", ".join(fmt_12h(t) for t in sched["times"])

        food = " (with food)" if sched.get("with_food") else ""
        lines.append(f"{i}. {med['name']} {med['dose']}{med['unit']} | {time_str}{food}")

    lines += [
        "",
        "Reply: 'all taken' | 'skip [name]' | 'took [name]: [dose]' | 'done [name1], [name2]'",
    ]

    send_message("\n".join(lines), dry_run)
    save_state(state, dry_run)
    log.info("DIGEST sent (%d active med(s))", len(active))


def handle_reset_deferred(dry_run: bool) -> None:
    """
    Midnight reset: all deferred meds → pending.
    Registered by setup-tasks.py at 00:00 daily.
    """
    state = load_state()
    changed = False
    for med in state["meds"]:
        if med["state"]["status"] == "deferred":
            med["state"]["status"] = "pending"
            changed = True
            log.info("RESET-DEFERRED [%s] → pending", med["id"])

    if changed:
        save_state(state, dry_run)
        log.info("RESET-DEFERRED complete")
    else:
        log.debug("RESET-DEFERRED — nothing to reset")

# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Med reminder dispatch. Call from Task Scheduler or crontab.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without sending messages or writing state",
    )

    sub = parser.add_subparsers(dest="mode", required=True, metavar="MODE")

    p_fire = sub.add_parser("fire", help="Send initial dose reminder")
    p_fire.add_argument("med_id",    help="Med ID, e.g. med-001")
    p_fire.add_argument("dose_index", type=int, help="Index into schedule.times[] (0-based)")

    p_check = sub.add_parser("check", help="Send LATE nudge if unconfirmed")
    p_check.add_argument("med_id")
    p_check.add_argument("dose_index", type=int)

    p_miss = sub.add_parser("miss", help="Log as MISSED if still unconfirmed")
    p_miss.add_argument("med_id")
    p_miss.add_argument("dose_index", type=int)

    sub.add_parser("digest",         help="Send daily med summary, reset deferred")
    sub.add_parser("reset-deferred", help="Midnight: reset deferred meds to pending")
    # confirm / defer live in reply.py — use: python3 reply.py confirm <med-id>

    args = parser.parse_args()
    setup_logging(args.dry_run)

    mode = args.mode
    dr   = args.dry_run

    if mode == "fire":
        handle_fire(args.med_id, args.dose_index, dr)
    elif mode == "check":
        handle_check(args.med_id, args.dose_index, dr)
    elif mode == "miss":
        handle_miss(args.med_id, args.dose_index, dr)
    elif mode == "digest":
        handle_digest(dr)
    elif mode == "reset-deferred":
        handle_reset_deferred(dr)


if __name__ == "__main__":
    main()
