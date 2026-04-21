"""
Cases 21-25: safe-write.sh — atomic write, validation, locking.

Calls safe-write.sh via subprocess (bash). Requires Git Bash or WSL on Windows.
Each test uses isolated temp directories so cases are fully independent.
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "safe-write.sh"
SCHEMA = Path(__file__).parent.parent / "meds.schema.json"


def run_safe_write(state_dir, content, env_extra=None):
    """Pipe `content` (str) into safe-write.sh. Returns CompletedProcess."""
    env = os.environ.copy()
    env["MEDS_STATE_FILE"]  = str(state_dir / "meds-state.json").replace("\\", "/")
    env["MEDS_SCHEMA_FILE"] = str(SCHEMA).replace("\\", "/")
    env["MEDS_LOG_FILE"]    = str(state_dir / "safe-write.log").replace("\\", "/")
    if env_extra:
        env.update(env_extra)

    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=content,
        capture_output=True,
        text=True,
        env=env,
    )


def valid_state(n_meds=1):
    meds = []
    for i in range(n_meds):
        meds.append({
            "id": f"med-{str(i+1).zfill(3)}",
            "name": "Metformin",
            "type": "pill",
            "dose": "500",
            "unit": "mg",
            "paused": False,
            "schedule": {
                "frequency": "once_daily",
                "times": ["08:00"],
                "interval_hours": None,
                "with_food": False,
                "notes": "",
            },
            "escalation": {
                "late_threshold_minutes": 30,
                "missed_threshold_minutes": 90,
                "late_message": "Late: {name}",
                "missed_message": "Missed: {name}",
            },
            "state": {
                "status": "pending",
                "last_taken": None,
                "last_reminded": None,
                "next_due": None,
                "missed_count": 0,
                "history": [],
            },
        })
    return json.dumps({
        "version": "1",
        "global": {
            "timezone": "America/Los_Angeles",
            "quiet_hours": {"start": "22:00", "end": "07:00"},
            "paused": False,
            "digest_time": "08:00",
            "delivery_channel": "whatsapp",
        },
        "meds": meds,
    }, indent=2)


# ── Case 21: valid JSON → success, backup created, state updated ──────────────

def test_case_21_valid_json_writes_and_backs_up():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        state_file  = d / "meds-state.json"
        backup_file = d / "meds-state.json.bak"

        # Write an initial state so backup has something to copy from
        state_file.write_text(valid_state(0))
        original = state_file.read_text()

        new_content = valid_state(1)
        result = run_safe_write(d, new_content)

        assert result.returncode == 0, f"Expected success:\n{result.stderr}"
        assert state_file.exists(), "State file should exist after write"
        assert backup_file.exists(), "Backup file should be created"
        assert state_file.read_text() == new_content
        assert backup_file.read_text() == original


# ── Case 22: invalid JSON → rejected, original file unchanged ────────────────

def test_case_22_invalid_json_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        state_file = d / "meds-state.json"
        original   = valid_state(1)
        state_file.write_text(original)

        result = run_safe_write(d, "{ this is not valid json }")

        assert result.returncode != 0, "Should fail on invalid JSON"
        assert state_file.read_text() == original, "Original file must be untouched"
        assert not (d / "meds-state.json.bak").exists(), \
            "No backup should be created on failed write"


# ── Case 23: lock exists → write fails loudly ─────────────────────────────────

def test_case_23_lock_present_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        state_file = d / "meds-state.json"
        lock_dir   = d / "meds-state.json.lock"
        state_file.write_text(valid_state(0))
        lock_dir.mkdir()  # simulate another process holding the lock

        result = run_safe_write(d, valid_state(1))

        assert result.returncode != 0, "Should fail when lock is held"
        assert state_file.read_text() == valid_state(0), "File must be unchanged"

        lock_dir.rmdir()  # cleanup


# ── Case 24: no existing state file → write succeeds, no backup created ───────

def test_case_24_first_install_no_backup():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        state_file  = d / "meds-state.json"
        backup_file = d / "meds-state.json.bak"

        assert not state_file.exists(), "Precondition: no existing state file"

        result = run_safe_write(d, valid_state(1))

        assert result.returncode == 0, f"Expected success:\n{result.stderr}"
        assert state_file.exists(), "State file should be created"
        assert not backup_file.exists(), "No backup on first install"


# ── Case 25: structurally invalid state → rejected ────────────────────────────

def test_case_25_invalid_structure_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        state_file = d / "meds-state.json"
        original   = valid_state(0)
        state_file.write_text(original)

        # Valid JSON but missing required fields
        bad = json.dumps({"version": "1", "global": {}, "meds": "not-an-array"})
        result = run_safe_write(d, bad)

        assert result.returncode != 0, "Structurally invalid state must be rejected"
        assert state_file.read_text() == original


# ── Extra: as_needed med without times is accepted ────────────────────────────

def test_as_needed_without_times_is_valid():
    """Regression: safe-write.sh must not require times for as_needed meds."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        state = {
            "version": "1",
            "global": {
                "timezone": "America/Los_Angeles",
                "quiet_hours": {"start": "22:00", "end": "07:00"},
                "paused": False,
                "digest_time": "08:00",
                "delivery_channel": "whatsapp",
            },
            "meds": [{
                "id": "med-001",
                "name": "Ibuprofen",
                "type": "pill",
                "dose": "400",
                "unit": "mg",
                "paused": False,
                "schedule": {
                    "frequency": "as_needed",
                    "interval_hours": None,
                    "with_food": False,
                    "notes": "",
                },
                "escalation": {
                    "late_threshold_minutes": 30,
                    "missed_threshold_minutes": 90,
                    "late_message": "Late: {name}",
                    "missed_message": "Missed: {name}",
                },
                "state": {
                    "status": "pending",
                    "last_taken": None,
                    "last_reminded": None,
                    "next_due": None,
                    "missed_count": 0,
                    "history": [],
                },
            }],
        }
        result = run_safe_write(d, json.dumps(state, indent=2))
        assert result.returncode == 0, \
            f"as_needed without times should be accepted:\n{result.stderr}"
