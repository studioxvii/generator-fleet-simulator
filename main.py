"""
Generator Fleet Simulator entry point.

Scales the simulator to large fleets by separating simulation, command routing,
state storage, Modbus register caching, and UI event publishing.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import sysconfig
import socket
import tempfile
import time
import uuid
from collections import Counter, deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, g, render_template, request
from flask_socketio import SocketIO
from werkzeug.exceptions import RequestEntityTooLarge, SecurityError
from werkzeug.wrappers import Request as WSGIRequest

from generator import Generator
from modbus_server import (ModbusServerGroup, UNITS_PER_PORT, create_modbus_context,
                           modbus_address, modbus_endpoints, read_command_register,
                           read_register, sync_generator_to_modbus)
from runbooks import FLEET_MODE_COMMANDS, FLEET_MODE_NUMERIC, MAX_RUNBOOK_REQUEST_BYTES, RunbookRegistry, sanitize_saved_runbook
from scenarios import ScenarioRunner, scenario_catalog_payload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


GENERATOR_SIZE_OPTIONS = [500, 1000, 1500, 2000, 2500]
DEFAULT_MAX_GENERATORS = 2000
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 250
DEFAULT_HTTP_RATE_LIMIT = 120
DEFAULT_HTTP_RATE_WINDOW = 60
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'"
)
MUTATING_HTTP_METHODS = frozenset({"POST", "PATCH", "DELETE", "PUT"})
SCADA_DEFAULT_RANGE_SIZE = 50
SCADA_MAX_RANGE_SIZE = 250
SUBFLEET_NAME_LIMIT = 80
METRICS_WINDOW = 120
DEFAULT_COMMAND_QUEUE_MAX = 256
DEFAULT_COMMAND_QUEUE_UNIT_LIMIT = DEFAULT_MAX_GENERATORS * 2
MAX_PERSISTENT_STATE_FILE_BYTES = 4 * 1024 * 1024
CONFIG_EXPORT_FILENAME = "generator-fleet-simulator-config.csv"
MODBUS_FUNCTION_CODES = "FC 3 Read Holding Registers; FC 6 Write Single Register; FC 16 Write Multiple Registers"
MODBUS_POLLING_GUIDANCE = "Poll each target unit every 1 second to match simulator updates"
MODBUS_REGISTER_MAP = [
    (0, "Output kW", "UINT16", "divide by 10", "0.0 to rated kW", "Read generated load output"),
    (1, "Utility Load kW", "UINT16", "divide by 10", "0.0 to site load kW", "Read utility-served load"),
    (2, "Utility Breaker", "UINT16", "raw", "0=open, 1=closed", "Read utility breaker position"),
    (3, "Gen Breaker", "UINT16", "raw", "0=open, 1=closed", "Read generator breaker position"),
    (4, "Fuel Level", "UINT16", "divide by 10", "0.0 to 100.0 percent", "Read remaining fuel percentage"),
    (5, "Run Hours Whole", "UINT16", "raw", "0 to 65535 hours", "Whole hours portion of runtime"),
    (6, "Run Hours Fraction", "UINT16", "divide by 10000", "0.0000 to 0.9999", "Fractional hours portion of runtime"),
    (7, "Coolant Temp", "UINT16", "raw", "0 to 300 F", "Read coolant temperature"),
    (8, "Oil Pressure", "UINT16", "raw", "0 to 100 PSI", "Read oil pressure"),
    (9, "Battery Voltage", "UINT16", "divide by 10", "0.0 to 40.0 V", "Read battery voltage"),
    (10, "Engine RPM", "UINT16", "raw", "0 to 2000 RPM", "Read engine speed"),
    (11, "Output Voltage", "UINT16", "raw", "0 to 600 V", "Read generator output voltage"),
    (12, "Output Frequency", "UINT16", "divide by 10", "0.0 to 70.0 Hz", "Read generator output frequency"),
    (13, "Alarm Word 1", "UINT16", "bitfield", "See alarm rows", "Decode bits 0-15 for status and alarms"),
    (14, "Alarm Word 2", "UINT16", "bitfield", "See alarm rows", "Decode bits 0-1 for overload and ground fault"),
    (15, "Transfer Mode", "UINT16", "raw", "0=transfer, 1=parallel, 2=island", "Read operating mode"),
    (16, "Parallel Setpoint kW", "UINT16", "divide by 10", "0.0 to rated kW", "Read/write parallel mode kW setpoint"),
    (17, "Alarm Word 3", "UINT16", "bitfield", "See alarm rows", "Decode bits 0-11 for extended alarms"),
    (18, "Reserved", "", "", "", "Reserved"),
    (19, "Reserved", "", "", "", "Reserved"),
    (20, "Command Register", "UINT16", "raw", "0 to 39", "Write a command value, then simulator clears back to 0"),
]
MODBUS_ALARM_BITS = [
    (13, 0, "Engine Running", "Status", "Engine is running"),
    (13, 1, "Ready", "Status", "Stopped with no active fault"),
    (13, 2, "Auto Mode", "Status", "Auto mode enabled"),
    (13, 3, "E-Stop", "Critical", "Emergency stop command issued"),
    (13, 4, "High Coolant Temp", "Critical", "Coolant above 220 F"),
    (13, 5, "Low Oil Pressure", "Critical", "Oil pressure below 25 PSI"),
    (13, 6, "Overspeed", "Critical", "RPM above 1950"),
    (13, 7, "Overcrank", "Fault", "Failed to start"),
    (13, 8, "Low Coolant Level", "Warning", "Simulated low coolant level"),
    (13, 9, "High Battery V", "Warning", "Battery above 31 V"),
    (13, 10, "Low Battery V", "Warning", "Battery below 24 V"),
    (13, 11, "Low Fuel", "Warning", "Fuel below 15 percent"),
    (13, 12, "Over Voltage", "Warning", "Voltage above 510 V"),
    (13, 13, "Under Voltage", "Warning", "Voltage below 450 V"),
    (13, 14, "Over Frequency", "Warning", "Frequency above 63 Hz"),
    (13, 15, "Under Frequency", "Warning", "Frequency below 57 Hz"),
    (14, 0, "Overload", "Warning", "Output exceeds 105 percent of rated kW"),
    (14, 1, "Ground Fault", "Fault", "Simulated ground fault"),
    (17, 0, "Reverse Power", "Critical", "Output kW negative in parallel mode"),
    (17, 1, "Sync Check Fail", "Fault", "Injectable only"),
    (17, 2, "Load Imbalance", "Warning", "Output deviates >20 percent from setpoint for 5s"),
    (17, 3, "Fuel Leak Detected", "Warning", "Fuel drops faster than 2x normal rate"),
    (17, 4, "Air Filter Restricted", "Warning", "Injectable only"),
    (17, 5, "Exhaust High Temp", "Warning", "Injectable only"),
    (17, 6, "Gen Bearing Temp", "Critical", "Injectable only"),
    (17, 7, "Overcurrent", "Critical", "Injectable only"),
    (17, 8, "Loss of Field", "Critical", "Injectable only"),
    (17, 9, "Utility Phase Loss", "Warning", "Injectable only"),
    (17, 10, "Parallel Sync Loss", "Fault", "Injectable only"),
    (17, 11, "Setpoint Not Reached", "Warning", "Output below 90 percent of setpoint for 10s"),
]
MODBUS_COMMANDS = [
    (0, "None", "No command or cleared state"),
    (1, "Start", "Begin engine start sequence"),
    (2, "Stop", "Initiate cooldown and stop"),
    (3, "Close Gen Breaker", "Close generator output breaker"),
    (4, "Open Gen Breaker", "Open generator output breaker"),
    (5, "Close Util Breaker", "Close utility breaker"),
    (6, "Open Util Breaker", "Open utility breaker"),
    (7, "E-Stop", "Immediate emergency stop fault"),
    (8, "Reset Alarms", "Clear latched faults and return to stopped"),
    (9, "Toggle Auto", "Toggle auto mode"),
    (10, "Utility Fail", "Simulate utility power failure"),
    (11, "Restore Utility", "Restore utility power"),
    (12, "Set Parallel Mode", "Switch to parallel (load sharing) mode"),
    (13, "Set Island Mode", "Switch to island (gen only) mode"),
    (14, "Set Transfer Mode", "Switch to transfer (mutual exclusion) mode"),
    (20, "High Coolant Temp", "Inject high coolant temperature fault"),
    (21, "Low Oil Pressure", "Inject low oil pressure fault"),
    (22, "Overspeed", "Inject overspeed fault"),
    (23, "Low Coolant Level", "Inject low coolant level fault"),
    (24, "Ground Fault", "Inject ground fault"),
    (25, "Over Voltage", "Inject over voltage fault"),
    (26, "Under Voltage", "Inject under voltage fault"),
    (27, "High Battery V", "Inject high battery voltage fault"),
    (28, "Reverse Power", "Inject reverse power fault"),
    (29, "Sync Check Fail", "Inject sync check fail fault"),
    (30, "Load Imbalance", "Inject load imbalance fault"),
    (31, "Fuel Leak", "Inject fuel leak fault"),
    (32, "Air Filter Restricted", "Inject air filter restriction fault"),
    (33, "Exhaust High Temp", "Inject exhaust high temp fault"),
    (34, "Gen Bearing Temp", "Inject generator bearing temperature fault"),
    (35, "Overcurrent", "Inject overcurrent fault"),
    (36, "Loss of Field", "Inject loss of field fault"),
    (37, "Utility Phase Loss", "Inject utility phase loss fault"),
    (38, "Parallel Sync Loss", "Inject parallel sync loss fault"),
    (39, "Setpoint Not Reached", "Inject setpoint not reached fault"),
]
ISSUABLE_COMMAND_VALUES = frozenset(command for command, _, _ in MODBUS_COMMANDS if command != 0)
RESTORE_UTILITY_COMMAND_SEQUENCE = (11, 14, 4)
STATUS_ALARM_IDS = {0, 1, 2}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a whole number.") from exc


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a whole number.") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number.")
    return parsed


def _clamp_num_generators(value: int, maximum: int) -> int:
    return max(1, min(maximum, value))


def _default_generator_size_counts(num_generators: int) -> dict[int, int]:
    return {
        size_kw: (num_generators if size_kw == GENERATOR_SIZE_OPTIONS[0] else 0)
        for size_kw in GENERATOR_SIZE_OPTIONS
    }


def _normalize_generator_size_counts(payload: Any, fallback_total: int, maximum: int) -> dict[int, int]:
    if payload is None:
        return _default_generator_size_counts(_clamp_num_generators(fallback_total, maximum))
    if not isinstance(payload, dict):
        raise ValueError("Generator size counts must be an object keyed by kW size.")

    normalized: dict[int, int] = {}
    total = 0
    for size_kw in GENERATOR_SIZE_OPTIONS:
        raw_value = payload.get(str(size_kw), payload.get(size_kw, 0))
        count = _normalize_whole_count(raw_value, f"{size_kw} kW count")
        if count < 0:
            raise ValueError(f"{size_kw} kW count cannot be negative.")
        normalized[size_kw] = count
        total += count

    extra_keys = {str(key) for key in payload.keys()} - {str(size_kw) for size_kw in GENERATOR_SIZE_OPTIONS}
    if extra_keys:
        raise ValueError("Unsupported generator sizes were provided.")
    if total < 1:
        raise ValueError("Select at least one generator.")
    if total > maximum:
        raise ValueError(f"Total generators cannot exceed {maximum}.")
    return normalized


def _normalize_whole_count(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        raise ValueError(f"{label} must be a whole number.")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("+-").isdigit():
            return int(stripped)
    raise ValueError(f"{label} must be a whole number.")


def _expand_generator_ratings(size_counts: dict[int, int]) -> list[int]:
    ratings: list[int] = []
    for size_kw in GENERATOR_SIZE_OPTIONS:
        ratings.extend([size_kw] * size_counts.get(size_kw, 0))
    return ratings


def _current_size_counts(generator_ratings: list[int]) -> dict[int, int]:
    counts = {size_kw: 0 for size_kw in GENERATOR_SIZE_OPTIONS}
    for rating in generator_ratings:
        if rating in counts:
            counts[rating] += 1
    return counts


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(0, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


def _client_rate_key() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def _safe_attachment_name(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")
    return cleaned[:96] or fallback


def _replace_file_atomically(tmp_path: str, dest: Path) -> None:
    if dest.exists():
        backup = dest.with_name(f"{dest.name}.bak")
        try:
            shutil.copy2(dest, backup)
        except OSError as exc:
            log.warning("Could not write backup %s: %s", backup, exc)
    os.replace(tmp_path, dest)


def _parse_cors_origins(value: str | None) -> Any:
    if not value or value.strip() == "":
        return None
    if value.strip() == "*":
        return "*"
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _bool_arg(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _int_arg(value: str | None, field_name: str = "value") -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc


def _normalize_command(value: Any, field_name: str = "cmd") -> int:
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError(f"A valid {field_name} is required.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field_name} must be a whole number.")
    try:
        command = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc
    if command not in ISSUABLE_COMMAND_VALUES:
        raise ValueError(f"Unsupported command: {command}.")
    return command


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def _float_arg(value: Any, field_name: str = "value") -> float:
    if _is_blank(value) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be a finite number.")
    return parsed


def _validate_tcp_port(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    if isinstance(value, int):
        port = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{label} must be a whole number.")
        port = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped or not stripped.lstrip("+-").isdigit():
            raise ValueError(f"{label} must be a whole number.")
        port = int(stripped)
    else:
        raise ValueError(f"{label} must be a whole number.")
    if port < 1 or port > 65535:
        raise ValueError(f"{label} must be between 1 and 65535.")
    return port


def _normalize_tcp_port(value: Any, fallback: int) -> int:
    if _is_blank(value):
        return fallback
    return _validate_tcp_port(value, "Modbus port")


def _validate_save_interval(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Save interval must be a whole number.")
    if isinstance(value, int):
        interval = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("Save interval must be a whole number.")
        interval = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped or not stripped.lstrip("+-").isdigit():
            raise ValueError("Save interval must be a whole number.")
        interval = int(stripped)
    else:
        raise ValueError("Save interval must be a whole number.")
    if interval < 1:
        raise ValueError("Save interval must be at least 1 second.")
    return interval


def _validate_tick_seconds(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Tick interval must be a number.")
    try:
        tick_seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Tick interval must be a number.") from exc
    if not math.isfinite(tick_seconds):
        raise ValueError("Tick interval must be finite.")
    if tick_seconds < 0:
        raise ValueError("Tick interval must be 0 or greater.")
    return tick_seconds


def _neutralize_csv_formula(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip(" \t\r\n")
    if value[0] in {"=", "+", "-", "@", "\t", "\r", "\n"} or (stripped and stripped[0] in {"=", "+", "-", "@"}):
        return "'" + value
    return value


def _csv_safe_row(values: list[Any]) -> list[Any]:
    return [_neutralize_csv_formula(value) for value in values]


def _subfleet_scope_arg(value: str | None) -> str:
    normalized = (value or "any").strip().lower()
    allowed = {"any", "assigned", "unassigned", "active", "other"}
    if normalized not in allowed:
        raise ValueError(f"Invalid sub-fleet scope: {value}")
    return normalized


def _serialize_subfleet(subfleet: "Subfleet") -> dict[str, Any]:
    return {
        "id": subfleet.subfleet_id,
        "name": subfleet.name,
        "member_ids": sorted(subfleet.member_ids),
    }


def _has_actionable_alarm(alarms: dict[int, str]) -> bool:
    return any(alarm_id not in STATUS_ALARM_IDS for alarm_id in alarms)


def _matches_generator_search(item: dict[str, Any], search: str) -> bool:
    term = search.strip().lower()
    if not term:
        return True
    unit_id = int(item["unit_id"])
    if term in item["name"].lower() or term in str(unit_id):
        return True
    if term.isdigit() and int(term) == unit_id:
        return True
    gen_match = re.fullmatch(r"gen[-_\s]*0*(\d+)", term)
    return bool(gen_match and int(gen_match.group(1)) == unit_id)


def _paginate(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    total_items = len(items)
    total_pages = max(1, math.ceil(total_items / page_size)) if total_items else 1
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * page_size
    end = start + page_size
    return {
        "items": items[start:end],
        "page": safe_page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
    }


def _scada_aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
    state_counts = Counter(item["state"] for item in items)
    running_items = [item for item in items if item["state"] == "RUNNING"]
    online_items = [item for item in running_items if item["gen_breaker"]]
    return {
        "unit_count": len(items),
        "running_count": state_counts.get("RUNNING", 0),
        "fault_count": state_counts.get("FAULT", 0),
        "alarm_count": sum(1 for item in items if item["has_actionable_alarm"]),
        "low_fuel_count": sum(1 for item in items if item["fuel_level"] < 15),
        "total_output_kw": round(sum(item["output_kw"] for item in items), 1),
        "total_utility_kw": round(sum(item["utility_load_kw"] for item in items), 1),
        "running_capacity_kw": round(sum(item["rated_kw"] for item in running_items), 1),
        "online_capacity_kw": round(sum(item["rated_kw"] for item in online_items), 1),
        "gen_breaker_closed_count": sum(1 for item in items if item["gen_breaker"]),
        "utility_breaker_closed_count": sum(1 for item in items if item["utility_breaker"]),
        "state_counts": dict(state_counts),
    }


def _scada_node(
    *,
    node_id: str,
    node_type: str,
    label: str,
    items: list[dict[str, Any]],
    parent_id: str | None,
    depth: int,
    subtitle: str = "",
    child_count: int = 0,
    command_scope: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    node = {
        "id": node_id,
        "type": node_type,
        "label": label,
        "subtitle": subtitle,
        "parent_id": parent_id,
        "depth": depth,
        "child_count": child_count,
        "command_scope": command_scope,
        **_scada_aggregate(items),
    }
    node.update(extra)
    return node


def _scada_unit_node(item: dict[str, Any], parent_id: str | None, depth: int) -> dict[str, Any]:
    return _scada_node(
        node_id=f"unit|{item['unit_id']}",
        node_type="unit",
        label=item["name"],
        subtitle=f"{int(item['rated_kw']):,} kW / {item['state']}",
        items=[item],
        parent_id=parent_id,
        depth=depth,
        command_scope="unit",
        unit_id=item["unit_id"],
        rated_kw=item["rated_kw"],
        state=item["state"],
        auto_mode=item["auto_mode"],
        output_kw=item["output_kw"],
        utility_load_kw=item["utility_load_kw"],
        fuel_level=item["fuel_level"],
        utility_breaker=item["utility_breaker"],
        gen_breaker=item["gen_breaker"],
        subfleet_id=item["subfleet_id"],
    )


def _scada_group_label(scope_kind: str, scope_id: str, subfleet_by_id: dict[str, dict[str, Any]]) -> str | None:
    if scope_kind == "unassigned":
        return "Unassigned Generators"
    if scope_kind == "fleet":
        return "Generator Fleet"
    if scope_kind == "subfleet":
        subfleet = subfleet_by_id.get(scope_id)
        return subfleet["name"] if subfleet else None
    return None


def _scada_scope_items(state_store: "StateStore", scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    if scope_kind == "fleet":
        return state_store.find_generators()
    if scope_kind == "unassigned":
        return state_store.find_generators(subfleet_scope="unassigned")
    if scope_kind == "subfleet":
        return state_store.find_generators(subfleet_id=scope_id)
    return []


def _scada_items_for_node(state_store: "StateStore", node_id: str) -> list[dict[str, Any]] | None:
    subfleets = state_store.list_subfleets()
    subfleet_by_id = {subfleet["id"]: subfleet for subfleet in subfleets}

    if node_id in {"", "fleet"}:
        return state_store.find_generators()

    parts = node_id.split("|")
    if len(parts) == 3 and parts[0] == "group":
        _, scope_kind, scope_id = parts
        if _scada_group_label(scope_kind, scope_id, subfleet_by_id) is None:
            return None
        return _scada_scope_items(state_store, scope_kind, scope_id)

    if len(parts) == 4 and parts[0] == "size":
        _, scope_kind, scope_id, raw_rated_kw = parts
        if _scada_group_label(scope_kind, scope_id, subfleet_by_id) is None:
            return None
        try:
            rated_kw = int(raw_rated_kw)
        except ValueError:
            return None
        return [
            item
            for item in _scada_scope_items(state_store, scope_kind, scope_id)
            if int(item["rated_kw"]) == rated_kw
        ]

    if len(parts) == 6 and parts[0] == "range":
        _, scope_kind, scope_id, raw_rated_kw, raw_start, raw_end = parts
        if _scada_group_label(scope_kind, scope_id, subfleet_by_id) is None:
            return None
        try:
            rated_kw = int(raw_rated_kw)
            start_unit = int(raw_start)
            end_unit = int(raw_end)
        except ValueError:
            return None
        return [
            item
            for item in _scada_scope_items(state_store, scope_kind, scope_id)
            if int(item["rated_kw"]) == rated_kw and start_unit <= int(item["unit_id"]) <= end_unit
        ]

    if len(parts) == 2 and parts[0] == "unit":
        try:
            unit_id = int(parts[1])
        except ValueError:
            return None
        matches = state_store.find_generators(search=str(unit_id))
        item = next((record for record in matches if int(record["unit_id"]) == unit_id), None)
        return [item] if item is not None else None

    return None


def _scada_alarm_records(state_store: "StateStore", node_id: str) -> list[dict[str, Any]] | None:
    items = _scada_items_for_node(state_store, node_id)
    if items is None:
        return None

    subfleet_by_id = {subfleet["id"]: subfleet for subfleet in state_store.list_subfleets()}
    alarms: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda record: int(record["unit_id"])):
        unit_id = int(item["unit_id"])
        detail = state_store.get_generator_detail(unit_id)
        if detail is None:
            continue
        for raw_alarm_id, alarm_name in sorted(detail.get("alarms", {}).items(), key=lambda pair: int(pair[0])):
            alarm_id = int(raw_alarm_id)
            if alarm_id in STATUS_ALARM_IDS:
                continue
            subfleet = subfleet_by_id.get(item.get("subfleet_id") or "")
            alarms.append(
                {
                    "unit_id": unit_id,
                    "name": item["name"],
                    "rated_kw": item["rated_kw"],
                    "state": item["state"],
                    "subfleet_id": item.get("subfleet_id"),
                    "subfleet_name": subfleet["name"] if subfleet else None,
                    "alarm_id": alarm_id,
                    "alarm_name": alarm_name,
                    "fuel_level": item["fuel_level"],
                    "output_kw": item["output_kw"],
                }
            )
    return alarms


def _scada_size_children(
    *,
    items: list[dict[str, Any]],
    scope_kind: str,
    scope_id: str,
    parent_id: str,
    depth: int,
    range_size: int,
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for rated_kw in sorted({int(item["rated_kw"]) for item in items}):
        size_items = [item for item in items if int(item["rated_kw"]) == rated_kw]
        range_count = math.ceil(len(size_items) / range_size) if len(size_items) > range_size else len(size_items)
        children.append(
            _scada_node(
                node_id=f"size|{scope_kind}|{scope_id}|{rated_kw}",
                node_type="size",
                label=f"{rated_kw:,} kW Generators",
                subtitle=f"{len(size_items):,} units",
                items=size_items,
                parent_id=parent_id,
                depth=depth,
                child_count=range_count,
                scope_kind=scope_kind,
                scope_id=scope_id,
                rated_kw=rated_kw,
            )
        )
    return children


def _scada_range_or_unit_children(
    *,
    items: list[dict[str, Any]],
    scope_kind: str,
    scope_id: str,
    rated_kw: int,
    parent_id: str,
    depth: int,
    range_size: int,
) -> list[dict[str, Any]]:
    sorted_items = sorted(items, key=lambda item: item["unit_id"])
    if len(sorted_items) <= range_size:
        return [_scada_unit_node(item, parent_id, depth) for item in sorted_items]

    children: list[dict[str, Any]] = []
    for start_index in range(0, len(sorted_items), range_size):
        chunk = sorted_items[start_index : start_index + range_size]
        start_unit = int(chunk[0]["unit_id"])
        end_unit = int(chunk[-1]["unit_id"])
        children.append(
            _scada_node(
                node_id=f"range|{scope_kind}|{scope_id}|{rated_kw}|{start_unit}|{end_unit}",
                node_type="range",
                label=f"{chunk[0]['name']} - {chunk[-1]['name']}",
                subtitle=f"{len(chunk):,} units / {rated_kw:,} kW",
                items=chunk,
                parent_id=parent_id,
                depth=depth,
                child_count=len(chunk),
                scope_kind=scope_kind,
                scope_id=scope_id,
                rated_kw=rated_kw,
                range_start=start_unit,
                range_end=end_unit,
            )
        )
    return children


def _build_scada_topology(
    state_store: "StateStore",
    node_id: str = "fleet",
    range_size: int = SCADA_DEFAULT_RANGE_SIZE,
) -> dict[str, Any] | None:
    range_size = min(SCADA_MAX_RANGE_SIZE, max(1, range_size))
    subfleets = state_store.list_subfleets()
    subfleet_by_id = {subfleet["id"]: subfleet for subfleet in subfleets}
    all_items = state_store.find_generators()
    root_node = _scada_node(
        node_id="fleet",
        node_type="fleet",
        label="Generator Fleet",
        subtitle=f"{len(all_items):,} configured units",
        items=all_items,
        parent_id=None,
        depth=0,
        command_scope="fleet",
    )

    def group_node(scope_kind: str, scope_id: str, parent_id: str, depth: int) -> dict[str, Any] | None:
        label = _scada_group_label(scope_kind, scope_id, subfleet_by_id)
        if label is None:
            return None
        items = _scada_scope_items(state_store, scope_kind, scope_id)
        return _scada_node(
            node_id=f"group|{scope_kind}|{scope_id}",
            node_type=scope_kind,
            label=label,
            subtitle=f"{len(items):,} units",
            items=items,
            parent_id=parent_id,
            depth=depth,
            child_count=len({int(item["rated_kw"]) for item in items}),
            command_scope="subfleet" if scope_kind == "subfleet" else None,
            scope_kind=scope_kind,
            scope_id=scope_id,
            subfleet_id=scope_id if scope_kind == "subfleet" else None,
        )

    root_children = [
        child
        for child in (group_node("subfleet", subfleet["id"], "fleet", 1) for subfleet in subfleets)
        if child is not None
    ]
    unassigned = group_node("unassigned", "-", "fleet", 1)
    if unassigned is not None and unassigned["unit_count"] > 0:
        root_children.append(unassigned)
    root_node["child_count"] = len(root_children)

    if node_id in {"", "fleet"}:
        return {"root": root_node, "selected_node": root_node, "breadcrumbs": [root_node], "children": root_children}

    parts = node_id.split("|")
    if len(parts) == 3 and parts[0] == "group":
        _, scope_kind, scope_id = parts
        selected = group_node(scope_kind, scope_id, "fleet", 1)
        if selected is None:
            return None
        items = _scada_scope_items(state_store, scope_kind, scope_id)
        children = _scada_size_children(
            items=items,
            scope_kind=scope_kind,
            scope_id=scope_id,
            parent_id=node_id,
            depth=2,
            range_size=range_size,
        )
        selected["child_count"] = len(children)
        return {"root": root_node, "selected_node": selected, "breadcrumbs": [root_node, selected], "children": children}

    if len(parts) == 4 and parts[0] == "size":
        _, scope_kind, scope_id, raw_rated_kw = parts
        try:
            rated_kw = int(raw_rated_kw)
        except ValueError:
            return None
        label = _scada_group_label(scope_kind, scope_id, subfleet_by_id)
        if label is None:
            return None
        parent = group_node(scope_kind, scope_id, "fleet", 1)
        if parent is None:
            return None
        items = [
            item
            for item in _scada_scope_items(state_store, scope_kind, scope_id)
            if int(item["rated_kw"]) == rated_kw
        ]
        selected = _scada_node(
            node_id=node_id,
            node_type="size",
            label=f"{rated_kw:,} kW Generators",
            subtitle=label,
            items=items,
            parent_id=parent["id"],
            depth=2,
            scope_kind=scope_kind,
            scope_id=scope_id,
            rated_kw=rated_kw,
        )
        children = _scada_range_or_unit_children(
            items=items,
            scope_kind=scope_kind,
            scope_id=scope_id,
            rated_kw=rated_kw,
            parent_id=node_id,
            depth=3,
            range_size=range_size,
        )
        selected["child_count"] = len(children)
        return {"root": root_node, "selected_node": selected, "breadcrumbs": [root_node, parent, selected], "children": children}

    if len(parts) == 6 and parts[0] == "range":
        _, scope_kind, scope_id, raw_rated_kw, raw_start, raw_end = parts
        try:
            rated_kw = int(raw_rated_kw)
            start_unit = int(raw_start)
            end_unit = int(raw_end)
        except ValueError:
            return None
        label = _scada_group_label(scope_kind, scope_id, subfleet_by_id)
        if label is None:
            return None
        parent = group_node(scope_kind, scope_id, "fleet", 1)
        if parent is None:
            return None
        size_parent_id = f"size|{scope_kind}|{scope_id}|{rated_kw}"
        size_parent = _scada_node(
            node_id=size_parent_id,
            node_type="size",
            label=f"{rated_kw:,} kW Generators",
            subtitle=label,
            items=[
                item
                for item in _scada_scope_items(state_store, scope_kind, scope_id)
                if int(item["rated_kw"]) == rated_kw
            ],
            parent_id=parent["id"],
            depth=2,
            scope_kind=scope_kind,
            scope_id=scope_id,
            rated_kw=rated_kw,
        )
        items = [
            item
            for item in _scada_scope_items(state_store, scope_kind, scope_id)
            if int(item["rated_kw"]) == rated_kw and start_unit <= int(item["unit_id"]) <= end_unit
        ]
        selected = _scada_node(
            node_id=node_id,
            node_type="range",
            label=f"GEN-{start_unit:02d} - GEN-{end_unit:02d}",
            subtitle=f"{rated_kw:,} kW range",
            items=items,
            parent_id=size_parent_id,
            depth=3,
            scope_kind=scope_kind,
            scope_id=scope_id,
            rated_kw=rated_kw,
            range_start=start_unit,
            range_end=end_unit,
        )
        children = [_scada_unit_node(item, node_id, 4) for item in sorted(items, key=lambda item: item["unit_id"])]
        selected["child_count"] = len(children)
        return {"root": root_node, "selected_node": selected, "breadcrumbs": [root_node, parent, size_parent, selected], "children": children}

    if len(parts) == 2 and parts[0] == "unit":
        try:
            unit_id = int(parts[1])
        except ValueError:
            return None
        detail = state_store.get_generator_detail(unit_id)
        if detail is None:
            return None
        item = state_store.find_generators(search=str(unit_id))
        compact = next((record for record in item if int(record["unit_id"]) == unit_id), None)
        if compact is None:
            return None
        selected = _scada_unit_node(compact, "fleet", 1)
        return {"root": root_node, "selected_node": selected, "breadcrumbs": [root_node, selected], "children": []}

    return None


@dataclass
class Settings:
    num_generators: int = field(default_factory=lambda: _env_int("GENSIM_NUM_GENERATORS", 15))
    generator_ratings: list[int] = field(default_factory=list)
    max_generators: int = field(default_factory=lambda: _env_int("GENSIM_MAX_GENERATORS", DEFAULT_MAX_GENERATORS))
    web_host: str = field(default_factory=lambda: os.getenv("GENSIM_WEB_HOST", "127.0.0.1"))
    web_port: int = field(default_factory=lambda: _env_int("GENSIM_WEB_PORT", 5000))
    modbus_host: str = field(default_factory=lambda: os.getenv("GENSIM_MODBUS_HOST", "127.0.0.1"))
    modbus_port: int = field(default_factory=lambda: _env_int("GENSIM_MODBUS_PORT", 5020))
    public_web_host: str = field(default_factory=lambda: os.getenv("GENSIM_PUBLIC_WEB_HOST", ""))
    public_web_port: int | None = field(default_factory=lambda: _env_optional_int("GENSIM_PUBLIC_WEB_PORT"))
    public_modbus_host: str = field(default_factory=lambda: os.getenv("GENSIM_PUBLIC_MODBUS_HOST", ""))
    public_modbus_port: int | None = field(default_factory=lambda: _env_optional_int("GENSIM_PUBLIC_MODBUS_PORT"))
    command_queue_max: int = field(default_factory=lambda: _env_int("GENSIM_COMMAND_QUEUE_MAX", DEFAULT_COMMAND_QUEUE_MAX))
    command_queue_unit_limit: int = field(
        default_factory=lambda: _env_int("GENSIM_COMMAND_QUEUE_UNIT_LIMIT", DEFAULT_COMMAND_QUEUE_UNIT_LIMIT)
    )
    state_file: Path = field(
        default_factory=lambda: Path(
            os.getenv("GENSIM_STATE_FILE", Path(__file__).with_name("generator_state.json"))
        )
    )
    runbooks_file: Path = field(
        default_factory=lambda: Path(
            os.getenv("GENSIM_RUNBOOKS_FILE", Path(__file__).with_name("generator_runbooks.json"))
        )
    )
    save_interval: int = field(default_factory=lambda: _env_int("GENSIM_SAVE_INTERVAL", 60))
    tick_seconds: float = field(default_factory=lambda: _env_float("GENSIM_TICK_SECONDS", 1.0))
    secret_key: str = field(default_factory=lambda: os.getenv("GENSIM_SECRET_KEY", secrets.token_hex(16)))
    cors_allowed_origins: Any = field(
        default_factory=lambda: _parse_cors_origins(os.getenv("GENSIM_CORS_ALLOWED_ORIGINS", ""))
    )
    http_rate_limit: int = field(
        default_factory=lambda: _env_int("GENSIM_HTTP_RATE_LIMIT", DEFAULT_HTTP_RATE_LIMIT)
    )
    http_rate_window: int = field(
        default_factory=lambda: _env_int("GENSIM_HTTP_RATE_WINDOW", DEFAULT_HTTP_RATE_WINDOW)
    )

    def __post_init__(self) -> None:
        self.max_generators = max(1, self.max_generators)
        self.num_generators = _clamp_num_generators(self.num_generators, self.max_generators)
        self.web_port = _validate_tcp_port(self.web_port, "Web port")
        self.modbus_port = _validate_tcp_port(self.modbus_port, "Modbus port")
        if self.public_web_port is not None:
            self.public_web_port = _validate_tcp_port(self.public_web_port, "Public web port")
        if self.public_modbus_port is not None:
            self.public_modbus_port = _validate_tcp_port(self.public_modbus_port, "Public Modbus port")
        if not self.generator_ratings:
            self.generator_ratings = _expand_generator_ratings(_default_generator_size_counts(self.num_generators))
        elif len(self.generator_ratings) > self.max_generators:
            self.generator_ratings = self.generator_ratings[: self.max_generators]
        self.num_generators = len(self.generator_ratings)
        self.command_queue_max = max(1, self.command_queue_max)
        self.command_queue_unit_limit = max(1, self.command_queue_unit_limit)
        self.save_interval = _validate_save_interval(self.save_interval)
        self.tick_seconds = _validate_tick_seconds(self.tick_seconds)
        self.http_rate_limit = max(0, self.http_rate_limit)
        self.http_rate_window = max(1, self.http_rate_window)


@dataclass
class RuntimeMetrics:
    tick_durations_ms: deque[float] = field(default_factory=lambda: deque(maxlen=METRICS_WINDOW))
    dirty_counts: deque[int] = field(default_factory=lambda: deque(maxlen=METRICS_WINDOW))
    payload_sizes: deque[int] = field(default_factory=lambda: deque(maxlen=METRICS_WINDOW))
    modbus_durations_ms: deque[float] = field(default_factory=lambda: deque(maxlen=METRICS_WINDOW))
    connected_clients: int = 0
    last_tick_started_at: float | None = None
    last_tick_finished_at: float | None = None

    def _avg(self, values: deque[float]) -> float:
        return round(sum(values) / len(values), 3) if values else 0.0

    def snapshot(self, queue_depth: int) -> dict[str, Any]:
        return {
            "connected_clients": self.connected_clients,
            "queue_depth": queue_depth,
            "avg_tick_ms": self._avg(self.tick_durations_ms),
            "avg_dirty_generators": self._avg(self.dirty_counts),
            "avg_payload_bytes": self._avg(self.payload_sizes),
            "avg_modbus_sync_ms": self._avg(self.modbus_durations_ms),
            "last_tick_started_at": self.last_tick_started_at,
            "last_tick_finished_at": self.last_tick_finished_at,
        }


@dataclass
class Subfleet:
    subfleet_id: str
    name: str
    member_ids: set[int] = field(default_factory=set)


@dataclass
class QueuedCommand:
    target: str
    commands: tuple[int, ...]
    unit_ids: list[int]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def command(self) -> int:
        return self.commands[0]


def load_persistent_state(state_file: Path) -> dict[str, Any]:
    if state_file.exists():
        try:
            if state_file.stat().st_size > MAX_PERSISTENT_STATE_FILE_BYTES:
                log.warning(
                    "Ignoring state file %s because it exceeds %s bytes",
                    state_file,
                    MAX_PERSISTENT_STATE_FILE_BYTES,
                )
                return {}
            with state_file.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                return saved
            log.warning("Ignoring state file %s because top-level JSON is not an object", state_file)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not load state file %s: %s", state_file, exc)
    return {}


class StateStore:
    """Compact in-memory read model for summary, lists, detail, and sub-fleets."""

    def __init__(self, generators: list[Generator]):
        self._lock = Lock()
        self._detail_records: dict[int, dict[str, Any]] = {}
        self._compact_records: dict[int, dict[str, Any]] = {}
        self._register_cache: dict[int, dict[int, int]] = {}
        self._subfleets: dict[str, Subfleet] = {}
        self._generator_subfleets: dict[int, str | None] = {gen.unit_id: None for gen in generators}
        self.refresh(generators)

    @staticmethod
    def _normalize_subfleet_name(name: Any) -> str:
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Sub-fleet name is required.")
        if len(normalized) > SUBFLEET_NAME_LIMIT:
            raise ValueError(f"Sub-fleet name cannot exceed {SUBFLEET_NAME_LIMIT} characters.")
        return normalized

    def refresh(self, generators: list[Generator]) -> set[int]:
        dirty_ids: set[int] = set()
        with self._lock:
            for gen in generators:
                detail = gen.get_state()
                detail["registers"] = gen.get_register_values()
                detail["subfleet_id"] = self._generator_subfleets.get(gen.unit_id)
                compact = self._build_compact_record(detail)
                if self._detail_records.get(gen.unit_id) != detail:
                    dirty_ids.add(gen.unit_id)
                    self._detail_records[gen.unit_id] = detail
                    self._compact_records[gen.unit_id] = compact
                    self._register_cache[gen.unit_id] = detail["registers"]
        return dirty_ids

    def _build_compact_record(self, detail: dict[str, Any]) -> dict[str, Any]:
        alarms = detail["alarms"]
        return {
            "unit_id": detail["unit_id"],
            "name": detail["name"],
            "rated_kw": detail["rated_kw"],
            "state": detail["state"],
            "auto_mode": detail["auto_mode"],
            "output_kw": detail["output_kw"],
            "utility_load_kw": detail["utility_load_kw"],
            "fuel_level": detail["fuel_level"],
            "engine_rpm": detail["engine_rpm"],
            "output_voltage": detail["output_voltage"],
            "output_frequency": detail["output_frequency"],
            "run_hours": detail["run_hours"],
            "utility_breaker": detail["utility_breaker"],
            "gen_breaker": detail["gen_breaker"],
            "subfleet_id": detail["subfleet_id"],
            "alarm_count": len([alarm_id for alarm_id in alarms if alarm_id not in STATUS_ALARM_IDS]),
            "has_actionable_alarm": _has_actionable_alarm(alarms),
        }

    def list_generators(
        self,
        *,
        page: int,
        page_size: int,
        state: str | None = None,
        alarmed_only: bool = False,
        search: str | None = None,
        subfleet_id: str | None = None,
        context_subfleet_id: str | None = None,
        rated_kw: int | None = None,
        auto_mode: bool | None = None,
        subfleet_scope: str = "any",
        sort: str = "unit_id",
        descending: bool = False,
    ) -> dict[str, Any]:
        items = self.find_generators(
            state=state,
            alarmed_only=alarmed_only,
            search=search,
            subfleet_id=subfleet_id,
            context_subfleet_id=context_subfleet_id,
            rated_kw=rated_kw,
            auto_mode=auto_mode,
            subfleet_scope=subfleet_scope,
        )
        sort_key_map = {
            "unit_id": lambda item: item["unit_id"],
            "name": lambda item: item["name"],
            "state": lambda item: item["state"],
            "rated_kw": lambda item: item["rated_kw"],
            "output_kw": lambda item: item["output_kw"],
            "fuel_level": lambda item: item["fuel_level"],
            "alarm_count": lambda item: item["alarm_count"],
            "subfleet_id": lambda item: item["subfleet_id"] or "",
        }
        sort_key = sort_key_map.get(sort, sort_key_map["unit_id"])
        items.sort(key=sort_key, reverse=descending)
        return _paginate(items, page, page_size)

    def find_generators(
        self,
        *,
        state: str | None = None,
        alarmed_only: bool = False,
        search: str | None = None,
        subfleet_id: str | None = None,
        context_subfleet_id: str | None = None,
        rated_kw: int | None = None,
        auto_mode: bool | None = None,
        subfleet_scope: str = "any",
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._compact_records.values())

        if state:
            items = [item for item in items if item["state"] == state]
        if alarmed_only:
            items = [item for item in items if item["has_actionable_alarm"]]
        if subfleet_id:
            items = [item for item in items if item["subfleet_id"] == subfleet_id]
        if rated_kw is not None:
            items = [item for item in items if int(item["rated_kw"]) == rated_kw]
        if auto_mode is not None:
            items = [item for item in items if item["auto_mode"] is auto_mode]
        if search:
            items = [item for item in items if _matches_generator_search(item, search)]
        if subfleet_scope == "assigned":
            items = [item for item in items if item["subfleet_id"]]
        elif subfleet_scope == "unassigned":
            items = [item for item in items if not item["subfleet_id"]]
        elif subfleet_scope == "active":
            items = [item for item in items if item["subfleet_id"] == context_subfleet_id]
        elif subfleet_scope == "other":
            items = [item for item in items if item["subfleet_id"] and item["subfleet_id"] != context_subfleet_id]
        return items

    def get_generator_detail(self, unit_id: int, include_registers: bool = False) -> dict[str, Any] | None:
        with self._lock:
            detail = self._detail_records.get(unit_id)
            if detail is None:
                return None
            result = dict(detail)
            if not include_registers:
                result.pop("registers", None)
            return result

    def get_registers(self, unit_id: int) -> dict[int, int] | None:
        with self._lock:
            registers = self._register_cache.get(unit_id)
            return dict(registers) if registers is not None else None

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._compact_records.values())
            subfleets = list(self._subfleets.values())
        state_counts = Counter(item["state"] for item in items)
        running_items = [item for item in items if item["state"] == "RUNNING"]
        online_items = [item for item in running_items if item["gen_breaker"]]
        return {
            "generator_count": len(items),
            "running_count": state_counts.get("RUNNING", 0),
            "fault_count": state_counts.get("FAULT", 0),
            "alarm_count": sum(1 for item in items if item["has_actionable_alarm"]),
            "total_output_kw": round(sum(item["output_kw"] for item in items), 1),
            "running_capacity_kw": round(sum(item["rated_kw"] for item in running_items), 1),
            "online_capacity_kw": round(sum(item["rated_kw"] for item in online_items), 1),
            "low_fuel_count": sum(1 for item in items if item["fuel_level"] < 15),
            "subfleet_count": len(subfleets),
            "state_counts": dict(state_counts),
        }

    def list_subfleets(self) -> list[dict[str, Any]]:
        with self._lock:
            subfleets = list(self._subfleets.values())
            compact_records = dict(self._compact_records)
        items = []
        for subfleet in sorted(subfleets, key=lambda item: item.name.lower()):
            members = [compact_records[unit_id] for unit_id in sorted(subfleet.member_ids) if unit_id in compact_records]
            state_counts = Counter(member["state"] for member in members)
            running_members = [member for member in members if member["state"] == "RUNNING"]
            online_members = [member for member in running_members if member["gen_breaker"]]
            items.append({
                "id": subfleet.subfleet_id,
                "name": subfleet.name,
                "member_ids": [member["unit_id"] for member in members],
                "unit_count": len(members),
                "running_count": state_counts.get("RUNNING", 0),
                "alarm_count": sum(1 for member in members if member["has_actionable_alarm"]),
                "low_fuel_count": sum(1 for member in members if member["fuel_level"] < 15),
                "total_output_kw": round(sum(member["output_kw"] for member in members), 1),
                "running_capacity_kw": round(sum(member["rated_kw"] for member in running_members), 1),
                "online_capacity_kw": round(sum(member["rated_kw"] for member in online_members), 1),
                "state_counts": dict(state_counts),
            })
        return items

    def get_subfleet(self, subfleet_id: str) -> dict[str, Any] | None:
        for subfleet in self.list_subfleets():
            if subfleet["id"] == subfleet_id:
                return subfleet
        return None

    def create_subfleet(self, name: str) -> dict[str, Any]:
        normalized = self._normalize_subfleet_name(name)
        with self._lock:
            if any(item.name.lower() == normalized.lower() for item in self._subfleets.values()):
                raise ValueError("Sub-fleet name must be unique.")
            subfleet_id = uuid.uuid4().hex[:12]
            self._subfleets[subfleet_id] = Subfleet(subfleet_id=subfleet_id, name=normalized)
        return self.get_subfleet(subfleet_id) or {}

    def rename_subfleet(self, subfleet_id: str, name: str) -> dict[str, Any]:
        normalized = self._normalize_subfleet_name(name)
        with self._lock:
            subfleet = self._subfleets.get(subfleet_id)
            if subfleet is None:
                raise KeyError("Sub-fleet not found.")
            if any(item.subfleet_id != subfleet_id and item.name.lower() == normalized.lower() for item in self._subfleets.values()):
                raise ValueError("Sub-fleet name must be unique.")
            subfleet.name = normalized
        return self.get_subfleet(subfleet_id) or {}

    def delete_subfleet(self, subfleet_id: str) -> bool:
        with self._lock:
            subfleet = self._subfleets.pop(subfleet_id, None)
            if subfleet is None:
                return False
            for unit_id in list(subfleet.member_ids):
                self._generator_subfleets[unit_id] = None
                detail = self._detail_records.get(unit_id)
                if detail:
                    detail["subfleet_id"] = None
                    self._compact_records[unit_id] = self._build_compact_record(detail)
        return True

    def assign_generators(self, subfleet_id: str, unit_ids: list[int]) -> dict[str, Any]:
        with self._lock:
            subfleet = self._subfleets.get(subfleet_id)
            if subfleet is None:
                raise KeyError("Sub-fleet not found.")
            missing = [unit_id for unit_id in unit_ids if unit_id not in self._generator_subfleets]
            if missing:
                raise ValueError(f"Unknown generator IDs: {missing}")
            moved_ids: list[int] = []
            for unit_id in unit_ids:
                previous_subfleet_id = self._generator_subfleets[unit_id]
                if previous_subfleet_id == subfleet_id:
                    continue
                if previous_subfleet_id and previous_subfleet_id in self._subfleets:
                    self._subfleets[previous_subfleet_id].member_ids.discard(unit_id)
                self._generator_subfleets[unit_id] = subfleet_id
                subfleet.member_ids.add(unit_id)
                detail = self._detail_records.get(unit_id)
                if detail:
                    detail["subfleet_id"] = subfleet_id
                    self._compact_records[unit_id] = self._build_compact_record(detail)
                moved_ids.append(unit_id)
        return {"subfleet": self.get_subfleet(subfleet_id), "assigned_unit_ids": sorted(moved_ids)}

    def assign_generators_by_query(
        self,
        subfleet_id: str,
        *,
        state: str | None = None,
        alarmed_only: bool = False,
        search: str | None = None,
        context_subfleet_id: str | None = None,
        rated_kw: int | None = None,
        auto_mode: bool | None = None,
        subfleet_scope: str = "any",
    ) -> dict[str, Any]:
        matches = self.find_generators(
            state=state,
            alarmed_only=alarmed_only,
            search=search,
            context_subfleet_id=context_subfleet_id or subfleet_id,
            rated_kw=rated_kw,
            auto_mode=auto_mode,
            subfleet_scope=subfleet_scope,
        )
        result = self.assign_generators(subfleet_id, [item["unit_id"] for item in matches])
        result["matched_unit_count"] = len(matches)
        return result

    def unassign_generator(self, subfleet_id: str, unit_id: int) -> bool:
        with self._lock:
            subfleet = self._subfleets.get(subfleet_id)
            if subfleet is None or unit_id not in subfleet.member_ids:
                return False
            subfleet.member_ids.discard(unit_id)
            self._generator_subfleets[unit_id] = None
            detail = self._detail_records.get(unit_id)
            if detail:
                detail["subfleet_id"] = None
                self._compact_records[unit_id] = self._build_compact_record(detail)
        return True

    def unassign_generators_by_query(
        self,
        subfleet_id: str,
        *,
        state: str | None = None,
        alarmed_only: bool = False,
        search: str | None = None,
        rated_kw: int | None = None,
        auto_mode: bool | None = None,
    ) -> dict[str, Any]:
        matches = self.find_generators(
            state=state,
            alarmed_only=alarmed_only,
            search=search,
            context_subfleet_id=subfleet_id,
            rated_kw=rated_kw,
            auto_mode=auto_mode,
            subfleet_scope="active",
        )
        removed_ids: list[int] = []
        for item in matches:
            unit_id = item["unit_id"]
            if self.unassign_generator(subfleet_id, unit_id):
                removed_ids.append(unit_id)
        return {
            "subfleet": self.get_subfleet(subfleet_id),
            "removed_unit_ids": sorted(removed_ids),
            "matched_unit_count": len(matches),
        }

    def get_subfleet_unit_ids(self, subfleet_id: str) -> list[int]:
        with self._lock:
            subfleet = self._subfleets.get(subfleet_id)
            if subfleet is None:
                raise KeyError("Sub-fleet not found.")
            return sorted(subfleet.member_ids)

    def export_subfleets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_serialize_subfleet(subfleet) for subfleet in self._subfleets.values()]

    def load_subfleets(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            self._subfleets = {}
            for unit_id in self._generator_subfleets:
                self._generator_subfleets[unit_id] = None
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                subfleet_id = str(item.get("id") or item.get("subfleet_id") or uuid.uuid4().hex[:12])
                try:
                    name = self._normalize_subfleet_name(item.get("name", ""))
                except ValueError:
                    continue
                members = set()
                raw_member_ids = item.get("member_ids", [])
                if not isinstance(raw_member_ids, list):
                    raw_member_ids = []
                for raw_unit_id in raw_member_ids:
                    try:
                        unit_id = int(raw_unit_id)
                    except (TypeError, ValueError):
                        continue
                    if unit_id in self._generator_subfleets:
                        self._generator_subfleets[unit_id] = subfleet_id
                        members.add(unit_id)
                self._subfleets[subfleet_id] = Subfleet(subfleet_id=subfleet_id, name=name, member_ids=members)
            for unit_id, detail in self._detail_records.items():
                detail["subfleet_id"] = self._generator_subfleets.get(unit_id)
                self._compact_records[unit_id] = self._build_compact_record(detail)


class SimulationEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.generators = [
            Generator(i + 1, rated_kw=rating)
            for i, rating in enumerate(settings.generator_ratings)
        ]
        self.generator_by_id = {gen.unit_id: gen for gen in self.generators}

    def apply_command(self, unit_id: int, command: int) -> bool:
        generator = self.generator_by_id.get(unit_id)
        if generator is None:
            return False
        generator.command(command)
        return True

    def tick(self) -> None:
        for generator in self.generators:
            generator.tick(dt=self.settings.tick_seconds)


class CommandRouter:
    def __init__(self, max_commands: int = DEFAULT_COMMAND_QUEUE_MAX, max_unit_targets: int = DEFAULT_COMMAND_QUEUE_UNIT_LIMIT):
        self._queue: deque[QueuedCommand] = deque()
        self._queued_unit_targets = 0
        self._max_commands = max(1, max_commands)
        self._max_unit_targets = max(1, max_unit_targets)
        self._lock = Lock()

    def enqueue(self, *, target: str, command: int, unit_ids: list[int], metadata: dict[str, Any] | None = None) -> bool:
        return self.enqueue_sequence(target=target, commands=(command,), unit_ids=unit_ids, metadata=metadata)

    def enqueue_sequence(
        self,
        *,
        target: str,
        commands: tuple[int, ...],
        unit_ids: list[int],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not commands:
            return False
        normalized_commands = tuple(_normalize_command(command) for command in commands)
        with self._lock:
            unit_target_count = len(unit_ids)
            if len(self._queue) >= self._max_commands or self._queued_unit_targets + unit_target_count > self._max_unit_targets:
                log.warning(
                    "Command queue rejected target=%s commands=%s units=%s depth=%s unit_targets=%s",
                    target,
                    list(normalized_commands),
                    unit_target_count,
                    len(self._queue),
                    self._queued_unit_targets,
                )
                return False
            self._queue.append(QueuedCommand(target=target, commands=normalized_commands, unit_ids=unit_ids, metadata=metadata or {}))
            self._queued_unit_targets += unit_target_count
            return True

    def drain(self) -> list[QueuedCommand]:
        with self._lock:
            commands = list(self._queue)
            self._queue.clear()
            self._queued_unit_targets = 0
        return commands

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)


class ModbusAdapter:
    def __init__(self, num_generators: int):
        self.context = create_modbus_context(num_generators=num_generators)
        self._last_setpoint_raw: dict[int, int] = {}

    def apply_external_commands(self, router: CommandRouter, num_generators: int) -> None:
        for unit_id in range(1, num_generators + 1):
            command = read_command_register(self.context, unit_id)
            if command != 0:
                try:
                    router.enqueue(target="modbus", command=command, unit_ids=[unit_id], metadata={"source": "modbus"})
                except ValueError as exc:
                    log.warning("Ignoring invalid Modbus command for unit %s: %s", unit_id, exc)

    def apply_external_setpoints(self, engine: SimulationEngine) -> set[int]:
        dirty_unit_ids: set[int] = set()
        for generator in engine.generators:
            unit_id = generator.unit_id
            modbus_setpoint_raw = read_register(self.context, unit_id, 16)
            expected_raw = self._last_setpoint_raw.get(unit_id)
            if expected_raw is not None and modbus_setpoint_raw != expected_raw:
                generator.set_parallel_setpoint(modbus_setpoint_raw / 10.0)
                dirty_unit_ids.add(unit_id)
                log.info("Modbus setpoint %.1f kW for GEN-%02d", modbus_setpoint_raw / 10.0, unit_id)
        return dirty_unit_ids

    def sync_dirty_units(self, dirty_unit_ids: set[int], state_store: StateStore, metrics: RuntimeMetrics) -> None:
        started = time.monotonic()
        for unit_id in dirty_unit_ids:
            registers = state_store.get_registers(unit_id)
            if registers is not None:
                sync_generator_to_modbus(self.context, unit_id, registers)
                if 16 in registers:
                    self._last_setpoint_raw[unit_id] = registers[16]
        metrics.modbus_durations_ms.append((time.monotonic() - started) * 1000)


class EventPublisher:
    def __init__(self):
        self._lock = Lock()
        self._subscriptions: dict[str, dict[str, Any]] = {}

    def connect(self, sid: str) -> None:
        with self._lock:
            self._subscriptions[sid] = {"detail_unit_id": None}

    def disconnect(self, sid: str) -> None:
        with self._lock:
            self._subscriptions.pop(sid, None)

    def watch_detail(self, sid: str, unit_id: int | None) -> None:
        with self._lock:
            if sid in self._subscriptions:
                self._subscriptions[sid]["detail_unit_id"] = unit_id

    def watched_units(self) -> dict[str, int]:
        with self._lock:
            return {
                sid: int(config["detail_unit_id"])
                for sid, config in self._subscriptions.items()
                if config.get("detail_unit_id") is not None
            }

    def count(self) -> int:
        with self._lock:
            return len(self._subscriptions)


def save_persistent_state(
    engine: SimulationEngine,
    state_store: StateStore,
    state_file: Path,
    scenario_runner: "ScenarioRunner | None" = None,
) -> str | None:
    data: dict[str, Any] = {
        "fleet_config": {
            "num_generators": engine.settings.num_generators,
            "generator_ratings": [int(rating) for rating in engine.settings.generator_ratings],
        },
        "generators": {},
        "subfleets": state_store.export_subfleets(),
    }
    for generator in engine.generators:
        with generator.lock:
            data["generators"][str(generator.unit_id)] = {"run_hours": generator.run_hours}
    if scenario_runner is not None:
        data["scenario_runtime"] = scenario_runner.snapshot_for_save()

    tmp_path: str | None = None
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=state_file.parent, prefix=f".{state_file.name}.", suffix=".tmp")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        _replace_file_atomically(tmp_path, state_file)
        tmp_path = None
        return None
    except OSError as exc:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        log.warning("Could not save state file %s: %s", state_file, exc)
        return str(exc)


def _persistent_state_matches_fleet(saved: dict[str, Any], settings: Settings) -> bool:
    expected_ratings = [int(rating) for rating in settings.generator_ratings]
    fleet_config = saved.get("fleet_config")
    if isinstance(fleet_config, dict):
        saved_ratings = fleet_config.get("generator_ratings")
        if isinstance(saved_ratings, list):
            try:
                return [int(rating) for rating in saved_ratings] == expected_ratings
            except (TypeError, ValueError):
                return False
        saved_count = fleet_config.get("num_generators")
        return saved_count == settings.num_generators

    saved_generators = saved.get("generators")
    if isinstance(saved_generators, dict):
        return len(saved_generators) == settings.num_generators

    legacy_unit_keys = [key for key in saved if str(key).isdigit()]
    return len(legacy_unit_keys) == settings.num_generators


def _coerce_persisted_run_hours(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        run_hours = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(run_hours) or run_hours < 0:
        return None
    return run_hours


class SimulatorRuntime:
    """Coordinates simulation, commands, state, Modbus, and event publishing."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = SimulationEngine(settings)
        self.command_router = CommandRouter(
            max_commands=settings.command_queue_max,
            max_unit_targets=settings.command_queue_unit_limit,
        )
        self.state_store = StateStore(self.engine.generators)
        self.modbus_adapter = ModbusAdapter(num_generators=settings.num_generators)
        self.event_publisher = EventPublisher()
        self.metrics = RuntimeMetrics()
        self.scenario_runner = ScenarioRunner()
        self.runbook_registry = RunbookRegistry(settings.runbooks_file)
        self._runbook_definition: dict[str, Any] | None = None
        self._runbook_report: dict[str, Any] | None = None
        self._last_runbook_report: dict[str, Any] | None = None
        self._runbook_elapsed_seconds = 0.0
        self._runbook_next_step_index = 0
        self._stop_requested = Event()
        self._loop_started = False
        self._loop_stopped = Event()
        self.last_save_error: str | None = None
        self._restore_state()
        self.state_store.refresh(self.engine.generators)
        self.modbus_adapter.sync_dirty_units(set(range(1, settings.num_generators + 1)), self.state_store, self.metrics)

    @property
    def generators(self) -> list[Generator]:
        return self.engine.generators

    @property
    def modbus_context(self):  # pragma: no cover - compatibility property
        return self.modbus_adapter.context

    def _restore_state(self) -> None:
        saved = load_persistent_state(self.settings.state_file)
        if not _persistent_state_matches_fleet(saved, self.settings):
            if saved:
                log.info("Skipping persisted runtime state because fleet configuration changed")
            return
        for generator in self.engine.generators:
            key = str(generator.unit_id)
            run_hours = saved.get("generators", {}).get(key, {}).get("run_hours")
            if run_hours is None:
                run_hours = saved.get(key, {}).get("run_hours")
            normalized_run_hours = _coerce_persisted_run_hours(run_hours)
            if normalized_run_hours is not None:
                generator.run_hours = normalized_run_hours
        self.state_store.load_subfleets(saved.get("subfleets", []))
        self.scenario_runner.restore_state(saved.get("scenario_runtime", {}))

    def save_state(self) -> None:
        self.last_save_error = save_persistent_state(
            self.engine, self.state_store, self.settings.state_file, self.scenario_runner
        )

    def active_scenario_id(self) -> str | None:
        return self.scenario_runner.active_scenario_id()

    def start_scenario(self, scenario_id: str) -> dict[str, Any]:
        return self.scenario_runner.start(scenario_id)

    def stop_scenario(self) -> dict[str, Any]:
        return self.scenario_runner.stop()

    def scenario_status(self) -> dict[str, Any]:
        return self.scenario_runner.status_payload()

    # ------------------------------------------------------------------
    # Runbook lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _copy_runbook_report(source: dict[str, Any]) -> dict[str, Any]:
        steps = [dict(step) for step in source["steps"]]
        report = {key: value for key, value in source.items() if key != "steps"}
        report["steps"] = steps
        return report

    def runbook_status(self) -> dict[str, Any] | None:
        if self._runbook_report is None or self._runbook_report.get("status") != "running":
            return None
        return self._copy_runbook_report(self._runbook_report)

    def last_runbook_report(self) -> dict[str, Any] | None:
        if self._last_runbook_report is None:
            return None
        return self._copy_runbook_report(self._last_runbook_report)

    def start_runbook(self, runbook: dict[str, Any]) -> dict[str, Any]:
        if self._runbook_report is not None and self._runbook_report.get("status") == "running":
            raise RuntimeError("A runbook is already running.")
        steps = []
        for index, step in enumerate(runbook["steps"], start=1):
            steps.append({
                "index": index,
                "at_seconds": step["at_seconds"],
                "action": step["action"],
                "summary": step["summary"],
                "status": "pending",
                "result_message": "",
                "observed_value": None,
                "label": step["params"].get("label", ""),
            })
        self._runbook_definition = deepcopy(runbook)
        self._last_runbook_report = None
        self._runbook_report = {
            "id": runbook["id"],
            "name": runbook["name"],
            "description": runbook["description"],
            "status": "running",
            "elapsed_seconds": 0.0,
            "total_steps": len(runbook["steps"]),
            "steps_completed": 0,
            "failed_steps": 0,
            "assertions_passed": 0,
            "assertions_failed": 0,
            "next_step_index": 0,
            "next_step_at_seconds": runbook["steps"][0]["at_seconds"] if runbook["steps"] else None,
            "steps": steps,
        }
        self._runbook_elapsed_seconds = 0.0
        self._runbook_next_step_index = 0
        log.info("Runbook started: %s (%s steps)", runbook["id"], len(runbook["steps"]))
        return self.runbook_status()

    def stop_runbook(self) -> dict[str, Any] | None:
        if self._runbook_report is None or self._runbook_report.get("status") != "running":
            return None
        self._runbook_report["status"] = "cancelled"
        self._update_runbook_progress()
        self._last_runbook_report = self._copy_runbook_report(self._runbook_report)
        log.info("Runbook cancelled: %s", self._runbook_report["id"])
        self._runbook_definition = None
        self._runbook_report = None
        self._runbook_elapsed_seconds = 0.0
        self._runbook_next_step_index = 0
        return self.last_runbook_report()

    def _fleet_metric_snapshot(self) -> dict[str, float]:
        states = self.snapshot_state()
        num_units = max(1, len(states))
        mode_counts: Counter[str] = Counter(
            item.get("transfer_mode", "TRANSFER") for item in states
        )
        dominant_mode = mode_counts.most_common(1)[0][0] if mode_counts else "TRANSFER"
        return {
            "running_units": float(sum(1 for item in states if item and item.get("state") == "RUNNING")),
            "faulted_units": float(sum(1 for item in states if item and item.get("state") == "FAULT")),
            "total_power_kw": float(sum(item.get("output_kw", 0.0) for item in states if item)),
            "average_frequency_hz": sum(item.get("output_frequency", 0.0) for item in states if item) / num_units,
            "average_voltage_v": sum(item.get("output_voltage", 0.0) for item in states if item) / num_units,
            "average_fuel_level": sum(item.get("fuel_level", 0.0) for item in states if item) / num_units,
            "fleet_mode": FLEET_MODE_NUMERIC.get(dominant_mode, 0.0),
        }

    @staticmethod
    def _compare_runbook_values(observed: float, expected: float, comparison: str, tolerance: float | None) -> bool:
        if comparison == "eq":
            if tolerance is not None:
                return abs(observed - expected) <= tolerance
            return observed == expected
        if comparison == "ne":
            if tolerance is not None:
                return abs(observed - expected) > tolerance
            return observed != expected
        if comparison == "gt":
            return observed > expected
        if comparison == "gte":
            return observed >= expected
        if comparison == "lt":
            return observed < expected
        if comparison == "lte":
            return observed <= expected
        return False

    def _execute_runbook_step(self, step: dict[str, Any]) -> dict[str, Any]:
        action = step["action"]
        params = step.get("params", {})
        if action == "note":
            return {"status": "executed", "result_message": params.get("message", ""), "observed_value": None}
        if action == "fleet_command":
            try:
                queued = self.enqueue_fleet_command(params["cmd"])
            except ValueError as exc:
                return {"status": "failed", "result_message": str(exc), "observed_value": None}
            if not queued:
                return {"status": "failed", "result_message": "Command queue is full.", "observed_value": None}
            return {"status": "executed", "result_message": step["summary"], "observed_value": float(params["cmd"])}
        if action == "unit_command":
            try:
                ok = self.enqueue_unit_command(int(params["unit_id"]), params["cmd"])
            except ValueError as exc:
                return {"status": "failed", "result_message": str(exc), "observed_value": None}
            if not ok:
                return {"status": "failed", "result_message": "Invalid unit_id for unit_command.", "observed_value": None}
            return {"status": "executed", "result_message": step["summary"], "observed_value": float(params["cmd"])}
        if action == "fleet_mode":
            mode = params["mode"]
            cmd = FLEET_MODE_COMMANDS.get(mode)
            if cmd is None:
                return {"status": "failed", "result_message": f"Unknown fleet mode: {mode}", "observed_value": None}
            self.enqueue_fleet_command(cmd)
            return {"status": "executed", "result_message": step["summary"], "observed_value": float(cmd)}
        if action == "fault_injection":
            try:
                ok = self.enqueue_unit_command(int(params["unit_id"]), params["cmd"])
            except ValueError as exc:
                return {"status": "failed", "result_message": str(exc), "observed_value": None}
            if not ok:
                return {"status": "failed", "result_message": "Invalid unit_id for fault_injection.", "observed_value": None}
            return {"status": "executed", "result_message": step["summary"], "observed_value": float(params["cmd"])}
        if action == "load_setpoint":
            ok = self.set_parallel_setpoint(int(params["unit_id"]), float(params["setpoint_kw"]))
            if not ok:
                return {"status": "failed", "result_message": "Invalid unit_id for load_setpoint.", "observed_value": None}
            return {"status": "executed", "result_message": step["summary"], "observed_value": float(params["setpoint_kw"])}
        if action == "assert_fleet_metric":
            metrics = self._fleet_metric_snapshot()
            observed = float(metrics.get(str(params["metric"]), 0.0))
            passed = self._compare_runbook_values(observed, params["value"], params["comparison"], params.get("tolerance"))
            label = params.get("label") or step["summary"]
            comparison_text = f"{params['metric']}={observed:.3f} {params['comparison']} {params['value']:.3f}"
            return {
                "status": "passed" if passed else "failed",
                "result_message": f"{label}: {comparison_text}",
                "observed_value": observed,
            }
        return {"status": "failed", "result_message": f"Unsupported runbook action: {action}", "observed_value": None}

    def _update_runbook_progress(self) -> None:
        if self._runbook_report is None:
            return
        steps = self._runbook_report["steps"]
        self._runbook_report["elapsed_seconds"] = round(self._runbook_elapsed_seconds, 1)
        self._runbook_report["steps_completed"] = sum(1 for step in steps if step["status"] != "pending")
        self._runbook_report["failed_steps"] = sum(1 for step in steps if step["status"] == "failed")
        self._runbook_report["assertions_passed"] = sum(1 for step in steps if step["status"] == "passed")
        self._runbook_report["assertions_failed"] = sum(
            1 for step in steps if step["action"] == "assert_fleet_metric" and step["status"] == "failed"
        )
        self._runbook_report["next_step_index"] = self._runbook_next_step_index
        if self._runbook_definition and self._runbook_next_step_index < len(self._runbook_definition["steps"]):
            self._runbook_report["next_step_at_seconds"] = (
                self._runbook_definition["steps"][self._runbook_next_step_index]["at_seconds"]
            )
        else:
            self._runbook_report["next_step_at_seconds"] = None

    def _execute_due_runbook_steps(self) -> None:
        if self._runbook_definition is None or self._runbook_report is None:
            return
        if self._runbook_report["status"] != "running":
            return
        steps = self._runbook_definition["steps"]
        while self._runbook_next_step_index < len(steps):
            step = steps[self._runbook_next_step_index]
            if step["at_seconds"] > self._runbook_elapsed_seconds:
                break
            result = self._execute_runbook_step(step)
            report_step = self._runbook_report["steps"][self._runbook_next_step_index]
            report_step["status"] = result["status"]
            report_step["result_message"] = result["result_message"]
            report_step["observed_value"] = result["observed_value"]
            log.info(
                "Runbook step %d/%d action=%s status=%s",
                report_step["index"],
                self._runbook_report["total_steps"],
                step["action"],
                result["status"],
            )
            self._runbook_next_step_index += 1
            self._update_runbook_progress()
        if self._runbook_next_step_index >= len(steps):
            self._runbook_report["status"] = (
                "completed_with_failures" if self._runbook_report["failed_steps"] > 0 else "completed"
            )
            self._update_runbook_progress()
            log.info("Runbook completed: %s status=%s", self._runbook_report["id"], self._runbook_report["status"])
            self._last_runbook_report = self._copy_runbook_report(self._runbook_report)
            self._runbook_definition = None
            self._runbook_report = None
            self._runbook_elapsed_seconds = 0.0
            self._runbook_next_step_index = 0

    def _advance_runbook_clock(self) -> None:
        if self._runbook_report is None or self._runbook_report["status"] != "running":
            return
        self._runbook_elapsed_seconds += self.settings.tick_seconds
        self._update_runbook_progress()

    def snapshot_state(self, include_registers: bool = False) -> list[dict[str, Any]]:
        return [
            self.state_store.get_generator_detail(generator.unit_id, include_registers=include_registers)
            for generator in self.engine.generators
        ]

    def enqueue_unit_command(self, unit_id: int, command: int) -> bool:
        if unit_id not in self.engine.generator_by_id:
            return False
        return self.command_router.enqueue(target="unit", command=command, unit_ids=[unit_id], metadata={"unit_id": unit_id})

    def set_parallel_setpoint(self, unit_id: int, setpoint_kw: float) -> bool:
        generator = self.engine.generator_by_id.get(unit_id)
        if generator is None:
            return False
        generator.set_parallel_setpoint(setpoint_kw)
        return True

    def enqueue_fleet_command(self, command: int) -> bool:
        return self.command_router.enqueue(
            target="fleet",
            command=command,
            unit_ids=[generator.unit_id for generator in self.engine.generators],
            metadata={"scope": "fleet"},
        )

    def enqueue_fleet_command_sequence(self, commands: tuple[int, ...]) -> bool:
        return self.command_router.enqueue_sequence(
            target="fleet",
            commands=commands,
            unit_ids=[generator.unit_id for generator in self.engine.generators],
            metadata={"scope": "fleet", "commands": list(commands)},
        )

    def enqueue_unit_group_command(self, unit_ids: list[Any], command: Any, target: str = "bulk") -> dict[str, Any]:
        valid_unit_ids: list[int] = []
        skipped_unit_ids: list[int] = []
        seen: set[int] = set()
        for raw_unit_id in unit_ids:
            try:
                unit_id = _int_arg(str(raw_unit_id), "unit_id")
            except (TypeError, ValueError):
                continue
            if unit_id is None:
                continue
            if unit_id in seen:
                continue
            seen.add(unit_id)
            if unit_id in self.engine.generator_by_id:
                valid_unit_ids.append(unit_id)
            else:
                skipped_unit_ids.append(unit_id)
        if not valid_unit_ids:
            raise ValueError("At least one valid unit ID is required.")
        queued = self.command_router.enqueue(
            target=target,
            command=command,
            unit_ids=valid_unit_ids,
            metadata={"scope": target, "requested_unit_count": len(unit_ids)},
        )
        if not queued:
            raise RuntimeError("Command queue is full; try again after the next simulation tick.")
        return {
            "queued_units": valid_unit_ids,
            "queued_count": len(valid_unit_ids),
            "skipped_unit_ids": skipped_unit_ids,
        }

    def enqueue_subfleet_command(self, subfleet_id: str, command: int) -> dict[str, Any]:
        unit_ids = self.state_store.get_subfleet_unit_ids(subfleet_id)
        if not unit_ids:
            raise ValueError("Sub-fleet has no members.")
        queued = self.command_router.enqueue(
            target="subfleet",
            command=command,
            unit_ids=unit_ids,
            metadata={"scope": "subfleet", "subfleet_id": subfleet_id},
        )
        if not queued:
            raise RuntimeError("Command queue is full; try again after the next simulation tick.")
        return {
            "subfleet_id": subfleet_id,
            "command": command,
            "queued_units": unit_ids,
            "queued_count": len(unit_ids),
            "skipped_unit_ids": [],
        }

    def create_subfleet(self, name: str) -> dict[str, Any]:
        return self.state_store.create_subfleet(name)

    def rename_subfleet(self, subfleet_id: str, name: str) -> dict[str, Any]:
        return self.state_store.rename_subfleet(subfleet_id, name)

    def delete_subfleet(self, subfleet_id: str) -> bool:
        return self.state_store.delete_subfleet(subfleet_id)

    def assign_generators_to_subfleet(self, subfleet_id: str, unit_ids: list[int]) -> dict[str, Any]:
        return self.state_store.assign_generators(subfleet_id, unit_ids)

    def unassign_generator_from_subfleet(self, subfleet_id: str, unit_id: int) -> bool:
        return self.state_store.unassign_generator(subfleet_id, unit_id)

    def simulation_step(self) -> set[int]:
        self.metrics.last_tick_started_at = time.time()
        tick_started = time.monotonic()
        self.modbus_adapter.apply_external_commands(self.command_router, self.settings.num_generators)
        queued_commands = self.command_router.drain()
        dirty_unit_ids: set[int] = set()
        for queued in queued_commands:
            for unit_id in queued.unit_ids:
                for command in queued.commands:
                    if self.engine.apply_command(unit_id, command):
                        dirty_unit_ids.add(unit_id)
        dirty_unit_ids |= self.modbus_adapter.apply_external_setpoints(self.engine)
        self.engine.tick()
        dirty_unit_ids |= self.state_store.refresh(self.engine.generators)
        self.modbus_adapter.sync_dirty_units(dirty_unit_ids, self.state_store, self.metrics)
        self.metrics.dirty_counts.append(len(dirty_unit_ids))
        self.metrics.tick_durations_ms.append((time.monotonic() - tick_started) * 1000)
        self.metrics.last_tick_finished_at = time.time()
        return dirty_unit_ids

    def emit_updates(self, socketio: SocketIO, dirty_unit_ids: set[int]) -> None:
        summary_payload = self.state_store.get_summary()
        subfleets_payload = self.state_store.list_subfleets()
        summary_envelope = {"summary": summary_payload, "metrics": self.metrics.snapshot(self.command_router.queue_depth())}
        subfleet_envelope = {"subfleets": subfleets_payload}
        self.metrics.payload_sizes.append(len(json.dumps(summary_envelope)) + len(json.dumps(subfleet_envelope)))
        socketio.emit("fleet_summary_update", summary_envelope)
        socketio.emit("subfleet_summary_update", subfleet_envelope)

        watched_units = self.event_publisher.watched_units()
        for sid, unit_id in watched_units.items():
            if unit_id in dirty_unit_ids:
                detail = self.state_store.get_generator_detail(unit_id, include_registers=True)
                if detail is not None:
                    payload = {"generator": detail}
                    self.metrics.payload_sizes.append(len(json.dumps(payload)))
                    socketio.emit("unit_detail_update", payload, to=sid)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_requested.set()
        if self._loop_started and not self._loop_stopped.wait(timeout=timeout):
            raise RuntimeError("Simulation loop did not stop before the timeout.")

    def simulation_loop(self, socketio: SocketIO) -> None:
        log.info("Simulation loop started")
        last_save = time.monotonic()
        self._loop_started = True
        try:
            while not self._stop_requested.is_set():
                started = time.monotonic()
                scenario_events = self.scenario_runner.advance()
                for event in scenario_events:
                    if event.action == "fleet_command":
                        cmd = event.params.get("cmd")
                        if cmd is not None:
                            try:
                                self.enqueue_fleet_command(cmd)
                            except ValueError as exc:
                                log.warning("Ignoring invalid scenario fleet command: %s", exc)
                    elif event.action == "unit_command":
                        uid = event.params.get("unit_id")
                        cmd = event.params.get("cmd")
                        if uid is not None and cmd is not None:
                            try:
                                self.enqueue_unit_command(int(uid), cmd)
                            except ValueError as exc:
                                log.warning("Ignoring invalid scenario unit command: %s", exc)
                self._execute_due_runbook_steps()
                dirty_unit_ids = self.simulation_step()
                self._advance_runbook_clock()
                self.emit_updates(socketio, dirty_unit_ids)
                if started - last_save >= self.settings.save_interval:
                    self.save_state()
                    last_save = started
                elapsed = time.monotonic() - started
                socketio.sleep(max(0.0, self.settings.tick_seconds - elapsed))
        finally:
            self._loop_stopped.set()
            log.info("Simulation loop stopped")


class AppController:
    def __init__(self, settings: Settings, runtime: SimulatorRuntime | None = None):
        self.settings = settings
        self.runtime = runtime
        self.modbus_server = None
        self._lock = Lock()

    @property
    def is_started(self) -> bool:
        return self.runtime is not None

    def _start_runtime(self, socketio: SocketIO) -> SimulatorRuntime:
        runtime = SimulatorRuntime(self.settings)
        modbus_server = ModbusServerGroup(
            runtime.modbus_context,
            num_generators=self.settings.num_generators,
            host=self.settings.modbus_host,
            port=self.settings.modbus_port,
        )
        modbus_server.start()
        try:
            socketio.start_background_task(runtime.simulation_loop, socketio)
        except Exception:
            modbus_server.stop()
            raise
        self.runtime = runtime
        self.modbus_server = modbus_server
        return runtime

    def _stop_runtime(self) -> None:
        runtime = self.runtime
        modbus_server = self.modbus_server
        self.runtime = None
        self.modbus_server = None
        try:
            if runtime is not None:
                runtime.stop()
                runtime.save_state()
        finally:
            if modbus_server is not None:
                modbus_server.stop()

    def configure_simulator(self, socketio: SocketIO, size_counts: dict[int, int], modbus_port: int) -> SimulatorRuntime:
        requested_ratings = _expand_generator_ratings(size_counts)
        endpoints = modbus_endpoints(len(requested_ratings), modbus_port)
        modbus_endpoints(len(requested_ratings), self.settings.public_modbus_port or modbus_port)
        with self._lock:
            previous_ratings = self.settings.generator_ratings
            previous_port = self.settings.modbus_port
            was_started = self.runtime is not None
            owned_ports = {
                item["port"] for item in modbus_endpoints(self.settings.num_generators, previous_port)
            } if was_started else set()
            for endpoint in endpoints:
                if endpoint["port"] not in owned_ports:
                    ensure_port_available(self.settings.modbus_host, endpoint["port"])
            if was_started:
                self._stop_runtime()
            self.settings.generator_ratings = requested_ratings
            self.settings.num_generators = len(requested_ratings)
            self.settings.modbus_port = modbus_port
            try:
                return self._start_runtime(socketio)
            except Exception:
                self.settings.generator_ratings = previous_ratings
                self.settings.num_generators = len(previous_ratings)
                self.settings.modbus_port = previous_port
                if was_started:
                    try:
                        self._start_runtime(socketio)
                    except Exception:
                        log.exception("Could not restore the previous fleet after startup failure")
                raise

    def start_simulator(self, socketio: SocketIO, size_counts: dict[int, int]) -> SimulatorRuntime:
        return self.configure_simulator(socketio, size_counts, self.settings.modbus_port)

    def save_state(self) -> None:
        if self.runtime is not None:
            self.runtime.save_state()


def _build_config_export_csv(settings: Settings, runtime: SimulatorRuntime | None) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "category", "field", "value", "notes"])
    writer.writerow(["simulator", "status", "configured", "yes" if runtime else "no", "Whether the simulator has been started"])
    writer.writerow(["simulator", "limits", "max_generators", settings.max_generators, "Configured fleet limit"])
    writer.writerow(["simulator", "web", "dashboard_url", f"http://{settings.web_host}:{settings.web_port}", "Browser UI endpoint"])
    writer.writerow(["modbus", "connection", "host", settings.modbus_host, "Default bind host for Modbus TCP"])
    writer.writerow(["modbus", "connection", "port", settings.modbus_port, "TCP port for Modbus master connections"])
    writer.writerow(["modbus", "connection", "function_codes", MODBUS_FUNCTION_CODES, "Supported Modbus function codes"])
    writer.writerow(["modbus", "connection", "polling_guidance", MODBUS_POLLING_GUIDANCE, "Recommended poll interval"])

    if runtime is not None:
        writer.writerow(["fleet", "summary", "generator_count", runtime.settings.num_generators, "Total configured generators"])
        for size_kw, count in _current_size_counts(runtime.settings.generator_ratings).items():
            writer.writerow(["fleet", "summary", f"size_{size_kw}_count", count, "Configured count for this size"])
        writer.writerow([])
        writer.writerow(["section", "unit_id", "name", "rated_kw", "subfleet", "notes"])
        for generator in runtime.generators:
            detail = runtime.state_store.get_generator_detail(generator.unit_id)
            writer.writerow(_csv_safe_row([
                "generator",
                generator.unit_id,
                generator.name,
                int(generator.rated_kw),
                detail["subfleet_id"] if detail else "",
                "Each unit exposes the same register map on its own Unit ID",
            ]))
        writer.writerow([])
        writer.writerow(["section", "subfleet_id", "name", "member_count", "member_ids"])
        for subfleet in runtime.state_store.list_subfleets():
            writer.writerow(_csv_safe_row([
                "subfleet",
                subfleet["id"],
                subfleet["name"],
                subfleet["unit_count"],
                " ".join(str(unit_id) for unit_id in subfleet["member_ids"]),
            ]))
    else:
        writer.writerow(["fleet", "summary", "generator_count", 0, "Start the simulator to export configured unit rows"])

    if runtime is not None:
        writer.writerow([])
        writer.writerow(["section", "generator_id", "modbus_port", "modbus_unit_id", "published_modbus_port"])
        for generator in runtime.generators:
            port, wire_unit = modbus_address(generator.unit_id, runtime.settings.modbus_port)
            public_port, _ = modbus_address(generator.unit_id, runtime.settings.public_modbus_port or runtime.settings.modbus_port)
            writer.writerow(["modbus_mapping", generator.unit_id, port, wire_unit, public_port])

    writer.writerow([])
    writer.writerow(["section", "register", "description", "data_type", "scaling", "engineering_range", "interaction_notes"])
    for register, description, data_type, scaling, engineering_range, notes in MODBUS_REGISTER_MAP:
        writer.writerow(["register_map", register, description, data_type, scaling, engineering_range, notes])
    writer.writerow([])
    writer.writerow(["section", "register", "bit", "alarm", "severity", "trigger_condition"])
    for register, bit, alarm, severity, trigger in MODBUS_ALARM_BITS:
        writer.writerow(["alarm_bits", register, bit, alarm, severity, trigger])
    writer.writerow([])
    writer.writerow(["section", "register", "command_value", "command_name", "description"])
    for command_value, command_name, description in MODBUS_COMMANDS:
        writer.writerow(["commands", 20, command_value, command_name, description])
    return output.getvalue()


def _parse_page_args() -> tuple[int, int]:
    page = max(1, _int_arg(request.args.get("page"), "page") or 1)
    page_size = min(MAX_PAGE_SIZE, max(1, _int_arg(request.args.get("page_size"), "page_size") or DEFAULT_PAGE_SIZE))
    return page, page_size


def _query_arg(name: str) -> str | None:
    value = request.args.get(name)
    return value if not _is_blank(value) else None


def _parse_generator_filters(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    source = payload if payload is not None else request.args
    get = source.get
    subfleet_id = get("subfleet_id")
    context_subfleet_id = get("context_subfleet_id")
    rated_kw = get("rated_kw")
    auto_mode = get("auto_mode")
    subfleet_scope = get("subfleet_scope")
    return {
        "state": get("state") or None,
        "alarmed_only": str(get("alarmed", "0")) == "1",
        "search": get("search") or None,
        "subfleet_id": str(subfleet_id) if not _is_blank(subfleet_id) else None,
        "context_subfleet_id": str(context_subfleet_id) if not _is_blank(context_subfleet_id) else None,
        "rated_kw": _int_arg(str(rated_kw), "rated_kw") if not _is_blank(rated_kw) else None,
        "auto_mode": _bool_arg(str(auto_mode)) if not _is_blank(auto_mode) else None,
        "subfleet_scope": _subfleet_scope_arg(str(subfleet_scope)) if not _is_blank(subfleet_scope) else "any",
    }


def _json_object_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object.")
    return payload


def _required_unit_id_list(payload: dict[str, Any], field_name: str = "unit_ids") -> list[int]:
    if field_name not in payload:
        raise ValueError(f"{field_name} is required.")
    raw_unit_ids = payload[field_name]
    if not isinstance(raw_unit_ids, list):
        raise ValueError(f"{field_name} must be an array.")
    if not raw_unit_ids:
        raise ValueError(f"{field_name} must include at least one generator ID.")

    unit_ids: list[int] = []
    for raw_unit_id in raw_unit_ids:
        if isinstance(raw_unit_id, bool):
            raise ValueError(f"{field_name} must contain whole-number generator IDs.")
        if isinstance(raw_unit_id, int):
            unit_id = raw_unit_id
        elif isinstance(raw_unit_id, float) and raw_unit_id.is_integer():
            unit_id = int(raw_unit_id)
        elif isinstance(raw_unit_id, str) and raw_unit_id.strip().isdecimal():
            unit_id = int(raw_unit_id)
        else:
            raise ValueError(f"{field_name} must contain whole-number generator IDs.")
        if unit_id < 1:
            raise ValueError(f"{field_name} must contain positive generator IDs.")
        unit_ids.append(unit_id)
    return unit_ids


def create_app(settings: Settings | None = None, runtime: SimulatorRuntime | None = None):
    settings = settings or Settings()
    controller = AppController(settings, runtime=runtime)
    asset_root = Path(__file__).resolve().parent
    if not (asset_root / "templates" / "dashboard.html").is_file():
        asset_root = Path(sysconfig.get_path("data")) / "share" / "generator-fleet-simulator"
    app = Flask(__name__, template_folder=str(asset_root / "templates"),
                static_folder=str(asset_root / "static"))
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["MAX_CONTENT_LENGTH"] = MAX_RUNBOOK_REQUEST_BYTES
    app.config["SIMULATOR_CONTROLLER"] = controller
    socketio = SocketIO(app, cors_allowed_origins=settings.cors_allowed_origins, async_mode="threading")
    rate_limiter = SlidingWindowRateLimiter(settings.http_rate_limit, settings.http_rate_window)

    # Wrap Socket.IO too: Flask hooks alone do not cover its WSGI endpoint.
    downstream = app.wsgi_app
    allowed_names = {name.lower() for name in ("localhost", settings.web_host, settings.public_web_host) if name}
    allowed_names.update(name.strip().lower() for name in os.getenv("GENSIM_TRUSTED_HOSTS", "").split(",") if name.strip())

    def validate_host(environ, start_response):
        try:
            parts = urlsplit("http://" + WSGIRequest(environ).host)
            if parts.username is not None or parts.password is not None or parts.path or parts.query or parts.fragment:
                raise ValueError("Invalid Host header")
            host = parts.hostname or ""
            try:
                ipaddress.ip_address(host)
                trusted = True
            except ValueError:
                trusted = host.lower() in allowed_names
        except (ValueError, SecurityError):
            trusted = False
        if not trusted:
            return Response("Untrusted Host header.", status=400)(environ, start_response)
        return downstream(environ, start_response)

    app.wsgi_app = validate_host

    @app.before_request
    def reject_cross_site_mutations():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        expected_origin = request.host_url.rstrip("/")
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return {"ok": False, "error": "Cross-site requests are not allowed."}, 403
        if origin is not None and origin != expected_origin:
            return {"ok": False, "error": "Cross-site requests are not allowed."}, 403
        if origin is None and referer:
            try:
                parts = urlsplit(referer)
                referer_origin = f"{parts.scheme}://{parts.netloc}"
            except ValueError:
                referer_origin = ""
            if referer_origin != expected_origin:
                return {"ok": False, "error": "Cross-site requests are not allowed."}, 403
        return None

    @app.errorhandler(RequestEntityTooLarge)
    def request_entity_too_large(_exc):
        return {"ok": False, "error": "Request body is too large."}, 413

    def _audit(action: str, **details: Any) -> None:
        request_id = getattr(g, "request_id", "-")
        remote = _client_rate_key()
        extras = " ".join(f"{key}={value}" for key, value in details.items() if value is not None)
        log.info("audit request_id=%s remote=%s action=%s%s", request_id, remote, action, f" {extras}" if extras else "")

    def _enforce_rate_limit(scope: str) -> bool:
        return rate_limiter.allow(f"{scope}:{_client_rate_key()}")

    @app.before_request
    def bind_request_context():
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        if request.method in MUTATING_HTTP_METHODS:
            if not _enforce_rate_limit("http"):
                return {"ok": False, "error": "Too many requests."}, 429
            _audit(f"{request.method} {request.path}")

    @app.after_request
    def apply_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers["X-Request-ID"] = getattr(g, "request_id", uuid.uuid4().hex)
        return response

    def _readiness_payload() -> dict[str, Any]:
        active_settings = controller.runtime.settings if controller.is_started else settings
        modbus_running = bool(controller.modbus_server and controller.modbus_server.is_running)
        simulation_running = bool(
            controller.is_started
            and controller.runtime._loop_started
            and not controller.runtime._loop_stopped.is_set()
        )
        ready = simulation_running and modbus_running
        return {
            "ok": ready,
            "ready": ready,
            "simulation_running": simulation_running,
            "configured": controller.is_started,
            "web_host": active_settings.web_host,
            "web_port": active_settings.web_port,
            "modbus_host": active_settings.modbus_host,
            "modbus_port": active_settings.modbus_port,
            "modbus_endpoints": modbus_endpoints(active_settings.num_generators, active_settings.modbus_port) if controller.is_started else [],
            "modbus_running": modbus_running,
            "num_generators": controller.runtime.settings.num_generators if controller.is_started else 0,
            "last_save_error": controller.runtime.last_save_error if controller.is_started else None,
        }

    def _reject_socket(message: str) -> None:
        socketio.emit("command_rejected", {"error": message}, to=request.sid)

    def _socket_payload(data: Any, event_name: str) -> dict[str, Any] | None:
        if isinstance(data, dict):
            return data
        _reject_socket(f"{event_name} payload must be an object.")
        return None

    def _allow_socket_command(event_name: str) -> bool:
        if not _enforce_rate_limit("socket"):
            _reject_socket("Too many requests.")
            return False
        _audit(f"socket {event_name}")
        return True

    @app.route("/")
    def dashboard():
        active_settings = controller.runtime.settings if controller.is_started else settings
        dashboard_hostport = request.host
        request_host_name = dashboard_hostport.rsplit(":", 1)[0] if ":" in dashboard_hostport else dashboard_hostport
        if active_settings.public_web_host:
            dashboard_port = active_settings.public_web_port or active_settings.web_port
            dashboard_hostport = f"{active_settings.public_web_host}:{dashboard_port}"
            request_host_name = active_settings.public_web_host
        public_modbus_host = active_settings.public_modbus_host or request_host_name
        return render_template(
            "dashboard.html",
            num_generators=active_settings.num_generators if controller.is_started else 0,
            suggested_num_generators=active_settings.num_generators,
            generator_size_options=GENERATOR_SIZE_OPTIONS,
            suggested_size_counts=_current_size_counts(active_settings.generator_ratings),
            simulator_configured=controller.is_started,
            max_generators=settings.max_generators,
            web_port=settings.web_port,
            modbus_port=active_settings.modbus_port,
            modbus_units_per_port=UNITS_PER_PORT,
            dashboard_hostport=dashboard_hostport,
            public_modbus_host=public_modbus_host,
            public_modbus_port=active_settings.public_modbus_port or active_settings.modbus_port,
        )

    @app.route("/favicon.ico")
    def favicon():
        svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#09090b"/>
<path d="M16 36h8l5-16 7 28 5-12h7" fill="none" stroke="#22c55e" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="48" cy="36" r="4" fill="#6366f1"/>
</svg>"""
        return Response(svg, mimetype="image/svg+xml")

    @app.route("/api/health")
    def api_health():
        active_settings = controller.runtime.settings if controller.is_started else settings
        active_runbook = controller.runtime.runbook_status() if controller.is_started else None
        return {
            "ok": True,
            "configured": controller.is_started,
            "web_host": active_settings.web_host,
            "web_port": active_settings.web_port,
            "modbus_host": active_settings.modbus_host,
            "modbus_port": active_settings.modbus_port,
            "modbus_endpoints": modbus_endpoints(active_settings.num_generators, active_settings.modbus_port) if controller.is_started else [],
            "modbus_running": bool(controller.modbus_server and controller.modbus_server.is_running),
            "num_generators": controller.runtime.settings.num_generators if controller.is_started else 0,
            "active_scenario": controller.runtime.active_scenario_id() if controller.is_started else None,
            "active_runbook": active_runbook["id"] if active_runbook else None,
            "last_save_error": controller.runtime.last_save_error if controller.is_started else None,
        }

    @app.route("/api/live")
    def api_live():
        return {"ok": True, "live": True}

    @app.route("/api/ready")
    def api_ready():
        payload = _readiness_payload()
        status = 200 if payload["ready"] else 503
        return payload, status

    @app.route("/api/modbus/endpoints")
    def api_modbus_endpoints():
        active = controller.runtime.settings if controller.is_started else settings
        endpoints = modbus_endpoints(active.num_generators, active.modbus_port) if controller.is_started else []
        public_base = active.public_modbus_port or active.modbus_port
        for endpoint in endpoints:
            endpoint["public_port"] = public_base + endpoint["port"] - active.modbus_port
        return {"configured": controller.is_started, "units_per_port": UNITS_PER_PORT,
                "host": active.public_modbus_host or urlsplit(request.host_url).hostname,
                "endpoints": endpoints}

    @app.route("/api/state")
    def api_state():
        if not controller.is_started:
            return {"configured": False, "generators": []}
        return {
            "configured": True,
            "num_generators": controller.runtime.settings.num_generators,
            "summary": controller.runtime.state_store.get_summary(),
            "generators": controller.runtime.snapshot_state(),
            "subfleets": controller.runtime.state_store.list_subfleets(),
        }

    @app.route("/api/fleet/summary")
    def api_fleet_summary():
        if not controller.is_started:
            return {"configured": False, "summary": None, "metrics": None}
        runtime = controller.runtime
        return {
            "configured": True,
            "summary": runtime.state_store.get_summary(),
            "metrics": runtime.metrics.snapshot(runtime.command_router.queue_depth()),
        }

    @app.route("/api/scada/topology")
    def api_scada_topology():
        if not controller.is_started:
            return {"configured": False, "root": None, "selected_node": None, "breadcrumbs": [], "children": []}
        node_id = request.args.get("node_id", "fleet")
        try:
            range_size = min(
                SCADA_MAX_RANGE_SIZE,
                max(1, int(request.args.get("range_size", str(SCADA_DEFAULT_RANGE_SIZE)))),
            )
        except ValueError:
            return {"configured": True, "ok": False, "error": "range_size must be a whole number."}, 400
        payload = _build_scada_topology(controller.runtime.state_store, node_id=node_id, range_size=range_size)
        if payload is None:
            return {"configured": True, "ok": False, "error": "SCADA topology node not found."}, 404
        return {"configured": True, "ok": True, **payload}

    @app.route("/api/scada/alarms")
    def api_scada_alarms():
        if not controller.is_started:
            return {"configured": False, "ok": False, "node_id": "fleet", "alarm_count": 0, "alarms": []}
        node_id = request.args.get("node_id", "fleet")
        alarms = _scada_alarm_records(controller.runtime.state_store, node_id)
        if alarms is None:
            return {"configured": True, "ok": False, "error": "SCADA topology node not found."}, 404
        try:
            max_rows = min(500, max(1, int(request.args.get("limit", "200"))))
        except ValueError:
            return {"configured": True, "ok": False, "error": "limit must be a whole number."}, 400
        return {
            "configured": True,
            "ok": True,
            "node_id": node_id,
            "alarm_count": len(alarms),
            "alarms": alarms[:max_rows],
            "truncated": len(alarms) > max_rows,
            "limit": max_rows,
        }

    @app.route("/api/fleet/generators")
    def api_fleet_generators():
        if not controller.is_started:
            return {"configured": False, "items": [], "page": 1, "page_size": DEFAULT_PAGE_SIZE, "total_items": 0, "total_pages": 1}
        try:
            page, page_size = _parse_page_args()
            filters = _parse_generator_filters()
        except ValueError as exc:
            return {"configured": True, "ok": False, "error": str(exc)}, 400
        runtime = controller.runtime
        data = runtime.state_store.list_generators(
            page=page,
            page_size=page_size,
            sort=request.args.get("sort", "unit_id"),
            descending=request.args.get("direction", "asc") == "desc",
            **filters,
        )
        data["configured"] = True
        return data

    @app.route("/api/fleet/generator-search")
    def api_fleet_generator_search():
        if not controller.is_started:
            return {"configured": False, "items": [], "total_items": 0, "limit": 0}
        runtime = controller.runtime
        try:
            filters = _parse_generator_filters()
            limit = min(200, max(1, _int_arg(request.args.get("limit"), "limit") or 50))
        except ValueError as exc:
            return {"configured": True, "ok": False, "items": [], "total_items": 0, "limit": 0, "error": str(exc)}, 400
        items = runtime.state_store.find_generators(**filters)
        items.sort(key=lambda item: item["unit_id"])
        return {
            "configured": True,
            "items": items[:limit],
            "total_items": len(items),
            "limit": limit,
            "filters": filters,
        }

    @app.route("/api/generators/<int:unit_id>")
    def api_generator_detail(unit_id: int):
        if not controller.is_started:
            return {"configured": False, "generator": None, "error": "Simulator is not started."}, 404
        detail = controller.runtime.state_store.get_generator_detail(unit_id)
        if detail is None:
            return {
                "configured": True,
                "unit_id": unit_id,
                "generator": None,
                "error": f"Generator unit {unit_id} was not found.",
            }, 404
        return {"configured": True, "generator": detail}

    @app.route("/api/generators/<int:unit_id>/registers")
    def api_generator_registers(unit_id: int):
        if not controller.is_started:
            return {"configured": False, "registers": None, "error": "Simulator is not started."}, 404
        registers = controller.runtime.state_store.get_registers(unit_id)
        if registers is None:
            return {
                "configured": True,
                "unit_id": unit_id,
                "registers": None,
                "error": f"Generator unit {unit_id} was not found.",
            }, 404
        port, wire_unit = modbus_address(unit_id, controller.runtime.settings.modbus_port)
        public_port, _ = modbus_address(unit_id, controller.runtime.settings.public_modbus_port or controller.runtime.settings.modbus_port)
        return {"configured": True, "unit_id": unit_id, "registers": registers,
                "modbus": {"port": port, "public_port": public_port, "unit_id": wire_unit}}

    @app.route("/api/subfleets")
    def api_subfleets():
        if not controller.is_started:
            return {"configured": False, "subfleets": []}
        return {"configured": True, "subfleets": controller.runtime.state_store.list_subfleets()}

    @app.route("/api/subfleets", methods=["POST"])
    def api_create_subfleet():
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        try:
            payload = _json_object_payload()
            subfleet = controller.runtime.create_subfleet(str(payload.get("name", "")))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "subfleet": subfleet}, 201

    @app.route("/api/subfleets/<subfleet_id>", methods=["PATCH"])
    def api_rename_subfleet(subfleet_id: str):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        try:
            payload = _json_object_payload()
            subfleet = controller.runtime.rename_subfleet(subfleet_id, str(payload.get("name", "")))
        except KeyError:
            return {"ok": False, "error": "Sub-fleet not found."}, 404
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "subfleet": subfleet}

    @app.route("/api/subfleets/<subfleet_id>", methods=["DELETE"])
    def api_delete_subfleet(subfleet_id: str):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        if not controller.runtime.delete_subfleet(subfleet_id):
            return {"ok": False, "error": "Sub-fleet not found."}, 404
        return {"ok": True}

    @app.route("/api/subfleets/<subfleet_id>/members", methods=["POST"])
    def api_assign_subfleet_members(subfleet_id: str):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        try:
            payload = _json_object_payload()
            unit_ids = _required_unit_id_list(payload)
            result = controller.runtime.assign_generators_to_subfleet(subfleet_id, unit_ids)
        except KeyError:
            return {"ok": False, "error": "Sub-fleet not found."}, 404
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, **result}

    @app.route("/api/subfleets/<subfleet_id>/members/query", methods=["POST"])
    def api_assign_subfleet_members_by_query(subfleet_id: str):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        try:
            payload = _json_object_payload()
            filters = _parse_generator_filters(payload)
            filters.pop("subfleet_id", None)
            filters["context_subfleet_id"] = subfleet_id
            result = controller.runtime.state_store.assign_generators_by_query(subfleet_id, **filters)
        except KeyError:
            return {"ok": False, "error": "Sub-fleet not found."}, 404
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, **result}

    @app.route("/api/subfleets/<subfleet_id>/members/<int:unit_id>", methods=["DELETE"])
    def api_remove_subfleet_member(subfleet_id: str, unit_id: int):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        if not controller.runtime.unassign_generator_from_subfleet(subfleet_id, unit_id):
            return {"ok": False, "error": "Sub-fleet membership not found."}, 404
        return {"ok": True}

    @app.route("/api/subfleets/<subfleet_id>/members/query-remove", methods=["POST"])
    def api_remove_subfleet_members_by_query(subfleet_id: str):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        try:
            payload = _json_object_payload()
            filters = _parse_generator_filters(payload)
            filters.pop("subfleet_id", None)
            filters["context_subfleet_id"] = subfleet_id
            result = controller.runtime.state_store.unassign_generators_by_query(
                subfleet_id,
                state=filters["state"],
                alarmed_only=filters["alarmed_only"],
                search=filters["search"],
                rated_kw=filters["rated_kw"],
                auto_mode=filters["auto_mode"],
            )
        except KeyError:
            return {"ok": False, "error": "Sub-fleet not found."}, 404
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, **result}

    @app.route("/api/subfleets/<subfleet_id>/commands", methods=["POST"])
    def api_subfleet_command(subfleet_id: str):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        try:
            payload = _json_object_payload()
            command = _normalize_command(payload.get("cmd"))
            result = controller.runtime.enqueue_subfleet_command(subfleet_id, command)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        except KeyError:
            return {"ok": False, "error": "Sub-fleet not found."}, 404
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}, 429
        return {"ok": True, **result}

    @app.route("/api/startup", methods=["POST"])
    def api_startup():
        if not request.is_json:
            return {"ok": False, "error": "JSON request body is required."}, 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not payload:
            return {"ok": False, "error": "Startup configuration is required."}, 400
        if "size_counts" not in payload:
            return {"ok": False, "error": "size_counts is required."}, 400
        try:
            size_counts = _normalize_generator_size_counts(
                payload.get("size_counts"),
                fallback_total=settings.num_generators,
                maximum=settings.max_generators,
            )
            modbus_port = _normalize_tcp_port(payload.get("modbus_port"), settings.modbus_port)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        try:
            runtime = controller.configure_simulator(socketio, size_counts, modbus_port)
        except (RuntimeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {
            "ok": True,
            "configured": True,
            "num_generators": runtime.settings.num_generators,
            "size_counts": size_counts,
            "modbus_port": runtime.settings.modbus_port,
            "modbus_endpoints": modbus_endpoints(runtime.settings.num_generators, runtime.settings.modbus_port),
        }

    @app.route("/api/scenarios")
    def api_scenarios():
        if not controller.is_started:
            return {"ok": True, "configured": False, "catalog": scenario_catalog_payload(), "active_run": None, "history": []}
        return {"ok": True, "configured": True, **controller.runtime.scenario_status()}

    @app.route("/api/scenarios/run", methods=["POST"])
    def api_run_scenario():
        if not controller.is_started:
            return {"ok": False, "error": "Simulator not started."}, 400
        try:
            payload = _json_object_payload()
            scenario_id = str(payload.get("scenario_id", "")).strip()
            if not scenario_id:
                return {"ok": False, "error": "scenario_id is required."}, 400
            run = controller.runtime.start_scenario(scenario_id)
        except (ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "run": run}

    @app.route("/api/scenarios/stop", methods=["POST"])
    def api_stop_scenario():
        if not controller.is_started:
            return {"ok": False, "error": "Simulator not started."}, 400
        try:
            run = controller.runtime.stop_scenario()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "run": run}

    @app.route("/api/runbooks")
    def api_list_runbooks():
        registry = controller.runtime.runbook_registry if controller.is_started else None
        runbooks = registry.list_runbooks() if registry is not None else []
        active = controller.runtime.runbook_status() if controller.is_started else None
        last_report = controller.runtime.last_runbook_report() if controller.is_started else None
        return {
            "ok": True,
            "configured": controller.is_started,
            "runbooks": runbooks,
            "active_runbook": active,
            "last_runbook_report": last_report,
        }

    @app.route("/api/runbooks", methods=["POST"])
    def api_save_runbook():
        if request.content_length is not None and request.content_length > MAX_RUNBOOK_REQUEST_BYTES:
            return {"ok": False, "error": "Runbook request body is too large."}, 413
        payload = request.get_json(silent=True)
        if not payload:
            return {"ok": False, "error": "Request body is required."}, 400
        try:
            runbook = sanitize_saved_runbook(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        registry = controller.runtime.runbook_registry if controller.is_started else None
        if registry is None:
            return {"ok": False, "error": "Simulator is not started."}, 400
        try:
            saved = registry.save_runbook(runbook)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "runbook": saved}, 201

    @app.route("/api/runbooks/<runbook_id>/apply", methods=["POST"])
    def api_apply_runbook(runbook_id: str):
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        runbook = controller.runtime.runbook_registry.get_runbook(runbook_id)
        if runbook is None:
            return {"ok": False, "error": "Runbook not found."}, 404
        try:
            report = controller.runtime.start_runbook(runbook)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "runbook": report}

    @app.route("/api/runbooks/stop", methods=["POST"])
    def api_stop_runbook():
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        report = controller.runtime.stop_runbook()
        if report is None:
            return {"ok": False, "error": "No runbook is currently running."}, 400
        return {"ok": True, "runbook": report, "active_runbook": None, "last_runbook_report": report}

    @app.route("/api/export/runbook.json")
    def api_export_runbook():
        if not controller.is_started:
            return {"ok": False, "error": "Simulator is not started."}, 400
        report = controller.runtime.runbook_status() or controller.runtime.last_runbook_report()
        if report is None:
            return {"ok": False, "error": "No runbook report is available."}, 404
        runbook_id = _safe_attachment_name(report.get("id"), "runbook")
        return Response(
            json.dumps(report, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={runbook_id}-report.json"},
        )

    @app.route("/api/export/config.csv")
    def export_config_csv():
        csv_text = _build_config_export_csv(settings, controller.runtime)
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={CONFIG_EXPORT_FILENAME}"},
        )

    @app.route("/api/metrics")
    def api_metrics():
        if not controller.is_started:
            return {"configured": False, "metrics": None}
        runtime = controller.runtime
        return {"configured": True, "metrics": runtime.metrics.snapshot(runtime.command_router.queue_depth())}

    @socketio.on("connect")
    def on_connect():
        if controller.is_started:
            controller.runtime.event_publisher.connect(request.sid)
            controller.runtime.metrics.connected_clients = controller.runtime.event_publisher.count()
            socketio.emit(
                "fleet_summary_update",
                {
                    "summary": controller.runtime.state_store.get_summary(),
                    "metrics": controller.runtime.metrics.snapshot(controller.runtime.command_router.queue_depth()),
                },
                to=request.sid,
            )
            socketio.emit(
                "subfleet_summary_update",
                {"subfleets": controller.runtime.state_store.list_subfleets()},
                to=request.sid,
            )
        log.info("Client connected")

    @socketio.on("disconnect")
    def on_disconnect():
        if controller.is_started:
            controller.runtime.event_publisher.disconnect(request.sid)
            controller.runtime.metrics.connected_clients = controller.runtime.event_publisher.count()

    @socketio.on("watch_unit_detail")
    def on_watch_unit_detail(data):
        payload = _socket_payload(data, "watch_unit_detail")
        if payload is None:
            return
        if not controller.is_started:
            _reject_socket("Simulator is not started.")
            return
        raw_unit_id = payload.get("unit_id")
        if _is_blank(raw_unit_id):
            controller.runtime.event_publisher.watch_detail(request.sid, None)
            return
        try:
            unit_id = _int_arg(str(raw_unit_id), "unit_id")
        except ValueError as exc:
            _reject_socket(str(exc))
            return
        if unit_id is None:
            controller.runtime.event_publisher.watch_detail(request.sid, None)
            return
        detail = controller.runtime.state_store.get_generator_detail(unit_id, include_registers=True)
        if detail is None:
            _reject_socket("Invalid unit ID.")
            return
        controller.runtime.event_publisher.watch_detail(request.sid, unit_id)
        socketio.emit("unit_detail_update", {"generator": detail}, to=request.sid)

    @socketio.on("command")
    def on_command(data):
        if not _allow_socket_command("command"):
            return
        payload = _socket_payload(data, "command")
        if payload is None:
            return
        if not controller.is_started:
            _reject_socket("Simulator is not started.")
            return
        unit_id = payload.get("unit_id")
        command = payload.get("cmd")
        if _is_blank(unit_id) or _is_blank(command):
            _reject_socket("unit_id and cmd are required.")
            return
        try:
            parsed_unit_id = _int_arg(str(unit_id), "unit_id")
            if parsed_unit_id is None or not controller.runtime.enqueue_unit_command(parsed_unit_id, command):
                _reject_socket("Invalid unit ID.")
        except ValueError as exc:
            _reject_socket(str(exc))

    @socketio.on("bulk_command")
    def on_bulk_command(data):
        if not _allow_socket_command("bulk_command"):
            return
        payload = _socket_payload(data, "bulk_command")
        if payload is None:
            return
        if not controller.is_started:
            _reject_socket("Simulator is not started.")
            return
        raw_unit_ids = payload.get("unit_ids")
        command = payload.get("cmd")
        if _is_blank(command):
            _reject_socket("cmd is required.")
            return
        if not isinstance(raw_unit_ids, list):
            _reject_socket("unit_ids must be a list.")
            return
        try:
            controller.runtime.enqueue_unit_group_command(raw_unit_ids, command)
        except (RuntimeError, ValueError) as exc:
            _reject_socket(str(exc))

    @socketio.on("set_setpoint")
    def on_set_setpoint(data):
        if not _allow_socket_command("set_setpoint"):
            return
        payload = _socket_payload(data, "set_setpoint")
        if payload is None:
            return
        if not controller.is_started:
            _reject_socket("Simulator is not started.")
            return
        unit_id = payload.get("unit_id")
        setpoint_kw = payload.get("setpoint_kw")
        if _is_blank(unit_id) or _is_blank(setpoint_kw):
            _reject_socket("unit_id and setpoint_kw are required.")
            return
        try:
            parsed_unit_id = _int_arg(str(unit_id), "unit_id")
            parsed_setpoint_kw = _float_arg(setpoint_kw, "setpoint_kw")
        except ValueError as exc:
            _reject_socket(str(exc))
            return
        if parsed_unit_id is None or not controller.runtime.set_parallel_setpoint(parsed_unit_id, parsed_setpoint_kw):
            _reject_socket("Invalid unit ID.")

    @socketio.on("fleet_command")
    def on_fleet_command(data):
        if not _allow_socket_command("fleet_command"):
            return
        payload = _socket_payload(data, "fleet_command")
        if payload is None:
            return
        if not controller.is_started:
            _reject_socket("Simulator is not started.")
            return
        command = payload.get("cmd")
        if _is_blank(command):
            _reject_socket("cmd is required.")
            return
        try:
            if not controller.runtime.enqueue_fleet_command(command):
                _reject_socket("Command queue is full.")
        except ValueError as exc:
            _reject_socket(str(exc))

    @socketio.on("fleet_command_sequence")
    def on_fleet_command_sequence(data):
        if not _allow_socket_command("fleet_command_sequence"):
            return
        payload = _socket_payload(data, "fleet_command_sequence")
        if payload is None:
            return
        if not controller.is_started:
            _reject_socket("Simulator is not started.")
            return
        raw_commands = payload.get("commands")
        if not isinstance(raw_commands, list):
            _reject_socket("Command sequence must be a list.")
            return
        try:
            commands = tuple(int(command) for command in raw_commands)
        except (TypeError, ValueError):
            _reject_socket("Command sequence must contain integers.")
            return
        if commands != RESTORE_UTILITY_COMMAND_SEQUENCE:
            _reject_socket("Unsupported fleet command sequence.")
            return
        if controller.is_started and not controller.runtime.enqueue_fleet_command_sequence(commands):
            _reject_socket("Command queue is full.")

    return app, socketio, controller


def ensure_port_available(host: str, port: int) -> None:
    if port < 1 or port > 65535:
        raise RuntimeError(f"Port {host}:{port} is unavailable: port must be between 1 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except (OSError, OverflowError) as exc:
            raise RuntimeError(f"Port {host}:{port} is unavailable: {exc}") from exc


def run() -> None:
    try:
        settings = Settings()
        ensure_port_available(settings.web_host, settings.web_port)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc
    app, socketio, controller = create_app(settings=settings)

    def _shutdown_handler(signum, frame):
        log.info("Shutdown signal received; saving generator state before exit")
        controller.save_state()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    log.info("Starting web dashboard on http://%s:%s", settings.web_host, settings.web_port)
    log.info("Simulator will start after UI configuration; Modbus reserved for %s:%s", settings.modbus_host, settings.modbus_port)
    try:
        socketio.run(
            app,
            host=settings.web_host,
            port=settings.web_port,
            debug=False,
            allow_unsafe_werkzeug=True,
        )
    finally:
        log.info("Saving generator state on exit")
        controller.save_state()


if __name__ == "__main__":
    run()
