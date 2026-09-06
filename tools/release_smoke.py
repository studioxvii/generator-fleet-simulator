#!/usr/bin/env python3
"""Release smoke test for a running Generator Fleet Simulator deployment."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from pymodbus.client import ModbusTcpClient


DEFAULT_SIZE_COUNTS = {"500": 2, "1000": 0, "1500": 0, "2000": 0, "2500": 0}


class SmokeFailure(RuntimeError):
    """Raised when a release smoke check fails."""


@dataclass(frozen=True)
class SmokeConfig:
    base_url: str
    modbus_host: str
    modbus_port: int
    startup_modbus_port: int
    timeout: float
    size_counts: dict[str, int]


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def _request_json(method: str, url: str, *, payload: dict[str, Any] | None = None, timeout: float) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        raise SmokeFailure(f"{method} {url} failed: {exc}") from exc


def _request_text(method: str, url: str, *, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, method=method, headers={"Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        raise SmokeFailure(f"{method} {url} failed: {exc}") from exc


def _expect_ok(label: str, status: int, payload: Any) -> dict[str, Any]:
    if status >= 400:
        raise SmokeFailure(f"{label} returned HTTP {status}: {payload}")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SmokeFailure(f"{label} returned an unexpected payload: {payload}")
    print(f"[ok] {label}")
    return payload


def _expect_status(label: str, expected_status: int, status: int, payload: Any) -> Any:
    if status != expected_status:
        raise SmokeFailure(f"{label} returned HTTP {status}, expected {expected_status}: {payload}")
    print(f"[ok] {label}")
    return payload


def _expect_text(label: str, status: int, body: str) -> str:
    if status >= 400:
        preview = body[:200].replace("\n", " ")
        raise SmokeFailure(f"{label} returned HTTP {status}: {preview}")
    if not body.strip():
        raise SmokeFailure(f"{label} returned an empty response body")
    print(f"[ok] {label}")
    return body


def _read_modbus_register(host: str, port: int, *, timeout: float, unit_id: int = 1) -> int:
    client = ModbusTcpClient(host, port=port, timeout=timeout)
    try:
        if not client.connect():
            raise SmokeFailure(f"Could not connect to Modbus at {host}:{port}")
        result = client.read_holding_registers(address=0, count=1, slave=unit_id)
        if result.isError():
            raise SmokeFailure(f"Modbus read returned an error: {result}")
        return int(result.registers[0])
    finally:
        client.close()


def _wait_for_liveness(base_url: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: SmokeFailure | None = None

    while time.monotonic() < deadline:
        try:
            status, payload = _request_json("GET", f"{base_url}/api/live", timeout=timeout)
            return _expect_ok("web liveness", status, payload)
        except SmokeFailure as exc:
            last_error = exc
            time.sleep(0.25)

    details = f": {last_error}" if last_error else ""
    raise SmokeFailure(f"web liveness did not become available before timeout{details}")


def _verify_dashboard_assets(base_url: str, *, timeout: float) -> None:
    dashboard_status, dashboard_body = _request_text("GET", f"{base_url}/", timeout=timeout)
    dashboard_body = _expect_text("dashboard shell", dashboard_status, dashboard_body)
    required_fragments = [
        'id="startup-overlay"',
        'role="dialog" aria-modal="true"',
        'src="/static/vendor/socket.io.min.js"',
    ]
    missing_fragments = [fragment for fragment in required_fragments if fragment not in dashboard_body]
    if missing_fragments:
        raise SmokeFailure(f"Dashboard shell is missing expected markup: {missing_fragments}")
    if "cdnjs.cloudflare.com" in dashboard_body:
        raise SmokeFailure("Dashboard shell unexpectedly references the Socket.IO CDN")

    favicon_status, favicon_body = _request_text("GET", f"{base_url}/favicon.ico", timeout=timeout)
    favicon_body = _expect_text("favicon asset", favicon_status, favicon_body)
    if "<svg" not in favicon_body:
        raise SmokeFailure("Favicon route did not return SVG content")

    socket_status, socket_body = _request_text("GET", f"{base_url}/static/vendor/socket.io.min.js", timeout=timeout)
    socket_body = _expect_text("vendored Socket.IO asset", socket_status, socket_body)
    if "Socket.IO" not in socket_body or len(socket_body) < 10_000:
        raise SmokeFailure("Vendored Socket.IO asset appears incomplete")


def run_smoke(config: SmokeConfig) -> None:
    base_url = _normalize_base_url(config.base_url)
    _wait_for_liveness(base_url, timeout=config.timeout)
    _verify_dashboard_assets(base_url, timeout=config.timeout)

    startup_payload = {"size_counts": config.size_counts, "modbus_port": config.startup_modbus_port}
    startup = _request_json("POST", f"{base_url}/api/startup", payload=startup_payload, timeout=config.timeout)
    startup_payload = _expect_ok("startup configuration", *startup)
    if int(startup_payload.get("num_generators", 0)) != sum(config.size_counts.values()):
        raise SmokeFailure(f"Startup configured unexpected fleet size: {startup_payload}")

    deadline = time.monotonic() + config.timeout
    ready_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status, payload = _request_json("GET", f"{base_url}/api/ready", timeout=config.timeout)
        if status == 200 and isinstance(payload, dict) and payload.get("ready") is True:
            ready_payload = payload
            break
        time.sleep(0.25)
    if ready_payload is None:
        raise SmokeFailure("simulator did not become ready before timeout")
    print("[ok] simulator readiness")

    summary = _request_json("GET", f"{base_url}/api/fleet/summary", timeout=config.timeout)
    summary_payload = _expect_status("fleet summary", 200, *summary)
    if not isinstance(summary_payload, dict) or not isinstance(summary_payload.get("summary"), dict):
        raise SmokeFailure(f"Fleet summary returned an unexpected payload: {summary_payload}")
    if int(summary_payload["summary"].get("generator_count", 0)) != sum(config.size_counts.values()):
        raise SmokeFailure(f"Fleet summary returned unexpected generator count: {summary_payload}")

    registers = _request_json("GET", f"{base_url}/api/generators/1/registers", timeout=config.timeout)
    registers_payload = _expect_status("unit register API", 200, *registers)
    if not isinstance(registers_payload.get("registers"), dict):
        raise SmokeFailure(f"Register API did not return a register map: {registers_payload}")

    subfleet_name = f"Smoke Test {int(time.time())}"
    created = _request_json("POST", f"{base_url}/api/subfleets", payload={"name": subfleet_name}, timeout=config.timeout)
    created_payload = _expect_status("sub-fleet create", 201, *created)
    subfleet_id = created_payload["subfleet"]["id"]
    invalid_members = _request_json(
        "POST",
        f"{base_url}/api/subfleets/{subfleet_id}/members",
        payload={},
        timeout=config.timeout,
    )
    invalid_payload = _expect_status("invalid sub-fleet member payload rejection", 400, *invalid_members)
    if not isinstance(invalid_payload, dict) or invalid_payload.get("error") != "unit_ids is required.":
        raise SmokeFailure(f"Unexpected invalid-member response: {invalid_payload}")
    _request_json("DELETE", f"{base_url}/api/subfleets/{subfleet_id}", timeout=config.timeout)

    raw_output = _read_modbus_register(config.modbus_host, config.modbus_port, timeout=config.timeout)
    print(f"[ok] Modbus holding register read unit=1 register=0 value={raw_output}")
    total = sum(config.size_counts.values())
    if total > 255:
        targets = {total}
        for boundary in range(255, total, 255):
            targets.update((boundary, boundary + 1))
        for generator_id in sorted(targets):
            offset, zero_based_unit = divmod(generator_id - 1, 255)
            wire_unit = zero_based_unit + 1
            status, payload = _request_json("GET", f"{base_url}/api/generators/{generator_id}/registers", timeout=config.timeout)
            _expect_status(f"generator {generator_id} mapping", 200, status, payload)
            mapping = payload.get("modbus", {})
            if mapping.get("port") != config.startup_modbus_port + offset or mapping.get("unit_id") != wire_unit:
                raise SmokeFailure(f"Wrong Modbus mapping for generator {generator_id}: {mapping}")
            _read_modbus_register(config.modbus_host, config.modbus_port + offset, timeout=config.timeout, unit_id=wire_unit)
            print(f"[ok] generator={generator_id} TCP port={config.modbus_port + offset} wire unit={wire_unit}")



def parse_args(argv: list[str]) -> SmokeConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="Dashboard/API base URL")
    parser.add_argument("--modbus-host", default="127.0.0.1", help="Modbus TCP host")
    parser.add_argument("--modbus-port", type=int, default=5020, help="Published Modbus TCP port to verify")
    parser.add_argument(
        "--startup-modbus-port",
        type=int,
        default=None,
        help="Modbus TCP port to send to /api/startup. Defaults to --modbus-port.",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Timeout in seconds for readiness and network calls")
    parser.add_argument("--generators", type=int, default=2, help="Fleet size to configure and verify (1-2000)")
    args = parser.parse_args(argv)
    if not 1 <= args.generators <= 2000:
        parser.error("--generators must be between 1 and 2000")
    return SmokeConfig(
        base_url=args.base_url,
        modbus_host=args.modbus_host,
        modbus_port=args.modbus_port,
        startup_modbus_port=args.startup_modbus_port if args.startup_modbus_port is not None else args.modbus_port,
        timeout=args.timeout,
        size_counts={**DEFAULT_SIZE_COUNTS, "500": args.generators},
    )


def main(argv: list[str] | None = None) -> int:
    try:
        run_smoke(parse_args(argv or sys.argv[1:]))
    except SmokeFailure as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1
    print("[ok] release smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
