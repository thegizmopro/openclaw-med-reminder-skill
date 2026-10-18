"""
Quiet-hours tests (issue #1 §3.4).

The tick-side quiet rules — reminder exempt, nudge muted except the
first 30 min of quiet hours, missed always logged with the message
muted, overnight summary after quiet end — live in tests/test_tick.py
(AC 26-28). This file keeps the setup-tasks edit-time checks.
"""
from tests.conftest import make_state, make_med, load_setup_tasks

setup_tasks = load_setup_tasks()


def test_in_quiet_window_matches():
    q = {"start": "22:00", "end": "07:00"}
    assert setup_tasks.in_quiet_window("23:00", q) is True
    assert setup_tasks.in_quiet_window("03:00", q) is True
    assert setup_tasks.in_quiet_window("22:00", q) is True    # start is quiet
    assert setup_tasks.in_quiet_window("07:00", q) is False   # end is not
    assert setup_tasks.in_quiet_window("12:00", q) is False


def test_in_quiet_window_same_day_window():
    q = {"start": "13:00", "end": "15:00"}
    assert setup_tasks.in_quiet_window("14:00", q) is True
    assert setup_tasks.in_quiet_window("12:59", q) is False


def test_build_tasks_warns_for_dose_inside_quiet_hours(capsys):
    med = make_med(med_id="med-001", times=["23:30"])
    setup_tasks.build_tasks(make_state(meds=[med]))
    out = capsys.readouterr().out
    assert "inside quiet hours" in out


def test_build_tasks_no_warning_for_normal_dose(capsys):
    med = make_med(med_id="med-001", times=["08:00"])
    setup_tasks.build_tasks(make_state(meds=[med]))
    assert "quiet hours" not in capsys.readouterr().out
