from unittest.mock import Mock

import pytest

from main import Settings, SimulatorRuntime, create_app
from tools.security.secret_guard import scan_file


@pytest.fixture
def configured_app(tmp_path):
    settings = Settings(num_generators=2, state_file=tmp_path / "state.json",
                        runbooks_file=tmp_path / "runbooks.json")
    runtime = SimulatorRuntime(settings)
    app, socketio, controller = create_app(settings, runtime)
    return app, socketio, controller


@pytest.mark.parametrize("headers", [
    {"Origin": "https://attacker.invalid"},
    {"Origin": "null"},
    {"Referer": "https://attacker.invalid/form"},
    {"Referer": "http://["},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_site_form_cannot_stop_a_runbook(configured_app, headers):
    app, _, controller = configured_app
    stop = Mock()
    controller.runtime.stop_runbook = stop
    response = app.test_client().post("/api/runbooks/stop", headers=headers)
    assert response.status_code == 403
    stop.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://localhost"},
                                     {"Referer": "http://localhost/"}])
def test_same_origin_and_non_browser_commands_work(configured_app, headers):
    app, _, controller = configured_app
    controller.runtime.stop_runbook = Mock(return_value={"status": "cancelled"})
    assert app.test_client().post("/api/runbooks/stop", headers=headers).status_code == 200
    controller.runtime.stop_runbook.assert_called_once()


@pytest.mark.parametrize("path", ["/", "/api/state", "/socket.io/?EIO=4&transport=polling"])
def test_rebinding_hostname_is_rejected_before_http_or_socketio(configured_app, path):
    app, _, _ = configured_app
    response = app.test_client().get(path, headers={"Host": "attacker.invalid:5000"})
    assert response.status_code == 400
    assert b"Untrusted Host" in response.data


@pytest.mark.parametrize("host", ["localhost:5000", "127.0.0.1:5000", "192.168.1.10:5000", "[::1]:5000"])
def test_local_and_lan_ip_access_is_preserved(configured_app, host):
    app, _, _ = configured_app
    assert app.test_client().get("/api/live", headers={"Host": host}).status_code == 200


def test_explicit_host_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("GENSIM_TRUSTED_HOSTS", "simulator.example")
    app, _, _ = create_app(Settings(state_file=tmp_path / "state.json"))
    client = app.test_client()
    assert client.get("/api/live", headers={"Host": "simulator.example:5000"}).status_code == 200
    assert client.get("/api/live", headers={"Host": "other.example:5000"}).status_code == 400


def test_socketio_rejects_cross_origin_connection(configured_app):
    app, _, _ = configured_app
    response = app.test_client().get("/socket.io/?EIO=4&transport=polling",
                                     headers={"Origin": "https://attacker.invalid"})
    assert response.status_code == 400


def test_readiness_fails_when_simulation_loop_exits(configured_app):
    app, _, controller = configured_app
    controller.modbus_server = Mock(is_running=True)
    controller.runtime._loop_started = True
    assert app.test_client().get("/api/ready").status_code == 200
    controller.runtime._loop_stopped.set()
    response = app.test_client().get("/api/ready")
    assert response.status_code == 503
    assert response.json["simulation_running"] is False


def test_credential_guard_does_not_skip_tokens_on_example_lines(tmp_path):
    path = tmp_path / "example.txt"
    token = "ghp_" + "A" * 36
    path.write_text("example token = " + token)
    findings = scan_file(path, tmp_path)
    assert findings == ["example.txt:1: github token"]
    assert token not in str(findings)
