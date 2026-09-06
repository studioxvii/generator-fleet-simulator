from tools import release_smoke


class _FakeModbusResult:
    registers = [0]

    def isError(self):
        return False


class _FakeModbusClient:
    def __init__(self, host, *, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.closed = False

    def connect(self):
        assert self.host == "127.0.0.1"
        assert self.port == 5502
        assert self.timeout == 3.0
        return True

    def read_holding_registers(self, *, address, count, slave):
        assert (address, count, slave) == (0, 1, 1)
        return _FakeModbusResult()

    def close(self):
        self.closed = True


def test_release_smoke_exercises_core_deployment_paths(monkeypatch):
    calls = []

    def fake_request_json(method, url, *, payload=None, timeout):
        path = url.removeprefix("http://smoke.test")
        calls.append((method, path, payload, timeout))
        if path == "/api/live":
            return 200, {"ok": True, "live": True}
        if path == "/api/startup":
            assert payload == {"size_counts": release_smoke.DEFAULT_SIZE_COUNTS, "modbus_port": 5502}
            return 200, {"ok": True, "num_generators": 2}
        if path == "/api/ready":
            return 200, {"ok": True, "ready": True, "modbus_running": True}
        if path == "/api/fleet/summary":
            return 200, {"configured": True, "summary": {"generator_count": 2}}
        if path == "/api/generators/1/registers":
            return 200, {"configured": True, "unit_id": 1, "registers": {"0": 0}}
        if path == "/api/subfleets":
            assert payload and payload["name"].startswith("Smoke Test ")
            return 201, {"ok": True, "subfleet": {"id": "smoke-id", "name": payload["name"]}}
        if path == "/api/subfleets/smoke-id/members":
            assert payload == {}
            return 400, {"ok": False, "error": "unit_ids is required."}
        if path == "/api/subfleets/smoke-id":
            return 200, {"ok": True}
        raise AssertionError(f"Unexpected smoke request: {method} {path}")

    def fake_request_text(method, url, *, timeout):
        path = url.removeprefix("http://smoke.test")
        calls.append((method, path, None, timeout))
        if path == "/":
            return 200, (
                '<html><body id="app">'
                '<div id="startup-overlay" role="dialog" aria-modal="true"></div>'
                '<script src="/static/vendor/socket.io.min.js"></script>'
                "</body></html>"
            )
        if path == "/favicon.ico":
            return 200, "<svg></svg>"
        if path == "/static/vendor/socket.io.min.js":
            return 200, "/*! Socket.IO */" + ("x" * 10_000)
        raise AssertionError(f"Unexpected smoke text request: {method} {path}")

    monkeypatch.setattr(release_smoke, "_request_json", fake_request_json)
    monkeypatch.setattr(release_smoke, "_request_text", fake_request_text)
    monkeypatch.setattr(release_smoke, "ModbusTcpClient", _FakeModbusClient)

    release_smoke.run_smoke(
        release_smoke.SmokeConfig(
            base_url="http://smoke.test/",
            modbus_host="127.0.0.1",
            modbus_port=5502,
            startup_modbus_port=5502,
            timeout=3.0,
            size_counts=dict(release_smoke.DEFAULT_SIZE_COUNTS),
        )
    )

    assert ("GET", "/api/live", None, 3.0) in calls
    assert ("GET", "/", None, 3.0) in calls
    assert ("GET", "/favicon.ico", None, 3.0) in calls
    assert ("GET", "/static/vendor/socket.io.min.js", None, 3.0) in calls
    assert ("GET", "/api/ready", None, 3.0) in calls
    assert ("DELETE", "/api/subfleets/smoke-id", None, 3.0) in calls


def test_release_smoke_rejects_incomplete_dashboard_assets(monkeypatch):
    def fake_request_text(method, url, *, timeout):
        path = url.removeprefix("http://smoke.test")
        if path == "/":
            return 200, '<html><body><script src="https://cdnjs.cloudflare.com/socket.io.js"></script></body></html>'
        raise AssertionError(f"Unexpected smoke text request: {method} {path}")

    monkeypatch.setattr(release_smoke, "_request_text", fake_request_text)

    try:
        release_smoke._verify_dashboard_assets("http://smoke.test", timeout=3.0)
    except release_smoke.SmokeFailure as exc:
        assert "Dashboard shell is missing expected markup" in str(exc)
    else:
        raise AssertionError("Expected incomplete dashboard shell to fail release smoke")


def test_release_smoke_waits_for_transient_liveness_failure(monkeypatch):
    calls = []
    sleeps = []

    def fake_request_json(method, url, *, payload=None, timeout=None):
        calls.append((method, url, payload, timeout))
        if len(calls) == 1:
            raise release_smoke.SmokeFailure("connection reset")
        return 200, {"ok": True, "live": True}

    monkeypatch.setattr(release_smoke, "_request_json", fake_request_json)
    monkeypatch.setattr(release_smoke.time, "sleep", lambda delay: sleeps.append(delay))

    payload = release_smoke._wait_for_liveness("http://smoke.test", timeout=1.0)

    assert payload == {"ok": True, "live": True}
    assert len(calls) == 2
    assert sleeps == [0.25]


def test_release_smoke_parse_args_uses_defaults_and_overrides():
    config = release_smoke.parse_args(
        ["--base-url", "http://127.0.0.1:5100/", "--modbus-port", "5120", "--timeout", "2"]
    )

    assert config.base_url == "http://127.0.0.1:5100/"
    assert config.modbus_host == "127.0.0.1"
    assert config.modbus_port == 5120
    assert config.startup_modbus_port == 5120
    assert config.timeout == 2.0
    assert config.size_counts == release_smoke.DEFAULT_SIZE_COUNTS


def test_release_smoke_parse_args_allows_docker_published_modbus_port():
    config = release_smoke.parse_args(
        [
            "--base-url",
            "http://127.0.0.1:5110/",
            "--modbus-port",
            "5130",
            "--startup-modbus-port",
            "5020",
        ]
    )

    assert config.modbus_port == 5130
    assert config.startup_modbus_port == 5020
