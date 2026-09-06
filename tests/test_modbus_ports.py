import socket
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest
from pymodbus.client import ModbusTcpClient

from main import AppController, Settings, SimulatorRuntime, create_app
from modbus_server import (ModbusServerGroup, create_modbus_context, modbus_address,
                           modbus_endpoints, read_command_register, read_register,
                           sync_generator_to_modbus)


def free_range(count):
    for _ in range(100):
        with ExitStack() as stack:
            first = stack.enter_context(socket.socket())
            first.bind(('127.0.0.1', 0))
            base = first.getsockname()[1]
            if base + count - 1 > 65535:
                continue
            try:
                for port in range(base + 1, base + count):
                    sock = stack.enter_context(socket.socket())
                    sock.bind(('127.0.0.1', port))
            except OSError:
                continue
            return base
    raise RuntimeError('No consecutive free test ports')


@pytest.mark.parametrize('generator_id,expected', [
    (1, (5020, 1)), (255, (5020, 255)), (256, (5021, 1)),
    (510, (5021, 255)), (511, (5022, 1)), (2000, (5027, 215)),
])
def test_wire_mapping_boundaries(generator_id, expected):
    assert modbus_address(generator_id) == expected


@pytest.mark.parametrize('count,base', [(0, 5020), (True, 5020), (256, 65535), (2000, 65529)])
def test_invalid_ranges_rejected(count, base):
    with pytest.raises(ValueError):
        modbus_endpoints(count, base)


def test_all_2000_generators_have_distinct_real_wire_addresses():
    base = free_range(8)
    context = create_modbus_context(2000)
    for gid in range(1, 2001):
        sync_generator_to_modbus(context, gid, {0: gid})
    servers = ModbusServerGroup(context, 2000, port=base)
    servers.start()
    try:
        assert servers.is_running
        for endpoint in servers.endpoints:
            client = ModbusTcpClient('127.0.0.1', port=endpoint['port'], timeout=2)
            try:
                assert client.connect()
                for gid in range(endpoint['first_generator_id'], endpoint['last_generator_id'] + 1):
                    _, unit = modbus_address(gid, base)
                    result = client.read_holding_registers(address=0, count=1, slave=unit)
                    assert not result.isError(), gid
                    assert result.registers == [gid], gid
                # Both write function codes reach the global generator datastore.
                gid = endpoint['first_generator_id']
                assert not client.write_register(address=20, value=7, slave=1).isError()
                assert read_command_register(context, gid) == 7
                assert read_command_register(context, gid) == 0
                assert not client.write_registers(address=16, values=[1234], slave=1).isError()
                assert read_register(context, gid, 16) == 1234
            finally:
                client.close()
        assert read_register(context, 2, 16) == 0
    finally:
        servers.stop()
    assert not servers.is_running
    for endpoint in servers.endpoints:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', endpoint['port']))


def test_failed_second_listener_closes_first_listener():
    base = free_range(2)
    servers = ModbusServerGroup(create_modbus_context(256), 256, port=base)
    with socket.socket() as blocker:
        blocker.bind(('127.0.0.1', base + 1))
        blocker.listen()
        with pytest.raises(RuntimeError, match='Could not bind'):
            servers.start()
    assert not servers.is_running
    assert all(not server._thread or not server._thread.is_alive() for server in servers.servers)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', base))


def test_missing_listener_makes_group_unready():
    group = ModbusServerGroup(create_modbus_context(256), 256)
    group.servers = [Mock(is_running=True), Mock(is_running=False)]
    assert not group.is_running


def test_api_and_export_report_global_and_wire_ids(tmp_path):
    settings = Settings(num_generators=2000, modbus_port=5020, public_modbus_port=5021,
                        state_file=tmp_path/'state', runbooks_file=tmp_path/'runbooks')
    app, _, _ = create_app(settings, SimulatorRuntime(settings))
    client = app.test_client()
    payload = client.get('/api/generators/2000/registers').json
    assert payload['unit_id'] == 2000
    assert payload['modbus'] == {'port': 5027, 'public_port': 5028, 'unit_id': 215}
    endpoints = client.get('/api/modbus/endpoints').json['endpoints']
    assert len(endpoints) == 8
    assert endpoints[1]['first_generator_id'] == 256
    assert endpoints[-1]['public_port'] == 5028
    assert 'modbus_mapping,2000,5027,215,5028' in client.get('/api/export/config.csv').text


def test_occupied_expansion_port_preserves_running_fleet(tmp_path):
    base = free_range(2)
    settings = Settings(num_generators=2, modbus_port=base,
                        state_file=tmp_path/'state', runbooks_file=tmp_path/'runbooks')
    runtime = Mock()
    controller = AppController(settings, runtime)
    with socket.socket() as blocker:
        blocker.bind(('127.0.0.1', base + 1))
        blocker.listen()
        with pytest.raises(RuntimeError, match='unavailable'):
            controller.configure_simulator(Mock(), {500: 256}, base)
    assert controller.runtime is runtime
    runtime.stop.assert_not_called()
    assert settings.num_generators == 2


def test_invalid_public_port_range_is_rejected_before_stopping(tmp_path):
    settings = Settings(num_generators=2, public_modbus_port=65535,
                        state_file=tmp_path/'state', runbooks_file=tmp_path/'runbooks')
    runtime = Mock()
    controller = AppController(settings, runtime)
    with pytest.raises(ValueError, match='range exceeds'):
        controller.configure_simulator(Mock(), {500: 256}, 5020)
    runtime.stop.assert_not_called()


def test_start_failure_restores_previous_configuration(tmp_path):
    settings = Settings(num_generators=2, modbus_port=5020,
                        state_file=tmp_path/'state', runbooks_file=tmp_path/'runbooks')
    controller = AppController(settings, Mock())
    old_ratings = settings.generator_ratings[:]
    with patch('main.ensure_port_available'), patch.object(controller, '_stop_runtime'), \
         patch.object(controller, '_start_runtime', side_effect=[RuntimeError('bind race'), Mock()]) as start:
        with pytest.raises(RuntimeError, match='bind race'):
            controller.configure_simulator(Mock(), {500: 256}, 5500)
    assert start.call_count == 2
    assert settings.generator_ratings == old_ratings
    assert settings.modbus_port == 5020
