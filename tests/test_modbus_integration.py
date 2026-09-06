"""
Modbus integration tests for Generator Fleet Simulator.

These tests start a real ModbusTcpServer on a dynamic port and verify
round-trip reads/writes via a pymodbus TCP client.  They are
integration tests — they bind real sockets and test the full
client → server path.
"""

import gc
import logging
import socket
import time

import pytest
from pymodbus.client import ModbusTcpClient

from modbus_server import ModbusServerThread, create_modbus_context, sync_generator_to_modbus


def _free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def modbus_server():
    """Start a real ModbusTcpServer on a free port and yield (server, context, port)."""
    port = _free_port()
    context = create_modbus_context(num_generators=3)
    server = ModbusServerThread(context, host="127.0.0.1", port=port)
    server.start()
    yield server, context, port
    try:
        server.stop(timeout=3.0)
    except Exception:
        pass  # daemon thread — safe to ignore cleanup errors in tests


@pytest.fixture()
def client(modbus_server):
    """Connect a pymodbus TCP client to the running test server."""
    _, _, port = modbus_server
    c = ModbusTcpClient("127.0.0.1", port=port)
    connected = c.connect()
    assert connected, "Could not connect to Modbus test server"
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_client_reads_zero_on_fresh_context(modbus_server, client):
    """All registers initialise to 0."""
    _, _, _ = modbus_server
    result = client.read_holding_registers(address=0, count=5, slave=1)
    assert not result.isError()
    assert result.registers == [0, 0, 0, 0, 0]


def test_server_write_is_visible_to_client(modbus_server, client):
    """Values written server-side appear when the client reads them."""
    _, context, _ = modbus_server
    sync_generator_to_modbus(context, 1, {0: 1234, 4: 750})

    result = client.read_holding_registers(address=0, count=5, slave=1)
    assert not result.isError()
    assert result.registers[0] == 1234
    assert result.registers[4] == 750


def test_client_write_single_register_is_visible_server_side(modbus_server, client):
    """FC-6 write from client is reflected in the server datastore."""
    _, context, _ = modbus_server
    resp = client.write_register(address=20, value=7, slave=1)
    assert not resp.isError()

    slave = context[1]
    stored = slave.getValues(3, 20, 1)
    assert stored == [7]


def test_client_write_multiple_registers(modbus_server, client):
    """FC-16 write from client updates a block of registers."""
    _, context, _ = modbus_server
    resp = client.write_registers(address=0, values=[100, 200, 300], slave=2)
    assert not resp.isError()

    slave = context[2]
    assert slave.getValues(3, 0, 3) == [100, 200, 300]


def test_independent_unit_ids_do_not_share_registers(modbus_server, client):
    """Writes to unit 1 do not affect unit 2."""
    _, context, _ = modbus_server
    sync_generator_to_modbus(context, 1, {0: 9999})
    sync_generator_to_modbus(context, 2, {0: 1111})

    result1 = client.read_holding_registers(address=0, count=1, slave=1)
    result2 = client.read_holding_registers(address=0, count=1, slave=2)
    assert not result1.isError()
    assert not result2.isError()
    assert result1.registers[0] == 9999
    assert result2.registers[0] == 1111


def test_command_register_write_and_clear_round_trip(modbus_server, client):
    """Client writes a command; server reads and clears it."""
    from modbus_server import read_command_register
    _, context, _ = modbus_server

    resp = client.write_register(address=20, value=3, slave=1)
    assert not resp.isError()

    cmd = read_command_register(context, 1)
    assert cmd == 3

    # After read_command_register the register is cleared
    slave = context[1]
    assert slave.getValues(3, 20, 1) == [0]


def test_uint16_clamping_on_server_side_write(modbus_server, client):
    """sync_generator_to_modbus clamps values that exceed UINT16 range."""
    _, context, _ = modbus_server
    # Write a value that exceeds 65535 — should be clamped
    sync_generator_to_modbus(context, 1, {5: 70000})

    result = client.read_holding_registers(address=5, count=1, slave=1)
    assert not result.isError()
    assert result.registers[0] == 65535


def test_all_generator_units_accessible(modbus_server, client):
    """All 3 unit IDs created by the context are reachable."""
    _, context, _ = modbus_server
    for unit_id in (1, 2, 3):
        sync_generator_to_modbus(context, unit_id, {0: unit_id * 10})

    for unit_id in (1, 2, 3):
        result = client.read_holding_registers(address=0, count=1, slave=unit_id)
        assert not result.isError()
        assert result.registers[0] == unit_id * 10


def test_server_shutdown_after_client_session_does_not_leave_pending_tasks(caplog):
    port = _free_port()
    server = ModbusServerThread(create_modbus_context(num_generators=1), host="127.0.0.1", port=port)
    client = ModbusTcpClient("127.0.0.1", port=port)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        server.start()
        assert client.connect()
        result = client.read_holding_registers(address=0, count=1, slave=1)
        assert not result.isError()
        client.close()
        server.stop(timeout=3.0)
        gc.collect()

    assert not any("Task was destroyed but it is pending" in record.message for record in caplog.records)
