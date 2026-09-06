import asyncio
import gc
import logging
from unittest.mock import patch

import pytest

from modbus_server import (
    MODBUS_HOST,
    ModbusServerThread,
    create_modbus_context,
    read_command_register,
    read_register,
    sync_generator_to_modbus,
)


def test_modbus_server_default_host_is_local_only():
    assert MODBUS_HOST == "127.0.0.1"


def test_sync_generator_to_modbus_writes_register_values():
    context = create_modbus_context()

    sync_generator_to_modbus(context, 1, {0: 1234, 4: 567})

    slave = context[1]
    assert slave.getValues(3, 0, 1) == [1234]
    assert slave.getValues(3, 4, 1) == [567]


def test_read_command_register_returns_value_and_clears_it():
    context = create_modbus_context()
    slave = context[1]
    slave.setValues(3, 20, [9])

    cmd = read_command_register(context, 1)

    assert cmd == 9
    assert slave.getValues(3, 20, 1) == [0]


def test_read_register_returns_value_without_clearing_it():
    context = create_modbus_context()
    slave = context[1]
    slave.setValues(3, 16, [4321])

    value = read_register(context, 1, 16)

    assert value == 4321
    assert slave.getValues(3, 16, 1) == [4321]


def test_modbus_server_thread_surfaces_delayed_startup_failures():
    class FailingServer:
        def __init__(self, *args, **kwargs):
            pass

        async def serve_forever(self, *, background=False):
            assert background is True
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")

    server = ModbusServerThread(create_modbus_context(), host="127.0.0.1", port=55099)

    with patch("modbus_server.ModbusTcpServer", FailingServer):
        with pytest.raises(RuntimeError, match="Modbus TCP server failed to start: boom"):
            server.start()


def test_modbus_server_stop_cancels_pending_event_loop_tasks(caplog):
    server = ModbusServerThread(create_modbus_context(), host="127.0.0.1", port=0)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        server.start()
        server.stop()
        gc.collect()

    assert server._thread is not None
    assert not server._thread.is_alive()
    assert not any("Task was destroyed but it is pending" in record.message for record in caplog.records)


def test_modbus_server_run_cancels_tasks_before_closing_loop(caplog):
    class ServerWithPendingTask(ModbusServerThread):
        async def _serve(self):
            asyncio.create_task(asyncio.sleep(60), name="test-pending-task")
            self._ready.set()

    server = ServerWithPendingTask(create_modbus_context(), host="127.0.0.1", port=55098)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        server._run()
        gc.collect()

    assert not any("Task was destroyed but it is pending" in record.message for record in caplog.records)
