#!/usr/bin/env bash
# safe-write.sh — atomic state write with structural validation and optimistic concurrency
#
# Usage:
#   safe-write.sh <new-state.json>      # write from file
#   echo '{...}' | safe-write.sh        # write from stdin
#
# Environment overrides:
#   MEDS_STATE_FILE   path to meds-state.json   (default: same dir as script)
#   MEDS_SCHEMA_FILE  path to meds.schema.json  (default: same dir as script)
#   MEDS_LOG_FILE     path to write log         (default: same dir as script)

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${MEDS_STATE_FILE:-$SCRIPT_DIR/meds-state.json}"
SCHEMA_FILE="${MEDS_SCHEMA_FILE:-$SCRIPT_DIR/meds.schema.json}"
LOCK_DIR="${STATE_FILE}.lock"
BACKUP_FILE="${STATE_FILE}.bak"
TMP_FILE="${STATE_FILE}.tmp"
LOG_FILE="${MEDS_LOG_FILE:-$SCRIPT_DIR/safe-write.log}"
LOCK_TIMEOUT_SECONDS=30
LOCK_HELD=false

# On Windows/Git Bash, Python needs native paths (C:/...) not POSIX paths (/c/...).
# cygpath -m converts to forward-slash Windows paths Python accepts on all platforms.
to_py() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$1"
    else
        echo "$1"
    fi
}

STATE_PY=$(to_py "$STATE_FILE")
TMP_PY=$(to_py "$TMP_FILE")

# ── Helpers ───────────────────────────────────────────────────────────────────

log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [safe-write] $*"
    echo "$msg" >> "$LOG_FILE"
    echo "$msg"
}

die() {
    log "ERROR: $*"
    exit 1
}

cleanup() {
    rm -f "$TMP_FILE"
    if [[ "$LOCK_HELD" == "true" ]]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
        LOCK_HELD=false
    fi
}

trap cleanup EXIT

# ── Python check ──────────────────────────────────────────────────────────────

python3 --version > /dev/null 2>&1 || die "Python 3 is required but not found in PATH"

PYVER=$(python3 -c "import sys; print(sys.version_info.major * 10 + sys.version_info.minor)")
[[ "$PYVER" -ge 39 ]] || die "Python 3.9+ required (found $(python3 --version 2>&1))"

# ── Step 1: Read new content into tmp file ────────────────────────────────────

if [[ $# -ge 1 ]]; then
    [[ -f "$1" ]] || die "Input file not found: $1"
    cp "$1" "$TMP_FILE"
else
    # Read from stdin
    cat > "$TMP_FILE"
fi

[[ -s "$TMP_FILE" ]] || die "New state content is empty"

# ── Step 2: Record mtime of current state (optimistic concurrency baseline) ───
# Must happen BEFORE acquiring the lock so we can detect concurrent writes.

if [[ -f "$STATE_FILE" ]]; then
    MTIME_BEFORE=$(python3 -c "import sys,os; print(int(os.path.getmtime(sys.argv[1])))" "$STATE_PY")
else
    MTIME_BEFORE=0
fi

# ── Step 3: Structural validation ─────────────────────────────────────────────
# Full ajv validation lives in the HTML editor. This validates required structure
# so corrupt state can never reach disk. Uses Python 3 stdlib — no pip installs.

VALIDATE_FILE="$TMP_PY" python3 << 'PYEOF'
import json, sys, os, re

def die(msg):
    print(f"VALIDATION ERROR: {msg}", file=sys.stderr)
    sys.exit(1)

tmp_file = os.environ["VALIDATE_FILE"]

# Valid JSON
try:
    with open(tmp_file) as f:
        state = json.load(f)
except json.JSONDecodeError as e:
    die(f"Invalid JSON — {e}")

# Top-level fields
for field in ["version", "global", "meds"]:
    if field not in state:
        die(f"Missing top-level field: '{field}'")

if state["version"] != "1":
    die(f"Unknown schema version '{state['version']}' (expected '1')")

if not isinstance(state["meds"], list):
    die("'meds' must be an array")

# Global config
g = state["global"]
for field in ["timezone", "quiet_hours", "paused", "digest_time", "delivery_channel"]:
    if field not in g:
        die(f"global.{field} is required")

if not isinstance(g["paused"], bool):
    die("global.paused must be a boolean")

if g["delivery_channel"] not in ["whatsapp"]:
    die(f"global.delivery_channel '{g['delivery_channel']}' is not a supported channel")

hhmm = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

for field in ["start", "end"]:
    if field not in g["quiet_hours"]:
        die(f"global.quiet_hours.{field} is required")
    if not hhmm.match(g["quiet_hours"][field]):
        die(f"global.quiet_hours.{field} must be HH:MM (24h, zero-padded), got '{g['quiet_hours'][field]}'")

if not hhmm.match(g["digest_time"]):
    die(f"global.digest_time must be HH:MM (24h, zero-padded), got '{g['digest_time']}'")

# Meds
seen_ids = set()
valid_types = ["pill", "liquid", "injection", "patch", "inhaler", "other"]
valid_freqs = ["once_daily", "twice_daily", "interval", "weekly", "as_needed"]
valid_statuses = ["pending", "reminded", "confirmed", "late", "missed", "deferred"]
valid_events = ["taken", "missed", "late_nudge_sent", "deferred"]

for i, med in enumerate(state["meds"]):
    p = f"meds[{i}]"

    for field in ["id", "name", "type", "dose", "unit", "paused", "schedule", "escalation", "state"]:
        if field not in med:
            die(f"{p}.{field} is required")

    mid = med["id"]
    if not re.match(r"^med-[0-9]{3,}$", mid):
        die(f"{p}.id '{mid}' must match pattern med-NNN (e.g. med-001)")
    if mid in seen_ids:
        die(f"{p}: duplicate id '{mid}'")
    seen_ids.add(mid)

    if med["type"] not in valid_types:
        die(f"{p}.type '{med['type']}' must be one of {valid_types}")

    if not isinstance(med["dose"], str) or not med["dose"].strip():
        die(f"{p}.dose must be a non-empty string")

    if not isinstance(med["unit"], str) or not med["unit"].strip():
        die(f"{p}.unit must be a non-empty string")

    if not isinstance(med["paused"], bool):
        die(f"{p}.paused must be a boolean")

    # Schedule
    sched = med["schedule"]
    if "frequency" not in sched:
        die(f"{p}.schedule.frequency is required")
    if sched["frequency"] not in valid_freqs:
        die(f"{p}.schedule.frequency must be one of {valid_freqs}")

    if sched["frequency"] == "interval":
        ih = sched.get("interval_hours")
        if ih is None or not isinstance(ih, int) or ih < 1 or ih > 168:
            die(f"{p}.schedule.interval_hours must be an integer 1–168 when frequency is 'interval'")
    elif sched["frequency"] != "as_needed":
        times = sched.get("times")
        if not times or not isinstance(times, list) or len(times) == 0:
            die(f"{p}.schedule.times required when frequency is '{sched['frequency']}'")
        for t in times:
            if not hhmm.match(str(t)):
                die(f"{p}.schedule.times contains invalid time '{t}' (expected HH:MM)")

    # Escalation
    esc = med["escalation"]
    for field in ["late_threshold_minutes", "missed_threshold_minutes", "late_message", "missed_message"]:
        if field not in esc:
            die(f"{p}.escalation.{field} is required")

    if not isinstance(esc["late_threshold_minutes"], int) or esc["late_threshold_minutes"] < 1:
        die(f"{p}.escalation.late_threshold_minutes must be a positive integer")
    if not isinstance(esc["missed_threshold_minutes"], int) or esc["missed_threshold_minutes"] < 1:
        die(f"{p}.escalation.missed_threshold_minutes must be a positive integer")
    if esc["late_threshold_minutes"] >= esc["missed_threshold_minutes"]:
        die(f"{p}: late_threshold_minutes ({esc['late_threshold_minutes']}) must be "
            f"less than missed_threshold_minutes ({esc['missed_threshold_minutes']})")

    # State
    mstate = med["state"]
    for field in ["status", "last_taken", "last_reminded", "next_due", "missed_count", "history"]:
        if field not in mstate:
            die(f"{p}.state.{field} is required")

    if mstate["status"] not in valid_statuses:
        die(f"{p}.state.status '{mstate['status']}' must be one of {valid_statuses}")

    if not isinstance(mstate["missed_count"], int) or mstate["missed_count"] < 0:
        die(f"{p}.state.missed_count must be a non-negative integer")

    if not isinstance(mstate["history"], list):
        die(f"{p}.state.history must be an array")

    if len(mstate["history"]) > 30:
        die(f"{p}.state.history has {len(mstate['history'])} entries (max 30)")

    iso = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
    for field in ["last_taken", "last_reminded", "next_due"]:
        val = mstate[field]
        if val is not None and not iso.match(str(val)):
            die(f"{p}.state.{field} must be an ISO 8601 datetime with timezone or null, got '{val}'")

    # History entries
    for j, entry in enumerate(mstate["history"]):
        ep = f"{p}.state.history[{j}]"
        for field in ["timestamp", "event", "dose_prescribed"]:
            if field not in entry:
                die(f"{ep}.{field} is required")
        if entry["event"] not in valid_events:
            die(f"{ep}.event '{entry['event']}' must be one of {valid_events}")
        if not iso.match(str(entry["timestamp"])):
            die(f"{ep}.timestamp must be ISO 8601 with timezone")

print("OK")
PYEOF

# shellcheck disable=SC2181
[[ $? -eq 0 ]] || die "Validation failed — state file not written"

# ── Step 4: Acquire lockfile (mkdir is atomic on POSIX and Git Bash) ──────────

LOCK_START=$SECONDS
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    # Check for stale lock
    if [[ -d "$LOCK_DIR" ]]; then
        LOCK_DIR_PY=$(to_py "$LOCK_DIR")
        LOCK_AGE=$(python3 -c "import sys,time,os; print(int(time.time()-os.path.getmtime(sys.argv[1])))" "$LOCK_DIR_PY" 2>/dev/null || echo "0")
        if [[ "$LOCK_AGE" -gt "$LOCK_TIMEOUT_SECONDS" ]]; then
            log "Stale lock detected (${LOCK_AGE}s old) — clearing"
            rmdir "$LOCK_DIR" 2>/dev/null || true
            continue
        fi
    fi
    if [[ $(( SECONDS - LOCK_START )) -ge 5 ]]; then
        die "Could not acquire lock after 5s — another process may be writing"
    fi
    sleep 0.1
done
LOCK_HELD=true

# ── Step 5: Optimistic concurrency check (re-read mtime under lock) ───────────
# If the file changed between our read (step 2) and acquiring the lock (step 4),
# another writer beat us. Our in-memory state is stale — abort and let the
# caller retry with fresh state on the next scheduled event.

if [[ -f "$STATE_FILE" ]]; then
    MTIME_AFTER=$(python3 -c "import sys,os; print(int(os.path.getmtime(sys.argv[1])))" "$STATE_PY")
    if [[ "$MTIME_AFTER" != "$MTIME_BEFORE" ]]; then
        die "Concurrent write detected — state changed since read (mtime ${MTIME_BEFORE} → ${MTIME_AFTER}). Aborting to avoid overwriting newer state. Caller should re-read and retry."
    fi
fi

# ── Step 6: Backup existing state ─────────────────────────────────────────────

if [[ -f "$STATE_FILE" ]]; then
    cp "$STATE_FILE" "$BACKUP_FILE"
fi

# ── Step 7: Atomic rename ─────────────────────────────────────────────────────

mv "$TMP_FILE" "$STATE_FILE"

# ── Step 8: Release lock ──────────────────────────────────────────────────────

rmdir "$LOCK_DIR"
LOCK_HELD=false

# ── Step 9: Log success ───────────────────────────────────────────────────────

MED_COUNT=$(python3 -c "import sys,json; s=json.load(open(sys.argv[1])); print(len(s['meds']))" "$STATE_PY")
log "Write OK — ${MED_COUNT} med(s) — backup: $BACKUP_FILE"
