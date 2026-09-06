"""
Tests for scenarios.py — ScenarioRunner lifecycle, event dispatch, and persistence.
"""

import time
from unittest.mock import patch

import pytest

from scenarios import (
    ScenarioRunner,
    get_scenario_definition,
    scenario_catalog_payload,
)


# ---------------------------------------------------------------------------
# get_scenario_definition / catalog
# ---------------------------------------------------------------------------

def test_get_scenario_definition_returns_known_scenario():
    defn = get_scenario_definition("utility-fail-recovery")
    assert defn is not None
    assert defn.scenario_id == "utility-fail-recovery"


def test_get_scenario_definition_returns_none_for_unknown():
    assert get_scenario_definition("does-not-exist") is None


def test_scenario_catalog_payload_includes_all_definitions():
    catalog = scenario_catalog_payload()
    ids = {item["scenario_id"] for item in catalog}
    assert "utility-fail-recovery" in ids
    assert "fault-and-reset" in ids
    assert "parallel-mode-demo" in ids
    assert "e-stop-drill" in ids


def test_scenario_public_payload_has_expected_keys():
    defn = get_scenario_definition("fault-and-reset")
    payload = defn.public_payload()
    assert "scenario_id" in payload
    assert "name" in payload
    assert "description" in payload
    assert "duration_seconds" in payload
    assert "event_count" in payload
    assert "events" in payload
    assert payload["event_count"] == len(payload["events"])


# ---------------------------------------------------------------------------
# ScenarioRunner — start / stop
# ---------------------------------------------------------------------------

def test_runner_active_scenario_id_returns_none_when_idle():
    runner = ScenarioRunner()
    assert runner.active_scenario_id() is None


def test_runner_start_sets_active_run():
    runner = ScenarioRunner()
    run = runner.start("utility-fail-recovery")
    assert run["scenario_id"] == "utility-fail-recovery"
    assert run["status"] == "running"
    assert "run_id" in run
    assert runner.active_scenario_id() == "utility-fail-recovery"


def test_utility_fail_recovery_scenario_stops_after_restore():
    definition = get_scenario_definition("utility-fail-recovery")
    events = definition.events
    restore_index = next(
        index for index, event in enumerate(events)
        if event.action == "fleet_command" and event.params.get("cmd") == 11
    )

    stop_event = events[restore_index + 1]
    assert stop_event.action == "fleet_command"
    assert stop_event.params["cmd"] == 2
    assert stop_event.at_seconds > events[restore_index].at_seconds


def test_runner_start_raises_for_unknown_scenario():
    runner = ScenarioRunner()
    with pytest.raises(ValueError, match="Unknown scenario"):
        runner.start("not-a-real-scenario")


def test_runner_start_raises_when_already_running():
    runner = ScenarioRunner()
    runner.start("utility-fail-recovery")
    with pytest.raises(RuntimeError, match="already running"):
        runner.start("fault-and-reset")


def test_runner_stop_returns_stopped_run():
    runner = ScenarioRunner()
    runner.start("e-stop-drill")
    result = runner.stop()
    assert result["status"] == "stopped"
    assert "stopped_at" in result
    assert runner.active_scenario_id() is None


def test_runner_stop_raises_when_no_active_run():
    runner = ScenarioRunner()
    with pytest.raises(RuntimeError, match="No scenario is running"):
        runner.stop()


def test_runner_stop_adds_to_history():
    runner = ScenarioRunner()
    runner.start("e-stop-drill")
    runner.stop()
    assert len(runner.history) == 1
    assert runner.history[0]["status"] == "stopped"


def test_runner_history_capped_at_ten():
    runner = ScenarioRunner()
    for _ in range(12):
        runner.start("e-stop-drill")
        runner.stop()
    assert len(runner.history) == 10


# ---------------------------------------------------------------------------
# ScenarioRunner — advance
# ---------------------------------------------------------------------------

def test_advance_returns_empty_list_when_no_active_run():
    runner = ScenarioRunner()
    assert runner.advance() == []


def test_advance_fires_events_at_correct_elapsed_time():
    runner = ScenarioRunner()
    # Use a fixed monotonic origin so we control elapsed time precisely.
    fixed_start = 1000.0
    with patch("scenarios.time.monotonic", return_value=fixed_start):
        runner.start("e-stop-drill")

    # At t=0 seconds there should be no events yet (event at 0.0 already
    # has elapsed_seconds == 0.0, so it fires on the first advance call
    # when we simulate being exactly at t=0).
    with patch("scenarios.time.monotonic", return_value=fixed_start + 0.0):
        events = runner.advance()
    # The e-stop-drill scenario has its first event at 0.0 seconds.
    assert any(e.at_seconds == 0.0 for e in events)


def test_advance_fires_fleet_command_event():
    runner = ScenarioRunner()
    fixed_start = 2000.0
    with patch("scenarios.time.monotonic", return_value=fixed_start):
        runner.start("e-stop-drill")

    # Advance to t=9 — should fire the fleet_command at 8.0s.
    with patch("scenarios.time.monotonic", return_value=fixed_start + 9.0):
        events = runner.advance()

    fleet_cmd_events = [e for e in events if e.action == "fleet_command"]
    assert len(fleet_cmd_events) >= 1


def test_advance_does_not_repeat_already_fired_events():
    runner = ScenarioRunner()
    fixed_start = 3000.0
    with patch("scenarios.time.monotonic", return_value=fixed_start):
        runner.start("e-stop-drill")

    # First advance at t=9
    with patch("scenarios.time.monotonic", return_value=fixed_start + 9.0):
        first = runner.advance()

    # Second advance at same time — no new events
    with patch("scenarios.time.monotonic", return_value=fixed_start + 9.0):
        second = runner.advance()

    assert len(second) == 0
    assert len(first) > 0


def test_advance_completes_scenario_when_duration_exceeded():
    runner = ScenarioRunner()
    fixed_start = 4000.0
    scenario = get_scenario_definition("e-stop-drill")
    with patch("scenarios.time.monotonic", return_value=fixed_start):
        runner.start("e-stop-drill")

    # Jump past duration
    with patch("scenarios.time.monotonic", return_value=fixed_start + scenario.duration_seconds + 1.0):
        events = runner.advance()

    assert events == []
    assert runner.active_scenario_id() is None
    assert runner.history[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# ScenarioRunner — status_payload
# ---------------------------------------------------------------------------

def test_status_payload_returns_catalog_and_no_active_when_idle():
    runner = ScenarioRunner()
    payload = runner.status_payload()
    assert "catalog" in payload
    assert payload["active_run"] is None
    assert "history" in payload


def test_status_payload_includes_active_run_when_running():
    runner = ScenarioRunner()
    runner.start("fault-and-reset")
    payload = runner.status_payload()
    assert payload["active_run"] is not None
    assert payload["active_run"]["scenario_id"] == "fault-and-reset"


# ---------------------------------------------------------------------------
# ScenarioRunner — restore_state / snapshot_for_save
# ---------------------------------------------------------------------------

def test_snapshot_for_save_returns_active_none_when_idle():
    runner = ScenarioRunner()
    snap = runner.snapshot_for_save()
    assert snap["active_scenario_run"] is None
    assert snap["scenario_history"] == []


def test_snapshot_captures_active_run():
    runner = ScenarioRunner()
    runner.start("parallel-mode-demo")
    snap = runner.snapshot_for_save()
    assert snap["active_scenario_run"]["scenario_id"] == "parallel-mode-demo"


def test_restore_state_resumes_active_scenario():
    source = ScenarioRunner()
    source.start("parallel-mode-demo")
    snap = source.snapshot_for_save()

    target = ScenarioRunner()
    target.restore_state(snap)
    assert target.active_scenario_id() == "parallel-mode-demo"


def test_restore_state_preserves_history():
    source = ScenarioRunner()
    source.start("e-stop-drill")
    source.stop()
    snap = source.snapshot_for_save()

    target = ScenarioRunner()
    target.restore_state(snap)
    assert len(target.history) == 1
    assert target.history[0]["status"] == "stopped"


def test_restore_state_ignores_unknown_scenario_id():
    target = ScenarioRunner()
    # Craft a saved state that references a non-existent scenario
    target.restore_state({"active_scenario_run": {"scenario_id": "ghost-scenario", "elapsed_seconds": 5.0}})
    assert target.active_scenario_id() is None


def test_restore_state_tolerates_empty_saved_state():
    target = ScenarioRunner()
    target.restore_state({})
    assert target.active_scenario_id() is None
    assert target.history == []


@pytest.mark.parametrize("saved", [[], "not-an-object", {"active_scenario_run": "bad"}])
def test_restore_state_ignores_invalid_saved_shapes(saved):
    target = ScenarioRunner()

    target.restore_state(saved)

    assert target.active_scenario_id() is None
    assert target.history == []


@pytest.mark.parametrize("elapsed_seconds", ["bad", "NaN", float("inf"), -5.0])
def test_restore_state_sanitizes_invalid_elapsed_seconds(elapsed_seconds):
    target = ScenarioRunner()
    fixed_now = 5000.0

    with patch("scenarios.time.monotonic", return_value=fixed_now):
        target.restore_state(
            {
                "active_scenario_run": {
                    "scenario_id": "parallel-mode-demo",
                    "elapsed_seconds": elapsed_seconds,
                    "status": "running",
                    "next_event_index": 0,
                }
            }
        )

    assert target.active_scenario_id() == "parallel-mode-demo"
    assert target.active_run["_started_monotonic"] == fixed_now
