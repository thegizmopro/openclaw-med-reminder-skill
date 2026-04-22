#!/usr/bin/env python3
"""
safe_write.py — atomic state write with structural validation and optimistic concurrency.

Pure-Python replacement for safe-write.sh. No bash / Git Bash required.
Uses os.replace() for atomic rename (works on NTFS since Python 3.3).

Usage (CLI — reads new state from stdin):
    echo '{...}' | python3 safe_write.py
    python3 safe_write.py < new-state.json

Usage (imported):
    from safe_write import safe_write
    safe_write(new_state_dict)          # dict
    safe_write('{"version": ...}')      # JSON string

Environment overrides:
    MEDS_STATE_FILE   path to meds-state.json   (default: same dir as this file)
    MEDS_LOG_FILE     path to write log         (default: same dir as this file)
"""

import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

SCRIPT_DIR   = Path(__file__).parent.resolve()
STATE_FILE   = Path(os.environ.get("MEDS_STATE_FILE",  SCRIPT_DIR / "meds-state.json"))
LOG_FILE     = Path(os.environ.get("MEDS_LOG_FILE",    SCRIPT_DIR / "safe-write.log"))
LOCK_DIR     = Path(str(STATE_FILE) + ".lock")
BACKUP_FILE  = Path(str(STATE_FILE) + ".bak")
TMP_FILE     = Path(str(STATE_FILE) + ".tmp")
LOCK_TIMEOUT = 30   # seconds before declaring lock stale
LOCK_WAIT    = 5    # seconds to wait before giving up on a live lock
HISTORY_MAX  = 30   # must match dispatch.HISTORY_MAX

log = logging.getLogger("safe_write")

# ── Logging ────────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    if log.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [safe_write] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

# ── Validation (mirrors safe-write.sh inline Python) ──────────────────────────

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_ISO  = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

_VALID_TYPES    = {"pill","liquid","injection","patch","inhaler","other"}
_VALID_FREQS    = {"once_daily","twice_daily","interval","weekly","as_needed"}
_VALID_STATUSES = {"pending","reminded","confirmed","late","missed","deferred"}
_VALID_EVENTS   = {"taken","missed","late_nudge_sent","deferred"}
_MED_ID         = re.compile(r"^med-[0-9]{3,}$")


def _die(msg: str) -> None:
    raise ValueError(msg)


def validate(state: dict) -> None:
    """Raise ValueError with a human-readable message if state is invalid."""
    if not isinstance(state, dict):
        _die("State must be a JSON object")

    for field in ("version", "global", "meds"):
        if field not in state:
            _die(f"Missing top-level field: '{field}'")

    if state["version"] != "1":
        _die(f"Unknown schema version '{state['version']}' (expected '1')")

    if not isinstance(state["meds"], list):
        _die("'meds' must be an array")

    g = state["global"]
    for field in ("timezone", "quiet_hours", "paused", "digest_time", "delivery_channel"):
        if field not in g:
            _die(f"global.{field} is required")

    if not isinstance(g["paused"], bool):
        _die("global.paused must be a boolean")

    if not isinstance(g["delivery_channel"], str) or not g["delivery_channel"].strip():
        _die("global.delivery_channel must be a non-empty string (e.g. 'whatsapp', 'telegram', 'sms')")

    for field in ("start", "end"):
        if field not in g["quiet_hours"]:
            _die(f"global.quiet_hours.{field} is required")
        if not _HHMM.match(g["quiet_hours"][field] or ""):
            _die(f"global.quiet_hours.{field} must be HH:MM (24h, zero-padded)")

    if not _HHMM.match(g["digest_time"] or ""):
        _die(f"global.digest_time must be HH:MM (24h, zero-padded)")

    seen_ids: set = set()
    for i, med in enumerate(state["meds"]):
        p = f"meds[{i}]"

        for field in ("id","name","type","dose","unit","paused","schedule","escalation","state"):
            if field not in med:
                _die(f"{p}.{field} is required")

        mid = med["id"]
        if not _MED_ID.match(mid or ""):
            _die(f"{p}.id '{mid}' must match pattern med-NNN")
        if mid in seen_ids:
            _die(f"{p}: duplicate id '{mid}'")
        seen_ids.add(mid)

        if med["type"] not in _VALID_TYPES:
            _die(f"{p}.type must be one of {sorted(_VALID_TYPES)}")

        if not isinstance(med["dose"], str) or not med["dose"].strip():
            _die(f"{p}.dose must be a non-empty string")

        if not isinstance(med["unit"], str) or not med["unit"].strip():
            _die(f"{p}.unit must be a non-empty string")

        if not isinstance(med["paused"], bool):
            _die(f"{p}.paused must be a boolean")

        sched = med["schedule"]
        if "frequency" not in sched:
            _die(f"{p}.schedule.frequency is required")
        freq = sched["frequency"]
        if freq not in _VALID_FREQS:
            _die(f"{p}.schedule.frequency must be one of {sorted(_VALID_FREQS)}")

        if freq == "interval":
            ih = sched.get("interval_hours")
            if ih is None or not isinstance(ih, int) or ih < 1 or ih > 168:
                _die(f"{p}.schedule.interval_hours must be integer 1-168 when frequency is 'interval'")
        elif freq != "as_needed":
            times = sched.get("times")
            if not times or not isinstance(times, list) or len(times) == 0:
                _die(f"{p}.schedule.times required when frequency is '{freq}'")
            for t in times:
                if not _HHMM.match(str(t)):
                    _die(f"{p}.schedule.times contains invalid time '{t}' (expected HH:MM)")

        esc = med["escalation"]
        for field in ("late_threshold_minutes","missed_threshold_minutes","late_message","missed_message"):
            if field not in esc:
                _die(f"{p}.escalation.{field} is required")

        if not isinstance(esc["late_threshold_minutes"], int) or esc["late_threshold_minutes"] < 1:
            _die(f"{p}.escalation.late_threshold_minutes must be a positive integer")
        if not isinstance(esc["missed_threshold_minutes"], int) or esc["missed_threshold_minutes"] < 1:
            _die(f"{p}.escalation.missed_threshold_minutes must be a positive integer")
        if esc["late_threshold_minutes"] >= esc["missed_threshold_minutes"]:
            _die(f"{p}: late_threshold_minutes must be less than missed_threshold_minutes")

        mstate = med["state"]
        for field in ("status","last_taken","last_reminded","next_due","missed_count","history"):
            if field not in mstate:
                _die(f"{p}.state.{field} is required")

        if mstate["status"] not in _VALID_STATUSES:
            _die(f"{p}.state.status must be one of {sorted(_VALID_STATUSES)}")

        if not isinstance(mstate["missed_count"], int) or mstate["missed_count"] < 0:
            _die(f"{p}.state.missed_count must be a non-negative integer")

        if not isinstance(mstate["history"], list):
            _die(f"{p}.state.history must be an array")

        if len(mstate["history"]) > HISTORY_MAX:
            _die(f"{p}.state.history has {len(mstate['history'])} entries (max {HISTORY_MAX})")

        for field in ("last_taken", "last_reminded", "next_due"):
            val = mstate[field]
            if val is not None and not _ISO.match(str(val)):
                _die(f"{p}.state.{field} must be ISO 8601 with timezone or null")

        for j, entry in enumerate(mstate["history"]):
            ep = f"{p}.state.history[{j}]"
            for field in ("timestamp", "event", "dose_prescribed"):
                if field not in entry:
                    _die(f"{ep}.{field} is required")
            if entry["event"] not in _VALID_EVENTS:
                _die(f"{ep}.event must be one of {sorted(_VALID_EVENTS)}")
            if not _ISO.match(str(entry["timestamp"])):
                _die(f"{ep}.timestamp must be ISO 8601 with timezone")


# ── Locking ────────────────────────────────────────────────────────────────────

def _acquire_lock() -> None:
    """Acquire lock via os.mkdir (atomic on all platforms). Clears stale locks."""
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            LOCK_DIR.mkdir()
            return
        except FileExistsError:
            # Check for stale lock
            try:
                age = time.time() - LOCK_DIR.stat().st_mtime
                if age > LOCK_TIMEOUT:
                    log.warning("Stale lock detected (%.0fs old) — clearing", age)
                    LOCK_DIR.rmdir()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Could not acquire lock after {LOCK_WAIT}s — another process may be writing"
                )
            time.sleep(0.1)


def _release_lock() -> None:
    try:
        LOCK_DIR.rmdir()
    except OSError:
        pass


# ── Core write ─────────────────────────────────────────────────────────────────

def safe_write(new_state: Union[dict, str, bytes]) -> None:
    """
    Validate and atomically write new_state to STATE_FILE.

    Steps:
      1. Parse input (dict, JSON string, or bytes)
      2. Validate structure
      3. Record mtime of current state (optimistic concurrency baseline)
      4. Write to .tmp file
      5. Acquire lockfile
      6. Re-check mtime (abort if changed)
      7. Backup existing state
      8. os.replace() .tmp → STATE_FILE (atomic on NTFS + POSIX)
      9. Release lock
      10. Log success
    """
    _setup_logging()

    # Step 1 — parse
    if isinstance(new_state, (str, bytes)):
        try:
            state = json.loads(new_state)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
    else:
        state = new_state

    # Step 2 — validate
    validate(state)

    payload = json.dumps(state, indent=2)

    # Step 3 — record mtime before acquiring lock
    mtime_before = STATE_FILE.stat().st_mtime if STATE_FILE.exists() else 0

    # Step 4 — write to tmp
    TMP_FILE.write_text(payload, encoding="utf-8")

    # Step 5 — acquire lock
    _acquire_lock()
    try:
        # Step 6 — optimistic concurrency check
        if STATE_FILE.exists():
            mtime_after = STATE_FILE.stat().st_mtime
            if mtime_after != mtime_before:
                raise RuntimeError(
                    f"Concurrent write detected — state changed since read "
                    f"(mtime {mtime_before} -> {mtime_after}). "
                    "Caller should re-read and retry."
                )

        # Step 7 — backup
        if STATE_FILE.exists():
            shutil.copy2(STATE_FILE, BACKUP_FILE)

        # Step 8 — atomic replace
        os.replace(TMP_FILE, STATE_FILE)

    finally:
        # Step 9 — always release lock, always clean tmp
        _release_lock()
        if TMP_FILE.exists():
            TMP_FILE.unlink(missing_ok=True)

    # Step 10 — log
    med_count = len(state.get("meds", []))
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"[{ts}] [safe_write] Write OK — {med_count} med(s) — backup: {BACKUP_FILE}"
    log.info("Write OK — %d med(s) — backup: %s", med_count, BACKUP_FILE)
    print(msg)


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    _setup_logging()
    content = sys.stdin.read()
    if not content.strip():
        print("ERROR: empty input", file=sys.stderr)
        sys.exit(1)
    try:
        safe_write(content)
    except (ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
