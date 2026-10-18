"""
tick.py — the tick engine (issue #1 §3, PR 2).

One entry point, run every N minutes (default 5). Each tick:

  1. Takes the non-blocking tick-instance lock (tick.lock) and exits 0
     at once if another tick holds it.
  2. Checks the clock against the watermark (a backwards jump logs
     clock_anomaly and exits; a gap over 72h caps catch-up and sends
     one "were you away?" message instead of back-filling).
  3. Enumerates occurrences per med in (max(watermark, active_since,
     now − LOOKBACK), now] using the PR 1 Occurrence helpers — all
     comparisons on aware UTC datetimes.
  4. Per occurrence only the highest due stage acts: reminder (quiet-
     hours exempt — the user chose the time), snooze_reminder, nudge
     (muted in quiet hours except the first 30 min), missed (always
     logged; message muted in quiet hours). Catch-up past the missed
     deadline writes silent misses plus ONE consolidated message.
  5. Sends are at-least-once with leases: send_intent{lease, attempt}
     is decided under transact, the subprocess runs outside the lock
     (60s timeout), and a second transact records sent/send_failed.
     Retries need an expired lease; retries after an unknown outcome
     are prefixed "(repeat — ignore if you already got this)". A
     SENDER_DOWN marker tracks outages; the first successful send
     afterwards prepends the outage window.
  6. Digest and overnight summary are stages keyed by local date,
     once/day, skipped if more than 4h late (folded into catch-up).
  7. Per-med try/except isolates faults (one alert/day per failing
     med); a bad timezone falls back to the last-good tz with an alert.
  8. The watermark advances in tick-heartbeat.json, written every run
     SEPARATELY from state — state (and .bak) is written only when
     events occur.

Snoozed events (written by reply.py snooze, PR 4) carry the wake
instant in `ref` (ISO) alongside the occurrence they snooze.

plan_tick(state, history, now, …) is PURE: it mutates only the state
dict it is given (the caller passes a copy) and returns a Plan of
events to append and sends to execute. Running it twice at the same
`now` produces the same events (deterministic ids) and no duplicate
sends.
"""

import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import attribution
import dispatch
import event_store
import occurrence
import safe_write as sw
from clock import Clock, get_clock, parse_instant
from occurrence import Occurrence

log = logging.getLogger("tick")

UTC = timezone.utc

# ── Constants (issue #1 §3) ────────────────────────────────────────────────────

LEASE = timedelta(minutes=5)          # send_intent lease
SEND_TIMEOUT = 60                     # subprocess timeout, seconds
CLOCK_BACK_TOLERANCE = timedelta(minutes=5)   # now < wm − 5min → anomaly
CATCHUP_CAP = timedelta(hours=72)     # gap beyond this → cap + "were you away?"
ANOMALY_LOG_INTERVAL = timedelta(hours=1)     # dedupe repeated clock_anomaly
DAILY_LATE_LIMIT = timedelta(hours=4)         # digest/overnight skipped past this
MISSED_FRESH_GRACE_BASE = timedelta(minutes=10)  # individual missed message window
STAGE_RETRY_GRACE = timedelta(hours=4)        # missed/catchup/digest retry horizon
BACKOFF_BASE = timedelta(minutes=5)
BACKOFF_MAX = timedelta(minutes=60)
QUIET_HEAD = timedelta(minutes=30)    # nudges may land in the first 30 min
LOOKBACK_CAP = timedelta(hours=72)

GLOBAL_MED_ID = "__global__"          # digest/overnight/catchup events
HEARTBEAT_NAME = "tick-heartbeat.json"
TICK_LOCK_NAME = "tick.lock"
SENDER_DOWN_NAME = "SENDER_DOWN"

MUTED_STAGES_ON_PAUSE = ("reminder", "snooze_reminder", "nudge")


# ── Derived paths (from safe_write.STATE_FILE at call time) ───────────────────

def tick_lock_file() -> Path:
    return sw.STATE_FILE.parent / TICK_LOCK_NAME

def heartbeat_file() -> Path:
    return sw.STATE_FILE.parent / HEARTBEAT_NAME

def sender_down_file() -> Path:
    return sw.STATE_FILE.parent / SENDER_DOWN_NAME


def event_id(*parts) -> str:
    """Deterministic event id — replanning never duplicates an event."""
    raw = "|".join(str(p) for p in parts)
    return "ev-" + sha256(raw.encode("utf-8")).hexdigest()[:24]


# ── Quiet-hours helpers (UTC in, UTC out) ─────────────────────────────────────

def in_quiet(instant, quiet: dict, tz: ZoneInfo) -> bool:
    return dispatch.in_quiet_hours(instant, quiet, tz)


def quiet_start_before(instant, quiet: dict, tz: ZoneInfo) -> Optional[object]:
    """Latest quiet-start instant at or before `instant` (UTC)."""
    local = instant.astimezone(tz)
    best = None
    for offset in (0, 1):
        day = local.date() - timedelta(days=offset)
        cand = occurrence.at_local(day, quiet["start"], tz).utc
        if cand <= instant and (best is None or cand > best):
            best = cand
    return best


def in_quiet_head(instant, quiet: dict, tz: ZoneInfo) -> bool:
    """True in the first 30 min of quiet hours — nudges may still land there."""
    start = quiet_start_before(instant, quiet, tz)
    return start is not None and start <= instant < start + QUIET_HEAD


def next_quiet_end(instant, quiet: dict, tz: ZoneInfo):
    """Soonest quiet-end instant at or after `instant`."""
    local = instant.astimezone(tz)
    for offset in (0, 1):
        cand = occurrence.at_local(local.date() + timedelta(days=offset),
                                   quiet["end"], tz).utc
        if cand >= instant:
            return cand
    return instant + timedelta(days=1)


def fmt_12h(dt) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


# ── Message builders ──────────────────────────────────────────────────────────

def double_dose_warning(med: dict, taken_event: dict, tz: ZoneInfo) -> str:
    at = parse_instant(taken_event.get("taken_at_utc") or taken_event["ts_utc"])
    return (f"You logged {med['name']} at {fmt_12h(at.astimezone(tz))}. "
            "Don't take another unless your prescriber says so. "
            "Reply 'took it' only if you take this dose.")


def reminder_text(med: dict, tz: ZoneInfo, warning: str = "", snoozed: bool = False) -> str:
    tag = "Reminder (snoozed)" if snoozed else "Reminder"
    msg = f"{tag}: {med['name']} {med['dose']}{med['unit']}"
    if med["schedule"].get("with_food"):
        msg += " — take with food"
    if warning:
        msg += f"\n{warning}"
    if med["schedule"].get("notes"):
        msg += f"\n{med['schedule']['notes']}"
    return msg


def nudge_text(med: dict, tz: ZoneInfo, warning: str = "") -> str:
    msg = dispatch.expand(med["escalation"]["late_message"], med)
    if warning:
        msg += f"\n{warning}"
    return msg


def missed_text(med: dict) -> str:
    return dispatch.expand(med["escalation"]["missed_message"], med)


def catchup_text(lines: list) -> str:
    if len(lines) == 1:
        return lines[0]
    return "While your computer was off:\n" + "\n".join(f"- {l}" for l in lines)


def catchup_line(med: dict, occ: Occurrence) -> str:
    when = fmt_12h(occ.local)
    at = f"{occ.local.hour % 12 or 12}"
    return (f"While your computer was off: {med['name']} {when} wasn't logged. "
            f"If you took it, reply 'took {med['name'].lower()} at {at}'. "
            "If not, check your med's instructions before taking a late dose.")


# ── Send tracking ──────────────────────────────────────────────────────────────

class SendTrack:
    """Latest send bookkeeping for one (med, occurrence, generation, stage)."""

    __slots__ = ("intent", "sent", "failed")

    def __init__(self):
        self.intent = None
        self.sent = None
        self.failed = None


def send_index(events) -> dict:
    idx: dict = {}
    for e in events:
        stage = e.get("stage")
        key = e.get("occurrence_key")
        if stage is None or key is None:
            continue
        if e.get("event") not in ("send_intent", "sent", "send_failed"):
            continue
        k = (e.get("med_id"), key, int(e.get("generation") or 0), stage)
        tr = idx.setdefault(k, SendTrack())
        if e["event"] == "send_intent":
            tr.intent = e
        elif e["event"] == "sent":
            tr.sent = e
        else:
            tr.failed = e
    return idx


def _ts(event: dict):
    return parse_instant(event["ts_utc"])


@dataclass
class Send:
    med_id: str
    occurrence_key: str
    occurrence_utc: str
    generation: int
    stage: str
    attempt: int
    text: str
    intent_id: str


class Plan:
    """What one tick decided: events to append, sends to run, state changes."""

    def __init__(self, now):
        self.now = now
        self.events = []
        self.sends = []
        self.alerts = []          # untracked best-effort alert texts
        self.catchup_lines = []   # (med, occurrence) for the consolidated message
        self.state_dirty = False  # state changes beyond the watermark
        self.abort = False        # clock anomaly — decide only, never send
        self.errors = 0
        self.handled = set()      # (med, key, gen, stage) decided this tick
        self.tz = UTC
        self.pending_floor = None  # oldest T still inside its escalation window
        self.watermark = now       # where the watermark advances to this tick
        self.capped = False        # 72h catch-up cap engaged this tick
        self.queued_rank = {}      # (med, key, gen) → highest stage queued this tick

    @property
    def noop(self) -> bool:
        return not self.events and not self.sends and not self.alerts \
            and not self.state_dirty


# ── Stage decisions ────────────────────────────────────────────────────────────

def snooze_until_for(events, med_id: str, key: str, gen: int):
    """Active snooze wake instant for (med, occurrence, generation), or None."""
    until = None
    for e in events:
        if (e.get("event") == "snoozed" and e.get("med_id") == med_id
                and e.get("occurrence_key") == key
                and int(e.get("generation") or 0) == gen and e.get("ref")):
            cand = parse_instant(e["ref"])
            if until is None or cand > until:
                until = cand
    return until


def missed_fresh_grace(tick_minutes: int) -> timedelta:
    return max(MISSED_FRESH_GRACE_BASE, timedelta(minutes=2 * tick_minutes))


# ── The pure tick planner ──────────────────────────────────────────────────────

def plan_tick(state: dict, history, now, tick_minutes: int = 5,
              heartbeat_wm=None) -> Plan:
    """
    Decide everything one tick does. Mutates `state` (the caller's copy):
    watermark advance, interval next_due init, alert bookkeeping. Pure
    otherwise — no I/O, no sends. See module docstring for the stages.
    """
    plan = Plan(now)
    g = state["global"]
    # Events from a jumped-forward clock are ignored until real time passes
    ev = [e for e in history if _ts(e) <= now + CLOCK_BACK_TOLERANCE]
    idx = send_index(ev)
    quiet = g["quiet_hours"]

    tz = _resolve_tz(state, plan, ev)
    plan.tz = tz

    state_tick = state.setdefault("tick", {})
    watermark_instants = []
    if state_tick.get("watermark_utc"):
        watermark_instants.append(parse_instant(state_tick["watermark_utc"]))
    if heartbeat_wm is not None:
        watermark_instants.append(heartbeat_wm)
    if watermark_instants:
        wm = max(watermark_instants)
    else:
        # Virgin state: this instant is the migration instant — nothing
        # before it is enumerated (matches event_store.migrate_state).
        wm = now
        plan.state_dirty = True   # force a transact so migration lands

    if now < wm - CLOCK_BACK_TOLERANCE:
        _clock_anomaly(plan, ev, wm)
        plan.abort = True
        return plan

    capped = (now - wm) > CATCHUP_CAP
    plan.capped = capped
    # 1s slack so the half-open (lo, now] still contains an occurrence that
    # sits exactly at the watermark (it holds the watermark while pending)
    lo_base = ((now - CATCHUP_CAP) if capped else wm) - timedelta(seconds=1)

    if not g.get("paused"):
        lookback = _lookback(state)
        for med in state.get("meds", []):
            try:
                _process_med(plan, state, med, ev, idx, tz, quiet,
                             lo_base, lookback, tick_minutes)
            except Exception as e:                      # fault isolation (§3.8)
                plan.errors += 1
                log.exception("tick: med %s failed", med.get("id"))
                _fault_alert(plan, state, tz, med.get("id"), med.get("name"), e)

        if capped:
            _away_message(plan, state, ev, idx, tz, wm)
        _daily_messages(plan, state, ev, idx, tz)
        if plan.catchup_lines and not in_quiet(now, quiet, tz):
            _catchup_message(plan, state, ev, idx)
        _retry_scan(plan, state, ev, idx, tz)

    # Advance the watermark (heartbeat carries it; the state copy is updated
    # for the next transact — never counts as state_dirty on its own). The
    # watermark may only pass work that is DONE: while any occurrence is
    # unresolved inside its escalation window, it trails at that occurrence
    # so later ticks keep enumerating it (nudge/missed would otherwise be
    # lost). The 1s slack keeps the half-open (lo, now] inclusive of T.
    advance = now
    if plan.pending_floor is not None:
        advance = min(advance, plan.pending_floor - timedelta(seconds=1))
    plan.watermark = advance
    state_tick["watermark_utc"] = advance.isoformat()

    return plan


def _resolve_tz(state: dict, plan: Plan, ev) -> ZoneInfo:
    """global.timezone with last-good fallback (issue §3.8)."""
    g = state["global"]
    name = g.get("timezone")
    tick = state.setdefault("tick", {})
    today = plan.now.astimezone(UTC).strftime("%Y-%m-%d")  # alert dedupe bucket

    def alert(bad: str, using: str):
        if tick.get("tz_alert_date") == today:
            return
        tick["tz_alert_date"] = today
        plan.state_dirty = True
        plan.alerts.append(
            f"Timezone '{bad}' in your med settings isn't valid — reminders are "
            f"using {using} until it's fixed. Run doctor.")

    try:
        tz = ZoneInfo(name)
    except Exception:   # ZoneInfoNotFoundError, ModuleNotFoundError (tzdata), …
        cached = tick.get("last_good_timezone")
        alert(name, f"'{cached}' (last known good)" if cached else "UTC")
        try:
            return ZoneInfo(cached) if cached else UTC
        except Exception:
            return UTC
    if tick.get("last_good_timezone") != name:
        tick["last_good_timezone"] = name
        plan.state_dirty = True
    return tz


def _lookback(state: dict) -> timedelta:
    """min(max missed threshold + 24h, 72h) across meds (issue §3.3)."""
    worst = timedelta(minutes=90)
    for med in state.get("meds", []):
        try:
            worst = max(worst, timedelta(minutes=med["escalation"]["missed_threshold_minutes"]))
        except (KeyError, TypeError, ValueError):
            continue
    return min(worst + timedelta(hours=24), LOOKBACK_CAP)


def _clock_anomaly(plan: Plan, ev, wm):
    last = None
    for e in ev:
        if e.get("event") == "clock_anomaly":
            ts = _ts(e)
            if last is None or ts > last:
                last = ts
    if last is not None and plan.now - last < ANOMALY_LOG_INTERVAL:
        log.warning("tick: clock behind watermark (%s) — anomaly already logged",
                    wm.isoformat())
        return
    plan.events.append(event_store.new_event(
        GLOBAL_MED_ID, "clock_anomaly", plan.now,
        id=event_id("clock_anomaly", plan.now.strftime("%Y%m%dT%H")),
        error=f"now {plan.now.isoformat()} behind watermark {wm.isoformat()}",
    ))


def _fault_alert(plan: Plan, state: dict, tz, med_id, name, exc):
    alerts = state.setdefault("tick", {}).setdefault("schedule_alerts", {})
    today = plan.now.astimezone(tz).strftime("%Y-%m-%d")
    if alerts.get(med_id) == today:
        return   # one alert per day per failing med
    alerts[med_id] = today
    plan.state_dirty = True
    plan.alerts.append(f"Can't process {name or med_id}'s schedule — run doctor.")


# ── Per-med processing ─────────────────────────────────────────────────────────

def _process_med(plan: Plan, state: dict, med: dict, ev, idx, tz, quiet,
                 lo_base, lookback, tick_minutes):
    if med.get("paused"):
        return
    sched = med.get("schedule") or {}
    freq = sched.get("frequency")
    if freq == "as_needed":
        return
    if freq not in ("interval",) and not sched.get("times"):
        raise ValueError("schedule has no times[]")

    active_since = None
    if med.get("active_since_utc"):
        active_since = parse_instant(med["active_since_utc"])
    # Normally LOOKBACK bounds enumeration (a dose from days ago is not
    # actionable). When the 72h cap applies the window IS the catch-up:
    # it reaches back the full 72h instead (issue §3.2 "cap catch-up at 72h").
    lo = lo_base if plan.capped else max(lo_base, plan.now - lookback)
    if active_since is not None and active_since > lo:
        lo = active_since

    if freq == "interval":
        occs = _interval_occurrences(plan, state, med, lo, tz, quiet)
    else:
        occs = occurrence.occurrences_between(sched, lo, plan.now, tz)

    for occ in occs:
        _process_occurrence(plan, state, med, occ, ev, idx, tz, quiet,
                            tick_minutes)


def _interval_occurrences(plan: Plan, state: dict, med: dict, lo, tz, quiet):
    """
    Interval occurrences from the anchor (state.next_due), keyed by UTC
    instant. A null anchor initializes from active_since, or from the
    next quiet-hours end when that lands inside quiet hours (issue §3.3).
    The anchor never moves on the tick — it re-anchors on confirm.
    """
    sched = med["schedule"]
    step = timedelta(hours=float(sched["interval_hours"]))
    if step <= timedelta(0):
        raise ValueError("interval_hours must be positive")
    mstate = med.setdefault("state", {})
    initialized = False
    raw = mstate.get("next_due")
    if raw:
        anchor = parse_instant(raw)
    else:
        if med.get("active_since_utc"):
            anchor = parse_instant(med["active_since_utc"])
        else:
            anchor = plan.now
        if in_quiet(anchor, quiet, tz):
            anchor = next_quiet_end(anchor, quiet, tz)
        mstate["next_due"] = anchor.isoformat()
        plan.state_dirty = True
        initialized = True
        log.info("tick: initialized next_due for %s at %s", med["id"], anchor.isoformat())

    step_s = step.total_seconds()
    k0 = 0
    delta = (lo - anchor).total_seconds()
    # A freshly initialized anchor is brand-new work: enumerate from it even
    # though it may predate the watermark (a null next_due was never ticked)
    if not initialized and delta > 0:
        k0 = max(0, math.ceil(delta / step_s - 1e-9))
    k1 = math.floor((plan.now - anchor).total_seconds() / step_s + 1e-9)
    return [occurrence.at_utc(anchor + k * step, tz)
            for k in range(k0, k1 + 1)]


def _process_occurrence(plan: Plan, state: dict, med: dict, occ: Occurrence,
                        ev, idx, tz, quiet, tick_minutes):
    med_id = med["id"]
    key = occ.key
    gen = attribution.generation(ev, med_id, key)
    res = event_store.outcome(ev, key, gen, med_id=med_id)
    if res in ("taken", "skipped"):
        return

    late = timedelta(minutes=med["escalation"]["late_threshold_minutes"])
    missed_min = timedelta(minutes=med["escalation"]["missed_threshold_minutes"])
    snoozed_until = snooze_until_for(ev, med_id, key, gen)
    now = plan.now
    T = occ.utc

    nudge_due = (snoozed_until or T) + late
    missed_due = max(T + missed_min,
                     (snoozed_until + late) if snoozed_until else T + missed_min)

    if now >= missed_due:
        if res == "missed":
            return   # already terminal; delivery retries happen in _retry_scan
        _log_missed(plan, state, med, occ, gen, tz)
        fresh = (now - missed_due) <= missed_fresh_grace(tick_minutes)
        if fresh and not in_quiet(now, quiet, tz):
            # missed-dose messaging rides the catchup stage (issue §1: the
            # stage vocabulary has no per-occurrence 'missed' stage)
            plan_send(plan, idx, med, occ, gen, "catchup", missed_text(med))
        elif not fresh and gen == 0:
            # a stale discovery means the machine was off → consolidated
            # message. An undone-and-redone generation (gen ≥ 1) stays
            # silent: the user just undid a taken dose, nothing was "off"
            # (AC 21).
            plan.catchup_lines.append((med, occ))
        # fresh + quiet: silent now; the overnight summary covers it
        return

    # Unresolved and still inside the escalation window: the watermark may
    # not pass T, or later ticks would never see this occurrence again
    # (the nudge/missed stages would be lost). The watermark trails at the
    # oldest still-pending occurrence and jumps to `now` once all are done.
    plan.pending_floor = T if plan.pending_floor is None else min(plan.pending_floor, T)

    if now >= nudge_due:
        # muted in quiet hours, except when it falls in the first 30 min
        if in_quiet(nudge_due, quiet, tz) and not in_quiet_head(nudge_due, quiet, tz):
            log.info("tick: nudge for %s %s muted (quiet hours)", med_id, key)
            return
        warning = _double_dose_guard(ev, med, T, tz)
        plan_send(plan, idx, med, occ, gen, "nudge", nudge_text(med, tz, warning))
        return

    if snoozed_until is not None and now >= snoozed_until:
        warning = _double_dose_guard(ev, med, T, tz)
        plan_send(plan, idx, med, occ, gen, "snooze_reminder",
                  reminder_text(med, tz, warning, snoozed=True))
        return

    if now >= T:
        warning = _double_dose_guard(ev, med, T, tz)
        plan_send(plan, idx, med, occ, gen, "reminder",
                  reminder_text(med, tz, warning))


def _double_dose_guard(ev, med: dict, T, tz) -> str:
    gap = attribution.min_gap(med)
    last = attribution.taken_within(ev, med["id"], T - gap, T)
    return double_dose_warning(med, last, tz) if last else ""


def _log_missed(plan: Plan, state: dict, med: dict, occ: Occurrence, gen: int, tz):
    plan.events.append(event_store.new_event(
        med["id"], "missed", plan.now, occurrence=occ, generation=gen,
        id=event_id(med["id"], occ.key, gen, "missed"),
    ))
    mstate = med["state"]
    mstate["status"] = "missed"
    mstate["missed_count"] = int(mstate.get("missed_count") or 0) + 1
    if med["schedule"].get("frequency") != "interval":
        nd = dispatch.compute_next_due(med, occ.utc, tz)
        mstate["next_due"] = nd.isoformat() if nd else None
    plan.state_dirty = True
    log.info("tick: missed %s %s (generation %d)", med["id"], occ.key, gen)


# ── Sending decisions ──────────────────────────────────────────────────────────

# Stage precedence per occurrence: only the highest due stage acts, so a
# retry of a lower stage is dropped when a higher one fires this tick.
_STAGE_RANK = {"reminder": 1, "snooze_reminder": 2, "nudge": 3, "catchup": 4}


def plan_send(plan: Plan, idx, med: dict, occ: Occurrence, gen: int,
              stage: str, text: str, occurrence_utc=None):
    """
    Decide a send for (med, occurrence, generation, stage) against the
    send index: new, retry (possibly with the repeat prefix), or nothing
    (already sent / lease in flight / backing off).
    """
    med_id = med["id"] if isinstance(med, dict) else med
    key = occ.key if occ is not None else None
    k4 = (med_id, key, gen, stage)
    tr = idx.get(k4)

    attempt, repeat = 1, False
    if tr is not None:
        if tr.sent is not None:
            plan.handled.add(k4)
            return
        if tr.intent is not None:
            lease = tr.intent.get("lease_until_utc")
            if lease and parse_instant(lease) > plan.now:
                plan.handled.add(k4)          # in flight
                return
            attempt = int(tr.intent.get("attempt") or 1) + 1
            failed_after = (tr.failed is not None
                            and _ts(tr.failed) > _ts(tr.intent))
            if failed_after:
                delay = min(BACKOFF_BASE * (2 ** max(0, attempt - 2)), BACKOFF_MAX)
                if plan.now < _ts(tr.failed) + delay:
                    plan.handled.add(k4)      # backing off
                    return
            else:
                repeat = True                 # unknown outcome (issue §3.6)

    queue_send(plan, med_id, key, gen, stage,
               occurrence_utc or (occ.utc.isoformat() if occ is not None else plan.now.isoformat()),
               attempt, text, repeat)


def queue_send(plan: Plan, med_id: str, key: str, gen: int, stage: str,
               occurrence_utc_iso: str, attempt: int, text: str, repeat: bool):
    if repeat:
        text = "(repeat — ignore if you already got this) " + text
    iid = event_id(med_id, key, gen, stage, attempt)
    occ = Occurrence(utc=parse_instant(occurrence_utc_iso), key=key,
                     tz=plan.tz)
    plan.events.append(event_store.new_event(
        med_id, "send_intent", plan.now, occurrence=occ, generation=gen,
        stage=stage, attempt=attempt,
        lease_until_utc=(plan.now + LEASE).isoformat(), id=iid,
    ))
    plan.sends.append(Send(med_id=med_id, occurrence_key=key,
                           occurrence_utc=occurrence_utc_iso, generation=gen,
                           stage=stage, attempt=attempt, text=text,
                           intent_id=iid))
    plan.handled.add((med_id, key, gen, stage))
    if med_id != GLOBAL_MED_ID:
        occ_key = (med_id, key, gen)
        rank = _STAGE_RANK.get(stage, 0)
        if rank > plan.queued_rank.get(occ_key, 0):
            plan.queued_rank[occ_key] = rank


def _retry_scan(plan: Plan, state: dict, ev, idx, tz):
    """
    Retries for sends whose occurrence is no longer enumerated (before
    the watermark): no `sent`, expired lease, backoff respected, and
    still inside the stage's retry horizon (issue §3.6).
    """
    meds = {m["id"]: m for m in state.get("meds", [])}
    quiet = state["global"]["quiet_hours"]
    for k4, tr in idx.items():
        if k4 in plan.handled or tr.sent is not None or tr.intent is None:
            continue
        med_id, key, gen, stage = k4
        lease = tr.intent.get("lease_until_utc")
        if lease and parse_instant(lease) > plan.now:
            continue
        # a higher stage firing this tick supersedes the lower one's retry
        if med_id != GLOBAL_MED_ID and \
                _STAGE_RANK.get(stage, 0) < plan.queued_rank.get((med_id, key, gen), 0):
            continue
        med = meds.get(med_id)
        if med_id != GLOBAL_MED_ID and (med is None or med.get("paused")):
            continue
        if med is None:
            med = _global_med_stub()
        if "occurrence_utc" not in tr.intent:
            continue
        occ_utc = parse_instant(tr.intent["occurrence_utc"])

        attempt = int(tr.intent.get("attempt") or 1) + 1
        repeat = False
        if tr.failed is not None and _ts(tr.failed) > _ts(tr.intent):
            delay = min(BACKOFF_BASE * (2 ** max(0, attempt - 2)), BACKOFF_MAX)
            if plan.now < _ts(tr.failed) + delay:
                continue
        else:
            repeat = True

        late = timedelta(minutes=(med.get("escalation") or {}).get("late_threshold_minutes", 30))
        missed_min = timedelta(
            minutes=(med.get("escalation") or {}).get("missed_threshold_minutes", 90))
        snoozed_until = snooze_until_for(ev, med_id, key, gen)
        missed_due = max(occ_utc + missed_min,
                         (snoozed_until + late) if snoozed_until else occ_utc + missed_min)
        if stage in MUTED_STAGES_ON_PAUSE:
            deadline = missed_due
        elif stage == "catchup":
            # a per-occurrence missed message outlives its missed deadline
            # by the retry grace; the consolidated/global messages don't
            deadline = missed_due + STAGE_RETRY_GRACE if med_id != GLOBAL_MED_ID \
                else _ts(tr.intent) + STAGE_RETRY_GRACE
        else:
            deadline = _ts(tr.intent) + STAGE_RETRY_GRACE
        if plan.now > deadline:
            continue

        try:
            text = _rebuild_text(plan, state, med, ev, tz, stage, key, occ_utc,
                                 tr, quiet)
        except Exception:
            log.exception("tick: could not rebuild %s text for %s", stage, key)
            continue
        queue_send(plan, med_id, key, gen, stage,
                   tr.intent["occurrence_utc"], attempt, text, repeat)


def _rebuild_text(plan: Plan, state: dict, med: dict, ev, tz, stage: str,
                  key: str, occ_utc, tr, quiet) -> str:
    occ = Occurrence(utc=occ_utc, key=key, tz=tz)
    warning = _double_dose_guard(ev, med, occ_utc, tz)
    if stage in ("reminder", "snooze_reminder"):
        return reminder_text(med, tz, warning, snoozed=(stage == "snooze_reminder"))
    if stage == "nudge":
        return nudge_text(med, tz, warning)
    if stage == "catchup" and med.get("id") is not None and med.get("id") != GLOBAL_MED_ID:
        return missed_text(med)          # per-occurrence missed message
    if stage == "digest":
        return digest_text(state, ev, tz, plan.now)
    if stage == "overnight":
        lines = _overnight_lines(state, ev, tz, plan.now)
        return "While you slept:\n" + "\n".join(f"- {l}" for l in lines)
    if stage == "catchup":
        misses = _silent_misses(ev, _ts(tr.intent) - CATCHUP_CAP, _ts(tr.intent))
        lines = [catchup_line(_med_by_id(state, m["med_id"]), Occurrence(
            utc=parse_instant(m["occurrence_utc"]), key=m["occurrence_key"], tz=tz))
            for m in misses]
        return catchup_text(lines)
    raise ValueError(f"unknown stage {stage}")


def _med_by_id(state: dict, med_id: str) -> dict:
    for m in state.get("meds", []):
        if m["id"] == med_id:
            return m
    return {"name": med_id, "dose": "", "unit": "", "schedule": {},
            "escalation": {"late_message": "{name}", "missed_message": "{name}"}}


def _global_med_stub() -> dict:
    """Placeholder med for global (digest/overnight/consolidated) sends."""
    return {"id": GLOBAL_MED_ID, "name": "", "dose": "", "unit": "",
            "schedule": {}, "escalation": {}, "state": {}}


# ── Digest / overnight / away / catch-up ───────────────────────────────────────

def _sent_for(idx, key: str, stage: str) -> bool:
    tr = idx.get((GLOBAL_MED_ID, key, 0, stage))
    return tr is not None and tr.sent is not None


def _away_message(plan: Plan, state: dict, ev, idx, tz, wm):
    local = plan.now.astimezone(tz)
    key = f"away#{local.date().isoformat()}"
    if _sent_for(idx, key, "catchup"):
        return
    cap_local = (plan.now - CATCHUP_CAP).astimezone(tz)
    text = (f"Your computer was off or asleep since {wm.astimezone(tz):%a %b %d} "
            f"(over 3 days), so reminders were paused. Doses older than "
            f"{cap_local:%a %b %d} were not logged. Were you away? "
            "Reply 'took <med> at <time>' for anything you took.")
    plan_send(plan, idx, GLOBAL_MED_ID,
              Occurrence(utc=plan.now, key=key, tz=tz), 0, "catchup", text)


def _daily_messages(plan: Plan, state: dict, ev, idx, tz):
    """Digest and overnight summary — once per local date, ≤4h late (§3.7)."""
    g = state["global"]
    quiet = g["quiet_hours"]
    now = plan.now
    local = now.astimezone(tz)
    today = local.date()

    digest_due = occurrence.at_local(today, g["digest_time"], tz).utc
    if digest_due <= now <= digest_due + DAILY_LATE_LIMIT:
        key = f"digest#{today.isoformat()}"
        listed = [m for m in state.get("meds", []) if not m.get("paused")]
        if not _sent_for(idx, key, "digest") and listed:
            # Record the listing order for --position replies (issue #1 §6):
            # "the 2nd one" resolves against today's digest, never an older
            # one (reply._resolve_target refuses a stale last_digest_date).
            g["last_digest_order"] = [m["id"] for m in listed]
            g["last_digest_date"] = today.isoformat()
            plan_send(plan, idx, GLOBAL_MED_ID,
                      Occurrence(utc=digest_due, key=key, tz=tz), 0,
                      "digest", digest_text(state, ev, tz, now))

    quiet_end = occurrence.at_local(today, quiet["end"], tz).utc
    if quiet_end <= now <= quiet_end + DAILY_LATE_LIMIT:
        key = f"overnight#{today.isoformat()}"
        if not _sent_for(idx, key, "overnight"):
            lines = _overnight_lines(state, ev, tz, now)
            if lines:
                text = "While you slept:\n" + "\n".join(f"- {l}" for l in lines)
                plan_send(plan, idx, GLOBAL_MED_ID,
                          Occurrence(utc=quiet_end, key=key, tz=tz), 0,
                          "overnight", text)


def _overnight_lines(state: dict, ev, tz, now) -> list:
    """One line per silently-missed dose during last night's quiet window."""
    quiet = state["global"]["quiet_hours"]
    quiet_end = occurrence.at_local(now.astimezone(tz).date(), quiet["end"], tz).utc
    night_start = quiet_start_before(quiet_end, quiet, tz)
    if night_start is None:
        return []
    misses = _silent_misses(ev, night_start, quiet_end)
    lines = []
    for m in misses:
        med = _med_by_id(state, m["med_id"])
        when = parse_instant(m["occurrence_utc"]).astimezone(tz)
        lines.append(f"{med['name']} ({fmt_12h(when)}) wasn't logged. "
                     f"If you took it, reply 'took {med['name'].lower()}'.")
    return lines


def _silent_misses(ev, lo, hi) -> list:
    """
    `missed` events with ts in (lo, hi] whose message never went out: no
    sent catchup event for their (med, occurrence, generation) — the
    per-occurrence missed message rides the catchup stage — and not
    covered by a delivered consolidated catch-up message.
    """
    last_consolidated_sent = None
    for e in ev:
        if (e.get("event") == "sent" and e.get("stage") == "catchup"
                and e.get("med_id") == GLOBAL_MED_ID):
            ts = _ts(e)
            if last_consolidated_sent is None or ts > last_consolidated_sent:
                last_consolidated_sent = ts
    delivered = {}
    for e in ev:
        if e.get("event") == "sent" and e.get("stage") == "catchup":
            delivered[(e.get("med_id"), e.get("occurrence_key"),
                       int(e.get("generation") or 0))] = _ts(e)
    out = []
    for e in ev:
        if e.get("event") != "missed":
            continue
        ts = _ts(e)
        if not (lo < ts <= hi):
            continue
        k = (e.get("med_id"), e.get("occurrence_key"), int(e.get("generation") or 0))
        if k in delivered:
            continue
        if last_consolidated_sent is not None and ts <= last_consolidated_sent:
            continue   # covered by the consolidated message that was delivered
        out.append(e)
    return out


def _catchup_message(plan: Plan, state: dict, ev, idx):
    lines = [catchup_line(med, occ) for med, occ in plan.catchup_lines]
    keys = sorted(f"{med['id']}:{occ.key}" for med, occ in plan.catchup_lines)
    key = "catchup#" + sha256("|".join(keys).encode("utf-8")).hexdigest()[:12]
    if _sent_for(idx, key, "catchup"):
        return
    plan_send(plan, idx, GLOBAL_MED_ID,
              Occurrence(utc=plan.now, key=key, tz=plan.tz), 0,
              "catchup", catchup_text(lines))


def _active_meds(state: dict) -> list:
    return [m for m in state.get("meds", [])
            if not m.get("paused") and m["schedule"].get("frequency") != "as_needed"]


def digest_text(state: dict, ev, tz, now) -> str:
    """Daily summary (format kept from the pre-tick digest)."""
    active = [m for m in state.get("meds", []) if not m.get("paused")]
    date_str = now.astimezone(tz).strftime("%a %b ") + str(now.astimezone(tz).day)
    lines = [f"Meds for {date_str}:"]
    for i, med in enumerate(active, 1):
        sched = med["schedule"]
        freq = sched["frequency"]
        if freq == "interval":
            time_str = f"every {sched['interval_hours']}h"
        elif freq == "as_needed":
            time_str = "as needed"
        else:
            day = sched.get("day_of_week") if freq == "weekly" else None
            prefix = f"{day.title()} " if day else ""
            time_str = prefix + ", ".join(dispatch.fmt_12h(t) for t in sched["times"])
        food = " (with food)" if sched.get("with_food") else ""
        lines.append(f"{i}. {med['name']} {med['dose']}{med['unit']} | {time_str}{food}")

    since = now - timedelta(days=7)
    counts = {"taken": 0, "missed": 0, "skipped": 0}
    seen = {}
    for e in ev:
        if e.get("event") not in ("taken", "missed", "skipped"):
            continue
        if parse_instant(e.get("occurrence_utc") or e["ts_utc"]) < since:
            continue
        k = (e.get("med_id"), e.get("occurrence_key"), int(e.get("generation") or 0))
        prev = seen.get(k)
        rank = {"taken": 3, "skipped": 2, "missed": 1}[e["event"]]
        if prev is None or rank > prev:
            seen[k] = rank
    for rank in seen.values():
        counts[{3: "taken", 2: "skipped", 1: "missed"}[rank]] += 1
    total = counts["taken"] + counts["missed"]
    if total:
        line = f"Last 7 days: {counts['taken']}/{total} doses taken"
        if counts["skipped"]:
            line += f", {counts['skipped']} skipped"
        lines.append("")
        lines.append(line)
    lines += ["", "Reply: 'all taken' | 'took [name]' | 'skip [name]' | 'snooze [name]'"]
    return "\n".join(lines)


# ── Execution ──────────────────────────────────────────────────────────────────

def read_heartbeat():
    try:
        return json.loads(heartbeat_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_heartbeat(started, ended, watermark, errors: int):
    """Heartbeat is written every run, separately from state (issue §3.9)."""
    payload = {
        "started_utc": started.isoformat(),
        "ended_utc": ended.isoformat(),
        "watermark_utc": watermark.isoformat(),
        "errors": errors,
    }
    tmp = heartbeat_file().with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, heartbeat_file())
    except OSError:
        log.exception("tick: could not write heartbeat")


def _load_state() -> dict:
    try:
        return sw.load_state()[0]
    except FileNotFoundError:
        sys.exit(f"State file not found: {sw.STATE_FILE}\n"
                 "Run: cp meds-state.template.json meds-state.json")


def run_tick(dry_run: bool = False, clock: Optional[Clock] = None,
             tick_minutes: int = 5) -> int:
    """The tick entry point (dispatch.py tick). Returns a process exit code."""
    clock = clock or get_clock()
    started = clock.now_utc()
    errors = 0
    watermark = started
    lock = sw.file_lock(timeout=0, _path=tick_lock_file())
    try:
        lock.__enter__()
    except RuntimeError:
        # Another tick is running: exit 0 WITHOUT touching the heartbeat —
        # a skipped tick must not advance the watermark (it did no work, and
        # the live tick writes its own heartbeat).
        log.info("tick: another tick is running — exiting")
        return 0
    try:
        watermark, errors = _tick_locked(dry_run, clock, tick_minutes)
    finally:
        try:
            lock.__exit__(None, None, None)
        finally:
            write_heartbeat(started, clock.now_utc(), watermark, errors)
    return 0


def _tick_locked(dry_run: bool, clock: Clock, tick_minutes: int):
    now = clock.now_utc()
    state = _load_state()
    history = event_store.load_events(sw.history_file())
    hb = read_heartbeat()
    hb_wm = parse_instant(hb["watermark_utc"]) if hb.get("watermark_utc") else None

    pre = plan_tick(state, history, now, tick_minutes, hb_wm)

    if dry_run:
        for s in pre.sends:
            print(f"\n{'-' * 56}\n[DRY RUN] Would send ({s.stage}):\n{s.text}\n{'-' * 56}")
        for text in pre.alerts:
            print(f"\n[DRY RUN] Alert: {text}")
        for e in pre.events:
            print(f"[DRY RUN] Event: {e['event']} {e.get('occurrence_key') or ''}")
        return pre.watermark, pre.errors

    if pre.noop:
        log.debug("tick: nothing to do")
        return pre.watermark, pre.errors

    holder = {}

    def fn(state_copy, history_view):
        p = plan_tick(state_copy, history_view, now, tick_minutes, hb_wm)
        holder["plan"] = p
        return state_copy, p.events

    sw.transact(fn, clock=clock)
    plan = holder["plan"]
    send_failures = _execute_sends(plan, clock)
    return plan.watermark, plan.errors + send_failures


def _execute_sends(plan: Plan, clock: Clock) -> int:
    """Send outside the lock, then record outcomes under a second transact."""
    if plan.abort:
        return 0
    now = clock.now_utc()
    sends = list(plan.sends)
    texts = list(plan.alerts)
    if not sends and not texts:
        return 0

    marker_ts = _read_sender_down()
    note_pending = marker_ts is not None
    failures = 0
    results = []   # (Send, ok, error)

    def with_outage_note(text):
        """First successful send after an outage prepends the window note."""
        if not note_pending:
            return text
        return (f"Delivery was down from {fmt_12h(marker_ts.astimezone(plan.tz))} "
                f"to {fmt_12h(now.astimezone(plan.tz))}; you may have missed:\n\n{text}")

    for s in sends:
        ok, err = dispatch.send_once(with_outage_note(s.text), timeout=SEND_TIMEOUT)
        if ok:
            if note_pending:
                _clear_sender_down()
                note_pending = False
            results.append((s, True, ""))
        else:
            failures += 1
            _mark_sender_down(now)
            if marker_ts is None:
                marker_ts = now
            results.append((s, False, err))
    for text in texts:
        ok, err = dispatch.send_once(with_outage_note(text), timeout=SEND_TIMEOUT)
        if ok:
            if note_pending:
                _clear_sender_down()
                note_pending = False
        else:
            failures += 1
            _mark_sender_down(now)
            if marker_ts is None:
                marker_ts = now
        if ok:
            log.info("tick: alert sent")
        else:
            log.warning("tick: alert send failed: %s", err)

    if not results:
        return failures

    recorded = clock.now_utc()

    def fn(state_copy, history_view):
        events = []
        meds = {m["id"]: m for m in state_copy.get("meds", [])}
        try:
            tz = ZoneInfo(state_copy["global"]["timezone"])
        except Exception:   # a bad tz must not block recording outcomes
            tz = UTC
        for s, ok, err in results:
            occ = Occurrence(utc=parse_instant(s.occurrence_utc),
                             key=s.occurrence_key, tz=tz)
            if ok:
                events.append(event_store.new_event(
                    s.med_id, "sent", recorded, occurrence=occ,
                    generation=s.generation, stage=s.stage,
                    id=s.intent_id + ":sent"))
                med = meds.get(s.med_id)
                resolved = (s.med_id == GLOBAL_MED_ID or
                            event_store.outcome(history_view, s.occurrence_key,
                                                s.generation, med_id=s.med_id)
                            in ("taken", "skipped"))
                # legacy status is rollback-only — never clobber a confirm
                # that landed while the send was in flight (AC 31)
                if med is not None and not resolved:
                    if s.stage in ("reminder", "snooze_reminder"):
                        med["state"]["status"] = "reminded"
                        med["state"]["last_reminded"] = recorded.isoformat()
                    elif s.stage == "nudge":
                        med["state"]["status"] = "late"
                        med["state"]["last_reminded"] = recorded.isoformat()
                elif med is not None:
                    med["state"]["last_reminded"] = recorded.isoformat()
            else:
                events.append(event_store.new_event(
                    s.med_id, "send_failed", recorded, occurrence=occ,
                    generation=s.generation, stage=s.stage,
                    attempt=s.attempt, error=(err or "")[:300],
                    id=f"{s.intent_id}:failed{s.attempt}"))
        return state_copy, events

    try:
        sw.transact(fn, clock=clock)
    except Exception:
        log.exception("tick: recording send outcomes failed")
        failures += 1
    return failures


# ── SENDER_DOWN marker ─────────────────────────────────────────────────────────

def _read_sender_down():
    try:
        data = json.loads(sender_down_file().read_text(encoding="utf-8"))
        return parse_instant(data["since_utc"])
    except (OSError, ValueError, KeyError):
        return None


def _mark_sender_down(now):
    """Write the marker once per outage — it keeps the outage START."""
    if sender_down_file().exists():
        return
    try:
        sender_down_file().write_text(
            json.dumps({"since_utc": now.isoformat()}), encoding="utf-8")
    except OSError:
        log.exception("tick: could not write SENDER_DOWN marker")


def _clear_sender_down():
    try:
        sender_down_file().unlink(missing_ok=True)
    except OSError:
        log.exception("tick: could not clear SENDER_DOWN marker")
