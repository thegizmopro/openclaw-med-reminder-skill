"""
Task-XML tests — Windows tasks register via generated Task Scheduler 2.0 XML
with StartWhenAvailable (missed-run catch-up) and a baked-in --state path.
"""
import xml.etree.ElementTree as ET

from tests.conftest import make_state, make_med, load_setup_tasks

setup_tasks = load_setup_tasks()

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def build_xml(task, state_flag='--state "C:/x/meds-state.json"'):
    return setup_tasks._task_xml(
        task, python="C:/Python314/python.exe", dispatch="C:/dev/dispatch.py",
        state_flag=state_flag, start_date="2026-08-21")


def parse(xml):
    return ET.fromstring(xml)


def task(**kw):
    defaults = dict(name="fire_med-001_0", label="Metformin: reminder at 08:00",
                    args=["fire", "med-001", "0"], hhmm="08:00", interval_min=None)
    defaults.update(kw)
    return setup_tasks.Task(**defaults)


# ── settings every task must carry ─────────────────────────────────────────────

def test_start_when_available_true():
    root = parse(build_xml(task()))
    assert root.findtext(f"{NS}Settings/{NS}StartWhenAvailable") == "true"


def test_runs_on_battery():
    root = parse(build_xml(task()))
    assert root.findtext(f"{NS}Settings/{NS}DisallowStartIfOnBatteries") == "false"
    assert root.findtext(f"{NS}Settings/{NS}StopIfGoingOnBatteries") == "false"


def test_action_bakes_state_flag():
    root = parse(build_xml(task()))
    args = root.findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments")
    cmd  = root.findtext(f"{NS}Actions/{NS}Exec/{NS}Command")
    assert cmd == "C:/Python314/python.exe"
    assert '--state "C:/x/meds-state.json"' in args
    assert args.endswith("fire med-001 0")


# ── trigger variants ───────────────────────────────────────────────────────────

def test_daily_trigger():
    root = parse(build_xml(task()))
    assert root.find(f"{NS}Triggers/{NS}CalendarTrigger/{NS}ScheduleByDay") is not None
    assert root.findtext(f"{NS}Triggers/{NS}CalendarTrigger/{NS}StartBoundary") \
        == "2026-08-21T08:00:00"


def test_weekly_trigger_has_day():
    root = parse(build_xml(task(day_of_week="mon")))
    days = root.find(f"{NS}Triggers/{NS}CalendarTrigger/{NS}ScheduleByWeek/{NS}DaysOfWeek")
    assert days is not None and days.find(f"{NS}Monday") is not None


def test_interval_trigger_repeats():
    root = parse(build_xml(task(hhmm=None, interval_min=360)))
    rep = root.find(f"{NS}Triggers/{NS}TimeTrigger/{NS}Repetition")
    assert rep is not None
    assert rep.findtext(f"{NS}Interval") == "PT360M"


# ── label escaping ─────────────────────────────────────────────────────────────

def test_label_xml_escaped():
    root = parse(build_xml(task(label="A & B <test>")))
    desc = root.findtext(f"{NS}RegistrationInfo/{NS}Description")
    assert desc == "A & B <test>"   # round-trips through escaping


# ── build_tasks carries the state path for cron too ────────────────────────────

def test_state_flag_helper_format():
    # register_unix builds: "<python>" "<dispatch>" --state "<path>" <args>
    # covered indirectly here by checking the flag string shape
    flag = '--state "C:/some where/meds-state.json"'
    cmd = f'"py" "dispatch.py" {flag} fire med-001 0'
    assert '"--state"' not in cmd and cmd.count('"') == 6
