"""
event_store.py — append-only history.jsonl event store (issue #1 PR 1).

history.jsonl is the source of truth for dose events; meds-state.json is a
cache (issue §0). One JSON object per line:

    {id, ts_utc, med_id, event, occurrence_key, occurrence_utc, generation,
     stage, dose_prescribed, dose_taken, taken_at_utc, ref, attempt,
     lease_until_utc, error}

Appends flush + fsync so a crash never loses a recorded event. Readers
dedupe by id and skip a torn last line (a crash mid-append), so a forced
retry never duplicates history.

Event vocabulary: taken, skipped, missed, undone, snoozed, send_intent,
sent, send_failed, clock_anomaly. `reminded`/`late_nudge_sent` are dead —
expressed as send_intent/sent with `stage` set (issue §1).
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("event_store")

VALID_EVENTS = frozenset({
    "taken", "skipped", "missed", "undone", "snoozed",
    "send_intent", "sent", "send_failed", "clock_anomaly",
})

VALID_STAGES = frozenset({
    "reminder", "snooze_reminder", "nudge", "catchup", "overnight", "digest",
})

# Serialization field order (issue §1); absent fields are omitted, not nulled.
_EVENT_FIELDS = (
    "id", "ts_utc", "med_id", "event",
    "occurrence_key", "occurrence_utc", "generation", "stage",
    "dose_prescribed", "dose_taken", "taken_at_utc",
    "ref", "attempt", "lease_until_utc", "error",
)
_OPTIONAL_FIELDS = _EVENT_FIELDS[4:]


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def new_event(med_id: str, event: str, ts_utc: datetime,
              occurrence=None, **fields) -> dict:
    """
    Build one event dict. `occurrence` (occurrence.Occurrence) fills
    occurrence_key/occurrence_utc; other optional fields per _EVENT_FIELDS.
    An explicit `id` overrides the generated uuid4 (used by migration).
    """
    if event not in VALID_EVENTS:
        raise ValueError(f"unknown event '{event}' (valid: {sorted(VALID_EVENTS)})")
    ev = {
        "id": fields.pop("id", None) or str(uuid.uuid4()),
        "ts_utc": _iso(ts_utc),
        "med_id": med_id,
        "event": event,
    }
    if occurrence is not None:
        ev["occurrence_key"] = occurrence.key
        ev["occurrence_utc"] = _iso(occurrence.utc)
    for key in _OPTIONAL_FIELDS:
        if key in fields and fields[key] is not None:
            ev[key] = fields.pop(key)
    if "stage" in ev and ev["stage"] not in VALID_STAGES:
        raise ValueError(f"unknown stage '{ev['stage']}' (valid: {sorted(VALID_STAGES)})")
    if fields:
        raise TypeError(f"unknown event fields: {sorted(fields)}")
    return ev


def append_events(path, events: Iterable[dict], logger=None) -> list:
    """
    Append events whose id is not already stored (one JSON line each,
    flush + fsync). Returns the events actually appended.

    Callers must hold the state lock — safe_write.transact does.
    """
    path = Path(path)
    events = list(events)
    if not events:
        return []

    known = set()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    known.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # a torn tail must not block appends; load_events reports it

    fresh, seen = [], set()
    for ev in events:
        eid = ev.get("id")
        if not eid or eid in known or eid in seen:
            continue
        seen.add(eid)
        fresh.append(ev)
    if not fresh:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for ev in fresh:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return fresh


def load_events(path, logger=None) -> list:
    """
    All stored events in file order, deduped by id (last line wins).

    A torn last line — a crash mid-append — is skipped and logged. A bad
    line anywhere else is real corruption and raises ValueError.
    """
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        lines = f.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline

    events, index = [], {}
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            eid = ev["id"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            if i == len(lines) - 1:
                (logger or log).warning(
                    "event_store: torn last line skipped (%d bytes): %s", len(line), e)
                continue
            raise ValueError(f"history corrupt at line {i + 1}: {e}") from e
        if eid in index:
            events[index[eid]] = ev  # last wins, order preserved
        else:
            index[eid] = len(events)
            events.append(ev)
    return events


# ── Outcome resolution ─────────────────────────────────────────────────────────
#
# Terminal outcome per (occurrence, generation). Precedence (issue §1):
# taken > skipped > missed — a late `taken` overrides an earlier `missed`,
# so adherence counts the dose as taken (late).

_PRECEDENCE = {"taken": 3, "skipped": 2, "missed": 1}


def outcome(events: Iterable[dict], occurrence_key: str, generation: int = 0) -> Optional[str]:
    """Resolve the terminal outcome of (occurrence, generation), or None."""
    best, best_rank, best_ts = None, 0, ""
    for ev in events:
        if ev.get("occurrence_key") != occurrence_key:
            continue
        if int(ev.get("generation") or 0) != generation:
            continue
        rank = _PRECEDENCE.get(ev.get("event"))
        if rank is None:
            continue
        ts = str(ev.get("ts_utc") or "")
        if rank > best_rank or (rank == best_rank and ts > best_ts):
            best, best_rank, best_ts = ev["event"], rank, ts
    return best
