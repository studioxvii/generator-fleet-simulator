"""
Runbook definitions, validation, and persistence for generator fleet simulator.

Runbooks are executable scenario timelines that combine timed control actions
and fleet assertions into one repeatable operating story.

Step schema uses a flat ``params`` dict rather than top-level action-specific
fields so that a future ``parameters`` key and ``{{variable}}`` substitution
layer can be added without schema changes (see P4.3).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

RUNBOOK_ACTION_OPTIONS = (
    "note",
    "fleet_command",
    "unit_command",
    "fleet_mode",
    "fault_injection",
    "load_setpoint",
    "assert_fleet_metric",
)

RUNBOOK_ASSERT_METRICS = (
    "running_units",
    "faulted_units",
    "total_power_kw",
    "average_frequency_hz",
    "average_voltage_v",
    "average_fuel_level",
    "fleet_mode",
)

RUNBOOK_COMPARISONS = ("eq", "ne", "gt", "gte", "lt", "lte")

FLEET_MODE_VALUES = ("transfer", "parallel", "island")

# Mapping from fleet_mode action value to generator command
FLEET_MODE_COMMANDS = {
    "transfer": 14,
    "parallel": 12,
    "island": 13,
}

# Numeric encoding used for the fleet_mode assertion metric
FLEET_MODE_NUMERIC = {
    "TRANSFER": 0.0,
    "PARALLEL": 1.0,
    "ISLAND": 2.0,
}

RUNBOOK_COMMAND_VALUES = frozenset((*range(1, 15), *range(20, 40)))
RUNBOOK_FAULT_COMMAND_VALUES = frozenset(range(20, 40))

MAX_RUNBOOK_REQUEST_BYTES = 256 * 1024
MAX_RUNBOOK_FILE_BYTES = 256 * 1024
MAX_SAVED_RUNBOOKS = 64
MAX_RUNBOOK_STEPS = 200
MAX_RUNBOOK_ID_LENGTH = 96
MAX_RUNBOOK_NAME_LENGTH = 120
MAX_RUNBOOK_DESCRIPTION_LENGTH = 1000
MAX_RUNBOOK_TEXT_LENGTH = 1000
MAX_RUNBOOK_SIZE_COUNT_FIELDS = 10
RUNBOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

RUNBOOK_PRESETS = [
    {
        "id": "utility-fail-recovery",
        "name": "Utility Fail and Recovery",
        "description": (
            "Simulates a utility outage: starts the fleet in auto mode, triggers a utility failure "
            "so generators assume island load, then restores utility power and verifies the handoff."
        ),
        "size_counts": None,
        "steps": [
            {"at_seconds": 0, "action": "note", "params": {"message": "Utility fail and recovery drill started."}},
            {"at_seconds": 2, "action": "fleet_command", "params": {"cmd": 9, "label": "Enable auto mode on all units"}},
            {"at_seconds": 5, "action": "fleet_command", "params": {"cmd": 1, "label": "Start all generators"}},
            {"at_seconds": 12, "action": "fleet_command", "params": {"cmd": 10, "label": "Simulate utility failure"}},
            {
                "at_seconds": 20,
                "action": "assert_fleet_metric",
                "params": {
                    "metric": "running_units",
                    "comparison": "gte",
                    "value": 1,
                    "label": "At least one generator should be running after utility fail",
                },
            },
            {"at_seconds": 30, "action": "fleet_command", "params": {"cmd": 11, "label": "Restore utility power"}},
            {"at_seconds": 35, "action": "fleet_command", "params": {"cmd": 2, "label": "Stop generators after utility restore"}},
            {"at_seconds": 45, "action": "note", "params": {"message": "Drill complete — generators should be returning to standby."}},
        ],
    },
    {
        "id": "fault-and-reset",
        "name": "Fault Injection and Reset",
        "description": (
            "Injects a high coolant temperature fault on Unit 1, demonstrates the alarm response, "
            "then clears the fault and restores normal operation."
        ),
        "size_counts": None,
        "steps": [
            {"at_seconds": 0, "action": "unit_command", "params": {"unit_id": 1, "cmd": 1, "label": "Ensure Unit 1 is running"}},
            {"at_seconds": 6, "action": "fault_injection", "params": {"unit_id": 1, "cmd": 20, "label": "Inject high coolant temperature fault on Unit 1"}},
            {
                "at_seconds": 10,
                "action": "assert_fleet_metric",
                "params": {
                    "metric": "faulted_units",
                    "comparison": "gte",
                    "value": 1,
                    "label": "At least one unit should be faulted",
                },
            },
            {"at_seconds": 18, "action": "unit_command", "params": {"unit_id": 1, "cmd": 8, "label": "Reset alarms on Unit 1"}},
            {"at_seconds": 22, "action": "unit_command", "params": {"unit_id": 1, "cmd": 1, "label": "Restart Unit 1"}},
            {"at_seconds": 32, "action": "note", "params": {"message": "Unit 1 should be running cleanly."}},
        ],
    },
    {
        "id": "parallel-mode-demo",
        "name": "Parallel Mode Demo",
        "description": (
            "Switches the fleet into parallel (load-sharing) mode, demonstrates coordinated kW "
            "output across multiple generators, then returns to island mode."
        ),
        "size_counts": None,
        "steps": [
            {"at_seconds": 0, "action": "fleet_command", "params": {"cmd": 1, "label": "Start all generators"}},
            {"at_seconds": 8, "action": "fleet_mode", "params": {"mode": "parallel", "label": "Switch to parallel mode"}},
            {
                "at_seconds": 15,
                "action": "assert_fleet_metric",
                "params": {
                    "metric": "fleet_mode",
                    "comparison": "eq",
                    "value": 1.0,
                    "label": "Fleet should be in parallel mode",
                },
            },
            {"at_seconds": 20, "action": "note", "params": {"message": "Fleet should be sharing load in parallel mode."}},
            {"at_seconds": 30, "action": "fleet_mode", "params": {"mode": "island", "label": "Switch back to island mode"}},
            {"at_seconds": 38, "action": "note", "params": {"message": "Fleet returned to island mode."}},
        ],
    },
    {
        "id": "e-stop-drill",
        "name": "Emergency Stop Drill",
        "description": (
            "Issues a fleet-wide emergency stop, verifies all units trip immediately, "
            "then resets alarms and returns to normal standby."
        ),
        "size_counts": None,
        "steps": [
            {"at_seconds": 0, "action": "fleet_command", "params": {"cmd": 1, "label": "Start all generators"}},
            {"at_seconds": 8, "action": "fleet_command", "params": {"cmd": 7, "label": "Issue fleet-wide E-Stop"}},
            {"at_seconds": 16, "action": "note", "params": {"message": "All units should show E-Stop fault."}},
            {
                "at_seconds": 17,
                "action": "assert_fleet_metric",
                "params": {
                    "metric": "faulted_units",
                    "comparison": "gte",
                    "value": 1,
                    "label": "Units should be in fault state after E-Stop",
                },
            },
            {"at_seconds": 20, "action": "fleet_command", "params": {"cmd": 8, "label": "Reset all alarms"}},
            {"at_seconds": 28, "action": "note", "params": {"message": "Fleet cleared and ready."}},
        ],
    },
]


def _coerce_int(raw_value: Any, field_name: str) -> int:
    if isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    if isinstance(raw_value, float) and not raw_value.is_integer():
        raise ValueError(f"{field_name} must be an integer.")
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def _coerce_command(raw_value: Any, field_name: str, *, fault_only: bool = False) -> int:
    command = _coerce_int(raw_value, field_name)
    allowed_values = RUNBOOK_FAULT_COMMAND_VALUES if fault_only else RUNBOOK_COMMAND_VALUES
    if command not in allowed_values:
        raise ValueError(f"{field_name} must be a supported command value.")
    return command


def _coerce_float(raw_value: Any, field_name: str) -> float:
    if isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be a number.")
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite number.")
    return value


def _coerce_text(raw_value: Any, field_name: str, max_length: int, *, required: bool = False) -> str:
    text = str(raw_value if raw_value is not None else "").strip()
    if required and not text:
        raise ValueError(f"{field_name} is required.")
    if len(text) > max_length:
        raise ValueError(f"{field_name} cannot exceed {max_length} characters.")
    return text


def _coerce_runbook_id(raw_value: Any) -> str:
    runbook_id = _coerce_text(raw_value, "Runbook id", MAX_RUNBOOK_ID_LENGTH, required=True)
    if not RUNBOOK_ID_PATTERN.fullmatch(runbook_id):
        raise ValueError(
            "Runbook id may contain only letters, numbers, dots, underscores, or hyphens, "
            "and must start with a letter or number."
        )
    return runbook_id


def _optional_float(raw_dict: dict[str, Any], field_name: str) -> float | None:
    if field_name not in raw_dict:
        return None
    raw_value = raw_dict[field_name]
    if raw_value is None or raw_value == "":
        return None
    return _coerce_float(raw_value, field_name)


def summarize_runbook_step(step: dict[str, Any]) -> str:
    action = step["action"]
    params = step.get("params", {})
    if action == "note":
        return params.get("message", "")
    if action == "fleet_command":
        label = params.get("label", "")
        return label if label else f"Send fleet command {params.get('cmd')}."
    if action == "unit_command":
        label = params.get("label", "")
        if label:
            return label
        return f"Send command {params.get('cmd')} to Unit {params.get('unit_id')}."
    if action == "fleet_mode":
        label = params.get("label", "")
        if label:
            return label
        return f"Switch fleet to {params.get('mode')} mode."
    if action == "fault_injection":
        label = params.get("label", "")
        if label:
            return label
        return f"Inject fault {params.get('cmd')} on Unit {params.get('unit_id')}."
    if action == "load_setpoint":
        return f"Set Unit {params.get('unit_id')} load setpoint to {params.get('setpoint_kw'):.1f} kW."
    if action == "assert_fleet_metric":
        tolerance_text = ""
        if params.get("tolerance") is not None:
            tolerance_text = f" +/- {params['tolerance']:.3f}"
        label = params.get("label") or "Assertion"
        return f"{label}: expect {params.get('metric')} {params.get('comparison')} {params.get('value'):.3f}{tolerance_text}."
    return action


def sanitize_runbook_step(raw_step: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw_step, dict):
        raise ValueError(f"Runbook step {index} must be an object.")

    at_seconds = _coerce_float(raw_step.get("at_seconds"), f"Runbook step {index} at_seconds")
    if at_seconds < 0.0:
        raise ValueError(f"Runbook step {index} at_seconds cannot be negative.")

    action = str(raw_step.get("action", "")).strip().lower()
    if action not in RUNBOOK_ACTION_OPTIONS:
        allowed = ", ".join(RUNBOOK_ACTION_OPTIONS)
        raise ValueError(f"Runbook step {index} action must be one of: {allowed}.")

    # Accept params as a sub-dict or as top-level fields for ergonomic authoring.
    raw_params: dict[str, Any] = {}
    if isinstance(raw_step.get("params"), dict):
        raw_params = dict(raw_step["params"])
    else:
        # Support flat top-level fields for compatibility
        for key in ("message", "cmd", "unit_id", "mode", "setpoint_kw", "metric", "comparison", "value", "tolerance", "label"):
            if key in raw_step:
                raw_params[key] = raw_step[key]

    params: dict[str, Any] = {}

    if action == "note":
        message = _coerce_text(
            raw_params.get("message"),
            f"Runbook step {index} message",
            MAX_RUNBOOK_TEXT_LENGTH,
            required=True,
        )
        params["message"] = message

    elif action == "fleet_command":
        params["cmd"] = _coerce_command(raw_params.get("cmd"), f"Runbook step {index} cmd")
        if raw_params.get("label") is not None:
            label = _coerce_text(raw_params["label"], f"Runbook step {index} label", MAX_RUNBOOK_TEXT_LENGTH)
            if label:
                params["label"] = label

    elif action == "unit_command":
        params["unit_id"] = _coerce_int(raw_params.get("unit_id"), f"Runbook step {index} unit_id")
        params["cmd"] = _coerce_command(raw_params.get("cmd"), f"Runbook step {index} cmd")
        if raw_params.get("label") is not None:
            label = _coerce_text(raw_params["label"], f"Runbook step {index} label", MAX_RUNBOOK_TEXT_LENGTH)
            if label:
                params["label"] = label

    elif action == "fleet_mode":
        mode = str(raw_params.get("mode", "")).strip().lower()
        if mode not in FLEET_MODE_VALUES:
            allowed = ", ".join(FLEET_MODE_VALUES)
            raise ValueError(f"Runbook step {index} mode must be one of: {allowed}.")
        params["mode"] = mode
        if raw_params.get("label") is not None:
            label = _coerce_text(raw_params["label"], f"Runbook step {index} label", MAX_RUNBOOK_TEXT_LENGTH)
            if label:
                params["label"] = label

    elif action == "fault_injection":
        params["unit_id"] = _coerce_int(raw_params.get("unit_id"), f"Runbook step {index} unit_id")
        params["cmd"] = _coerce_command(raw_params.get("cmd"), f"Runbook step {index} cmd", fault_only=True)
        if raw_params.get("label") is not None:
            label = _coerce_text(raw_params["label"], f"Runbook step {index} label", MAX_RUNBOOK_TEXT_LENGTH)
            if label:
                params["label"] = label

    elif action == "load_setpoint":
        params["unit_id"] = _coerce_int(raw_params.get("unit_id"), f"Runbook step {index} unit_id")
        params["setpoint_kw"] = _coerce_float(raw_params.get("setpoint_kw"), f"Runbook step {index} setpoint_kw")
        if raw_params.get("label") is not None:
            label = _coerce_text(raw_params["label"], f"Runbook step {index} label", MAX_RUNBOOK_TEXT_LENGTH)
            if label:
                params["label"] = label

    elif action == "assert_fleet_metric":
        metric = str(raw_params.get("metric", "")).strip().lower()
        if metric not in RUNBOOK_ASSERT_METRICS:
            allowed = ", ".join(RUNBOOK_ASSERT_METRICS)
            raise ValueError(f"Runbook step {index} metric must be one of: {allowed}.")
        comparison = str(raw_params.get("comparison", "")).strip().lower()
        if comparison not in RUNBOOK_COMPARISONS:
            allowed = ", ".join(RUNBOOK_COMPARISONS)
            raise ValueError(f"Runbook step {index} comparison must be one of: {allowed}.")
        params["metric"] = metric
        params["comparison"] = comparison
        params["value"] = _coerce_float(raw_params.get("value"), f"Runbook step {index} value")
        tolerance = _optional_float(raw_params, "tolerance")
        if tolerance is not None and tolerance < 0.0:
            raise ValueError(f"Runbook step {index} tolerance cannot be negative.")
        if tolerance is not None:
            params["tolerance"] = tolerance
        if raw_params.get("label") is not None:
            label = _coerce_text(raw_params["label"], f"Runbook step {index} label", MAX_RUNBOOK_TEXT_LENGTH)
            if label:
                params["label"] = label

    step: dict[str, Any] = {
        "at_seconds": at_seconds,
        "action": action,
        "params": params,
    }
    step["summary"] = summarize_runbook_step(step)
    return step


def sanitize_saved_runbook(raw_runbook: Any) -> dict[str, Any]:
    if not isinstance(raw_runbook, dict):
        raise ValueError("Runbook must be an object.")
    name = _coerce_text(raw_runbook.get("name"), "Runbook name", MAX_RUNBOOK_NAME_LENGTH, required=True)
    runbook_id = _coerce_runbook_id(raw_runbook.get("id"))
    description = _coerce_text(raw_runbook.get("description"), "Runbook description", MAX_RUNBOOK_DESCRIPTION_LENGTH)

    raw_size_counts = raw_runbook.get("size_counts")
    size_counts: dict[str, int] | None = None
    if raw_size_counts is not None and isinstance(raw_size_counts, dict):
        if len(raw_size_counts) > MAX_RUNBOOK_SIZE_COUNT_FIELDS:
            raise ValueError(f"Runbook size_counts cannot exceed {MAX_RUNBOOK_SIZE_COUNT_FIELDS} fields.")
        size_counts = {str(k): _coerce_int(v, f"Runbook size_counts {k}") for k, v in raw_size_counts.items()}

    raw_steps = raw_runbook.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Runbook steps must be a non-empty list.")
    if len(raw_steps) > MAX_RUNBOOK_STEPS:
        raise ValueError(f"Runbook steps cannot exceed {MAX_RUNBOOK_STEPS}.")
    steps = [sanitize_runbook_step(step, index) for index, step in enumerate(raw_steps, start=1)]
    steps.sort(key=lambda item: item["at_seconds"])

    return {
        "id": runbook_id,
        "name": name,
        "description": description,
        "size_counts": size_counts,
        "steps": steps,
    }


def default_runbook_presets() -> list[dict[str, Any]]:
    return [deepcopy(sanitize_saved_runbook(preset)) for preset in RUNBOOK_PRESETS]


def load_runbooks(path: Path) -> list[dict[str, Any]]:
    """Load runbooks from a JSON file. Returns default presets if file is absent or invalid."""
    if not path.exists():
        return default_runbook_presets()
    try:
        if path.stat().st_size > MAX_RUNBOOK_FILE_BYTES:
            return default_runbook_presets()
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            return default_runbook_presets()
        validated: list[dict[str, Any]] = []
        for raw in data:
            try:
                validated.append(sanitize_saved_runbook(raw))
            except (ValueError, TypeError):
                continue
            if len(validated) >= MAX_SAVED_RUNBOOKS:
                break
        return validated if validated else default_runbook_presets()
    except (json.JSONDecodeError, OSError):
        return default_runbook_presets()


def save_runbooks(runbooks: list[dict[str, Any]], path: Path) -> None:
    """Atomically write runbooks to a JSON file."""
    if len(runbooks) > MAX_SAVED_RUNBOOKS:
        raise ValueError(f"Saved runbooks cannot exceed {MAX_SAVED_RUNBOOKS}.")
    json_text = json.dumps(runbooks, indent=2, allow_nan=False)
    if len(json_text.encode("utf-8")) > MAX_RUNBOOK_FILE_BYTES:
        raise ValueError(f"Saved runbooks cannot exceed {MAX_RUNBOOK_FILE_BYTES} bytes.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(json_text)
        if path.exists():
            backup = path.with_name(f"{path.name}.bak")
            try:
                shutil.copy2(path, backup)
            except OSError:
                pass
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class RunbookRegistry:
    """In-memory library of saved runbooks backed by a JSON persistence file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._runbooks: list[dict[str, Any]] = load_runbooks(path)

    def list_runbooks(self) -> list[dict[str, Any]]:
        return [deepcopy(rb) for rb in self._runbooks]

    def get_runbook(self, runbook_id: str) -> dict[str, Any] | None:
        for rb in self._runbooks:
            if rb["id"] == runbook_id:
                return deepcopy(rb)
        return None

    def save_runbook(self, runbook: dict[str, Any]) -> dict[str, Any]:
        """Upsert a validated runbook into the registry and persist."""
        for i, existing in enumerate(self._runbooks):
            if existing["id"] == runbook["id"]:
                self._runbooks[i] = deepcopy(runbook)
                save_runbooks(self._runbooks, self._path)
                return deepcopy(runbook)
        if len(self._runbooks) >= MAX_SAVED_RUNBOOKS:
            raise ValueError(f"Saved runbooks cannot exceed {MAX_SAVED_RUNBOOKS}.")
        self._runbooks.append(deepcopy(runbook))
        save_runbooks(self._runbooks, self._path)
        return deepcopy(runbook)

    def delete_runbook(self, runbook_id: str) -> bool:
        before = len(self._runbooks)
        self._runbooks = [rb for rb in self._runbooks if rb["id"] != runbook_id]
        if len(self._runbooks) < before:
            save_runbooks(self._runbooks, self._path)
            return True
        return False
