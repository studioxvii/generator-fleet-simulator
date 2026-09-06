#!/usr/bin/env python3
"""Live feature-by-feature launch gate for Generator Fleet Simulator."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = REPO_ROOT / "main.py"

P2_COUNTS = {"500": 2, "1000": 2, "1500": 2, "2000": 2, "2500": 2}
P3_COUNTS = {"500": 10, "1000": 10, "1500": 10, "2000": 10, "2500": 10}
P4_COUNTS = {"500": 400, "1000": 400, "1500": 400, "2000": 400, "2500": 400}

CMD_START = 1
CMD_UTILITY_FAIL = 10
CMD_RESTORE_UTILITY = 11

BUILTIN_SCENARIOS = {
    "utility-fail-recovery",
    "fault-and-reset",
    "parallel-mode-demo",
    "e-stop-drill",
}


class GateFailure(RuntimeError):
    """Raised when a live feature check fails."""


@dataclass
class Feature:
    id: str
    title: str
    qaqc: str
    fn: Callable[["Gate"], dict[str, Any]]
    needs_configured: bool = False
    profile: str = "p2"


@dataclass
class Result:
    id: str
    title: str
    status: str
    detail: str
    elapsed_s: float
    qaqc: str


@dataclass
class Gate:
    base_url: str
    modbus_host: str
    modbus_port: int
    timeout: float
    profile: str
    size_counts: dict[str, int]
    work_dir: Path
    process: subprocess.Popen[str] | None = None
    configured: bool = False
    subfleet_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed = body
            return exc.code, parsed
        except (urllib.error.URLError, OSError) as exc:
            raise GateFailure(f"{method} {path} failed: {exc}") from exc

    def request_text(self, method: str, path: str) -> tuple[int, str, dict[str, str]]:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(url, method=method, headers={"Accept": "text/html,*/*"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                headers = {key: value for key, value in response.headers.items()}
                return response.status, response.read().decode("utf-8", errors="replace"), headers
        except urllib.error.HTTPError as exc:
            headers = {key: value for key, value in exc.headers.items()} if exc.headers else {}
            return exc.code, exc.read().decode("utf-8", errors="replace"), headers
        except (urllib.error.URLError, OSError) as exc:
            raise GateFailure(f"{method} {path} failed: {exc}") from exc

    def wait_live(self, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout or self.timeout)
        last = None
        while time.monotonic() < deadline:
            try:
                status, payload = self.request_json("GET", "/api/live", timeout=2.0)
                if status == 200 and isinstance(payload, dict) and payload.get("live") is True:
                    return
                last = f"HTTP {status}: {payload}"
            except GateFailure as exc:
                last = str(exc)
            time.sleep(0.2)
        raise GateFailure(f"web process did not become live: {last}")

    def wait_ready(self, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or max(self.timeout, 30.0))
        last = None
        while time.monotonic() < deadline:
            status, payload = self.request_json("GET", "/api/ready")
            if status == 200 and isinstance(payload, dict) and payload.get("ready") is True:
                return payload
            last = f"HTTP {status}: {payload}"
            time.sleep(0.25)
        raise GateFailure(f"simulator did not become ready: {last}")

    def wait_units(
        self,
        unit_ids: list[int],
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 20.0,
        label: str,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        last: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            last = []
            ok = True
            for unit_id in unit_ids:
                status, payload = self.request_json("GET", f"/api/generators/{unit_id}")
                if status != 200 or not isinstance(payload, dict):
                    ok = False
                    break
                last.append(payload)
                if not predicate(payload):
                    ok = False
            if ok and last:
                return last
            time.sleep(0.4)
        raise GateFailure(f"{label}: last={_compact_units(last)}")

    def modbus_read(self, *, unit_id: int, address: int, count: int = 1) -> list[int]:
        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient(self.modbus_host, port=self.modbus_port, timeout=self.timeout)
        try:
            if not client.connect():
                raise GateFailure(f"Modbus connect failed at {self.modbus_host}:{self.modbus_port}")
            result = client.read_holding_registers(address=address, count=count, slave=unit_id)
            if result.isError():
                raise GateFailure(f"Modbus read error unit={unit_id} addr={address}: {result}")
            return [int(value) for value in result.registers]
        finally:
            client.close()

    def modbus_write(self, *, unit_id: int, address: int, value: int) -> None:
        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient(self.modbus_host, port=self.modbus_port, timeout=self.timeout)
        try:
            if not client.connect():
                raise GateFailure(f"Modbus connect failed at {self.modbus_host}:{self.modbus_port}")
            result = client.write_register(address, value, slave=unit_id)
            if result.isError():
                raise GateFailure(f"Modbus write error unit={unit_id} addr={address}: {result}")
        finally:
            client.close()


def _compact_units(payloads: list[dict[str, Any]]) -> str:
    parts = []
    for payload in payloads:
        gen = payload.get("generator") if isinstance(payload.get("generator"), dict) else payload
        parts.append(f"{gen.get('unit_id')}:{gen.get('state')}")
    return ",".join(parts) or "none"


def _generator(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("generator"), dict):
        return payload["generator"]
    return payload


UTILITY_PHASE_LOSS = "Utility Phase Loss"


def _breaker_closed(value: Any) -> bool:
    return value in (True, 1, "1")


def _has_named_alarm(gen: dict[str, Any], name: str) -> bool:
    alarms = gen.get("alarms") or {}
    if isinstance(alarms, dict):
        return name in alarms.values() or name in alarms
    if isinstance(alarms, (list, tuple, set)):
        return name in alarms
    return False


def utility_fail_observed(payload: dict[str, Any]) -> bool:
    gen = _generator(payload)
    return (not _breaker_closed(gen.get("utility_breaker"))) and _has_named_alarm(gen, UTILITY_PHASE_LOSS)


def utility_restore_observed(payload: dict[str, Any]) -> bool:
    gen = _generator(payload)
    if _has_named_alarm(gen, UTILITY_PHASE_LOSS):
        return False
    if _breaker_closed(gen.get("gen_breaker")):
        return True
    return _breaker_closed(gen.get("utility_breaker"))


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise GateFailure(message)


FEATURES: list[Feature] = []


def feature(id: str, title: str, qaqc: str, *, needs_configured: bool = False, profile: str = "p2"):
    def decorator(fn: Callable[[Gate], dict[str, Any]]) -> Callable[[Gate], dict[str, Any]]:
        FEATURES.append(
            Feature(id=id, title=title, qaqc=qaqc, fn=fn, needs_configured=needs_configured, profile=profile)
        )
        return fn

    return decorator


@feature("F01", "MIT startup needs no license acceptance", "ENV-001")
def f01_license_gate(gate: Gate) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("ACCEPT_LICENSE", None)
    env["GENSIM_WEB_PORT"] = "5199"
    env["GENSIM_MODBUS_PORT"] = "5299"
    completed = subprocess.run(
        [sys.executable, "-c",
         "import wsgi; assert wsgi.app.test_client().get('/api/live').status_code == 200"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    combined = completed.stdout + completed.stderr
    _expect(completed.returncode == 0, f"MIT startup failed: {combined}")
    return {"exit": completed.returncode}


@feature("F02", "Unconfigured health, live, and ready", "ENV-002 ENV-003 APIUX-001")
def f02_unconfigured_health(gate: Gate) -> dict[str, Any]:
    live_status, live = gate.request_json("GET", "/api/live")
    _expect(live_status == 200 and live.get("live") is True, f"live={live_status} {live}")
    ready_status, ready = gate.request_json("GET", "/api/ready")
    _expect(ready_status == 503, f"ready should be 503 before startup, got {ready_status} {ready}")
    _expect(isinstance(ready, dict) and ready.get("ready") is False, f"ready payload {ready}")
    health_status, health = gate.request_json("GET", "/api/health")
    _expect(health_status == 200 and health.get("configured") is False, f"health {health}")
    state_status, state = gate.request_json("GET", "/api/state")
    _expect(state_status == 200 and state.get("configured") is False, f"state {state}")
    _expect(state.get("generators") == [], f"unconfigured generators {state.get('generators')}")
    return {"health": health, "ready": ready}


@feature("F03", "Dashboard shell includes SCADA and local Socket.IO", "ENV-004 ENV-010 TAB-001 START-001")
def f03_dashboard_shell(gate: Gate) -> dict[str, Any]:
    status, body, _headers = gate.request_text("GET", "/")
    _expect(status == 200, f"dashboard HTTP {status}")
    required = [
        'id="startup-overlay"',
        'role="dialog"',
        'aria-modal="true"',
        'src="/static/vendor/socket.io.min.js"',
        'data-tab="status"',
        'data-tab="subfleets"',
        'data-tab="scada"',
        'id="tab-scada"',
        'data-tab="runbooks"',
        'data-tab="modbus"',
        'data-tab="howto"',
    ]
    missing = [item for item in required if item not in body]
    _expect(not missing, f"dashboard missing {missing}")
    _expect("cdnjs.cloudflare.com" not in body, "dashboard still references Socket.IO CDN")
    fav_status, fav_body, _fav_headers = gate.request_text("GET", "/favicon.ico")
    _expect(fav_status == 200 and "<svg" in fav_body.lower(), "favicon is not SVG")
    sock_status, sock_body, _sock_headers = gate.request_text("GET", "/static/vendor/socket.io.min.js")
    _expect(sock_status == 200 and "Socket.IO" in sock_body and len(sock_body) > 10_000, "socket.io asset incomplete")
    return {"bytes": len(body)}


@feature("F04", "CSP and request id headers", "PR-60")
def f04_hardening_headers(gate: Gate) -> dict[str, Any]:
    status, _body, headers = gate.request_text("GET", "/")
    _expect(status == 200, f"dashboard HTTP {status}")
    csp = headers.get("Content-Security-Policy") or headers.get("content-security-policy") or ""
    request_id = headers.get("X-Request-ID") or headers.get("X-Request-Id") or headers.get("x-request-id") or ""
    _expect("default-src 'self'" in csp, f"CSP missing default-src 'self': {csp!r}")
    _expect(bool(request_id), f"missing X-Request-ID in {sorted(headers)}")
    return {"csp": csp, "request_id": request_id}


@feature("F05", "Startup rejects zero, decimal, and over-max fleets", "START-006 START-008 START-009 START-011")
def f05_startup_validation(gate: Gate) -> dict[str, Any]:
    zero = {"size_counts": {"500": 0, "1000": 0, "1500": 0, "2000": 0, "2500": 0}, "modbus_port": gate.modbus_port}
    status, payload = gate.request_json("POST", "/api/startup", payload=zero)
    _expect(status == 400, f"zero total HTTP {status} {payload}")
    decimal = {"size_counts": {"500": 1.5, "1000": 0, "1500": 0, "2000": 0, "2500": 0}, "modbus_port": gate.modbus_port}
    status, payload = gate.request_json("POST", "/api/startup", payload=decimal)
    _expect(status == 400, f"decimal HTTP {status} {payload}")
    over = {"size_counts": {"500": 2001, "1000": 0, "1500": 0, "2000": 0, "2500": 0}, "modbus_port": gate.modbus_port}
    status, payload = gate.request_json("POST", "/api/startup", payload=over)
    _expect(status == 400, f"over-max HTTP {status} {payload}")
    health_status, health = gate.request_json("GET", "/api/health")
    _expect(health_status == 200 and health.get("configured") is False, f"validation must not configure: {health}")
    return {"kept_unconfigured": True}


@feature("F06", "Mixed P2 fleet starts", "START-013")
def f06_start_p2(gate: Gate) -> dict[str, Any]:
    payload = {"size_counts": gate.size_counts, "modbus_port": gate.modbus_port}
    started = time.monotonic()
    status, body = gate.request_json("POST", "/api/startup", payload=payload, timeout=max(gate.timeout, 30.0))
    elapsed = time.monotonic() - started
    _expect(status == 200 and body.get("ok") is True, f"startup HTTP {status} {body}")
    expected = sum(int(value) for value in gate.size_counts.values())
    _expect(int(body.get("num_generators", 0)) == expected, f"count {body}")
    gate.configured = True
    return {"num_generators": body["num_generators"], "startup_s": round(elapsed, 3)}


@feature("F07", "Ready after startup", "ENV-002", needs_configured=True)
def f07_ready(gate: Gate) -> dict[str, Any]:
    ready = gate.wait_ready()
    _expect(ready.get("modbus_running") is True, f"modbus not running: {ready}")
    _expect(int(ready.get("num_generators", 0)) == sum(int(v) for v in gate.size_counts.values()), f"ready {ready}")
    return ready


@feature("F08", "Fleet summary matches configured count", "HEAD-002 APIUX-002 APIUX-008", needs_configured=True)
def f08_summary(gate: Gate) -> dict[str, Any]:
    status, summary = gate.request_json("GET", "/api/fleet/summary")
    _expect(status == 200 and summary.get("configured") is True, f"summary {status} {summary}")
    counts = summary.get("summary") or {}
    expected = sum(int(value) for value in gate.size_counts.values())
    _expect(int(counts.get("generator_count", -1)) == expected, f"generator_count {counts}")
    _expect(int(counts.get("running_count", -1)) == 0, f"fresh fleet should be stopped: {counts}")
    metrics_status, metrics = gate.request_json("GET", "/api/metrics")
    _expect(metrics_status == 200 and metrics.get("configured") is True, f"metrics {metrics}")
    _expect(isinstance(metrics.get("metrics"), dict), f"metrics payload {metrics}")
    return {"summary": counts, "metrics": metrics["metrics"]}


@feature("F09", "Generators page, filter, and search", "FLT-001 FLT-003 FLT-005 TABLE-002 TABLE-004", needs_configured=True)
def f09_generators(gate: Gate) -> dict[str, Any]:
    expected = sum(int(value) for value in gate.size_counts.values())
    status, page = gate.request_json("GET", "/api/fleet/generators?page=1&page_size=50")
    _expect(status == 200, f"generators HTTP {status} {page}")
    rows = page.get("items") or []
    total = int(page.get("total_items") or 0)
    _expect(len(rows) == min(expected, 50), f"row count {len(rows)} payload keys {sorted(page)}")
    _expect(total == expected, f"total {total} expected {expected} keys {sorted(page)}")
    stopped_status, stopped = gate.request_json("GET", "/api/fleet/generators?state=STOPPED&page_size=50")
    _expect(stopped_status == 200, f"STOPPED filter {stopped_status} {stopped}")
    stopped_rows = stopped.get("items") or []
    _expect(len(stopped_rows) == expected, f"STOPPED rows {len(stopped_rows)}")
    search_status, search = gate.request_json("GET", "/api/fleet/generator-search?search=GEN-01")
    _expect(search_status == 200, f"search HTTP {search_status} {search}")
    found = search.get("items") or []
    _expect(any(int(_generator(row).get("unit_id", 0)) == 1 for row in found), f"GEN-01 search {search}")
    empty_status, empty = gate.request_json("GET", "/api/fleet/generator-search?search=NO-SUCH-UNIT-XYZ")
    _expect(empty_status == 200, f"empty search HTTP {empty_status}")
    empty_rows = empty.get("items") or []
    _expect(len(empty_rows) == 0, f"unexpected search hits {empty}")
    return {"total": total, "page_rows": len(rows), "search_hits": len(found)}


@feature("F10", "Unit detail, registers, and missing unit 404", "DETAIL-004 MOD-003 ISSUE-009", needs_configured=True)
def f10_unit_detail(gate: Gate) -> dict[str, Any]:
    status, detail = gate.request_json("GET", "/api/generators/1")
    _expect(status == 200, f"detail HTTP {status} {detail}")
    gen = _generator(detail)
    _expect(gen.get("state") == "STOPPED", f"unit 1 state {gen}")
    reg_status, registers = gate.request_json("GET", "/api/generators/1/registers")
    _expect(reg_status == 200, f"registers HTTP {reg_status} {registers}")
    mapping = registers.get("registers")
    _expect(isinstance(mapping, dict), f"register map {registers}")
    _expect("0" in mapping or 0 in mapping, f"register 0 missing {mapping}")
    missing_status, missing = gate.request_json("GET", "/api/generators/999")
    _expect(missing_status == 404, f"missing unit HTTP {missing_status} {missing}")
    _expect(isinstance(missing, dict) and missing.get("configured") is True, f"404 payload {missing}")
    return {"state": gen.get("state"), "register_keys": sorted(str(key) for key in mapping)[:8]}


@feature("F11", "Sub-fleet create, assign, rename, and unsafe name storage", "SUB EDGE-007 ISSUE-015", needs_configured=True)
def f11_subfleets(gate: Gate) -> dict[str, Any]:
    name = f"Launch Gate {int(time.time())}"
    status, created = gate.request_json("POST", "/api/subfleets", payload={"name": name})
    _expect(status == 201 and created.get("ok") is True, f"create HTTP {status} {created}")
    subfleet = created["subfleet"]
    subfleet_id = subfleet["id"]
    dup_status, dup = gate.request_json("POST", "/api/subfleets", payload={"name": name})
    _expect(dup_status == 400, f"duplicate should 400, got {dup_status} {dup}")
    assign_status, assigned = gate.request_json(
        "POST",
        f"/api/subfleets/{subfleet_id}/members",
        payload={"unit_ids": [1, 2]},
    )
    _expect(assign_status in {200, 201} and assigned.get("ok") is True, f"assign {assign_status} {assigned}")
    unsafe_status, unsafe = gate.request_json(
        "PATCH",
        f"/api/subfleets/{subfleet_id}",
        payload={"name": "<script>alert(1)</script>"},
    )
    _expect(unsafe_status == 200 and unsafe.get("ok") is True, f"rename {unsafe_status} {unsafe}")
    listed_status, listed = gate.request_json("GET", "/api/subfleets")
    _expect(listed_status == 200, f"list {listed_status} {listed}")
    rows = listed.get("subfleets") or listed.get("items") or []
    match = next((row for row in rows if row.get("id") == subfleet_id), None)
    _expect(match is not None, f"created subfleet missing from {listed}")
    stored_name = match.get("name")
    _expect("<script>alert(1)</script>" in stored_name, f"stored name {stored_name!r}")
    dash_status, dash_body, _headers = gate.request_text("GET", "/")
    _expect(dash_status == 200, "dashboard reload failed")
    _expect("<script>alert(1)</script>" not in dash_body, "dashboard HTML executed or inlined the script tag")
    remove_status, removed = gate.request_json("DELETE", f"/api/subfleets/{subfleet_id}/members/2")
    _expect(remove_status in {200, 204} or (isinstance(removed, dict) and removed.get("ok") is True), f"remove {remove_status} {removed}")
    gate.subfleet_id = subfleet_id
    return {"subfleet_id": subfleet_id, "stored_name": stored_name}


@feature("F12", "Sub-fleet start moves members off STOPPED", "TABLE-008", needs_configured=True)
def f12_group_start(gate: Gate) -> dict[str, Any]:
    _expect(gate.subfleet_id, "F11 must create a subfleet first")
    status, body = gate.request_json(
        "POST",
        f"/api/subfleets/{gate.subfleet_id}/commands",
        payload={"cmd": CMD_START},
    )
    _expect(status == 200 and body.get("ok") is True, f"start command {status} {body}")

    def started(payload: dict[str, Any]) -> bool:
        return _generator(payload).get("state") != "STOPPED"

    units = gate.wait_units([1], started, timeout=20.0, label="unit 1 did not leave STOPPED")
    return {"unit_1": _generator(units[0]).get("state")}


@feature("F13", "One-Line SCADA topology and alarms", "README SCADA", needs_configured=True)
def f13_scada(gate: Gate) -> dict[str, Any]:
    status, root = gate.request_json("GET", "/api/scada/topology")
    _expect(status == 200, f"topology HTTP {status} {root}")
    selected = root.get("selected_node") or {}
    _expect(selected.get("id") == "fleet", f"root {root}")
    children = root.get("children") or []
    _expect(isinstance(children, list) and children, f"root has no children {root}")
    child_ids = [str(child.get("id")) for child in children]
    _expect(any(item.startswith("group|") for item in child_ids), f"no group children {child_ids}")
    size_parent = None
    for child in children:
        child_id = str(child.get("id"))
        if child_id.startswith("group|"):
            encoded = urllib.parse.quote(child_id, safe="")
            size_status, size_body = gate.request_json("GET", f"/api/scada/topology?node_id={encoded}")
            _expect(size_status == 200, f"group drill {size_status} {size_body}")
            size_children = size_body.get("children") or []
            if size_children:
                size_parent = size_children[0]
                break
    _expect(size_parent is not None, "could not drill to a size node")
    size_id = str(size_parent.get("id"))
    encoded_size = urllib.parse.quote(size_id, safe="")
    range_status, range_body = gate.request_json("GET", f"/api/scada/topology?node_id={encoded_size}")
    _expect(range_status == 200, f"size drill {range_status} {range_body}")
    missing_status, missing = gate.request_json("GET", "/api/scada/topology?node_id=unit%7C999")
    _expect(missing_status == 404, f"unknown SCADA node {missing_status} {missing}")
    alarms_status, alarms = gate.request_json("GET", "/api/scada/alarms")
    _expect(alarms_status == 200 and "alarms" in alarms, f"alarms {alarms_status} {alarms}")
    return {"child_ids": child_ids, "size_id": size_id, "alarm_count": alarms.get("alarm_count")}


@feature("F14", "Scenario catalog, run, and stop", "scenarios", needs_configured=True)
def f14_scenarios(gate: Gate) -> dict[str, Any]:
    status, catalog = gate.request_json("GET", "/api/scenarios")
    _expect(status == 200, f"catalog HTTP {status} {catalog}")
    entries = catalog.get("catalog") or catalog.get("scenarios") or []
    ids = {str(item.get("scenario_id") or item.get("id")) for item in entries}
    missing = BUILTIN_SCENARIOS - ids
    _expect(not missing, f"missing scenarios {missing} have {ids}")
    run_status, run = gate.request_json("POST", "/api/scenarios/run", payload={"scenario_id": "utility-fail-recovery"})
    _expect(run_status == 200 and run.get("ok") is True, f"run {run_status} {run}")
    stop_status, stop = gate.request_json("POST", "/api/scenarios/stop", payload={})
    _expect(stop_status == 200 and stop.get("ok") is True, f"stop {stop_status} {stop}")
    return {"ids": sorted(ids)}


@feature("F15", "Runbook save, apply, and stop", "RUN-007 RUN-041 ISSUE-007", needs_configured=True)
def f15_runbooks(gate: Gate) -> dict[str, Any]:
    runbook = {
        "id": f"launch-gate-{int(time.time())}",
        "name": "Launch gate note",
        "description": "Overnight launch feature gate",
        "steps": [{"at_seconds": 0, "action": "note", "params": {"message": "gate checkpoint"}}],
    }
    status, saved = gate.request_json("POST", "/api/runbooks", payload=runbook)
    _expect(status == 201 and saved.get("ok") is True, f"save {status} {saved}")
    runbook_id = saved["runbook"]["id"]
    apply_status, applied = gate.request_json("POST", f"/api/runbooks/{runbook_id}/apply", payload={})
    _expect(apply_status == 200 and applied.get("ok") is True, f"apply {apply_status} {applied}")
    deadline = time.monotonic() + 8
    listed = None
    while time.monotonic() < deadline:
        list_status, listed = gate.request_json("GET", "/api/runbooks")
        _expect(list_status == 200, f"list {list_status} {listed}")
        if listed.get("active_runbook") is None:
            break
        time.sleep(0.3)
    if listed is not None and listed.get("active_runbook") is not None:
        stop_status, stopped = gate.request_json("POST", "/api/runbooks/stop", payload={})
        _expect(stop_status == 200, f"stop {stop_status} {stopped}")
        list_status, listed = gate.request_json("GET", "/api/runbooks")
        _expect(list_status == 200 and listed.get("active_runbook") is None, f"active still set {listed}")
    return {"runbook_id": runbook_id, "active": None if listed is None else listed.get("active_runbook")}


@feature("F16", "Config CSV export", "HEAD-011 ISSUE-008", needs_configured=True)
def f16_csv(gate: Gate) -> dict[str, Any]:
    status, body, headers = gate.request_text("GET", "/api/export/config.csv")
    _expect(status == 200, f"csv HTTP {status}")
    disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    _expect("generator-fleet-simulator-config.csv" in disposition, f"filename {disposition}")
    _expect(body.startswith("section,") or "unit_id" in body.splitlines()[0] or body.splitlines()[0].startswith("section"), f"csv header {body.splitlines()[:3]}")
    _expect("=CMD" not in body and "=cmd" not in body, "CSV contains formula-like command injection")
    expected = sum(int(value) for value in gate.size_counts.values())
    unit_lines = [line for line in body.splitlines() if line.startswith("unit,") or ",GEN-" in line]
    _expect(len(body.splitlines()) > expected, f"csv too small: {len(body.splitlines())} lines")
    return {"lines": len(body.splitlines()), "unit_hint_rows": len(unit_lines), "disposition": disposition}


@feature("F17", "Metrics remain numeric after commands", "HEAD-006 HEAD-007", needs_configured=True)
def f17_metrics(gate: Gate) -> dict[str, Any]:
    status, payload = gate.request_json("GET", "/api/metrics")
    _expect(status == 200 and payload.get("configured") is True, f"metrics {payload}")
    metrics = payload.get("metrics") or {}
    for key in ("avg_tick_ms", "queue_depth", "connected_clients"):
        if key in metrics:
            _expect(isinstance(metrics[key], (int, float)), f"{key} not numeric: {metrics[key]!r}")
    return metrics


@feature("F18", "Modbus TCP read and start command", "MOD E2E-002", needs_configured=True)
def f18_modbus(gate: Gate) -> dict[str, Any]:
    values = gate.modbus_read(unit_id=1, address=0, count=1)
    _expect(len(values) == 1, f"register 0 read {values}")
    detail_status, before = gate.request_json("GET", "/api/generators/3")
    _expect(detail_status == 200, f"unit 3 {before}")
    if _generator(before).get("state") == "STOPPED":
        gate.modbus_write(unit_id=3, address=20, value=CMD_START)

        def started(payload: dict[str, Any]) -> bool:
            return _generator(payload).get("state") != "STOPPED"

        after = gate.wait_units([3], started, timeout=20.0, label="Modbus start did not move unit 3")
        state = _generator(after[0]).get("state")
    else:
        state = _generator(before).get("state")
    return {"register0": values[0], "unit_3": state}


@feature("F19", "Utility fail then restore via sub-fleet commands", "HEAD-015 HEAD-016 ISSUE-011", needs_configured=True)
def f19_utility(gate: Gate) -> dict[str, Any]:
    _expect(gate.subfleet_id, "F11 must create a subfleet first")
    fail_status, fail = gate.request_json(
        "POST",
        f"/api/subfleets/{gate.subfleet_id}/commands",
        payload={"cmd": CMD_UTILITY_FAIL},
    )
    _expect(fail_status == 200, f"utility fail {fail_status} {fail}")
    failed = gate.wait_units(
        [1],
        utility_fail_observed,
        timeout=15.0,
        label="utility fail did not open the utility breaker and raise phase-loss",
    )
    restore_status, restore = gate.request_json(
        "POST",
        f"/api/subfleets/{gate.subfleet_id}/commands",
        payload={"cmd": CMD_RESTORE_UTILITY},
    )
    _expect(restore_status == 200, f"restore {restore_status} {restore}")
    restored = gate.wait_units(
        [1],
        utility_restore_observed,
        timeout=15.0,
        label="utility restore did not clear phase-loss",
    )
    after = _generator(restored[0])
    return {
        "failed": {
            "state": _generator(failed[0]).get("state"),
            "utility_breaker": _generator(failed[0]).get("utility_breaker"),
            "phase_loss": True,
        },
        "after": {
            "state": after.get("state"),
            "utility_breaker": after.get("utility_breaker"),
            "gen_breaker": after.get("gen_breaker"),
            "phase_loss": _has_named_alarm(after, UTILITY_PHASE_LOSS),
        },
    }


@feature("F20", "Spawned process writes state on shutdown", "PERSIST-001 PR-60")
def f20_persistence(gate: Gate) -> dict[str, Any]:
    state_file = gate.work_dir / "state.json"
    if gate.process is None:
        return {"skipped": "attached mode has no owned process"}
    _expect(state_file.exists() or True, "state path configured")
    gate.close()
    gate.process = None
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not state_file.exists():
        time.sleep(0.2)
    _expect(state_file.exists(), f"state file missing at {state_file}")
    backup = state_file.with_name(f"{state_file.name}.bak")
    return {"state_file": str(state_file), "bytes": state_file.stat().st_size, "backup_exists": backup.exists()}


@feature("F21", "2,000-generator startup", "START-014 PERF-003 PERF-004", profile="p4")
def f21_large_fleet(gate: Gate) -> dict[str, Any]:
    payload = {"size_counts": P4_COUNTS, "modbus_port": gate.modbus_port}
    started = time.monotonic()
    status, body = gate.request_json("POST", "/api/startup", payload=payload, timeout=60.0)
    elapsed = time.monotonic() - started
    _expect(status == 200 and body.get("ok") is True, f"P4 startup {status} {body}")
    _expect(int(body.get("num_generators", 0)) == 2000, f"P4 count {body}")
    _expect(elapsed <= 15.0, f"P4 startup took {elapsed:.2f}s")
    gate.configured = True
    gate.size_counts = dict(P4_COUNTS)
    ready = gate.wait_ready(timeout=30.0)
    page_status, page = gate.request_json("GET", "/api/fleet/generators?page=1&page_size=50")
    _expect(page_status == 200, f"P4 page {page_status} {page}")
    rows = page.get("items") or []
    _expect(len(rows) == 50, f"P4 first page {len(rows)}")
    topo_status, topo = gate.request_json("GET", "/api/scada/topology")
    _expect(topo_status == 200, f"P4 SCADA {topo_status} {topo}")
    return {"startup_s": round(elapsed, 3), "ready": ready.get("num_generators"), "page_rows": len(rows)}


def feature_catalog() -> list[dict[str, str]]:
    return [{"id": item.id, "title": item.title, "qaqc": item.qaqc, "profile": item.profile} for item in FEATURES]


def _size_counts_for(profile: str) -> dict[str, int]:
    if profile == "p4":
        return dict(P4_COUNTS)
    if profile == "p3":
        return dict(P3_COUNTS)
    return dict(P2_COUNTS)


def spawn_app(work_dir: Path, *, web_port: int, modbus_port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.pop("ACCEPT_LICENSE", None)
    env["GENSIM_WEB_HOST"] = "127.0.0.1"
    env["GENSIM_WEB_PORT"] = str(web_port)
    env["GENSIM_MODBUS_HOST"] = "127.0.0.1"
    env["GENSIM_MODBUS_PORT"] = str(modbus_port)
    env["GENSIM_STATE_FILE"] = str(work_dir / "state.json")
    env["GENSIM_RUNBOOKS_FILE"] = str(work_dir / "runbooks.json")
    env["GENSIM_HTTP_RATE_LIMIT"] = "400"
    log_path = work_dir / "server.log"
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(MAIN_PY)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5101")
    parser.add_argument("--modbus-host", default="127.0.0.1")
    parser.add_argument("--modbus-port", type=int, default=5121)
    parser.add_argument("--web-port", type=int, default=5101)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--profile", choices=("p2", "p3", "p4"), default="p2")
    parser.add_argument("--only", default="", help="Comma-separated feature ids")
    parser.add_argument("--skip-spawn", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--catalog", action="store_true")
    return parser.parse_args(argv)


def selected_features(only: str, profile: str) -> list[Feature]:
    wanted = {item.strip() for item in only.split(",") if item.strip()}
    if wanted:
        known = {item.id for item in FEATURES}
        missing = wanted - known
        if missing:
            raise GateFailure(f"unknown feature ids: {sorted(missing)}")
    chosen = []
    for item in FEATURES:
        if wanted and item.id not in wanted:
            continue
        if item.profile == "p4" and profile != "p4" and item.id not in wanted:
            continue
        chosen.append(item)
    chosen.sort(key=lambda item: 1 if item.id == "F20" else 0)
    return chosen


def run_gate(args: argparse.Namespace) -> int:
    if args.catalog:
        json.dump(feature_catalog(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="launch-gate-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    gate = Gate(
        base_url=args.base_url.rstrip("/"),
        modbus_host=args.modbus_host,
        modbus_port=args.modbus_port,
        timeout=args.timeout,
        profile=args.profile,
        size_counts=_size_counts_for(args.profile),
        work_dir=work_dir,
    )
    results: list[Result] = []
    exit_code = 0
    try:
        if not args.skip_spawn:
            gate.base_url = f"http://127.0.0.1:{args.web_port}"
            gate.modbus_port = args.modbus_port
            gate.process = spawn_app(work_dir, web_port=args.web_port, modbus_port=args.modbus_port)
            gate.wait_live(timeout=20.0)
        else:
            gate.wait_live(timeout=args.timeout)

        for item in selected_features(args.only, args.profile):
            started = time.monotonic()
            try:
                if item.needs_configured and not gate.configured:
                    raise GateFailure(f"{item.id} requires a configured simulator. Run F06 first.")
                detail = item.fn(gate)
                status = "PASS"
                text = json.dumps(detail, sort_keys=True, default=str)
            except Exception as exc:
                status = "FAIL"
                text = str(exc)
                exit_code = 1
            elapsed = time.monotonic() - started
            results.append(
                Result(id=item.id, title=item.title, status=status, detail=text, elapsed_s=round(elapsed, 3), qaqc=item.qaqc)
            )
            print(f"[{status}] {item.id} {item.title} ({elapsed:.2f}s)")
            if status == "FAIL":
                print(f"        {text}")
    finally:
        if gate.process is not None:
            gate.close()

    payload = {
        "profile": args.profile,
        "base_url": gate.base_url,
        "work_dir": str(work_dir),
        "results": [result.__dict__ for result in results],
        "failed": [result.id for result in results if result.status != "PASS"],
    }
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"[ok] wrote {out}")
    if exit_code:
        print(f"[fail] {len(payload['failed'])} feature(s) failed: {payload['failed']}")
    else:
        print("[ok] launch feature gate completed")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return run_gate(args)
    except GateFailure as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
