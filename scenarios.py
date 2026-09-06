"""
Scenario catalog and timeline helpers for repeatable simulator playback.

Each scenario is a pre-scripted sequence of commands that can be applied to the
Generator Fleet simulator to demonstrate common operational patterns.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


SCENARIO_ACTION_OPTIONS = (
    "note",
    "fleet_command",
    "unit_command",
)


@dataclass(frozen=True)
class ScenarioEvent:
    at_seconds: float
    action: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)

    def public_payload(self) -> dict[str, Any]:
        return {
            "at_seconds": self.at_seconds,
            "action": self.action,
            "label": self.label,
            "params": self.params,
        }


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_id: str
    name: str
    description: str
    duration_seconds: float
    events: tuple[ScenarioEvent, ...]

    def public_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "description": self.description,
            "duration_seconds": self.duration_seconds,
            "event_count": len(self.events),
            "events": [event.public_payload() for event in self.events],
        }


# Generator Fleet command values (from MODBUS_COMMANDS in main.py)
# 1=Start, 2=Stop, 7=E-Stop, 8=Reset Alarms, 9=Toggle Auto,
# 10=Utility Fail, 11=Restore Utility, 12=Set Parallel Mode, 13=Set Island Mode
SCENARIO_DEFINITIONS: dict[str, ScenarioDefinition] = {
    "utility-fail-recovery": ScenarioDefinition(
        scenario_id="utility-fail-recovery",
        name="Utility Fail and Recovery",
        description=(
            "Simulates a utility outage: starts the fleet in auto mode, triggers a utility failure "
            "so generators assume island load, then restores utility power and verifies the handoff."
        ),
        duration_seconds=45.0,
        events=(
            ScenarioEvent(0.0, "note", "Utility fail and recovery drill started"),
            ScenarioEvent(2.0, "fleet_command", "Enable auto mode on all units", {"cmd": 9}),
            ScenarioEvent(5.0, "fleet_command", "Start all generators", {"cmd": 1}),
            ScenarioEvent(12.0, "fleet_command", "Simulate utility failure", {"cmd": 10}),
            ScenarioEvent(30.0, "fleet_command", "Restore utility power", {"cmd": 11}),
            ScenarioEvent(35.0, "fleet_command", "Stop generators after utility restore", {"cmd": 2}),
            ScenarioEvent(45.0, "note", "Drill complete — generators should be returning to standby"),
        ),
    ),
    "fault-and-reset": ScenarioDefinition(
        scenario_id="fault-and-reset",
        name="Fault Injection and Reset",
        description=(
            "Injects a high coolant temperature fault on Unit 1, demonstrates the alarm response, "
            "then clears the fault and restores normal operation."
        ),
        duration_seconds=35.0,
        events=(
            ScenarioEvent(0.0, "unit_command", "Ensure Unit 1 is running", {"unit_id": 1, "cmd": 1}),
            ScenarioEvent(6.0, "unit_command", "Inject high coolant temperature fault on Unit 1", {"unit_id": 1, "cmd": 20}),
            ScenarioEvent(18.0, "unit_command", "Reset alarms on Unit 1", {"unit_id": 1, "cmd": 8}),
            ScenarioEvent(22.0, "unit_command", "Restart Unit 1", {"unit_id": 1, "cmd": 1}),
            ScenarioEvent(32.0, "note", "Unit 1 should be running cleanly"),
        ),
    ),
    "parallel-mode-demo": ScenarioDefinition(
        scenario_id="parallel-mode-demo",
        name="Parallel Mode Demo",
        description=(
            "Switches the fleet into parallel (load-sharing) mode, demonstrates coordinated kW "
            "output across multiple generators, then returns to island mode."
        ),
        duration_seconds=40.0,
        events=(
            ScenarioEvent(0.0, "fleet_command", "Start all generators", {"cmd": 1}),
            ScenarioEvent(8.0, "fleet_command", "Switch to parallel mode", {"cmd": 12}),
            ScenarioEvent(20.0, "note", "Fleet should be sharing load in parallel mode"),
            ScenarioEvent(30.0, "fleet_command", "Switch back to island mode", {"cmd": 13}),
            ScenarioEvent(38.0, "note", "Fleet returned to island mode"),
        ),
    ),
    "e-stop-drill": ScenarioDefinition(
        scenario_id="e-stop-drill",
        name="Emergency Stop Drill",
        description=(
            "Issues a fleet-wide emergency stop, verifies all units trip immediately, "
            "then resets alarms and returns to normal standby."
        ),
        duration_seconds=30.0,
        events=(
            ScenarioEvent(0.0, "fleet_command", "Start all generators", {"cmd": 1}),
            ScenarioEvent(8.0, "fleet_command", "Issue fleet-wide E-Stop", {"cmd": 7}),
            ScenarioEvent(16.0, "note", "All units should show E-Stop fault"),
            ScenarioEvent(20.0, "fleet_command", "Reset all alarms", {"cmd": 8}),
            ScenarioEvent(28.0, "note", "Fleet cleared and ready"),
        ),
    ),
}


def get_scenario_definition(scenario_id: str) -> ScenarioDefinition | None:
    return SCENARIO_DEFINITIONS.get(scenario_id)


def scenario_catalog_payload() -> list[dict[str, Any]]:
    return [definition.public_payload() for definition in SCENARIO_DEFINITIONS.values()]


class ScenarioRunner:
    """Tracks and advances an active scenario against the simulator tick clock."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.active_run: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []

    def active_scenario_id(self) -> str | None:
        with self._lock:
            if self.active_run is None:
                return None
            return self.active_run.get("scenario_id")

    def start(self, scenario_id: str) -> dict[str, Any]:
        with self._lock:
            scenario = get_scenario_definition(scenario_id)
            if scenario is None:
                raise ValueError(f"Unknown scenario: {scenario_id}")
            if self.active_run is not None:
                raise RuntimeError("A scenario is already running")
            now = time.monotonic()
            self.active_run = {
                "run_id": f"{scenario_id}-{int(time.time() * 1000)}",
                "scenario_id": scenario.scenario_id,
                "name": scenario.name,
                "description": scenario.description,
                "started_at": time.time(),
                "_started_monotonic": now,
                "elapsed_seconds": 0.0,
                "duration_seconds": scenario.duration_seconds,
                "status": "running",
                "next_event_index": 0,
            }
            return {k: v for k, v in self.active_run.items() if not k.startswith("_")}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self.active_run is None:
                raise RuntimeError("No scenario is running")
            self.active_run["status"] = "stopped"
            self.active_run["stopped_at"] = time.time()
            finished = {k: v for k, v in self.active_run.items() if not k.startswith("_")}
            self.history.insert(0, finished)
            self.history = self.history[:10]
            self.active_run = None
            return finished

    def advance(self) -> list[ScenarioEvent]:
        """
        Compute elapsed time since this scenario started and return any events
        that should fire in this tick. Called once per simulation tick.
        Returns list of events to apply.
        """
        with self._lock:
            if self.active_run is None:
                return []
            scenario = get_scenario_definition(str(self.active_run["scenario_id"]))
            if scenario is None:
                self.active_run = None
                return []

            elapsed_seconds = time.monotonic() - self.active_run["_started_monotonic"]
            self.active_run["elapsed_seconds"] = elapsed_seconds

            if elapsed_seconds >= scenario.duration_seconds:
                self.active_run["status"] = "completed"
                finished = {k: v for k, v in self.active_run.items() if not k.startswith("_")}
                self.history.insert(0, finished)
                self.history = self.history[:10]
                self.active_run = None
                return []

            pending: list[ScenarioEvent] = []
            idx = self.active_run["next_event_index"]
            while idx < len(scenario.events):
                event = scenario.events[idx]
                if event.at_seconds <= elapsed_seconds:
                    pending.append(event)
                    idx += 1
                else:
                    break
            self.active_run["next_event_index"] = idx
            return pending

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            active = (
                {k: v for k, v in self.active_run.items() if not k.startswith("_")}
                if self.active_run else None
            )
            return {
                "catalog": scenario_catalog_payload(),
                "active_run": active,
                "history": list(self.history[:5]),
            }

    def restore_state(self, saved: dict[str, Any]) -> None:
        """Restore persisted scenario runner state on startup."""
        with self._lock:
            if not isinstance(saved, dict):
                return
            active_run = saved.get("active_scenario_run")
            if isinstance(active_run, dict) and isinstance(active_run.get("scenario_id"), str):
                if get_scenario_definition(active_run["scenario_id"]) is not None:
                    # Resume from the previously elapsed position using current monotonic clock
                    # as origin, offset by already-elapsed seconds, so timing stays coherent.
                    try:
                        already_elapsed = float(active_run.get("elapsed_seconds", 0.0))
                    except (TypeError, ValueError):
                        already_elapsed = 0.0
                    if not math.isfinite(already_elapsed) or already_elapsed < 0.0:
                        already_elapsed = 0.0
                    active_run = dict(active_run)
                    active_run["_started_monotonic"] = time.monotonic() - already_elapsed
                    self.active_run = active_run
            history = saved.get("scenario_history")
            if history and isinstance(history, list):
                self.history = [item for item in history if isinstance(item, dict)][:10]

    def snapshot_for_save(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_scenario_run": dict(self.active_run) if self.active_run else None,
                "scenario_history": list(self.history[:10]),
            }
