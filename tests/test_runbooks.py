"""
Tests for runbooks.py — validation, sanitization, presets, persistence, and registry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runbooks import (
    FLEET_MODE_VALUES,
    MAX_RUNBOOK_FILE_BYTES,
    MAX_RUNBOOK_STEPS,
    MAX_RUNBOOK_TEXT_LENGTH,
    MAX_SAVED_RUNBOOKS,
    RUNBOOK_ACTION_OPTIONS,
    RUNBOOK_ASSERT_METRICS,
    RUNBOOK_COMPARISONS,
    RunbookRegistry,
    default_runbook_presets,
    load_runbooks,
    sanitize_runbook_step,
    sanitize_saved_runbook,
    save_runbooks,
    summarize_runbook_step,
)


# ---------------------------------------------------------------------------
# summarize_runbook_step
# ---------------------------------------------------------------------------

def test_summarize_note():
    step = {"action": "note", "params": {"message": "Starting drill."}}
    assert summarize_runbook_step(step) == "Starting drill."


def test_summarize_fleet_command_with_label():
    step = {"action": "fleet_command", "params": {"cmd": 1, "label": "Start all generators"}}
    assert "Start all generators" in summarize_runbook_step(step)


def test_summarize_fleet_command_no_label():
    step = {"action": "fleet_command", "params": {"cmd": 7}}
    assert "7" in summarize_runbook_step(step)


def test_summarize_unit_command():
    step = {"action": "unit_command", "params": {"unit_id": 2, "cmd": 8}}
    summary = summarize_runbook_step(step)
    assert "2" in summary and "8" in summary


def test_summarize_fleet_mode():
    step = {"action": "fleet_mode", "params": {"mode": "parallel"}}
    assert "parallel" in summarize_runbook_step(step).lower()


def test_summarize_fault_injection():
    step = {"action": "fault_injection", "params": {"unit_id": 1, "cmd": 20}}
    summary = summarize_runbook_step(step)
    assert "1" in summary and "20" in summary


def test_summarize_load_setpoint():
    step = {"action": "load_setpoint", "params": {"unit_id": 3, "setpoint_kw": 400.0}}
    summary = summarize_runbook_step(step)
    assert "3" in summary and "400" in summary


def test_summarize_assert_fleet_metric():
    step = {
        "action": "assert_fleet_metric",
        "params": {"metric": "running_units", "comparison": "gte", "value": 2.0, "label": "Two units running"},
    }
    summary = summarize_runbook_step(step)
    assert "Two units running" in summary
    assert "running_units" in summary


# ---------------------------------------------------------------------------
# sanitize_runbook_step — valid cases
# ---------------------------------------------------------------------------

def test_sanitize_note_step():
    raw = {"at_seconds": 0, "action": "note", "params": {"message": "Hello world"}}
    step = sanitize_runbook_step(raw, 1)
    assert step["action"] == "note"
    assert step["params"]["message"] == "Hello world"
    assert "summary" in step


def test_sanitize_note_step_rejects_long_message():
    raw = {"at_seconds": 1, "action": "note", "params": {"message": "x" * (MAX_RUNBOOK_TEXT_LENGTH + 1)}}
    with pytest.raises(ValueError, match="cannot exceed"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_fleet_command_step():
    raw = {"at_seconds": 5, "action": "fleet_command", "params": {"cmd": 1}}
    step = sanitize_runbook_step(raw, 1)
    assert step["action"] == "fleet_command"
    assert step["params"]["cmd"] == 1


def test_sanitize_unit_command_step():
    raw = {"at_seconds": 10, "action": "unit_command", "params": {"unit_id": 2, "cmd": 8}}
    step = sanitize_runbook_step(raw, 1)
    assert step["params"]["unit_id"] == 2
    assert step["params"]["cmd"] == 8


def test_sanitize_fleet_mode_step():
    raw = {"at_seconds": 8, "action": "fleet_mode", "params": {"mode": "parallel"}}
    step = sanitize_runbook_step(raw, 1)
    assert step["params"]["mode"] == "parallel"


def test_sanitize_fault_injection_step():
    raw = {"at_seconds": 6, "action": "fault_injection", "params": {"unit_id": 1, "cmd": 20}}
    step = sanitize_runbook_step(raw, 1)
    assert step["params"]["unit_id"] == 1
    assert step["params"]["cmd"] == 20


def test_sanitize_rejects_unsupported_command_values():
    raw = {"at_seconds": 5, "action": "fleet_command", "params": {"cmd": 999}}
    with pytest.raises(ValueError, match="supported command value"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_rejects_fractional_command_values():
    raw = {"at_seconds": 5, "action": "fleet_command", "params": {"cmd": 1.5}}
    with pytest.raises(ValueError, match="must be an integer"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_fault_injection_requires_fault_command():
    raw = {"at_seconds": 6, "action": "fault_injection", "params": {"unit_id": 1, "cmd": 1}}
    with pytest.raises(ValueError, match="supported command value"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_load_setpoint_step():
    raw = {"at_seconds": 20, "action": "load_setpoint", "params": {"unit_id": 3, "setpoint_kw": 300.0}}
    step = sanitize_runbook_step(raw, 1)
    assert step["params"]["setpoint_kw"] == 300.0


def test_sanitize_assert_fleet_metric_step():
    raw = {
        "at_seconds": 30,
        "action": "assert_fleet_metric",
        "params": {"metric": "running_units", "comparison": "gte", "value": 1},
    }
    step = sanitize_runbook_step(raw, 1)
    assert step["params"]["metric"] == "running_units"
    assert step["params"]["comparison"] == "gte"
    assert step["params"]["value"] == 1.0


def test_sanitize_accepts_flat_top_level_fields():
    """Flat fields (not under params) should be accepted for ergonomic authoring."""
    raw = {"at_seconds": 0, "action": "note", "message": "flat field"}
    step = sanitize_runbook_step(raw, 1)
    assert step["params"]["message"] == "flat field"


def test_sanitize_assert_with_tolerance():
    raw = {
        "at_seconds": 10,
        "action": "assert_fleet_metric",
        "params": {"metric": "total_power_kw", "comparison": "eq", "value": 500.0, "tolerance": 50.0},
    }
    step = sanitize_runbook_step(raw, 1)
    assert step["params"]["tolerance"] == 50.0


# ---------------------------------------------------------------------------
# sanitize_runbook_step — invalid cases
# ---------------------------------------------------------------------------

def test_sanitize_raises_for_non_dict_step():
    with pytest.raises(ValueError, match="must be an object"):
        sanitize_runbook_step("not a dict", 1)


def test_sanitize_raises_for_negative_at_seconds():
    raw = {"at_seconds": -1, "action": "note", "params": {"message": "oops"}}
    with pytest.raises(ValueError, match="cannot be negative"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_raises_for_non_finite_at_seconds():
    raw = {"at_seconds": "NaN", "action": "note", "params": {"message": "oops"}}
    with pytest.raises(ValueError, match="finite number"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_raises_for_unknown_action():
    raw = {"at_seconds": 0, "action": "invalid_action", "params": {}}
    with pytest.raises(ValueError, match="action must be one of"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_note_requires_message():
    raw = {"at_seconds": 0, "action": "note", "params": {}}
    with pytest.raises(ValueError, match="message is required"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_fleet_mode_rejects_unknown_mode():
    raw = {"at_seconds": 0, "action": "fleet_mode", "params": {"mode": "turbo"}}
    with pytest.raises(ValueError, match="mode must be one of"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_assert_rejects_unknown_metric():
    raw = {
        "at_seconds": 0,
        "action": "assert_fleet_metric",
        "params": {"metric": "ghost_metric", "comparison": "eq", "value": 0},
    }
    with pytest.raises(ValueError, match="metric must be one of"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_assert_rejects_unknown_comparison():
    raw = {
        "at_seconds": 0,
        "action": "assert_fleet_metric",
        "params": {"metric": "running_units", "comparison": "between", "value": 0},
    }
    with pytest.raises(ValueError, match="comparison must be one of"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_assert_rejects_negative_tolerance():
    raw = {
        "at_seconds": 0,
        "action": "assert_fleet_metric",
        "params": {"metric": "running_units", "comparison": "eq", "value": 0, "tolerance": -1.0},
    }
    with pytest.raises(ValueError, match="tolerance cannot be negative"):
        sanitize_runbook_step(raw, 1)


def test_sanitize_assert_rejects_non_finite_value():
    raw = {
        "at_seconds": 0,
        "action": "assert_fleet_metric",
        "params": {"metric": "running_units", "comparison": "eq", "value": "Infinity"},
    }
    with pytest.raises(ValueError, match="finite number"):
        sanitize_runbook_step(raw, 1)


# ---------------------------------------------------------------------------
# sanitize_saved_runbook
# ---------------------------------------------------------------------------

def _make_minimal_runbook(**overrides):
    base = {
        "id": "test-rb",
        "name": "Test Runbook",
        "description": "A test runbook",
        "steps": [{"at_seconds": 0, "action": "note", "params": {"message": "hi"}}],
    }
    base.update(overrides)
    return base


def test_sanitize_saved_runbook_valid():
    rb = sanitize_saved_runbook(_make_minimal_runbook())
    assert rb["id"] == "test-rb"
    assert rb["name"] == "Test Runbook"
    assert len(rb["steps"]) == 1
    assert "summary" in rb["steps"][0]


def test_sanitize_saved_runbook_accepts_url_safe_id_characters():
    rb = sanitize_saved_runbook(_make_minimal_runbook(id="Runbook_1.2-3"))

    assert rb["id"] == "Runbook_1.2-3"


@pytest.mark.parametrize("runbook_id", ["bad id", "../bad", "-bad", ".bad", "bad/id", "bad\r\nid"])
def test_sanitize_saved_runbook_rejects_url_unsafe_ids(runbook_id):
    with pytest.raises(ValueError, match="Runbook id may contain only"):
        sanitize_saved_runbook(_make_minimal_runbook(id=runbook_id))


def test_sanitize_saved_runbook_sorts_steps():
    raw = _make_minimal_runbook(steps=[
        {"at_seconds": 20, "action": "note", "params": {"message": "second"}},
        {"at_seconds": 5, "action": "note", "params": {"message": "first"}},
    ])
    rb = sanitize_saved_runbook(raw)
    assert rb["steps"][0]["at_seconds"] == 5
    assert rb["steps"][1]["at_seconds"] == 20


def test_sanitize_saved_runbook_requires_name():
    with pytest.raises(ValueError, match="name is required"):
        sanitize_saved_runbook(_make_minimal_runbook(name=""))


def test_sanitize_saved_runbook_requires_id():
    with pytest.raises(ValueError, match="id is required"):
        sanitize_saved_runbook(_make_minimal_runbook(id=""))


def test_sanitize_saved_runbook_requires_steps():
    with pytest.raises(ValueError, match="steps must be a non-empty list"):
        sanitize_saved_runbook(_make_minimal_runbook(steps=[]))


def test_sanitize_saved_runbook_rejects_too_many_steps():
    steps = [
        {"at_seconds": idx, "action": "note", "params": {"message": f"step {idx}"}}
        for idx in range(MAX_RUNBOOK_STEPS + 1)
    ]
    with pytest.raises(ValueError, match=f"cannot exceed {MAX_RUNBOOK_STEPS}"):
        sanitize_saved_runbook(_make_minimal_runbook(steps=steps))


def test_sanitize_saved_runbook_rejects_non_dict():
    with pytest.raises(ValueError, match="Runbook must be an object"):
        sanitize_saved_runbook("not a dict")


# ---------------------------------------------------------------------------
# default_runbook_presets
# ---------------------------------------------------------------------------

def test_default_runbook_presets_returns_four_presets():
    presets = default_runbook_presets()
    assert len(presets) == 4


def test_default_runbook_presets_have_required_keys():
    presets = default_runbook_presets()
    for preset in presets:
        assert "id" in preset
        assert "name" in preset
        assert "steps" in preset
        assert len(preset["steps"]) > 0


def test_default_runbook_presets_cover_all_scenarios():
    presets = default_runbook_presets()
    ids = {p["id"] for p in presets}
    assert "utility-fail-recovery" in ids
    assert "fault-and-reset" in ids
    assert "parallel-mode-demo" in ids
    assert "e-stop-drill" in ids


def test_utility_fail_recovery_runbook_stops_after_restore():
    presets = default_runbook_presets()
    runbook = next(p for p in presets if p["id"] == "utility-fail-recovery")
    steps = runbook["steps"]

    restore_index = next(
        index for index, step in enumerate(steps)
        if step["action"] == "fleet_command" and step["params"]["cmd"] == 11
    )

    stop_step = steps[restore_index + 1]
    assert stop_step["action"] == "fleet_command"
    assert stop_step["params"]["cmd"] == 2
    assert stop_step["at_seconds"] > steps[restore_index]["at_seconds"]


def test_default_runbook_presets_are_copies():
    """Each call should return independent copies."""
    a = default_runbook_presets()
    b = default_runbook_presets()
    a[0]["name"] = "mutated"
    assert b[0]["name"] != "mutated"


# ---------------------------------------------------------------------------
# load_runbooks / save_runbooks persistence
# ---------------------------------------------------------------------------

def test_load_runbooks_returns_defaults_when_file_absent(tmp_path):
    path = tmp_path / "runbooks.json"
    runbooks = load_runbooks(path)
    assert len(runbooks) == 4  # default presets


def test_save_and_load_runbooks_roundtrip(tmp_path):
    path = tmp_path / "runbooks.json"
    rb = sanitize_saved_runbook(_make_minimal_runbook())
    save_runbooks([rb], path)
    loaded = load_runbooks(path)
    assert len(loaded) == 1
    assert loaded[0]["id"] == "test-rb"


def test_save_runbooks_keeps_previous_file_as_backup(tmp_path):
    path = tmp_path / "runbooks.json"
    first = sanitize_saved_runbook(_make_minimal_runbook(id="first-rb"))
    second = sanitize_saved_runbook(_make_minimal_runbook(id="second-rb"))
    save_runbooks([first], path)
    original = path.read_text(encoding="utf-8")
    save_runbooks([second], path)

    backup = path.with_name(f"{path.name}.bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original
    assert load_runbooks(path)[0]["id"] == "second-rb"


def test_save_runbooks_is_atomic(tmp_path):
    """save_runbooks should produce a valid JSON file (not partial)."""
    path = tmp_path / "runbooks.json"
    rb = sanitize_saved_runbook(_make_minimal_runbook())
    save_runbooks([rb], path)
    assert path.exists()
    with path.open() as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert data[0]["id"] == "test-rb"


def test_save_runbooks_rejects_non_finite_json_values(tmp_path):
    path = tmp_path / "runbooks.json"
    rb = sanitize_saved_runbook(_make_minimal_runbook())
    rb["steps"][0]["at_seconds"] = float("nan")

    with pytest.raises(ValueError, match="Out of range float values"):
        save_runbooks([rb], path)

    assert not path.exists()


def test_save_runbooks_rejects_oversized_file(tmp_path):
    path = tmp_path / "runbooks.json"
    rb = sanitize_saved_runbook(_make_minimal_runbook())
    rb["description"] = "x" * MAX_RUNBOOK_FILE_BYTES

    with pytest.raises(ValueError, match=f"cannot exceed {MAX_RUNBOOK_FILE_BYTES} bytes"):
        save_runbooks([rb], path)

    assert not path.exists()


def test_load_runbooks_returns_defaults_for_invalid_json(tmp_path):
    path = tmp_path / "runbooks.json"
    path.write_text("not valid json")
    runbooks = load_runbooks(path)
    assert len(runbooks) == 4  # defaults


def test_load_runbooks_skips_invalid_entries(tmp_path):
    path = tmp_path / "runbooks.json"
    good = _make_minimal_runbook()
    bad = {"id": "bad", "name": "", "steps": []}  # invalid: empty name
    path.write_text(json.dumps([good, bad]))
    runbooks = load_runbooks(path)
    assert len(runbooks) == 1
    assert runbooks[0]["id"] == "test-rb"


# ---------------------------------------------------------------------------
# RunbookRegistry
# ---------------------------------------------------------------------------

def test_registry_initializes_with_defaults(tmp_path):
    registry = RunbookRegistry(tmp_path / "runbooks.json")
    assert len(registry.list_runbooks()) == 4


def test_registry_save_runbook_adds_new(tmp_path):
    registry = RunbookRegistry(tmp_path / "runbooks.json")
    initial_count = len(registry.list_runbooks())
    rb = sanitize_saved_runbook(_make_minimal_runbook(id="new-rb", name="New Runbook"))
    registry.save_runbook(rb)
    assert len(registry.list_runbooks()) == initial_count + 1


def test_registry_rejects_new_runbook_over_saved_limit(tmp_path):
    path = tmp_path / "runbooks.json"
    runbooks = [
        sanitize_saved_runbook(_make_minimal_runbook(id=f"rb-{idx}", name=f"Runbook {idx}"))
        for idx in range(MAX_SAVED_RUNBOOKS)
    ]
    save_runbooks(runbooks, path)
    registry = RunbookRegistry(path)
    extra = sanitize_saved_runbook(_make_minimal_runbook(id="extra-rb", name="Extra Runbook"))

    with pytest.raises(ValueError, match=f"cannot exceed {MAX_SAVED_RUNBOOKS}"):
        registry.save_runbook(extra)


def test_registry_save_runbook_upserts_existing(tmp_path):
    registry = RunbookRegistry(tmp_path / "runbooks.json")
    rb = sanitize_saved_runbook(_make_minimal_runbook(id="test-rb", name="Test Runbook"))
    registry.save_runbook(rb)
    count_after_first = len(registry.list_runbooks())

    rb_updated = sanitize_saved_runbook(_make_minimal_runbook(id="test-rb", name="Updated Name"))
    registry.save_runbook(rb_updated)
    assert len(registry.list_runbooks()) == count_after_first

    found = registry.get_runbook("test-rb")
    assert found["name"] == "Updated Name"


def test_registry_get_runbook_returns_none_for_unknown(tmp_path):
    registry = RunbookRegistry(tmp_path / "runbooks.json")
    assert registry.get_runbook("ghost-id") is None


def test_registry_delete_runbook_removes_it(tmp_path):
    registry = RunbookRegistry(tmp_path / "runbooks.json")
    rb = sanitize_saved_runbook(_make_minimal_runbook(id="to-delete", name="To Delete"))
    registry.save_runbook(rb)
    assert registry.get_runbook("to-delete") is not None
    result = registry.delete_runbook("to-delete")
    assert result is True
    assert registry.get_runbook("to-delete") is None


def test_registry_delete_runbook_returns_false_for_unknown(tmp_path):
    registry = RunbookRegistry(tmp_path / "runbooks.json")
    assert registry.delete_runbook("does-not-exist") is False


def test_registry_persists_across_instances(tmp_path):
    path = tmp_path / "runbooks.json"
    registry1 = RunbookRegistry(path)
    rb = sanitize_saved_runbook(_make_minimal_runbook(id="persistent", name="Persistent"))
    registry1.save_runbook(rb)

    registry2 = RunbookRegistry(path)
    found = registry2.get_runbook("persistent")
    assert found is not None
    assert found["name"] == "Persistent"


def test_registry_list_returns_copies(tmp_path):
    """Mutations to returned list should not affect registry internals."""
    registry = RunbookRegistry(tmp_path / "runbooks.json")
    runbooks = registry.list_runbooks()
    runbooks[0]["name"] = "mutated"
    fresh = registry.list_runbooks()
    assert fresh[0]["name"] != "mutated"
