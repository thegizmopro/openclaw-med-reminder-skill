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
    confirm --all              Mark all pending/reminded/late meds as taken
    defer   <med-id>           Defer one med until next digest cycle

Options:
    --dose-taken TEXT          Actual dose if different from prescribed (confirm only)
    --dry-run                  Print without writing state

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

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Import shared helpers from dispatch — no scheduler-side code brought in
from dispatch import (
    append_history,
    compute_next_due,
    find_med,
    get_tz,
    load_state,
    save_state,
)
from datetime import datetime
from typing import Optional

log = logging.getLogger("reply")


def setup_logging(dry_run: bool) -> None:
    log_path = Path(os.environ.get("MEDS_LOG_FILE", SCRIPT_DIR / "reply.log"))
    level = logging.DEBUG if dry_run else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [reply] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── Handlers ───────────────────────────────────────────────────────────────────

def handle_confirm(med_id: str, dose_taken: Optional[str], dry_run: bool) -> None:
    state = load_state()
    tz    = get_tz(state)
    now   = datetime.now(tz=tz)
    med   = find_med(state, med_id)

    if med.get("paused"):
        log.info("SKIP [confirm] %s — med is paused", med_id)
        print(f"Skipped: {med['name']} is paused.")
        return

    med["state"]["status"]     = "confirmed"
    med["state"]["last_taken"] = now.isoformat()

    nd = compute_next_due(med, now, tz)
    med["state"]["next_due"] = nd.isoformat() if nd else None

    append_history(med, "taken", dose_taken=dose_taken or None)

    save_state(state, dry_run)
    next_str = nd.strftime("%b %d %H:%M %Z") if nd else "n/a"
    log.info("CONFIRM [%s] taken=%s dose_taken=%s next_due=%s",
             med_id, now.isoformat(), dose_taken or "(prescribed)", med["state"]["next_due"])
    print(f"Confirmed: {med['name']} {med['dose']}{med['unit']} taken at "
          f"{now.strftime('%H:%M')}. Next due: {next_str}.")


def handle_confirm_all(dose_taken: Optional[str], dry_run: bool) -> None:
    state = load_state()
    tz    = get_tz(state)
    now   = datetime.now(tz=tz)

    confirmed = []
    skipped   = []

    for med in state["meds"]:
        if med.get("paused"):
            skipped.append(f"{med['name']} (paused)")
            continue
        if med["schedule"]["frequency"] == "as_needed":
            skipped.append(f"{med['name']} (as-needed — confirm individually if taken)")
            continue
        if med["state"]["status"] == "confirmed":
            skipped.append(f"{med['name']} (already confirmed)")
            continue

        med["state"]["status"]     = "confirmed"
        med["state"]["last_taken"] = now.isoformat()

        nd = compute_next_due(med, now, tz)
        med["state"]["next_due"] = nd.isoformat() if nd else None

        append_history(med, "taken", dose_taken=dose_taken or None)
        confirmed.append(med["name"])
        log.info("CONFIRM-ALL [%s] taken=%s next_due=%s",
                 med["id"], now.isoformat(), med["state"]["next_due"])

    if confirmed:
        save_state(state, dry_run)

    if confirmed:
        print(f"Confirmed: {', '.join(confirmed)}.")
    if skipped:
        print(f"Skipped: {', '.join(skipped)}.")


def handle_defer(med_id: str, dry_run: bool) -> None:
    state = load_state()
    med   = find_med(state, med_id)

    if med.get("paused"):
        log.info("SKIP [defer] %s — already paused", med_id)
        print(f"Note: {med['name']} is already paused.")
        return

    med["state"]["status"] = "deferred"
    append_history(med, "deferred")

    save_state(state, dry_run)
    log.info("DEFER [%s]", med_id)
    print(f"Deferred: {med['name']} — skipped until next cycle.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI reply handler — confirm or defer a medication dose.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print without writing state")

    sub = parser.add_subparsers(dest="mode", required=True, metavar="MODE")

    p_confirm = sub.add_parser("confirm", help="Mark a med as taken")
    p_confirm_grp = p_confirm.add_mutually_exclusive_group(required=True)
    p_confirm_grp.add_argument("med_id", nargs="?", default=None,
                               help="Med ID, e.g. med-001")
    p_confirm_grp.add_argument("--all", dest="confirm_all", action="store_true",
                               help="Confirm all pending/reminded/late meds")
    p_confirm.add_argument("--dose-taken", default="",
                           help="Actual dose if different from prescribed, e.g. '250mg'")

    p_defer = sub.add_parser("defer", help="Defer a med until next cycle")
    p_defer.add_argument("med_id", help="Med ID, e.g. med-001")

    args = parser.parse_args()
    setup_logging(args.dry_run)

    if args.mode == "confirm":
        if args.confirm_all:
            handle_confirm_all(args.dose_taken or None, args.dry_run)
        else:
            if not args.med_id:
                parser.error("confirm requires a med_id or --all")
            handle_confirm(args.med_id, args.dose_taken or None, args.dry_run)
    elif args.mode == "defer":
        handle_defer(args.med_id, args.dry_run)


if __name__ == "__main__":
    main()
