"""
Modbus TCP server for the generator fleet simulator.

Runs up to 255 generator units per TCP port, starting at port 5020.
Each unit's holding registers are synced from the corresponding
Generator instance every simulation tick.
"""

import asyncio
import logging
from threading import Event
from threading import Thread

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext

try:
    from pymodbus.datastore import ModbusDeviceContext as _ModbusUnitContext
except ImportError:
    from pymodbus.datastore import ModbusSlaveContext as _ModbusUnitContext
from pymodbus.server import ModbusTcpServer

log = logging.getLogger(__name__)

MODBUS_HOST = "127.0.0.1"
MODBUS_PORT = 5020
DEFAULT_NUM_GENERATORS = 15
NUM_REGISTERS = 32  # reserve space beyond register 20 for compatibility
UNITS_PER_PORT = 255


def modbus_address(generator_id: int, base_port: int = MODBUS_PORT) -> tuple[int, int]:
    """Return (TCP port, wire unit ID) for a one-based generator ID."""
    if isinstance(generator_id, bool) or not isinstance(generator_id, int) or generator_id < 1:
        raise ValueError("Generator ID must be a positive whole number.")
    if isinstance(base_port, bool) or not isinstance(base_port, int) or not 1 <= base_port <= 65535:
        raise ValueError("Modbus base port must be between 1 and 65535.")
    offset, unit = divmod(generator_id - 1, UNITS_PER_PORT)
    port = base_port + offset
    if port > 65535:
        raise ValueError("Modbus port range exceeds 65535 for this fleet size.")
    return port, unit + 1


def modbus_endpoints(num_generators: int, base_port: int = MODBUS_PORT) -> list[dict[str, int]]:
    """Describe the consecutive ports required for a configured fleet."""
    last_port, _ = modbus_address(num_generators, base_port)
    return [
        {"port": port, "first_generator_id": offset * UNITS_PER_PORT + 1,
         "last_generator_id": min((offset + 1) * UNITS_PER_PORT, num_generators),
         "first_unit_id": 1, "last_unit_id": min(UNITS_PER_PORT, num_generators - offset * UNITS_PER_PORT)}
        for offset, port in enumerate(range(base_port, last_port + 1))
    ]


def _server_context(slaves: dict) -> ModbusServerContext:
    try:
        return ModbusServerContext(devices=slaves, single=False)
    except TypeError:
        return ModbusServerContext(slaves=slaves, single=False)


def create_modbus_context(num_generators: int = DEFAULT_NUM_GENERATORS) -> ModbusServerContext:
    """Create a Modbus server context with one unit per generator."""
    slaves = {}
    for unit_id in range(1, num_generators + 1):
        # Create holding registers (address 0..20, initialized to 0)
        hr_block = ModbusSequentialDataBlock(0, [0] * NUM_REGISTERS)
        slave = _ModbusUnitContext(
            di=ModbusSequentialDataBlock(0, [0] * 1),  # unused
            co=ModbusSequentialDataBlock(0, [0] * 1),  # unused
            hr=hr_block,
            ir=ModbusSequentialDataBlock(0, [0] * 1),  # unused
        )
        slaves[unit_id] = slave
    return _server_context(slaves)


def sync_generator_to_modbus(context: ModbusServerContext, unit_id: int,
                              register_values: dict):
    """Write generator register values into the Modbus context.

    Called from the simulation thread each tick. The pymodbus datastore
    is thread-safe for individual setValues calls.
    """
    slave = context[unit_id]
    for reg_addr, value in register_values.items():
        # Clamp to unsigned 16-bit
        value = max(0, min(65535, int(value)))
        # pymodbus sequential block: setValues(fc, address, [values])
        # fc=3 => holding registers
        slave.setValues(3, reg_addr, [value])


def read_command_register(context: ModbusServerContext, unit_id: int) -> int:
    """Read and clear the command register (reg 20) for a given unit."""
    slave = context[unit_id]
    values = slave.getValues(3, 20, 1)
    cmd = values[0] if values else 0
    if cmd != 0:
        # Clear after reading
        slave.setValues(3, 20, [0])
    return cmd


def read_register(context: ModbusServerContext, unit_id: int, register: int) -> int:
    """Read a single holding register value for a given unit."""
    slave = context[unit_id]
    values = slave.getValues(3, register, 1)
    return values[0] if values else 0


class ModbusServerThread:
    """Manages the Modbus TCP server in a background daemon thread."""

    def __init__(self, context: ModbusServerContext, host: str = MODBUS_HOST, port: int = MODBUS_PORT):
        self.context = context
        self.host = host
        self.port = port
        self._thread = None
        self._loop = None
        self._server = None
        self._ready = Event()
        self._error = None

    def start(self):
        """Start the Modbus server in a daemon thread."""
        self._thread = Thread(target=self._run, daemon=True, name="modbus-server")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Modbus TCP server did not report ready within 5 seconds")
        if self._error is not None:
            raise RuntimeError(f"Modbus TCP server failed to start: {self._error}") from self._error
        log.info("Modbus TCP server starting on %s:%s", self.host, self.port)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._ready.is_set() and self._error is None)

    def stop(self, timeout: float = 5.0):
        """Stop the Modbus server and wait briefly for the thread to exit."""
        if (self._error is None and self._loop is not None and self._loop.is_running()
                and self._server is not None):
            future = asyncio.run_coroutine_threadsafe(self._server.shutdown(), self._loop)
            future.result(timeout=timeout)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError(f"Modbus TCP server did not stop within {timeout} seconds")

    def _run(self):
        """Run the async Modbus server."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:  # pragma: no cover - exercised by startup failures
            self._error = exc
            log.exception("Modbus server crashed during startup or runtime")
        finally:
            if not self._ready.is_set():
                self._ready.set()
            pending_tasks = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                self._loop.run_until_complete(asyncio.gather(*pending_tasks, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _serve(self):
        """Start and run the Modbus TCP server forever."""
        self._server = ModbusTcpServer(
            self.context,
            address=(self.host, self.port),
        )
        # pymodbus >= 3.7 added serve_forever(background=True); 3.6.x exposes
        # listen() + serving separately.  Try the newer API first.
        try:
            await self._server.serve_forever(background=True)
        except TypeError:
            if not await self._server.listen():
                raise RuntimeError(f"Could not bind Modbus TCP port {self.host}:{self.port}")
        self._ready.set()
        await self._server.serving


class ModbusServerGroup:
    """Expose a shared generator datastore through consecutive TCP ports."""

    def __init__(self, context: ModbusServerContext, num_generators: int,
                 host: str = MODBUS_HOST, port: int = MODBUS_PORT):
        self.endpoints = modbus_endpoints(num_generators, port)
        self.servers = []
        for endpoint in self.endpoints:
            first, last = endpoint["first_generator_id"], endpoint["last_generator_id"]
            # Share the original datastore objects: wire writes must reach the
            # correct global generator without copying or periodic mirroring.
            local = {gid - first + 1: context[gid] for gid in range(first, last + 1)}
            self.servers.append(ModbusServerThread(_server_context(local), host, endpoint["port"]))

    @property
    def is_running(self) -> bool:
        return bool(self.servers) and all(server.is_running for server in self.servers)

    def start(self) -> None:
        try:
            for server in self.servers:
                server.start()
        except Exception:
            self.stop()
            raise

    def stop(self, timeout: float = 5.0) -> None:
        errors = []
        for server in reversed(self.servers):
            try:
                server.stop(timeout=timeout)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("Failed to stop Modbus listeners: " + "; ".join(errors))
