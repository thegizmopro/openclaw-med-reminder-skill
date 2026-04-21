"""Shared fixtures for med reminder tests."""
import sys
from pathlib import Path

# Make dispatch importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")


def make_state(paused=False, quiet_start="22:00", quiet_end="07:00",
               digest_time="08:00", meds=None):
    return {
        "version": "1",
        "global": {
            "timezone": "America/Los_Angeles",
            "quiet_hours": {"start": quiet_start, "end": quiet_end},
            "paused": paused,
            "digest_time": digest_time,
            "delivery_channel": "whatsapp",
        },
        "meds": meds if meds is not None else [],
    }


def make_med(
    med_id="med-001",
    name="Metformin",
    med_type="pill",
    dose="500",
    unit="mg",
    paused=False,
    frequency="once_daily",
    times=None,
    interval_hours=None,
    with_food=False,
    notes="",
    late_threshold=30,
    missed_threshold=90,
    status="pending",
    last_taken=None,
    last_reminded=None,
    next_due=None,
    missed_count=0,
    history=None,
):
    sched = {"frequency": frequency, "with_food": with_food, "notes": notes}
    if frequency == "interval":
        sched["interval_hours"] = interval_hours
    elif frequency != "as_needed":
        sched["times"] = times if times is not None else ["08:00"]

    return {
        "id": med_id,
        "name": name,
        "type": med_type,
        "dose": dose,
        "unit": unit,
        "paused": paused,
        "schedule": sched,
        "escalation": {
            "late_threshold_minutes": late_threshold,
            "missed_threshold_minutes": missed_threshold,
            "late_message": "Late: {name} {dose}{unit}",
            "missed_message": "Missed: {name} {dose}{unit}",
        },
        "state": {
            "status": status,
            "last_taken": last_taken,
            "last_reminded": last_reminded,
            "next_due": next_due,
            "missed_count": missed_count,
            "history": history if history is not None else [],
        },
    }


def local_dt(hour, minute=0, second=0, tz=TZ,
             year=2026, month=4, day=21):
    """Build a timezone-aware datetime in TZ."""
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


def iso(dt):
    return dt.isoformat()
