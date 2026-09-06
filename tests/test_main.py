import csv
import io
import json
from unittest.mock import ANY, Mock, patch

import pytest

from generator import ALARM_NAMES, State
from main import (
    AppController,
    MAX_PERSISTENT_STATE_FILE_BYTES,
    RESTORE_UTILITY_COMMAND_SEQUENCE,
    Settings,
    SimulatorRuntime,
    create_app,
    ensure_port_available,
    load_persistent_state,
)
from runbooks import MAX_RUNBOOK_REQUEST_BYTES


def _settings(tmp_path, **kwargs):
    kwargs.setdefault("runbooks_file", tmp_path / "runbooks.json")
    return Settings(state_file=tmp_path / "state.json", **kwargs)


def _runbook_payload(runbook_id="qa-runbook", *, name="QA Runbook", at_seconds=0):
    return {
        "id": runbook_id,
        "name": name,
        "description": "QA runbook",
        "steps": [{"at_seconds": at_seconds, "action": "note", "params": {"message": "QA checkpoint"}}],
    }


FAULT_COMMAND_ALARMS = [
    (20, 4),
    (21, 5),
    (22, 6),
    (23, 8),
    (24, 17),
    (25, 12),
    (26, 13),
    (27, 9),
    (28, 18),
    (29, 19),
    (30, 20),
    (31, 21),
    (32, 22),
    (33, 23),
    (34, 24),
    (35, 25),
    (36, 26),
    (37, 27),
    (38, 28),
    (39, 29),
]


def _alarm_word_and_mask(alarm_id):
    if alarm_id <= 15:
        return 13, 1 << alarm_id
    if alarm_id <= 17:
        return 14, 1 << (alarm_id - 16)
    return 17, 1 << (alarm_id - 18)


def test_api_state_returns_generators_summary_and_subfleets(tmp_path):
    settings = _settings(tmp_path, num_generators=3)
    runtime = SimulatorRuntime(settings)
    runtime.create_subfleet("North")
    runtime.assign_generators_to_subfleet(runtime.state_store.list_subfleets()[0]["id"], [1, 2])
    app, _, _ = create_app(settings=settings, runtime=runtime)

    client = app.test_client()
    response = client.get("/api/state")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert len(payload["generators"]) == 3
    assert payload["summary"]["generator_count"] == 3
    assert payload["subfleets"][0]["unit_count"] == 2


def test_api_summary_separates_generated_output_from_running_capacity(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime.generators[0].state = State.RUNNING
    runtime.generators[0].output_kw = 0.0
    runtime.generators[0].gen_breaker = False
    runtime.generators[1].state = State.RUNNING
    runtime.generators[1].output_kw = 250.5
    runtime.generators[1].gen_breaker = True
    runtime.state_store.refresh(runtime.generators)
    subfleet = runtime.create_subfleet("Capacity Test")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1, 2])
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().get("/api/state")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["running_count"] == 2
    assert payload["summary"]["total_output_kw"] == 250.5
    assert payload["summary"]["running_capacity_kw"] == 1500.0
    assert payload["summary"]["online_capacity_kw"] == 1000.0
    assert payload["subfleets"][0]["total_output_kw"] == 250.5
    assert payload["subfleets"][0]["running_capacity_kw"] == 1500.0
    assert payload["subfleets"][0]["online_capacity_kw"] == 1000.0


def test_runtime_simulation_step_syncs_registers(tmp_path):
    settings = _settings(tmp_path, num_generators=1, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime.generators[0].state = State.RUNNING
    runtime.generators[0].output_kw = 123.4

    runtime.simulation_step()

    slave = runtime.modbus_context[1]
    assert slave.getValues(3, 0, 1) == [1234]


def test_high_coolant_fault_injection_updates_register_bit_while_stopped(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)

    runtime.enqueue_unit_command(1, 20)
    runtime.simulation_step()

    registers = runtime.state_store.get_registers(1)
    detail = runtime.state_store.get_generator_detail(1)
    assert registers[13] & (1 << 4)
    assert detail["alarms"][4] == "High Coolant Temp"


def test_all_fault_injection_commands_sync_fault_state_and_modbus_alarm(tmp_path):
    for command, alarm_id in FAULT_COMMAND_ALARMS:
        settings = _settings(tmp_path / f"cmd-{command}", generator_ratings=[500], tick_seconds=0.0)
        runtime = SimulatorRuntime(settings)

        runtime.enqueue_unit_command(1, command)
        runtime.simulation_step()

        register, mask = _alarm_word_and_mask(alarm_id)
        registers = runtime.state_store.get_registers(1)
        detail = runtime.state_store.get_generator_detail(1)

        assert detail["state"] == "FAULT", f"command {command} should put unit in FAULT"
        assert detail["alarms"][alarm_id] == ALARM_NAMES[alarm_id]
        assert registers[register] & mask, f"command {command} should set alarm {alarm_id} in register {register}"


def test_settings_clamp_num_generators_to_supported_range():
    assert Settings(num_generators=0).num_generators == 1
    assert Settings(num_generators=2500).num_generators == 2000


def test_settings_default_modbus_host_is_local_only(monkeypatch, tmp_path):
    monkeypatch.delenv("GENSIM_MODBUS_HOST", raising=False)

    settings = Settings(state_file=tmp_path / "state.json")

    assert settings.modbus_host == "127.0.0.1"


def test_settings_modbus_host_allows_explicit_lan_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GENSIM_MODBUS_HOST", "0.0.0.0")

    settings = Settings(state_file=tmp_path / "state.json")

    assert settings.modbus_host == "0.0.0.0"


def test_default_socketio_cors_allows_same_origin_and_rejects_cross_origin(monkeypatch, tmp_path):
    monkeypatch.delenv("GENSIM_CORS_ALLOWED_ORIGINS", raising=False)
    app, _, _ = create_app(settings=_settings(tmp_path))

    same_origin_response = app.test_client().get(
        "/socket.io/?EIO=4&transport=polling",
        headers={"Origin": "http://localhost"},
    )
    cross_origin_response = app.test_client().get(
        "/socket.io/?EIO=4&transport=polling",
        headers={"Origin": "https://evil.example"},
    )

    assert same_origin_response.status_code == 200
    assert cross_origin_response.status_code == 400


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("*", "*"),
        (
            "http://localhost:5000, http://127.0.0.1:5000",
            ["http://localhost:5000", "http://127.0.0.1:5000"],
        ),
    ],
)
def test_settings_accepts_explicit_socketio_cors_origins(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv("GENSIM_CORS_ALLOWED_ORIGINS", value)

    settings = _settings(tmp_path)

    assert settings.cors_allowed_origins == expected


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"web_port": 0}, "Web port must be between 1 and 65535."),
        ({"web_port": 65536}, "Web port must be between 1 and 65535."),
        ({"web_port": "abc"}, "Web port must be a whole number."),
        ({"web_port": 5000.5}, "Web port must be a whole number."),
        ({"public_web_port": 5001.5}, "Public web port must be a whole number."),
        ({"public_web_port": 70000}, "Public web port must be between 1 and 65535."),
        ({"modbus_port": 0}, "Modbus port must be between 1 and 65535."),
        ({"modbus_port": 65536}, "Modbus port must be between 1 and 65535."),
        ({"modbus_port": "abc"}, "Modbus port must be a whole number."),
        ({"modbus_port": 5020.5}, "Modbus port must be a whole number."),
        ({"public_modbus_port": 5021.5}, "Public Modbus port must be a whole number."),
        ({"public_modbus_port": 70000}, "Public Modbus port must be between 1 and 65535."),
    ],
)
def test_settings_reject_invalid_tcp_ports(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        _settings(tmp_path, **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"save_interval": 0}, "Save interval must be at least 1 second."),
        ({"save_interval": -1}, "Save interval must be at least 1 second."),
        ({"save_interval": "abc"}, "Save interval must be a whole number."),
        ({"save_interval": 1.5}, "Save interval must be a whole number."),
        ({"tick_seconds": -0.1}, "Tick interval must be 0 or greater."),
        ({"tick_seconds": float("inf")}, "Tick interval must be finite."),
        ({"tick_seconds": True}, "Tick interval must be a number."),
    ],
)
def test_settings_reject_invalid_runtime_timing(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        _settings(tmp_path, **kwargs)


def test_settings_allows_zero_tick_interval_for_deterministic_tests(tmp_path):
    settings = _settings(tmp_path, tick_seconds=0.0)

    assert settings.tick_seconds == 0.0


@pytest.mark.parametrize(
    ("env_name", "env_value", "message"),
    [
        ("GENSIM_WEB_PORT", "abc", "GENSIM_WEB_PORT must be a whole number."),
        ("GENSIM_PUBLIC_WEB_PORT", "abc", "GENSIM_PUBLIC_WEB_PORT must be a whole number."),
        ("GENSIM_PUBLIC_MODBUS_PORT", "abc", "GENSIM_PUBLIC_MODBUS_PORT must be a whole number."),
        ("GENSIM_SAVE_INTERVAL", "abc", "GENSIM_SAVE_INTERVAL must be a whole number."),
        ("GENSIM_TICK_SECONDS", "nan", "GENSIM_TICK_SECONDS must be a finite number."),
        ("GENSIM_TICK_SECONDS", "fast", "GENSIM_TICK_SECONDS must be a number."),
    ],
)
def test_settings_reports_invalid_env_values(monkeypatch, tmp_path, env_name, env_value, message):
    monkeypatch.setenv(env_name, env_value)

    with pytest.raises(ValueError, match=message):
        _settings(tmp_path)


def test_api_state_reports_unconfigured_before_startup(tmp_path):
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    response = client.get("/api/state")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is False
    assert payload["generators"] == []
    assert controller.is_started is False


def test_load_persistent_state_ignores_non_object_json(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("[]", encoding="utf-8")

    assert load_persistent_state(state_file) == {}


def test_load_persistent_state_ignores_oversized_json_without_reading(tmp_path):
    state_file = tmp_path / "state.json"
    with state_file.open("wb") as handle:
        handle.truncate(MAX_PERSISTENT_STATE_FILE_BYTES + 1)

    assert load_persistent_state(state_file) == {}


def test_runtime_restores_numeric_persisted_run_hours(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    settings.state_file.write_text(
        json.dumps({
            "fleet_config": {"num_generators": 1, "generator_ratings": [500]},
            "generators": {"1": {"run_hours": "12.5"}},
        }),
        encoding="utf-8",
    )

    runtime = SimulatorRuntime(settings)

    assert runtime.generators[0].run_hours == 12.5


@pytest.mark.parametrize("bad_run_hours", ["not-a-number", -1, True])
def test_runtime_ignores_invalid_persisted_run_hours(tmp_path, bad_run_hours):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    settings.state_file.write_text(
        json.dumps({
            "fleet_config": {"num_generators": 1, "generator_ratings": [500]},
            "generators": {"1": {"run_hours": bad_run_hours}},
        }),
        encoding="utf-8",
    )

    with patch("generator.random.uniform", return_value=22.25):
        runtime = SimulatorRuntime(settings)

    assert runtime.generators[0].run_hours == 22.25


def test_runtime_starts_when_state_file_has_non_object_json(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("[]", encoding="utf-8")

    settings = Settings(
        state_file=state_file,
        runbooks_file=tmp_path / "runbooks.json",
        generator_ratings=[500],
    )
    runtime = SimulatorRuntime(settings)

    assert runtime.settings.num_generators == 1
    assert runtime.state_store.get_summary()["generator_count"] == 1


def test_dashboard_includes_accessibility_scaffold_and_favicon(tmp_path):
    settings = _settings(tmp_path, num_generators=1)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<link rel="icon" href="/favicon.ico">' in html
    assert 'src="/static/vendor/socket.io.min.js"' in html
    assert "cdnjs.cloudflare.com" not in html
    assert 'id="startup-overlay"' in html
    assert 'role="dialog" aria-modal="true"' in html
    assert 'aria-label="Close asset setup modal"' in html
    assert 'aria-label="Close generator detail modal"' in html
    assert 'id="app-status" role="status" aria-live="polite"' in html
    assert 'id="summary-running-kw"' in html
    assert "Running Rated kW" in html
    assert "Gen Output kW" in html
    assert "Total kW" not in html
    assert 'data-tab="scada"' in html
    assert 'id="tab-scada"' in html
    assert "/api/scada/topology" in html
    assert "/api/scada/alarms" in html
    assert 'id="scada-alarm-overlay"' in html
    assert "function scadaBreaker" in html
    assert "function scadaUpOneLevel" in html
    assert "function openScadaAlarms" in html
    assert 'id="runbook-id-input"' in html
    assert 'pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,95}"' in html
    assert "const RUNBOOK_ID_PATTERN" in html
    assert "apiFetch(" not in html
    assert 'fetchJson("/api/runbooks"' in html
    assert 'const contentType = response.headers.get("content-type") || "";' in html
    assert 'contentType.includes("application/json")' in html
    assert "Request failed (${response.status})" in html


@pytest.mark.parametrize("path", ["/", "/api/live", "/favicon.ico"])
def test_responses_include_baseline_security_headers(tmp_path, path):
    app, _, _ = create_app(settings=_settings(tmp_path, num_generators=1))

    response = app.test_client().get(path)

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Request-ID"]


def test_dashboard_connection_hints_use_request_host_and_public_modbus_port(tmp_path):
    settings = _settings(tmp_path, num_generators=1, public_modbus_port=5021)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/", headers={"Host": "localhost:5001"}).get_data(as_text=True)

    assert "Dashboard: localhost:5001" in html
    assert "Dashboard: http://localhost:5001" in html
    assert "Modbus TCP: localhost:5021" in html
    assert "Modbus TCP: 127.0.0.1:5020" not in html


def test_dashboard_connection_hints_follow_lan_request_host(tmp_path):
    settings = _settings(tmp_path, num_generators=1, public_modbus_port=5021)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/", headers={"Host": "10.0.0.24:5001"}).get_data(as_text=True)

    assert "Dashboard: 10.0.0.24:5001" in html
    assert "Modbus TCP: 10.0.0.24:5021" in html


def test_dashboard_public_web_hint_overrides_request_host(tmp_path):
    settings = _settings(
        tmp_path,
        num_generators=1,
        public_web_host="localhost",
        public_web_port=5001,
        public_modbus_host="localhost",
        public_modbus_port=5021,
    )
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/", headers={"Host": "127.0.0.1:5000"}).get_data(as_text=True)

    assert "Dashboard: localhost:5001" in html
    assert "Dashboard: 127.0.0.1:5000" not in html


def test_dashboard_modbus_hint_follows_active_port_without_public_override(tmp_path):
    settings = _settings(tmp_path, num_generators=1, modbus_port=5502)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/", headers={"Host": "localhost:5080"}).get_data(as_text=True)

    assert "Modbus TCP: localhost:5502" in html


def test_dashboard_escapes_operator_text_in_inner_html(tmp_path):
    settings = _settings(tmp_path, num_generators=1)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "function escapeHtml(value)" in html
    assert 'const div = document.createElement("div");' in html
    assert "div.textContent = value == null ? \"\" : String(value);" in html
    assert "${escapeHtml(subfleet.name)}" in html
    assert "${escapeHtml(subfleetName(row.subfleet_id))}" in html
    assert "${escapeHtml(step.summary || \"\")}" in html
    assert "${escapeHtml(node.label)}" in html
    assert "function scadaNodeKwLine" in html
    assert "function scadaIssueLine" in html
    assert "function scadaLineTone" in html
    assert 'class="scada-bus ${rowLineTone}"' in html
    assert "const RESTORE_UTILITY_FLEET_SEQUENCE = [11, 14, 4];" in html
    assert 'onclick="restoreUtilityAll()"' in html
    assert "function restoreUtilityAll()" in html
    assert 'socket.emit("fleet_command_sequence", { commands });' in html
    assert 'socket.emit("bulk_command", { unit_ids: selectedIds(), cmd });' in html
    assert 'socket.on("command_rejected", payload =>' in html
    assert "function restoreScadaFleetUtility()" in html
    assert "const label = escapeHtml(scadaNodeDisplayLabel(node, compact));" in html
    assert 'class="scada-node-kind"' in html
    assert 'button.className = `scada-tree-item ${scadaTone(node)}`;' in html
    assert 'scadaMetric(formatScadaCount(node.fault_count), "Faulted")' in html
    assert ".scada-breaker-symbol.closed .scada-breaker-contact" in html
    assert "stroke: var(--red);" in html
    assert '<text class="scada-svg-kw"' in html
    assert "${escapeHtml(alarm.alarm_name)}" in html
    assert "${this._esc(String(step.at_seconds))}s" in html
    assert "${this._esc(step.action)}" in html
    assert "function validateImportedRunbookSteps(steps)" in html
    assert "${subfleet.name}</div>" not in html
    assert "${step.summary || \"\"}</td>" not in html
    assert '<span class="tle-badge">${step.action}</span>' not in html


def test_dashboard_clears_stale_modbus_rows_after_failed_register_load(tmp_path):
    settings = _settings(tmp_path, num_generators=1)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "function renderModbusTableMessage(message)" in html
    assert "Loading registers for Unit ${unitId}..." in html
    assert "No register data loaded for Unit ${unitId}." in html
    assert "if (isActiveModbusRequest(unitId))" in html
    assert "if (!isActiveModbusRequest(unitId)) return;" in html


def test_dashboard_modbus_use_selected_unit_accepts_checked_row_selection(tmp_path):
    settings = _settings(tmp_path, num_generators=2)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "function modbusSelectedUnitId()" in html
    assert "const ids = selectedIds();" in html
    assert "if (ids.length === 1) return ids[0];" in html
    assert "Select exactly one generator for Modbus register inspection." in html
    assert "unitId = modbusSelectedUnitId();" in html
    assert "loadModbus(unitId).catch(showError);" in html


def test_dashboard_disables_invalid_fleet_row_commands(tmp_path):
    settings = _settings(tmp_path, num_generators=2)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "function fleetRowCommandButton(row, { className, cmd, text, enabled })" in html
    assert 'disabled aria-disabled="true"' in html
    assert "function fleetRowActionButtons(row)" in html
    assert 'enabled: row.state === "STOPPED"' in html
    assert 'enabled: row.state === "RUNNING" || row.state === "CRANKING"' in html
    assert 'data-demo-highlight="fleet-row-actions" onclick="event.stopPropagation()"' in html
    assert "${fleetRowActionButtons(row)}" in html


def test_dashboard_strictly_parses_pasted_unit_ids(tmp_path):
    settings = _settings(tmp_path, num_generators=2)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "function maxConfiguredUnitId()" in html
    assert "function parsePositiveUnitIdToken(token, maxUnitId)" in html
    assert "if (!/^[0-9]+$/.test(token))" in html
    assert "Unit ID ${unitId} exceeds configured fleet size ${maxUnitId}." in html
    assert "const match = token.match(/^([0-9]+)-([0-9]+)$/);" in html
    assert "const start = parsePositiveUnitIdToken(match[1], maxUnitId);" in html
    assert "const end = parsePositiveUnitIdToken(match[2], maxUnitId);" in html
    assert "ids.add(parsePositiveUnitIdToken(token, maxUnitId));" in html
    assert "const unitId = parseInt(token, 10);" not in html


def test_dashboard_rejects_oversized_runbook_imports_before_reading(tmp_path):
    settings = _settings(tmp_path, num_generators=2)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "const MAX_RUNBOOK_IMPORT_BYTES = 256 * 1024;" in html
    assert "if (file.size > MAX_RUNBOOK_IMPORT_BYTES)" in html
    assert "Import failed: runbook file is too large." in html
    assert 'event.target.value = "";' in html
    assert "const reader = new FileReader();" in html


def test_dashboard_page_jump_uses_whole_number_validation(tmp_path):
    settings = _settings(tmp_path, num_generators=2)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "function goToPage()" in html
    assert 'const page = parseWholeNumberInput($("goto-page-input"));' in html
    assert 'showError(new Error("Enter a valid page number."));' in html
    assert 'parseInt($("goto-page-input").value, 10)' not in html


def test_dashboard_clears_deleted_subfleet_filter_state(tmp_path):
    settings = _settings(tmp_path, num_generators=1)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "const deletedSubfleetId = state.activeSubfleetId;" in html
    assert 'state.filters.subfleet_id === deletedSubfleetId' in html
    assert 'state.filters.subfleet_id = "";' in html
    assert 'state.filters.subfleet_id && !state.subfleets.some(item => item.id === state.filters.subfleet_id)' in html


def test_dashboard_hardens_wave18_a11y_guardrails(tmp_path):
    settings = _settings(tmp_path, num_generators=1)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    html = app.test_client().get("/").get_data(as_text=True)

    assert "--text-dim: #a1a1aa;" in html
    assert "--red: #fb7185;" in html
    assert "--blue: #0ea5e9;" in html
    assert "--blue-text: #7dd3fc;" in html
    assert ".btn-danger:hover { background: var(--red); color: #000;" in html
    assert ".btn-outline" in html
    assert "class=\"unit-link\"" in html
    assert "background: #111114;" in html
    assert "runbook-layout" in html
    assert "Button Color Rules" in html
    assert "Run Scenario Timelines" in html
    assert "function confirmAction(message)" in html
    assert 'markPageStale("Loading...");' in html
    assert "Loading matching generators..." in html
    assert "Loading members..." in html
    assert "Delete \"${active ? active.name : \"the active sub-fleet\"}\"?" in html
    assert "Remove all matching members" in html
    assert "Remove ${assignments.length} selected generator" in html
    assert "Stopping active runbook..." in html
    assert "Saving \"${name}\"..." in html


def test_favicon_route_returns_svg(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path, num_generators=1))

    response = app.test_client().get("/favicon.ico")

    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert "<svg" in response.get_data(as_text=True)


def test_api_startup_accepts_generator_size_mix(tmp_path):
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    runtime = SimulatorRuntime(_settings(tmp_path, generator_ratings=[500, 500, 1000], tick_seconds=0.0, modbus_port=5502))

    with patch.object(controller, "configure_simulator", return_value=runtime) as mocked_configure:
        response = client.post(
            "/api/startup",
            json={"size_counts": {"500": 2, "1000": 1, "1500": 0, "2000": 0, "2500": 0}, "modbus_port": 5502},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["num_generators"] == 3
    assert payload["modbus_port"] == 5502
    mocked_configure.assert_called_once_with(ANY, {500: 2, 1000: 1, 1500: 0, 2000: 0, 2500: 0}, 5502)


def test_api_startup_rejects_empty_non_json_post(tmp_path):
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    with patch.object(controller, "configure_simulator") as mocked_configure:
        response = client.post("/api/startup")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON request body is required."
    mocked_configure.assert_not_called()


def test_api_startup_requires_explicit_size_counts(tmp_path):
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    with patch.object(controller, "configure_simulator") as mocked_configure:
        response = client.post("/api/startup", json={"modbus_port": 5502})

    assert response.status_code == 400
    assert response.get_json()["error"] == "size_counts is required."
    mocked_configure.assert_not_called()


def test_api_startup_rejects_generator_mix_above_cap(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    response = client.post(
        "/api/startup",
        json={"size_counts": {"500": 2000, "1000": 1, "1500": 0, "2000": 0, "2500": 0}},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "Total generators cannot exceed 2000."


def test_api_startup_rejects_decimal_generator_counts(tmp_path):
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    with patch.object(controller, "configure_simulator") as mocked_configure:
        response = client.post(
            "/api/startup",
            json={"size_counts": {"500": 1.5, "1000": 0, "1500": 0, "2000": 0, "2500": 0}},
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "500 kW count must be a whole number."
    mocked_configure.assert_not_called()


def test_api_startup_rejects_decimal_string_generator_counts(tmp_path):
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    with patch.object(controller, "configure_simulator") as mocked_configure:
        response = client.post(
            "/api/startup",
            json={"size_counts": {"500": "1.5", "1000": 0, "1500": 0, "2000": 0, "2500": 0}},
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "500 kW count must be a whole number."
    mocked_configure.assert_not_called()


def test_api_startup_rejects_invalid_modbus_port(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    response = client.post(
        "/api/startup",
        json={"size_counts": {"500": 1, "1000": 0, "1500": 0, "2000": 0, "2500": 0}, "modbus_port": 70000},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "Modbus port must be between 1 and 65535."


def test_api_startup_rejects_decimal_modbus_port(tmp_path):
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=15))

    client = app.test_client()
    with patch.object(controller, "configure_simulator") as mocked_configure:
        response = client.post(
            "/api/startup",
            json={"size_counts": {"500": 1, "1000": 0, "1500": 0, "2000": 0, "2500": 0}, "modbus_port": 5502.5},
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "Modbus port must be a whole number."
    mocked_configure.assert_not_called()


def test_runtime_restores_subfleets_for_matching_saved_fleet(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000])
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Persisted")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1, 2])
    runtime.save_state()

    restored = SimulatorRuntime(settings)

    subfleets = restored.state_store.list_subfleets()
    assert len(subfleets) == 1
    assert subfleets[0]["name"] == "Persisted"
    assert subfleets[0]["member_ids"] == [1, 2]


def test_runtime_skips_persistent_state_for_incompatible_fleet(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000])
    runtime = SimulatorRuntime(settings)
    with runtime.generators[0].lock:
        runtime.generators[0].run_hours = 4321.0
    subfleet = runtime.create_subfleet("Old Fleet")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1, 2])
    runtime.save_state()

    restored = SimulatorRuntime(_settings(tmp_path, generator_ratings=[500]))

    assert restored.state_store.list_subfleets() == []
    assert restored.generators[0].run_hours != 4321.0


def test_ensure_port_available_rejects_out_of_range_port():
    try:
        ensure_port_available("127.0.0.1", 70000)
    except RuntimeError as exc:
        assert "port must be between 1 and 65535" in str(exc)
    else:
        raise AssertionError("Expected invalid web port to raise RuntimeError")


def test_controller_configure_simulator_restarts_runtime_and_updates_modbus_port(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], modbus_port=5020)
    existing_runtime = Mock()
    existing_server = Mock()
    controller = AppController(settings, runtime=existing_runtime)
    controller.modbus_server = existing_server
    socketio = Mock()
    new_runtime = Mock()
    new_runtime.modbus_context = object()
    new_server = Mock()

    with patch("main.ensure_port_available") as mocked_port_check, \
         patch("main.SimulatorRuntime", return_value=new_runtime) as mocked_runtime_cls, \
         patch("main.ModbusServerGroup", return_value=new_server) as mocked_server_cls:
        runtime = controller.configure_simulator(
            socketio,
            {500: 0, 1000: 2, 1500: 0, 2000: 0, 2500: 0},
            5502,
        )

    assert runtime is new_runtime
    existing_runtime.save_state.assert_called_once_with()
    existing_runtime.stop.assert_called_once_with()
    existing_server.stop.assert_called_once_with()
    mocked_port_check.assert_called_once_with(settings.modbus_host, 5502)
    mocked_runtime_cls.assert_called_once_with(settings)
    mocked_server_cls.assert_called_once_with(new_runtime.modbus_context, num_generators=2, host=settings.modbus_host, port=5502)
    new_server.start.assert_called_once_with()
    socketio.start_background_task.assert_called_once_with(new_runtime.simulation_loop, socketio)
    assert controller.runtime is new_runtime
    assert controller.modbus_server is new_server
    assert settings.generator_ratings == [1000, 1000]
    assert settings.num_generators == 2
    assert settings.modbus_port == 5502


def test_export_config_csv_includes_fleet_and_subfleet_sections(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Critical")
    runtime.assign_generators_to_subfleet(subfleet["id"], [2])
    app, _, _ = create_app(settings=settings, runtime=runtime)

    client = app.test_client()
    response = client.get("/api/export/config.csv")

    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == "attachment; filename=generator-fleet-simulator-config.csv"
    csv_text = response.get_data(as_text=True)
    assert "fleet,summary,generator_count,2,Total configured generators" in csv_text
    assert "generator,2,GEN-02,1000" in csv_text
    assert "section,subfleet_id,name,member_count,member_ids" in csv_text
    assert "subfleet," in csv_text


def test_export_config_csv_neutralizes_subfleet_formula_names(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("=WEBSERVICE(\"http://example.invalid\")")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1])
    app, _, _ = create_app(settings=settings, runtime=runtime)

    csv_text = app.test_client().get("/api/export/config.csv").get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(csv_text)))
    subfleet_rows = [row for row in rows if row and row[0] == "subfleet"]

    assert subfleet_rows[0][2].startswith("'=")


def test_fleet_generators_endpoint_supports_filters_and_paging(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000, 1500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime.generators[1].state = State.RUNNING
    runtime.generators[1].output_kw = 440.0
    runtime.generators[2].state = State.FAULT
    runtime.state_store.refresh(runtime.generators)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    client = app.test_client()
    response = client.get("/api/fleet/generators?page=1&page_size=1&state=RUNNING")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["total_items"] == 1
    assert payload["items"][0]["state"] == "RUNNING"

    response = client.get("/api/fleet/generators?search=GEN-0001")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total_items"] == 1
    assert payload["items"][0]["unit_id"] == 1


def test_fleet_generators_endpoint_rejects_malformed_query_params(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    cases = [
        ("/api/fleet/generators?page=abc", "page must be a whole number."),
        ("/api/fleet/generators?page_size=wide", "page_size must be a whole number."),
        ("/api/fleet/generators?rated_kw=large", "rated_kw must be a whole number."),
        ("/api/fleet/generators?auto_mode=maybe", "Invalid boolean value: maybe"),
        ("/api/fleet/generators?subfleet_scope=outside", "Invalid sub-fleet scope: outside"),
    ]

    for url, message in cases:
        response = client.get(url)
        payload = response.get_json()
        assert response.status_code == 400
        assert payload["configured"] is True
        assert payload["ok"] is False
        assert payload["error"] == message


def test_subfleet_crud_and_membership_endpoints(tmp_path):
    settings = _settings(tmp_path, num_generators=4, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    created = client.post("/api/subfleets", json={"name": "West"})
    assert created.status_code == 201
    subfleet_id = created.get_json()["subfleet"]["id"]

    assigned = client.post(f"/api/subfleets/{subfleet_id}/members", json={"unit_ids": [1, 3]})
    assert assigned.status_code == 200
    assert assigned.get_json()["assigned_unit_ids"] == [1, 3]

    renamed = client.patch(f"/api/subfleets/{subfleet_id}", json={"name": "West Prime"})
    assert renamed.status_code == 200
    assert renamed.get_json()["subfleet"]["name"] == "West Prime"

    listed = client.get("/api/subfleets")
    payload = listed.get_json()
    assert payload["subfleets"][0]["unit_count"] == 2

    removed = client.delete(f"/api/subfleets/{subfleet_id}/members/1")
    assert removed.status_code == 200

    deleted = client.delete(f"/api/subfleets/{subfleet_id}")
    assert deleted.status_code == 200


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({}, "unit_ids is required."),
        ({"unit_ids": []}, "unit_ids must include at least one generator ID."),
        ({"unit_ids": "1,2"}, "unit_ids must be an array."),
        ({"unit_ids": [None]}, "unit_ids must contain whole-number generator IDs."),
        ({"unit_ids": [True]}, "unit_ids must contain whole-number generator IDs."),
        ({"unit_ids": [1.5]}, "unit_ids must contain whole-number generator IDs."),
        ({"unit_ids": [0]}, "unit_ids must contain positive generator IDs."),
    ],
)
def test_subfleet_members_endpoint_rejects_invalid_unit_id_payloads(tmp_path, payload, error):
    settings = _settings(tmp_path, num_generators=3, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("West")
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().post(f"/api/subfleets/{subfleet['id']}/members", json=payload)

    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": error}
    assert runtime.state_store.get_subfleet(subfleet["id"])["member_ids"] == []


def test_subfleet_rename_rejects_name_over_limit(tmp_path):
    settings = _settings(tmp_path, num_generators=2, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("West")
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().patch(f"/api/subfleets/{subfleet['id']}", json={"name": "x" * 81})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Sub-fleet name cannot exceed 80 characters."
    assert runtime.state_store.list_subfleets()[0]["name"] == "West"


def test_runtime_skips_invalid_persisted_subfleets(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "fleet_config": {"num_generators": 2, "generator_ratings": [500, 500]},
                "generators": {},
                "subfleets": [
                    "not-an-object",
                    {"id": "too-long", "name": "x" * 81, "member_ids": [1]},
                    {"id": "bad-members", "name": "Bad Members", "member_ids": "1,2"},
                    {"id": "valid", "name": "Valid", "member_ids": [1, "ghost"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        state_file=state_file,
        runbooks_file=tmp_path / "runbooks.json",
        generator_ratings=[500, 500],
    )

    runtime = SimulatorRuntime(settings)

    subfleets = runtime.state_store.list_subfleets()
    assert [subfleet["id"] for subfleet in subfleets] == ["bad-members", "valid"]
    assert subfleets[0]["member_ids"] == []
    assert subfleets[1]["member_ids"] == [1]


def test_generator_search_endpoint_filters_across_fleet(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000, 2000, 2000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Assigned")
    runtime.assign_generators_to_subfleet(subfleet["id"], [2, 4])
    runtime.generators[3].state = State.RUNNING
    runtime.state_store.refresh(runtime.generators)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.get(f"/api/fleet/generator-search?subfleet_scope=other&context_subfleet_id={subfleet['id']}&state=RUNNING")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["total_items"] == 0

    response = client.get("/api/fleet/generator-search?subfleet_scope=assigned&rated_kw=2000")
    payload = response.get_json()
    assert payload["total_items"] == 1
    assert payload["items"][0]["unit_id"] == 4


def test_generator_search_endpoint_rejects_malformed_query_params(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    cases = [
        ("/api/fleet/generator-search?limit=many", "limit must be a whole number."),
        ("/api/fleet/generator-search?rated_kw=large", "rated_kw must be a whole number."),
        ("/api/fleet/generator-search?auto_mode=maybe", "Invalid boolean value: maybe"),
        ("/api/fleet/generator-search?subfleet_scope=outside", "Invalid sub-fleet scope: outside"),
    ]

    for url, message in cases:
        response = client.get(url)
        payload = response.get_json()
        assert response.status_code == 400
        assert payload["configured"] is True
        assert payload["ok"] is False
        assert payload["items"] == []
        assert payload["total_items"] == 0
        assert payload["limit"] == 0
        assert payload["error"] == message


def test_scada_topology_root_groups_subfleets_and_unassigned_units(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 500, 1000, 1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("North")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1, 3])
    runtime.generators[0].state = State.RUNNING
    runtime.generators[0].output_kw = 250.0
    runtime.state_store.refresh(runtime.generators)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().get("/api/scada/topology")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["root"]["command_scope"] == "fleet"
    assert payload["selected_node"]["id"] == "fleet"
    children_by_label = {child["label"]: child for child in payload["children"]}
    assert children_by_label["North"]["unit_count"] == 2
    assert children_by_label["North"]["command_scope"] == "subfleet"
    assert children_by_label["Unassigned Generators"]["unit_count"] == 2
    assert children_by_label["North"]["running_count"] == 1
    assert children_by_label["North"]["total_output_kw"] == 250.0


def test_scada_topology_drills_from_subfleet_to_size_and_range(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 500, 500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Critical")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1, 2, 3])
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    group_response = client.get(
        "/api/scada/topology",
        query_string={"node_id": f"group|subfleet|{subfleet['id']}", "range_size": "1"},
    )

    assert group_response.status_code == 200
    group_payload = group_response.get_json()
    assert group_payload["selected_node"]["label"] == "Critical"
    assert group_payload["children"][0]["type"] == "size"
    size_node_id = group_payload["children"][0]["id"]

    size_response = client.get(
        "/api/scada/topology",
        query_string={"node_id": size_node_id, "range_size": "1"},
    )

    assert size_response.status_code == 200
    size_payload = size_response.get_json()
    assert size_payload["selected_node"]["type"] == "size"
    assert {child["type"] for child in size_payload["children"]} == {"range"}
    range_node_id = size_payload["children"][0]["id"]

    range_response = client.get("/api/scada/topology", query_string={"node_id": range_node_id})

    assert range_response.status_code == 200
    range_payload = range_response.get_json()
    assert range_payload["selected_node"]["type"] == "range"
    assert range_payload["children"][0]["type"] == "unit"
    assert range_payload["children"][0]["command_scope"] == "unit"


def test_scada_topology_returns_404_for_unknown_node(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().get("/api/scada/topology", query_string={"node_id": "unit|999"})

    assert response.status_code == 404
    assert response.get_json()["error"] == "SCADA topology node not found."


def test_scada_alarms_follow_hierarchy_scope(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Critical")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1])
    runtime.enqueue_unit_command(1, 20)
    runtime.simulation_step()
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    fleet_response = client.get("/api/scada/alarms")

    assert fleet_response.status_code == 200
    fleet_payload = fleet_response.get_json()
    assert fleet_payload["alarm_count"] == 1
    assert fleet_payload["alarms"][0]["unit_id"] == 1
    assert fleet_payload["alarms"][0]["alarm_id"] == 4
    assert fleet_payload["alarms"][0]["subfleet_name"] == "Critical"

    scoped_response = client.get("/api/scada/alarms", query_string={"node_id": f"group|subfleet|{subfleet['id']}"})

    assert scoped_response.status_code == 200
    scoped_payload = scoped_response.get_json()
    assert scoped_payload["alarm_count"] == 1
    assert scoped_payload["alarms"][0]["alarm_name"] == ALARM_NAMES[4]

    unassigned_response = client.get("/api/scada/alarms", query_string={"node_id": "group|unassigned|-"})

    assert unassigned_response.status_code == 200
    assert unassigned_response.get_json()["alarm_count"] == 0


def test_subfleet_query_assignment_and_removal_endpoints(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000, 1000, 2000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime.generators[1].state = State.RUNNING
    runtime.generators[2].state = State.RUNNING
    runtime.state_store.refresh(runtime.generators)
    subfleet = runtime.create_subfleet("Peak")
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    assigned = client.post(
        f"/api/subfleets/{subfleet['id']}/members/query",
        json={"state": "RUNNING", "rated_kw": 1000, "subfleet_scope": "unassigned"},
    )
    assert assigned.status_code == 200
    payload = assigned.get_json()
    assert payload["matched_unit_count"] == 2
    assert payload["assigned_unit_ids"] == [2, 3]

    removed = client.post(
        f"/api/subfleets/{subfleet['id']}/members/query-remove",
        json={"search": "3"},
    )
    assert removed.status_code == 200
    payload = removed.get_json()
    assert payload["matched_unit_count"] == 1
    assert payload["removed_unit_ids"] == [3]


def test_mutation_endpoints_reject_non_object_json_payloads(tmp_path):
    settings = _settings(tmp_path, num_generators=3, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Alpha")
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()
    cases = [
        ("post", "/api/subfleets"),
        ("patch", f"/api/subfleets/{subfleet['id']}"),
        ("post", f"/api/subfleets/{subfleet['id']}/members"),
        ("post", f"/api/subfleets/{subfleet['id']}/members/query"),
        ("post", f"/api/subfleets/{subfleet['id']}/members/query-remove"),
        ("post", f"/api/subfleets/{subfleet['id']}/commands"),
        ("post", "/api/scenarios/run"),
    ]

    for method, url in cases:
        response = getattr(client, method)(url, json=[])
        payload = response.get_json()

        assert response.status_code == 400, url
        assert payload["ok"] is False, url
        assert payload["error"] == "JSON request body must be an object.", url


def test_subfleet_command_endpoint_queues_member_commands(tmp_path):
    settings = _settings(tmp_path, num_generators=3, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Alpha")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1, 2])
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.post(f"/api/subfleets/{subfleet['id']}/commands", json={"cmd": 10})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["queued_count"] == 2
    assert payload["queued_units"] == [1, 2]


def test_subfleet_command_endpoint_rejects_empty_subfleet(tmp_path):
    settings = _settings(tmp_path, num_generators=2, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Empty Group")
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().post(f"/api/subfleets/{subfleet['id']}/commands", json={"cmd": 10})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Sub-fleet has no members."
    assert runtime.command_router.queue_depth() == 0


def test_subfleet_command_endpoint_rejects_unsupported_command(tmp_path):
    settings = _settings(tmp_path, num_generators=2, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Alpha")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1])
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().post(f"/api/subfleets/{subfleet['id']}/commands", json={"cmd": 999})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Unsupported command: 999."
    assert runtime.command_router.queue_depth() == 0


def test_subfleet_command_endpoint_rejects_when_command_queue_is_full(tmp_path):
    settings = _settings(
        tmp_path,
        generator_ratings=[500, 500],
        tick_seconds=0.0,
        command_queue_max=1,
        command_queue_unit_limit=2,
    )
    runtime = SimulatorRuntime(settings)
    subfleet = runtime.create_subfleet("Alpha")
    runtime.assign_generators_to_subfleet(subfleet["id"], [1])
    assert runtime.enqueue_unit_command(1, 1) is True
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().post(f"/api/subfleets/{subfleet['id']}/commands", json={"cmd": 10})

    assert response.status_code == 429
    assert response.get_json()["error"] == "Command queue is full; try again after the next simulation tick."


def test_fleet_commands_are_bounded_by_unit_target_limit(tmp_path):
    settings = _settings(
        tmp_path,
        generator_ratings=[500, 500, 500],
        tick_seconds=0.0,
        command_queue_max=10,
        command_queue_unit_limit=3,
    )
    runtime = SimulatorRuntime(settings)

    assert runtime.enqueue_fleet_command(1) is True
    assert runtime.enqueue_fleet_command(2) is False
    assert runtime.command_router.queue_depth() == 1


def test_runtime_rejects_unsupported_command_values(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)

    with pytest.raises(ValueError, match="Unsupported command: 999"):
        runtime.enqueue_unit_command(1, 999)
    with pytest.raises(ValueError, match="Unsupported command: 0"):
        runtime.enqueue_fleet_command(0)
    with pytest.raises(ValueError, match="cmd must be a whole number"):
        runtime.enqueue_fleet_command(1.5)

    assert runtime.command_router.queue_depth() == 0


def test_modbus_unsupported_command_is_ignored_and_cleared(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime.modbus_context[1].setValues(3, 20, [999])

    runtime.simulation_step()

    assert runtime.modbus_context[1].getValues(3, 20, 1) == [0]
    assert runtime.command_router.queue_depth() == 0
    assert runtime.generators[0].state == State.STOPPED


def test_unit_group_command_queues_many_units_as_single_command(tmp_path):
    settings = _settings(
        tmp_path,
        generator_ratings=[500] * 300,
        tick_seconds=0.0,
        command_queue_max=1,
        command_queue_unit_limit=300,
    )
    runtime = SimulatorRuntime(settings)

    result = runtime.enqueue_unit_group_command(list(range(1, 301)), 9)

    assert result["queued_count"] == 300
    assert runtime.command_router.queue_depth() == 1

    runtime.simulation_step()

    assert all(generator.auto_mode is False for generator in runtime.generators)


def test_fleet_command_sequence_counts_each_unit_once(tmp_path):
    settings = _settings(
        tmp_path,
        generator_ratings=[500, 500, 500],
        tick_seconds=0.0,
        command_queue_max=10,
        command_queue_unit_limit=3,
    )
    runtime = SimulatorRuntime(settings)
    for generator in runtime.generators:
        generator._utility_available = False
        generator.utility_breaker = True
        generator.gen_breaker = True

    assert runtime.enqueue_fleet_command_sequence(RESTORE_UTILITY_COMMAND_SEQUENCE) is True
    assert runtime.command_router.queue_depth() == 1

    runtime.simulation_step()

    for generator in runtime.generators:
        assert generator._utility_available is True
        assert generator.transfer_mode == "TRANSFER"
        assert generator.gen_breaker is False


def test_restore_utility_sequence_fits_default_2000_unit_queue(tmp_path):
    settings = _settings(tmp_path, num_generators=2000, tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)

    assert runtime.enqueue_fleet_command_sequence(RESTORE_UTILITY_COMMAND_SEQUENCE) is True
    assert runtime.command_router.queue_depth() == 1


def test_socket_restore_utility_sequence_queues_one_fleet_command(tmp_path):
    settings = _settings(
        tmp_path,
        generator_ratings=[500, 500, 500],
        tick_seconds=0.0,
        command_queue_max=10,
        command_queue_unit_limit=3,
    )
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit("fleet_command_sequence", {"commands": list(RESTORE_UTILITY_COMMAND_SEQUENCE)})

    assert runtime.command_router.queue_depth() == 1
    assert not [event for event in client.get_received() if event["name"] == "command_rejected"]
    client.disconnect()


def test_socket_rejects_unknown_fleet_command_sequence(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit("fleet_command_sequence", {"commands": [11, 4]})

    assert runtime.command_router.queue_depth() == 0
    rejected = [event for event in client.get_received() if event["name"] == "command_rejected"]
    assert rejected[0]["args"][0]["error"] == "Unsupported fleet command sequence."
    client.disconnect()


def test_socket_rejects_unsupported_unit_command(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit("command", {"unit_id": 1, "cmd": 999})

    assert runtime.command_router.queue_depth() == 0
    rejected = [event for event in client.get_received() if event["name"] == "command_rejected"]
    assert rejected[0]["args"][0]["error"] == "Unsupported command: 999."
    client.disconnect()


def test_socket_bulk_command_queues_selected_units_once(tmp_path):
    settings = _settings(
        tmp_path,
        generator_ratings=[500] * 300,
        tick_seconds=0.0,
        command_queue_max=1,
        command_queue_unit_limit=300,
    )
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit("bulk_command", {"unit_ids": list(range(1, 301)), "cmd": 9})

    assert runtime.command_router.queue_depth() == 1
    assert not [event for event in client.get_received() if event["name"] == "command_rejected"]
    client.disconnect()


def test_socket_bulk_command_rejects_invalid_unit_list(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit("bulk_command", {"unit_ids": "1,2", "cmd": 9})

    assert runtime.command_router.queue_depth() == 0
    rejected = [event for event in client.get_received() if event["name"] == "command_rejected"]
    assert rejected[0]["args"][0]["error"] == "unit_ids must be a list."
    client.disconnect()


@pytest.mark.parametrize(
    ("event_name", "expected_error"),
    [
        ("watch_unit_detail", "watch_unit_detail payload must be an object."),
        ("command", "command payload must be an object."),
        ("bulk_command", "bulk_command payload must be an object."),
        ("set_setpoint", "set_setpoint payload must be an object."),
        ("fleet_command", "fleet_command payload must be an object."),
        ("fleet_command_sequence", "fleet_command_sequence payload must be an object."),
    ],
)
def test_socket_handlers_reject_non_object_payloads(tmp_path, event_name, expected_error):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit(event_name, "not-an-object")

    assert runtime.command_router.queue_depth() == 0
    rejected = [event for event in client.get_received() if event["name"] == "command_rejected"]
    assert rejected[0]["args"][0]["error"] == expected_error
    client.disconnect()


def test_socket_setpoint_rejects_invalid_values_without_mutating_generator(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit("set_setpoint", {"unit_id": 1, "setpoint_kw": "nan"})

    assert runtime.generators[0].parallel_setpoint_kw == 300.0
    rejected = [event for event in client.get_received() if event["name"] == "command_rejected"]
    assert rejected[0]["args"][0]["error"] == "setpoint_kw must be a finite number."
    client.disconnect()


def test_socket_watch_detail_rejects_unknown_unit_without_subscribing(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, socketio, _ = create_app(settings=settings, runtime=runtime)
    client = socketio.test_client(app)

    client.emit("watch_unit_detail", {"unit_id": 999})

    assert runtime.event_publisher.watched_units() == {}
    rejected = [event for event in client.get_received() if event["name"] == "command_rejected"]
    assert rejected[0]["args"][0]["error"] == "Invalid unit ID."
    client.disconnect()


def test_runtime_reads_parallel_setpoint_from_modbus_register(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime.modbus_context[1].setValues(3, 16, [2500])

    runtime.simulation_step()

    assert runtime.generators[0].parallel_setpoint_kw == 250.0


# ---------------------------------------------------------------------------
# Additional API endpoint coverage
# ---------------------------------------------------------------------------

def test_api_health_returns_ok_when_unconfigured(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["configured"] is False
    assert payload["num_generators"] == 0


def test_api_live_reports_web_liveness_before_configuration(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))

    response = app.test_client().get("/api/live")

    assert response.status_code == 200
    assert response.get_json() == {"live": True, "ok": True}


def test_api_ready_requires_configured_runtime_and_modbus(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))

    response = app.test_client().get("/api/ready")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["ready"] is False
    assert payload["configured"] is False
    assert payload["modbus_running"] is False


def test_api_health_returns_configured_after_startup(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["num_generators"] == 2


def test_api_ready_returns_ok_when_modbus_thread_is_running(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime._loop_started = True
    app, _, controller = create_app(settings=settings, runtime=runtime)
    fake_thread = Mock()
    fake_thread.is_alive.return_value = True
    controller.modbus_server = Mock(is_running=True)

    response = app.test_client().get("/api/ready")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["configured"] is True
    assert payload["modbus_running"] is True
    assert payload["num_generators"] == 2


def test_api_fleet_summary_returns_unconfigured_when_not_started(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.get("/api/fleet/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is False
    assert payload["summary"] is None


def test_api_fleet_summary_returns_summary_when_configured(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.get("/api/fleet/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert "summary" in payload


def test_api_generator_detail_returns_404_when_not_configured(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.get("/api/generators/1")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["configured"] is False
    assert payload["generator"] is None
    assert payload["error"] == "Simulator is not started."


def test_api_generator_detail_returns_data_for_valid_unit(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500, 1000], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.get("/api/generators/1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["generator"]["unit_id"] == 1


def test_api_generator_detail_returns_404_for_invalid_unit(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.get("/api/generators/999")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["unit_id"] == 999
    assert payload["generator"] is None
    assert payload["error"] == "Generator unit 999 was not found."


def test_api_generator_registers_returns_data(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.get("/api/generators/1/registers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["unit_id"] == 1
    assert "registers" in payload


def test_api_generator_registers_returns_404_when_not_configured(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.get("/api/generators/1/registers")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["configured"] is False
    assert payload["registers"] is None
    assert payload["error"] == "Simulator is not started."


def test_api_generator_registers_returns_404_for_invalid_unit(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.get("/api/generators/999/registers")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["unit_id"] == 999
    assert payload["registers"] is None
    assert payload["error"] == "Generator unit 999 was not found."


def test_api_scenarios_returns_catalog_when_unconfigured(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.get("/api/scenarios")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is False
    assert isinstance(payload["catalog"], list)
    assert len(payload["catalog"]) > 0


def test_api_scenarios_run_returns_error_when_not_started(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.post("/api/scenarios/run", json={"scenario_id": "e-stop-drill"})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_api_scenarios_run_requires_scenario_id(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.post("/api/scenarios/run", json={})

    assert response.status_code == 400
    assert "scenario_id is required" in response.get_json()["error"]


def test_api_scenarios_run_and_stop_lifecycle(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    run_resp = client.post("/api/scenarios/run", json={"scenario_id": "e-stop-drill"})
    assert run_resp.status_code == 200
    assert run_resp.get_json()["ok"] is True

    stop_resp = client.post("/api/scenarios/stop")
    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["ok"] is True


def test_api_scenarios_run_rejects_unknown_scenario(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    response = client.post("/api/scenarios/run", json={"scenario_id": "ghost-scenario"})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_api_scenarios_stop_returns_error_when_not_started(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.post("/api/scenarios/stop")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_api_runbook_save_rejects_oversized_request_body(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().post(
        "/api/runbooks",
        data="x" * (MAX_RUNBOOK_REQUEST_BYTES + 1),
        content_type="application/json",
    )

    assert response.status_code == 413
    assert response.get_json()["error"] == "Runbook request body is too large."


def test_api_returns_json_for_global_oversized_request_body(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path, num_generators=1))

    response = app.test_client().post(
        "/api/startup",
        data=" " * (MAX_RUNBOOK_REQUEST_BYTES + 1),
        content_type="application/json",
    )

    assert response.status_code == 413
    assert response.is_json
    assert response.get_json() == {"ok": False, "error": "Request body is too large."}


def test_api_runbook_save_rejects_url_unsafe_id(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().post("/api/runbooks", json=_runbook_payload("../unsafe"))

    assert response.status_code == 400
    assert "Runbook id may contain only" in response.get_json()["error"]


def test_api_runbooks_clears_active_after_terminal_completion(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    save_resp = client.post("/api/runbooks", json=_runbook_payload("terminal-runbook"))
    assert save_resp.status_code == 201

    apply_resp = client.post("/api/runbooks/terminal-runbook/apply")
    assert apply_resp.status_code == 200
    assert apply_resp.get_json()["runbook"]["status"] == "running"

    runtime._execute_due_runbook_steps()

    list_resp = client.get("/api/runbooks")
    assert list_resp.status_code == 200
    payload = list_resp.get_json()
    assert payload["active_runbook"] is None
    assert payload["last_runbook_report"]["id"] == "terminal-runbook"
    assert payload["last_runbook_report"]["status"] == "completed"

    health_resp = client.get("/api/health")
    assert health_resp.status_code == 200
    assert health_resp.get_json()["active_runbook"] is None

    stop_resp = client.post("/api/runbooks/stop")
    assert stop_resp.status_code == 400
    assert stop_resp.get_json()["error"] == "No runbook is currently running."


def test_api_runbook_stop_clears_active_and_keeps_last_report(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    save_resp = client.post("/api/runbooks", json=_runbook_payload("cancel-runbook", at_seconds=300))
    assert save_resp.status_code == 201

    apply_resp = client.post("/api/runbooks/cancel-runbook/apply")
    assert apply_resp.status_code == 200
    assert apply_resp.get_json()["runbook"]["status"] == "running"

    stop_resp = client.post("/api/runbooks/stop")
    assert stop_resp.status_code == 200
    stop_payload = stop_resp.get_json()
    assert stop_payload["active_runbook"] is None
    assert stop_payload["runbook"]["status"] == "cancelled"
    assert stop_payload["last_runbook_report"]["status"] == "cancelled"

    list_resp = client.get("/api/runbooks")
    assert list_resp.status_code == 200
    payload = list_resp.get_json()
    assert payload["active_runbook"] is None
    assert payload["last_runbook_report"]["id"] == "cancel-runbook"
    assert payload["last_runbook_report"]["status"] == "cancelled"


def test_api_runbook_export_uses_last_report_after_completion(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    app, _, _ = create_app(settings=settings, runtime=runtime)
    client = app.test_client()

    save_resp = client.post("/api/runbooks", json=_runbook_payload("exported-terminal-runbook"))
    assert save_resp.status_code == 201
    apply_resp = client.post("/api/runbooks/exported-terminal-runbook/apply")
    assert apply_resp.status_code == 200
    runtime._execute_due_runbook_steps()

    export_resp = client.get("/api/export/runbook.json")

    assert export_resp.status_code == 200
    assert export_resp.headers["Content-Disposition"] == "attachment; filename=exported-terminal-runbook-report.json"
    payload = export_resp.get_json()
    assert payload["id"] == "exported-terminal-runbook"
    assert payload["status"] == "completed"


def test_api_metrics_returns_unconfigured_when_not_started(tmp_path):
    app, _, _ = create_app(settings=_settings(tmp_path))
    client = app.test_client()

    response = client.get("/api/metrics")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is False
    assert payload["metrics"] is None


def test_api_startup_with_generator_ratings_payload(tmp_path):
    """Startup via unit_configs / ratings-style payload."""
    app, _, controller = create_app(settings=_settings(tmp_path, num_generators=3))
    client = app.test_client()

    with patch.object(controller, "configure_simulator") as mocked:
        from unittest.mock import Mock
        fake_runtime = Mock()
        fake_runtime.settings.num_generators = 2
        fake_runtime.settings.modbus_port = 5020
        mocked.return_value = fake_runtime
        response = client.post(
            "/api/startup",
            json={"size_counts": {"500": 1, "1000": 1, "1500": 0, "2000": 0, "2500": 0}},
        )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_mutating_http_requests_are_rate_limited(tmp_path):
    settings = _settings(tmp_path, num_generators=1, http_rate_limit=2, http_rate_window=60)
    app, _, _ = create_app(settings=settings)
    client = app.test_client()

    first = client.post("/api/runbooks", json={})
    second = client.post("/api/runbooks", json={})
    third = client.post("/api/runbooks", json={})

    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429
    assert third.get_json()["error"] == "Too many requests."


def test_state_save_keeps_previous_file_as_backup(tmp_path):
    settings = _settings(tmp_path, num_generators=1)
    runtime = SimulatorRuntime(settings)
    runtime.save_state()
    original = settings.state_file.read_text(encoding="utf-8")
    runtime.generators[0].run_hours = 12.5
    runtime.save_state()

    backup = settings.state_file.with_name(f"{settings.state_file.name}.bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original
    assert runtime.last_save_error is None


def test_runbook_export_sanitizes_content_disposition(tmp_path):
    settings = _settings(tmp_path, generator_ratings=[500], tick_seconds=0.0)
    runtime = SimulatorRuntime(settings)
    runtime._last_runbook_report = {"id": 'ok"; filename="evil\r\n.json', "status": "completed", "steps": []}
    app, _, _ = create_app(settings=settings, runtime=runtime)

    response = app.test_client().get("/api/export/runbook.json")

    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert '"' not in disposition.split("filename=", 1)[-1]
    assert disposition == "attachment; filename=ok-filename-evil-.json-report.json"
