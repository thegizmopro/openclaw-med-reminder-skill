"""
Cases 21-25: safe_write.py — atomic write, validation, locking.

Tests the Python module directly (no bash subprocess).
"""
import json
import os
import sys
import tempfile
import time
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import safe_write as sw


def _state(n_meds=1):
    meds = []
    for i in range(n_meds):
        meds.append({
            "id": f"med-{str(i+1).zfill(3)}",
            "name": "Metformin", "type": "pill",
            "dose": "500", "unit": "mg", "paused": False,
            "schedule": {"frequency": "once_daily", "times": ["08:00"],
                         "interval_hours": None, "with_food": False, "notes": ""},
            "escalation": {"late_threshold_minutes": 30, "missed_threshold_minutes": 90,
                           "late_message": "Late: {name}", "missed_message": "Missed: {name}"},
            "state": {"status": "pending", "last_taken": None, "last_reminded": None,
                      "next_due": None, "missed_count": 0, "history": []},
        })
    return {"version": "1", "global": {
        "timezone": "America/Los_Angeles",
        "quiet_hours": {"start": "22:00", "end": "07:00"},
        "paused": False, "digest_time": "08:00", "delivery_channel": "whatsapp",
    }, "meds": meds}


def with_paths(tmp_path, fn):
    """Run fn with sw paths pointed at tmp_path."""
    orig_state  = sw.STATE_FILE
    orig_log    = sw.LOG_FILE
    orig_lock   = sw.LOCK_DIR
    orig_backup = sw.BACKUP_FILE
    orig_tmp    = sw.TMP_FILE
    try:
        sw.STATE_FILE   = tmp_path / "meds-state.json"
        sw.LOG_FILE     = tmp_path / "safe-write.log"
        sw.LOCK_DIR     = tmp_path / "meds-state.json.lock"
        sw.BACKUP_FILE  = tmp_path / "meds-state.json.bak"
        sw.TMP_FILE     = tmp_path / "meds-state.json.tmp"
        fn(tmp_path)
    finally:
        sw.STATE_FILE   = orig_state
        sw.LOG_FILE     = orig_log
        sw.LOCK_DIR     = orig_lock
        sw.BACKUP_FILE  = orig_backup
        sw.TMP_FILE     = orig_tmp


# ── Case 21: valid state → success, backup created, file updated ──────────────

def test_case_21_valid_write_and_backup(tmp_path):
    def run(d):
        sw.STATE_FILE.write_text(json.dumps(_state(0)), encoding="utf-8")
        original = sw.STATE_FILE.read_text()
        new = json.dumps(_state(1), indent=2)
        sw.safe_write(new)
        assert sw.STATE_FILE.read_text() == new
        assert sw.BACKUP_FILE.exists()
        assert sw.BACKUP_FILE.read_text() == original
    with_paths(tmp_path, run)


# ── Case 22: invalid JSON → ValueError, file unchanged ───────────────────────

def test_case_22_invalid_json_rejected(tmp_path):
    def run(d):
        original = json.dumps(_state(0))
        sw.STATE_FILE.write_text(original, encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            sw.safe_write("{ not valid json }")
        assert sw.STATE_FILE.read_text() == original
    with_paths(tmp_path, run)


# ── Case 23: lock held → RuntimeError ────────────────────────────────────────

def test_case_23_lock_held_raises(tmp_path):
    def run(d):
        sw.STATE_FILE.write_text(json.dumps(_state(0)), encoding="utf-8")
        sw.LOCK_DIR.mkdir()
        # Make it look fresh (not stale)
        try:
            with pytest.raises(RuntimeError, match="Could not acquire lock"):
                sw.safe_write(_state(1))
        finally:
            sw.LOCK_DIR.rmdir()
    with_paths(tmp_path, run)


# ── Case 24: first install (no existing file) → write, no backup ─────────────

def test_case_24_first_install(tmp_path):
    def run(d):
        assert not sw.STATE_FILE.exists()
        sw.safe_write(_state(1))
        assert sw.STATE_FILE.exists()
        assert not sw.BACKUP_FILE.exists()
    with_paths(tmp_path, run)


# ── Case 25: structurally invalid state → ValueError ─────────────────────────

def test_case_25_invalid_structure_rejected(tmp_path):
    def run(d):
        original = json.dumps(_state(0))
        sw.STATE_FILE.write_text(original, encoding="utf-8")
        bad = {"version": "1", "global": {}, "meds": "not-an-array"}
        with pytest.raises(ValueError):
            sw.safe_write(bad)
        assert sw.STATE_FILE.read_text() == original
    with_paths(tmp_path, run)


# ── Regression: as_needed without times must be accepted ─────────────────────

def test_as_needed_without_times_accepted(tmp_path):
    def run(d):
        state = _state(0)
        state["meds"] = [{
            "id": "med-001", "name": "Ibuprofen", "type": "pill",
            "dose": "400", "unit": "mg", "paused": False,
            "schedule": {"frequency": "as_needed", "interval_hours": None,
                         "with_food": False, "notes": ""},
            "escalation": {"late_threshold_minutes": 30, "missed_threshold_minutes": 90,
                           "late_message": "x", "missed_message": "y"},
            "state": {"status": "pending", "last_taken": None, "last_reminded": None,
                      "next_due": None, "missed_count": 0, "history": []},
        }]
        sw.safe_write(state)  # must not raise
        assert sw.STATE_FILE.exists()
    with_paths(tmp_path, run)


# ── Validate: duplicate med IDs rejected ────────────────────────────────────

def test_duplicate_med_id_rejected():
    state = _state(1)
    state["meds"].append(state["meds"][0].copy())  # duplicate med-001
    with pytest.raises(ValueError, match="duplicate id"):
        sw.validate(state)


# ── Validate: late >= missed rejected ────────────────────────────────────────

def test_late_threshold_gte_missed_rejected():
    state = _state(1)
    state["meds"][0]["escalation"]["late_threshold_minutes"]   = 90
    state["meds"][0]["escalation"]["missed_threshold_minutes"] = 30
    with pytest.raises(ValueError, match="less than missed"):
        sw.validate(state)
