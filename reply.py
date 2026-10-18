#!/usr/bin/env python3
"""
reply.py — AI reply handler for med reminder skill.

Called by the AI agent when the user sends a dose confirmation or deferral.
Separate from dispatch.py (the scheduler) because these have different:
  - failure modes: user is present, errors should be descriptive stdout
  - call patterns: invoked by the AI, not by a scheduler
  - retry semantics: agent can re-invoke; scheduler uses advisory skip

Subcommands:
    confirm <med-id>           Mark one med as taken (prescribed dose)
    confirm --all              Mark all scheduled, non-paused meds as taken
    defer   <med-id>           Defer one med until next digest cycle

Options:
    --dose-taken TEXT          Actual dose if different from prescribed (confirm only)
    --dose HH:MM               Pin the scheduled dose being confirmed (confirm only)
    --taken-at HH:MM           When the dose was taken — most recent past wall time
    --force NONCE              Override a duplicate refusal (nonce from the exit-3 message)
    --dry-run                  Print without writing state
    --state PATH               Path to meds-state.json (overrides MEDS_STATE_FILE)

Exit codes (confirm): 3 = duplicate within the med's minimum gap (a
--force nonce is printed); 4 = ambiguous which dose (the agent asks).

Environment:
    MEDS_STATE_FILE   Path to meds-state.json   (default: same dir as script)
    MEDS_LOG_FILE     Path to reply.log          (default: same dir as script)
"""

import argparse
import logging
import os
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit(f"Python 3.9+ required — found {sys.version.split()[0]}")

# Console output includes unicode (em-dashes) that cp1252 consoles can't encode
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Import shared helpers from dispatch — no scheduler-side code brought in
import dispatch as _dispatch_mod
import event_store as _event_store_mod
import safe_write as _safe_write_mod
from clock import Clock, get_clock, parse_instant
from dispatch import (
    append_history,
    compute_next_due,
    find_med,
    get_tz,
    load_state,
    save_state,
)
from attribution import (
    ALL_AFTER_DIGEST_WINDOW,
    DIGEST_GRACE,
    EARLY_WINDOW,
    attribute,
    generation as occurrence_generation,
    last_taken_before,
    min_gap,
    nearest_dose_occurrence,
    resolve_taken_at,
)
from datetime import datetime, timedelta
from secrets import token_hex
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("reply")

# Exit codes (issue #1 §4): 3 duplicate within min-gap, 4 ambiguous dose.
EXIT_DUPLICATE = 3
EXIT_AMBIGUOUS = 4
EXIT_BAD_OPTION = 2

# A printed duplicate-override nonce is valid for this long (issue §4).
NONCE_TTL = timedelta(minutes=10)


def setup_logging(dry_run: bool) -> None:
    log_path = Path(os.environ.get("MEDS_LOG_FILE", SCRIPT_DIR / "reply.log"))
    _safe_write_mod.configure_root_logging(
        "reply", log_path, logging.DEBUG if dry_run else logging.INFO)


# ── Confirm (issue #1 §4) ──────────────────────────────────────────────────────

def _fmt_12h(dt: datetime) -> str:
    """08:05 → '8:05am', 20:00 → '8:00pm'."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def _taken_instant(event: dict) -> datetime:
    return parse_instant(event.get("taken_at_utc") or event["ts_utc"])


def _find_med(state: dict, med_id: str) -> Optional[dict]:
    return next((m for m in state["meds"] if m["id"] == med_id), None)


def _digest_window_open(history, now: datetime) -> bool:
    """
    True within DIGEST_GRACE after the last delivered digest — the only
    case where the wide 6h early window applies (issue §4).
    """
    last = None
    for e in history:
        if e.get("event") == "sent" and e.get("stage") == "digest":
            ts = parse_instant(e["ts_utc"])
            if last is None or ts > last:
                last = ts
    return last is not None and now - last <= DIGEST_GRACE


def _decide_credit(med: dict, history, taken_at, tz, dose_pin=None,
                   early_window=EARLY_WINDOW):
    """
    Pure decision: which occurrence (if any) a confirm credits.
    Raises SystemExit(4) on ambiguity, SystemExit(EXIT_BAD_OPTION) on a
    bad --dose. Returns the Occurrence or None (unattributed).
    """
    if dose_pin is not None:
        if not med["schedule"].get("times"):
            print(f"Error: {med['name']} has no scheduled dose times to pin.")
            raise SystemExit(EXIT_BAD_OPTION)
        try:
            return nearest_dose_occurrence(med, dose_pin, taken_at, tz)
        except ValueError as e:
            print(f"Error: {e}")
            raise SystemExit(EXIT_BAD_OPTION)

    kind, payload = attribute(med, history, taken_at, tz, early_window)
    if kind == "ambiguous":
        past, future = payload
        print(f"Which dose do you mean for {med['name']}?")
        print(f"  1) {_fmt_12h(past.local)} ({past.local:%a}) — the dose you may have taken late")
        print(f"  2) {_fmt_12h(future.local)} ({future.local:%a}) — early, ahead of schedule")
        print("Reply with the time you took it, e.g. 'took it at 8'.")
        log.info("CONFIRM [%s] ambiguous between %s and %s — asking",
                 med["id"], past.key, future.key)
        raise SystemExit(EXIT_AMBIGUOUS)
    return payload if kind == "credit" else None


def _next_due_for(med: dict, occ, taken_at: datetime, tz) -> Optional[datetime]:
    """Legacy next_due re-anchor; interval meds re-anchor on taken_at."""
    sched = med["schedule"]
    if sched["frequency"] == "as_needed":
        return None
    if sched["frequency"] == "interval":
        return taken_at + timedelta(hours=float(sched["interval_hours"]))
    return compute_next_due(med, occ.utc if occ is not None else taken_at, tz)


def _apply_taken(state: dict, med: dict, occ, taken_at: datetime,
                 dose_taken: Optional[str], history_view) -> dict:
    """Build the `taken` event + legacy state updates (pure)."""
    gen = 0
    if occ is not None:
        gen = occurrence_generation(history_view, med["id"], occ.key)
    event = _event_store_mod.new_event(
        med["id"], "taken", taken_at, occurrence=occ, generation=gen,
        dose_prescribed=f"{med['dose']}{med['unit']}",
        dose_taken=dose_taken, taken_at_utc=taken_at.isoformat(),
    )
    tz = ZoneInfo(state["global"]["timezone"])
    mstate = med["state"]
    mstate["status"] = "confirmed"
    mstate["last_taken"] = taken_at.isoformat()
    if occ is not None and med["schedule"]["frequency"] not in ("interval", "as_needed"):
        mstate["confirmed_dose"] = occ.utc.isoformat()
    nd = _next_due_for(med, occ, taken_at, tz)
    mstate["next_due"] = nd.isoformat() if nd else None
    return event


def _offer_nonce(state: dict, med_id: str, taken_at: datetime, now: datetime) -> str:
    nonce = token_hex(4)
    tick = state.setdefault("tick", {})
    offers = {k: v for k, v in (tick.get("force_nonces") or {}).items()
              if parse_instant(v["expires_utc"]) > now}
    offers[nonce] = {"med_id": med_id, "taken_at_utc": taken_at.isoformat(),
                     "expires_utc": (now + NONCE_TTL).isoformat()}
    tick["force_nonces"] = offers
    return nonce


def _valid_nonce(state: dict, med_id: str, nonce: str, now: datetime) -> bool:
    offer = ((state.get("tick") or {}).get("force_nonces") or {}).get(nonce)
    return bool(offer and offer.get("med_id") == med_id
                and parse_instant(offer["expires_utc"]) > now)


def handle_confirm(med_id: str, dose_taken: Optional[str], dry_run: bool,
                   clock: Optional[Clock] = None, dose_pin: str = None,
                   taken_at_raw: str = None, force: str = None) -> None:
    clock = clock or get_clock()
    state = load_state()
    tz = get_tz(state)
    now = clock.now_utc()
    med = find_med(state, med_id)

    if med.get("paused"):
        log.info("SKIP [confirm] %s — med is paused", med_id)
        print(f"Skipped: {med['name']} is paused.")
        return

    if taken_at_raw:
        try:
            taken_at = resolve_taken_at(taken_at_raw, tz, now)
        except ValueError as e:
            print(f"Error: {e}")
            raise SystemExit(EXIT_BAD_OPTION)
    else:
        taken_at = now

    history = _event_store_mod.load_events(_safe_write_mod.history_file())
    occ = _decide_credit(med, history, taken_at, tz, dose_pin)

    last = last_taken_before(history, med_id, taken_at)
    gap = min_gap(med)
    duplicate = last is not None and (taken_at - _taken_instant(last)) < gap

    if dry_run:
        if duplicate:
            print(f"[DRY RUN] Would refuse: already logged within {gap}.")
        else:
            slot = _fmt_12h(occ.local) if occ else "no scheduled dose"
            print(f"[DRY RUN] Would log {med['name']} as your {slot} dose.")
        return

    outcome = {}

    def fn(state_copy, history_view):
        med_c = _find_med(state_copy, med_id)
        if med_c is None:
            raise ValueError(f"med {med_id} disappeared mid-confirm")
        # re-decide under the lock — a tick or another confirm may have acted
        occ_c = _decide_credit(med_c, history_view, taken_at, tz, dose_pin)
        last_c = last_taken_before(history_view, med_id, taken_at)
        dup_c = last_c is not None and (taken_at - _taken_instant(last_c)) < gap

        if dup_c and not (force and _valid_nonce(state_copy, med_id, force, now)):
            if force:
                print("Error: that override nonce is not valid (wrong or older "
                      "than 10 minutes). Here is a fresh one.")
            nonce = _offer_nonce(state_copy, med_id, taken_at, now)
            outcome["duplicate"] = (last_c, nonce)
            return state_copy, []

        if dup_c and force:
            state_copy.setdefault("tick", {}).get("force_nonces", {}).pop(force, None)
        event = _apply_taken(state_copy, med_c, occ_c, taken_at, dose_taken, history_view)
        outcome["ok"] = (occ_c, med_c["state"].get("next_due"))
        return state_copy, [event]

    _safe_write_mod.transact(fn, clock=clock)

    if "duplicate" in outcome:
        last_ev, nonce = outcome["duplicate"]
        at = _taken_instant(last_ev).astimezone(tz)
        print(f"Already logged {med['name']} at {_fmt_12h(at)}. "
              f"That's within the {int(gap.total_seconds() // 3600)}-hour "
              f"minimum gap for this med.")
        print(f"If you really took it again, rerun with: --force {nonce} "
              f"(valid 10 minutes)")
        log.info("CONFIRM [%s] refused as duplicate of %s", med_id, at.isoformat())
        raise SystemExit(EXIT_DUPLICATE)

    occ, next_due = outcome["ok"]
    if occ is not None:
        print(f"Logged as your {_fmt_12h(occ.local)} dose.")
    else:
        print("Couldn't tell which scheduled dose this was — logged it "
              "without a schedule slot.")
    nd_str = ""
    if next_due:
        nd_dt = parse_instant(next_due).astimezone(tz)
        nd_str = f" Next due: {nd_dt:%a} {_fmt_12h(nd_dt)}."
    print(f"{med['name']} {med['dose']}{med['unit']}.{nd_str}")
    log.info("CONFIRM [%s] credited %s", med_id, occ.key if occ else "(unattributed)")


def handle_confirm_all(dose_taken: Optional[str], dry_run: bool,
                       clock: Optional[Clock] = None) -> None:
    clock = clock or get_clock()
    state = load_state()
    tz = get_tz(state)
    now = clock.now_utc()
    history = _event_store_mod.load_events(_safe_write_mod.history_file())
    early = ALL_AFTER_DIGEST_WINDOW if _digest_window_open(history, now) else EARLY_WINDOW

    notes, credited = [], []

    def fn(state_copy, history_view):
        events = []
        for med in state_copy["meds"]:
            if med.get("paused"):
                notes.append(f"{med['name']} (paused)")
                continue
            if med["schedule"]["frequency"] == "as_needed":
                notes.append(f"{med['name']} (as-needed — confirm individually if taken)")
                continue
            try:
                occ = _decide_credit(med, history_view, now, tz, None, early)
            except SystemExit:
                notes.append(f"{med['name']} (which dose? confirm it individually)")
                continue
            last = last_taken_before(history_view, med["id"], now)
            if last is not None and (now - _taken_instant(last)) < min_gap(med):
                notes.append(f"{med['name']} (already logged recently)")
                continue
            events.append(_apply_taken(state_copy, med, occ, now, dose_taken, history_view))
            credited.append(med["name"])
        return state_copy, events

    if dry_run:
        print(f"[DRY RUN] Would confirm {len(state['meds'])} med(s).")
        return

    _safe_write_mod.transact(fn, clock=clock)

    if credited:
        print(f"Confirmed: {', '.join(credited)}.")
    if notes:
        print(f"Skipped: {', '.join(notes)}.")
    if not credited and not notes:
        print("Nothing to confirm — no active meds found.")


def handle_defer(med_id: str, dry_run: bool, clock: Optional[Clock] = None) -> None:
    clock = clock or get_clock()
    state = load_state()
    med   = find_med(state, med_id)

    if med.get("paused"):
        log.info("SKIP [defer] %s — already paused", med_id)
        print(f"Note: {med['name']} is already paused.")
        return

    med["state"]["status"] = "deferred"
    append_history(med, "deferred", clock=clock)

    save_state(state, dry_run)
    log.info("DEFER [%s]", med_id)
    print(f"Deferred: {med['name']} — skipped until next cycle.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    _safe_write_mod.apply_process_umask()   # issue #1 §2: private files, before any are created
    parser = argparse.ArgumentParser(
        description="AI reply handler — confirm or defer a medication dose.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print without writing state")
    parser.add_argument("--state", default=None, metavar="PATH",
                        help="Path to meds-state.json (overrides MEDS_STATE_FILE)")

    sub = parser.add_subparsers(dest="mode", required=True, metavar="MODE")

    p_confirm = sub.add_parser("confirm", help="Mark a med as taken")
    p_confirm_grp = p_confirm.add_mutually_exclusive_group(required=True)
    p_confirm_grp.add_argument("med_id", nargs="?", default=None,
                               help="Med ID, e.g. med-001")
    p_confirm_grp.add_argument("--all", dest="confirm_all", action="store_true",
                               help="Confirm all pending/reminded/late meds")
    p_confirm.add_argument("--dose-taken", default="",
                           help="Actual dose if different from prescribed, e.g. '250mg'")
    p_confirm.add_argument("--dose", default=None, metavar="HH:MM",
                           help="Pin the scheduled dose being confirmed (must match a times[] entry)")
    p_confirm.add_argument("--taken-at", default=None, metavar="HH:MM", dest="taken_at",
                           help="Wall time the dose was taken (most recent past instance, <=24h back)")
    p_confirm.add_argument("--force", default=None, metavar="NONCE",
                           help="Override a duplicate refusal (nonce from the exit-3 message, valid 10 min)")

    p_defer = sub.add_parser("defer", help="Defer a med until next cycle")
    p_defer.add_argument("med_id", help="Med ID, e.g. med-001")

    args = parser.parse_args()
    setup_logging(args.dry_run)

    if args.state:
        _dispatch_mod.STATE_FILE = Path(args.state)
        _safe_write_mod.STATE_FILE = Path(args.state)  # writer keeps its own copy
    _safe_write_mod.secure_private_files(
        extra=(Path(os.environ.get("MEDS_LOG_FILE", SCRIPT_DIR / "reply.log")),))

    if args.mode == "confirm":
        if args.confirm_all:
            handle_confirm_all(args.dose_taken or None, args.dry_run)
        else:
            if not args.med_id:
                parser.error("confirm requires a med_id or --all")
            handle_confirm(args.med_id, args.dose_taken or None, args.dry_run,
                           dose_pin=args.dose, taken_at_raw=args.taken_at,
                           force=args.force)
    elif args.mode == "defer":
        handle_defer(args.med_id, args.dry_run)


if __name__ == "__main__":
    main()
