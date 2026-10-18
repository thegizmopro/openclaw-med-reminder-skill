"""
Reply command tests — issue #1 PR 4 §6: snooze, skip (defer's alias tests
live in test_reply_handler.py), then undo, pause/unpause, the edit-time
quiet-hours rejections, and --position resolution.

Every command goes through transact and writes store events (snoozed with
the wake instant in `ref`, skipped, undone with `ref` = the undone event's
id) — the shapes tick.py already reads (PR 2).
"""
import json

import pytest

import dispatch
import event_store
import safe_write as sw
import tick
from clock import FakeClock, parse_instant
from reply import (
    handle_pause,
    handle_snooze,
    handle_skip,
    handle_undo,
    handle_unpause,
)
from tests.conftest import local_dt, make_med, make_state, make_tick_env, utc

import datetime as dtmod


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    sf = tmp_path / "meds-state.json"
    monkeypatch.setattr(dispatch, "STATE_FILE", sf)
    monkeypatch.setattr(sw, "STATE_FILE", sf)
    monkeypatch.setattr(sw, "LOG_FILE", tmp_path / "sw.log")
    return sf


def write_state(sf, meds=None, **kw):
    sf.write_text(json.dumps(make_state(meds=meds if meds is not None else [], **kw)),
                  encoding="utf-8")


def events(sf):
    return event_store.load_events(sw.history_file())


def on_disk(sf):
    return json.loads(sf.read_text(encoding="utf-8"))


# ── snooze ─────────────────────────────────────────────────────────────────────

def test_snooze_writes_snoozed_event_with_wake_instant(state_file):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])

    handle_snooze("med-001", 30, dry_run=False, clock=FakeClock(local_dt(8, 5)))

    evs = events(state_file)
    assert len(evs) == 1 and evs[0]["event"] == "snoozed"
    assert evs[0]["occurrence_key"] == "2026-04-21T08:00#0"
    assert evs[0]["generation"] == 0
    # the wake instant rides in `ref` — the format tick.snooze_until_for reads
    assert parse_instant(evs[0]["ref"]) == utc(local_dt(8, 35))


def test_snooze_defaults_to_30_minutes(state_file):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])

    handle_snooze("med-001", None, dry_run=False, clock=FakeClock(local_dt(8, 5)))

    assert parse_instant(events(state_file)[0]["ref"]) == utc(local_dt(8, 35))


@pytest.mark.parametrize("minutes", [4, 241])
def test_snooze_out_of_range_rejected(state_file, minutes):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])

    with pytest.raises(SystemExit) as e:
        handle_snooze("med-001", minutes, dry_run=False, clock=FakeClock(local_dt(8, 5)))
    assert e.value.code != 0
    assert events(state_file) == []


def test_snooze_cap_refusal_and_boundary(state_file, capsys):
    # 08:00/11:00 → gap 3h → cap 90 minutes
    med = make_med(med_id="med-001", frequency="twice_daily", times=["08:00", "11:00"])
    write_state(state_file, meds=[med])

    with pytest.raises(SystemExit) as e:
        handle_snooze("med-001", 120, dry_run=False, clock=FakeClock(local_dt(8, 5)))
    assert e.value.code != 0
    out = capsys.readouterr().out
    assert "run into your next dose" in out and "skip instead?" in out
    assert events(state_file) == []

    # exactly at the cap is allowed
    handle_snooze("med-001", 90, dry_run=False, clock=FakeClock(local_dt(8, 5)))
    assert parse_instant(events(state_file)[0]["ref"]) == utc(local_dt(9, 35))


def test_snooze_before_first_tracked_dose_errors(state_file):
    # fresh install: active_since prunes yesterday's occurrence
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    med["active_since_utc"] = local_dt(7, 0).isoformat()
    write_state(state_file, meds=[med])

    with pytest.raises(SystemExit) as e:
        handle_snooze("med-001", 30, dry_run=False, clock=FakeClock(local_dt(7, 30)))
    assert e.value.code != 0
    assert events(state_file) == []


def test_snooze_already_logged_dose_errors(state_file):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])
    taken = event_store.new_event("med-001", "taken", local_dt(8, 10),
                                  occurrence_key="2026-04-21T08:00#0",
                                  occurrence_utc=utc(local_dt(8, 0)).isoformat())
    event_store.append_events(sw.history_file(), [taken])

    with pytest.raises(SystemExit):
        handle_snooze("med-001", 30, dry_run=False, clock=FakeClock(local_dt(8, 20)))
    assert len(events(state_file)) == 1


def test_snooze_interval_med_caps_at_half_interval(state_file):
    med = make_med(med_id="med-001", frequency="interval", interval_hours=4,
                   next_due=local_dt(8, 0).isoformat())
    write_state(state_file, meds=[med])

    # cap = 4h/2 = 120min: 240 exceeds it
    with pytest.raises(SystemExit):
        handle_snooze("med-001", 240, dry_run=False, clock=FakeClock(local_dt(8, 10)))
    handle_snooze("med-001", 120, dry_run=False, clock=FakeClock(local_dt(8, 10)))
    evs = events(state_file)
    # occurrence keyed by the UTC instant 08:00 PDT = 15:00Z
    assert evs[0]["occurrence_key"] == "2026-04-21T15:00:00Z"
    assert parse_instant(evs[0]["ref"]) == utc(local_dt(10, 10))


def test_ac19_snooze_command_then_tick_stages(tmp_path, monkeypatch):
    env = make_tick_env(tmp_path, monkeypatch)
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    env.write(meds=[med])

    env.run(local_dt(7, 55))                 # watermark init before anything
    env.run(local_dt(8, 1))                  # the reminder that gets snoozed
    assert env.sent_count("Reminder") == 1
    handle_snooze("med-001", 30, dry_run=False, clock=FakeClock(local_dt(8, 5)))

    env.run(local_dt(8, 6))                  # already reminded; the snooze holds
    assert env.sent_count("Reminder") == 1 and env.sent()[-1] == env.sent()[0]
    env.run(local_dt(8, 36))                 # until → snooze reminder
    assert env.sent_count("snoozed") == 1
    env.run(local_dt(9, 6))                  # until+30 → nudge
    assert env.sent_count("Late:") == 1
    env.run(local_dt(9, 29))                 # missed at max(T+90, until+30) = 09:30
    assert env.events("missed") == []
    env.run(local_dt(9, 31))
    assert len(env.events("missed")) == 1


# ── skip (AC 20) ───────────────────────────────────────────────────────────────

def test_skip_targets_past_or_current_occurrence(state_file):
    med = make_med(med_id="med-001", frequency="twice_daily", times=["08:00", "20:00"])
    write_state(state_file, meds=[med])

    handle_skip("med-001", None, dry_run=False, clock=FakeClock(local_dt(8, 10)))

    evs = events(state_file)
    assert len(evs) == 1 and evs[0]["event"] == "skipped"
    assert evs[0]["occurrence_key"] == "2026-04-21T08:00#0"


def test_skip_without_dose_never_targets_future(state_file):
    # fresh install, next dose still ahead: nothing due → refuse
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    med["active_since_utc"] = local_dt(7, 0).isoformat()
    write_state(state_file, meds=[med])

    with pytest.raises(SystemExit) as e:
        handle_skip("med-001", None, dry_run=False, clock=FakeClock(local_dt(7, 30)))
    assert e.value.code != 0
    assert events(state_file) == []


def test_skip_with_dose_pins_future_occurrence(state_file):
    med = make_med(med_id="med-001", frequency="twice_daily", times=["08:00", "20:00"])
    write_state(state_file, meds=[med])

    handle_skip("med-001", "20:00", dry_run=False, clock=FakeClock(local_dt(14, 0)))

    assert events(state_file)[0]["occurrence_key"] == "2026-04-21T20:00#1"


def test_skip_with_unscheduled_dose_rejected(state_file):
    med = make_med(med_id="med-001", frequency="twice_daily", times=["08:00", "20:00"])
    write_state(state_file, meds=[med])

    with pytest.raises(SystemExit) as e:
        handle_skip("med-001", "09:00", dry_run=False, clock=FakeClock(local_dt(14, 0)))
    assert e.value.code != 0
    assert events(state_file) == []


def test_ac20_skip_never_becomes_missed(tmp_path, monkeypatch):
    env = make_tick_env(tmp_path, monkeypatch)
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    env.write(meds=[med])

    env.run(local_dt(7, 55))
    handle_skip("med-001", None, dry_run=False, clock=FakeClock(local_dt(8, 10)))
    env.run(local_dt(9, 0))
    env.run(local_dt(10, 31))                # past T+90

    assert env.events("skipped") and env.events("missed") == []
    assert env.sent_count("Missed:") == 0


def test_skip_already_taken_dose_refuses(state_file, capsys):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])
    taken = event_store.new_event("med-001", "taken", local_dt(8, 10),
                                  occurrence_key="2026-04-21T08:00#0",
                                  occurrence_utc=utc(local_dt(8, 0)).isoformat())
    event_store.append_events(sw.history_file(), [taken])

    with pytest.raises(SystemExit):
        handle_skip("med-001", None, dry_run=False, clock=FakeClock(local_dt(8, 20)))
    assert len(events(state_file)) == 1
    assert "already logged as taken" in capsys.readouterr().out


def test_skip_overrides_an_earlier_missed(state_file):
    # precedence taken > skipped > missed: a later `skipped` flips a missed dose
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])
    missed = event_store.new_event("med-001", "missed", local_dt(9, 30),
                                   occurrence_key="2026-04-21T08:00#0",
                                   occurrence_utc=utc(local_dt(8, 0)).isoformat())
    event_store.append_events(sw.history_file(), [missed])

    handle_skip("med-001", None, dry_run=False, clock=FakeClock(local_dt(10, 0)))

    assert event_store.outcome(events(state_file), "2026-04-21T08:00#0",
                               0, med_id="med-001") == "skipped"


# ── undo (AC 21) ───────────────────────────────────────────────────────────────

def test_undo_taken_writes_undone_with_ref(state_file, capsys):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])
    taken = event_store.new_event(
        "med-001", "taken", local_dt(8, 14),
        occurrence_key="2026-04-21T08:00#0",
        occurrence_utc=utc(local_dt(8, 0)).isoformat(),
        taken_at_utc=utc(local_dt(8, 14)).isoformat())
    event_store.append_events(sw.history_file(), [taken])

    handle_undo("med-001", dry_run=False, clock=FakeClock(local_dt(10, 0)))

    evs = events(state_file)
    assert [e["event"] for e in evs] == ["taken", "undone"]
    undone = evs[1]
    assert undone["ref"] == taken["id"]
    assert undone["occurrence_key"] == "2026-04-21T08:00#0"
    assert undone["generation"] == 0
    out = capsys.readouterr().out
    assert "Undid" in out and "8:00am" in out and "8:14am" in out
    # generation bumps: the occurrence is unresolved again
    from attribution import generation as occurrence_generation
    assert occurrence_generation(evs, "med-001", "2026-04-21T08:00#0") == 1


def test_undo_second_time_fails_with_nothing_to_undo(state_file, capsys):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])
    event_store.append_events(sw.history_file(), [
        event_store.new_event("med-001", "taken", local_dt(8, 14),
                              occurrence_key="2026-04-21T08:00#0",
                              occurrence_utc=utc(local_dt(8, 0)).isoformat())])

    handle_undo("med-001", dry_run=False, clock=FakeClock(local_dt(9, 0)))
    with pytest.raises(SystemExit) as e:
        handle_undo("med-001", dry_run=False, clock=FakeClock(local_dt(9, 5)))
    assert e.value.code != 0
    assert "nothing to undo" in capsys.readouterr().out
    assert len(events(state_file)) == 2


def test_undo_older_than_24h_refused(state_file):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])
    event_store.append_events(sw.history_file(), [
        event_store.new_event("med-001", "taken", local_dt(8, 14) - dtmod.timedelta(days=2),
                              occurrence_key="2026-04-19T08:00#0",
                              occurrence_utc=utc(local_dt(8, 0) - dtmod.timedelta(days=2)).isoformat())])

    with pytest.raises(SystemExit) as e:
        handle_undo("med-001", dry_run=False, clock=FakeClock(local_dt(10, 0)))
    assert e.value.code != 0
    assert len(events(state_file)) == 1


def test_undo_skipped_echoes_it(state_file, capsys):
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    write_state(state_file, meds=[med])
    skipped = event_store.new_event(
        "med-001", "skipped", local_dt(8, 10),
        occurrence_key="2026-04-21T08:00#0",
        occurrence_utc=utc(local_dt(8, 0)).isoformat())
    event_store.append_events(sw.history_file(), [skipped])

    handle_undo("med-001", dry_run=False, clock=FakeClock(local_dt(9, 0)))

    evs = events(state_file)
    assert evs[1]["event"] == "undone" and evs[1]["ref"] == skipped["id"]
    assert "Undid: skipped" in capsys.readouterr().out


def test_ac21_undo_before_late_re_reminds(tmp_path, monkeypatch):
    env = make_tick_env(tmp_path, monkeypatch)
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    env.write(meds=[med])

    env.run(local_dt(7, 55))                 # establish the watermark
    from reply import handle_confirm
    handle_confirm("med-001", None, dry_run=False, clock=FakeClock(local_dt(8, 10)))
    handle_undo("med-001", dry_run=False, clock=FakeClock(local_dt(8, 20)))

    env.run(local_dt(8, 25))                 # before T+late → re-remind, new gen
    msgs = env.sent()
    assert len(msgs) == 1 and "Reminder" in msgs[0]
    assert env.events("send_intent")[0]["generation"] == 1


def test_ac21_undo_after_missed_writes_silent_missed(tmp_path, monkeypatch):
    env = make_tick_env(tmp_path, monkeypatch)
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    env.write(meds=[med])

    env.run(local_dt(7, 55))
    from reply import handle_confirm
    handle_confirm("med-001", None, dry_run=False, clock=FakeClock(local_dt(8, 10)))
    handle_undo("med-001", dry_run=False, clock=FakeClock(local_dt(11, 0)))

    env.run(local_dt(11, 5))                 # past T+missed → silent missed
    assert len(env.events("missed")) == 1
    assert env.events("missed")[0]["generation"] == 1
    assert env.sent() == []


# ── pause / unpause ────────────────────────────────────────────────────────────

def test_pause_and_unpause(state_file):
    med = make_med(med_id="med-001")
    write_state(state_file, meds=[med])

    handle_pause("med-001", dry_run=False, clock=FakeClock(local_dt(10, 0)))
    assert on_disk(state_file)["meds"][0]["paused"] is True
    assert events(state_file) == []          # no events — state-only change

    handle_unpause("med-001", dry_run=False, clock=FakeClock(local_dt(12, 0)))
    med_disk = on_disk(state_file)["meds"][0]
    assert med_disk["paused"] is False
    # unpause re-anchors tracking: nothing during the pause is enumerated
    assert parse_instant(med_disk["active_since_utc"]) == utc(local_dt(12, 0))


def test_unpause_active_since_prevents_phantom_misses(tmp_path, monkeypatch):
    env = make_tick_env(tmp_path, monkeypatch)
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    env.write(meds=[med])

    env.run(local_dt(7, 55))
    handle_pause("med-001", dry_run=False, clock=FakeClock(local_dt(7, 58)))
    env.run(local_dt(10, 30))                # the 08:00 dose passes while paused
    handle_unpause("med-001", dry_run=False, clock=FakeClock(local_dt(11, 0)))

    env.run(local_dt(11, 30))

    assert env.events("missed") == []
    assert env.sent() == []
