"""
Generator state machine, physics simulation, and alarm logic.

Each Generator instance models a diesel standby generator with configurable
rated output, correlated analog values, state transitions, auto-mode
behavior, and 30 discrete alarms.
"""

import random
from enum import Enum
from threading import Lock


class State(Enum):
    STOPPED = "STOPPED"
    CRANKING = "CRANKING"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    FAULT = "FAULT"


# Alarm bit indices (alarm word 1: bits 0-15, alarm word 2: bits 0-1)
ALARM_ENGINE_RUNNING = 0
ALARM_READY = 1
ALARM_AUTO_MODE = 2
ALARM_ESTOP = 3
ALARM_HIGH_COOLANT_TEMP = 4
ALARM_LOW_OIL_PRESSURE = 5
ALARM_OVERSPEED = 6
ALARM_OVERCRANK = 7
ALARM_LOW_COOLANT_LEVEL = 8
ALARM_HIGH_BATTERY_V = 9
ALARM_LOW_BATTERY_V = 10
ALARM_LOW_FUEL = 11
ALARM_OVER_VOLTAGE = 12
ALARM_UNDER_VOLTAGE = 13
ALARM_OVER_FREQUENCY = 14
ALARM_UNDER_FREQUENCY = 15
ALARM_OVERLOAD = 16  # alarm word 2, bit 0
ALARM_GROUND_FAULT = 17  # alarm word 2, bit 1

# Alarm word 3 (bits 0-11, indices 18-29)
ALARM_REVERSE_POWER = 18
ALARM_SYNC_CHECK_FAIL = 19
ALARM_LOAD_IMBALANCE = 20
ALARM_FUEL_LEAK = 21
ALARM_AIR_FILTER_RESTRICTED = 22
ALARM_EXHAUST_HIGH_TEMP = 23
ALARM_GEN_BEARING_TEMP = 24
ALARM_OVERCURRENT = 25
ALARM_LOSS_OF_FIELD = 26
ALARM_UTILITY_PHASE_LOSS = 27
ALARM_PARALLEL_SYNC_LOSS = 28
ALARM_SETPOINT_NOT_REACHED = 29

ALARM_NAMES = {
    0: "Engine Running",
    1: "Ready",
    2: "Auto Mode",
    3: "E-Stop",
    4: "High Coolant Temp",
    5: "Low Oil Pressure",
    6: "Overspeed",
    7: "Overcrank",
    8: "Low Coolant Level",
    9: "High Battery Voltage",
    10: "Low Battery Voltage",
    11: "Low Fuel",
    12: "Over Voltage",
    13: "Under Voltage",
    14: "Over Frequency",
    15: "Under Frequency",
    16: "Overload",
    17: "Ground Fault",
    18: "Reverse Power",
    19: "Sync Check Fail",
    20: "Load Imbalance",
    21: "Fuel Leak Detected",
    22: "Air Filter Restricted",
    23: "Exhaust High Temp",
    24: "Gen Bearing Temp",
    25: "Overcurrent",
    26: "Loss of Field",
    27: "Utility Phase Loss",
    28: "Parallel Sync Loss",
    29: "Setpoint Not Reached",
}

# Critical alarms that trigger automatic shutdown
CRITICAL_ALARMS = {
    ALARM_HIGH_COOLANT_TEMP,
    ALARM_LOW_OIL_PRESSURE,
    ALARM_OVERSPEED,
    ALARM_ESTOP,
    ALARM_REVERSE_POWER,
    ALARM_GEN_BEARING_TEMP,
    ALARM_OVERCURRENT,
    ALARM_LOSS_OF_FIELD,
}

DEFAULT_RATED_KW = 500.0


class Generator:
    """Simulates a single diesel standby generator."""

    def __init__(self, unit_id: int, rated_kw: float = DEFAULT_RATED_KW):
        self.unit_id = unit_id
        self.name = f"GEN-{unit_id:02d}"
        self.lock = Lock()
        self.rated_kw = float(rated_kw)

        # State
        self.state = State.STOPPED
        self.auto_mode = True

        # Analog values
        self.output_kw = 0.0
        min_load_kw = max(100.0, self.rated_kw * 0.4)
        max_load_kw = max(min_load_kw, self.rated_kw * 0.9)
        self.building_load_kw = random.uniform(min_load_kw, max_load_kw)  # actual demand
        self.utility_load_kw = self.building_load_kw       # what utility is delivering
        self.utility_breaker = True  # utility available on start
        self.gen_breaker = False
        self.transfer_mode = "TRANSFER"
        self.fuel_level = random.uniform(75, 100)
        self.run_hours = round(random.uniform(10, 95), 4)
        self.coolant_temp = 75.0
        self.oil_pressure = 0.0
        self.battery_voltage = 27.6
        self.engine_rpm = 0.0
        self.output_voltage = 0.0
        self.output_frequency = 0.0

        # Alarms (set of active alarm indices)
        self._alarms = set()
        self._latched_fault = None
        self._manual_injected_alarms = set()

        # Internal timing
        self._crank_duration = 0.0
        self._cooldown_duration = 0.0
        self._crank_elapsed = 0.0
        self._cooldown_elapsed = 0.0
        self._overcrank_checked = False
        self._auto_start_pending = False
        self._auto_start_delay = 0.0
        self._auto_retransfer_pending = False
        self._auto_retransfer_delay = 0.0
        self._utility_available = True

        # Track previous utility state for auto mode
        self._last_utility_available = True
        self.transfer_mode = "TRANSFER"  # TRANSFER, PARALLEL, or ISLAND

        # Parallel setpoint
        self.parallel_setpoint_kw = self.rated_kw * 0.6

        # Alarm word 3 timers
        self._load_imbalance_timer = 0.0
        self._setpoint_not_reached_timer = 0.0
        self._last_fuel_level = self.fuel_level

        # Anomaly system: {alarm_id: remaining_duration}
        # Active anomalies override sensor values to trigger alarms.
        self._anomalies = {}
        self._anomaly_cooldown = 0.0  # prevent multiple anomalies stacking

    # ----- Public interface -----

    def tick(self, dt: float = 1.0):
        """Advance the simulation by dt seconds."""
        with self.lock:
            self._check_auto_mode(dt)
            self._update_state(dt)
            self._update_alarms()

    def set_parallel_setpoint(self, kw: float):
        """Set the parallel mode kW setpoint, clamped to [0, rated_kw]."""
        with self.lock:
            self.parallel_setpoint_kw = max(0.0, min(self.rated_kw, float(kw)))

    def get_state(self) -> dict:
        """Return a snapshot of all generator values."""
        with self.lock:
            return {
                "unit_id": self.unit_id,
                "name": self.name,
                "state": self.state.value,
                "auto_mode": self.auto_mode,
                "output_kw": round(self.output_kw, 1),
                "utility_load_kw": round(self.utility_load_kw, 1),
                "utility_breaker": self.utility_breaker,
                "gen_breaker": self.gen_breaker,
                "fuel_level": round(self.fuel_level, 1),
                "run_hours": round(self.run_hours, 4),
                "coolant_temp": round(self.coolant_temp, 1),
                "oil_pressure": round(self.oil_pressure, 1),
                "battery_voltage": round(self.battery_voltage, 1),
                "engine_rpm": round(self.engine_rpm, 0),
                "output_voltage": round(self.output_voltage, 1),
                "output_frequency": round(self.output_frequency, 2),
                "alarm_word_1": self._alarm_word_1(),
                "alarm_word_2": self._alarm_word_2(),
                "alarm_word_3": self._alarm_word_3(),
                "alarms": {i: ALARM_NAMES[i] for i in sorted(self._alarms)},
                "rated_kw": self.rated_kw,
                "transfer_mode": self.transfer_mode,
                "parallel_setpoint_kw": round(self.parallel_setpoint_kw, 1),
            }

    def command(self, cmd: int):
        """Process a command (matches Modbus command register values)."""
        with self.lock:
            if cmd == 1:    # Start
                self._cmd_start()
            elif cmd == 2:  # Stop
                self._cmd_stop()
            elif cmd == 3:  # Close Gen Breaker
                self._cmd_close_gen_breaker()
            elif cmd == 4:  # Open Gen Breaker
                self._cmd_open_gen_breaker()
            elif cmd == 5:  # Close Utility Breaker
                self._cmd_close_util_breaker()
            elif cmd == 6:  # Open Utility Breaker
                self._cmd_open_util_breaker()
            elif cmd == 7:  # E-Stop
                self._cmd_estop()
            elif cmd == 8:  # Reset Alarms
                self._cmd_reset_alarms()
            elif cmd == 9:  # Toggle Auto
                self._cmd_toggle_auto()
            elif cmd == 10:  # Utility Fail
                self._cmd_utility_fail()
            elif cmd == 11:  # Restore Utility
                self._cmd_restore_utility()
            elif cmd == 12:  # Set Parallel mode
                self.transfer_mode = "PARALLEL"
            elif cmd == 13:  # Set Island mode
                self.transfer_mode = "ISLAND"
                # Force utility breaker open and close gen breaker if running
                if self.utility_breaker:
                    self.utility_breaker = False
                if self.state == State.RUNNING and not self.gen_breaker:
                    self.gen_breaker = True
            elif cmd == 14:  # Set Transfer mode
                self.transfer_mode = "TRANSFER"
                # If both breakers closed, open gen breaker to restore mutual exclusion
                if self.gen_breaker and self.utility_breaker:
                    self.gen_breaker = False
                    self.output_kw = 0.0
            # Fault injection commands (20-27)
            elif cmd == 20:
                self._inject_fault(ALARM_HIGH_COOLANT_TEMP)
            elif cmd == 21:
                self._inject_fault(ALARM_LOW_OIL_PRESSURE)
            elif cmd == 22:
                self._inject_fault(ALARM_OVERSPEED)
            elif cmd == 23:
                self._inject_latched_fault(ALARM_LOW_COOLANT_LEVEL)
            elif cmd == 24:
                self._inject_latched_fault(ALARM_GROUND_FAULT)
            elif cmd == 25:
                self._inject_fault(ALARM_OVER_VOLTAGE)
            elif cmd == 26:
                self._inject_fault(ALARM_UNDER_VOLTAGE)
            elif cmd == 27:
                self._inject_fault(ALARM_HIGH_BATTERY_V)
            elif cmd == 28:
                self._inject_fault(ALARM_REVERSE_POWER)
            elif cmd == 29:
                self._inject_latched_fault(ALARM_SYNC_CHECK_FAIL)
            elif cmd == 30:
                self._inject_fault(ALARM_LOAD_IMBALANCE)
            elif cmd == 31:
                self._inject_fault(ALARM_FUEL_LEAK)
            elif cmd == 32:
                self._inject_latched_fault(ALARM_AIR_FILTER_RESTRICTED)
            elif cmd == 33:
                self._inject_latched_fault(ALARM_EXHAUST_HIGH_TEMP)
            elif cmd == 34:
                self._inject_fault(ALARM_GEN_BEARING_TEMP)
            elif cmd == 35:
                self._inject_fault(ALARM_OVERCURRENT)
            elif cmd == 36:
                self._inject_fault(ALARM_LOSS_OF_FIELD)
            elif cmd == 37:
                self._inject_latched_fault(ALARM_UTILITY_PHASE_LOSS)
            elif cmd == 38:
                self._inject_latched_fault(ALARM_PARALLEL_SYNC_LOSS)
            elif cmd == 39:
                self._inject_fault(ALARM_SETPOINT_NOT_REACHED)

    def get_register_values(self) -> dict:
        """Return register-ready values (scaled integers) for Modbus."""
        with self.lock:
            rh_frac = self.run_hours % 1
            return {
                0: int(self.output_kw * 10),
                1: int(self.utility_load_kw * 10),
                2: int(self.utility_breaker),
                3: int(self.gen_breaker),
                4: int(self.fuel_level * 10),
                5: int(self.run_hours),
                6: int(rh_frac * 10000),
                7: int(self.coolant_temp),
                8: int(self.oil_pressure),
                9: int(self.battery_voltage * 10),
                10: int(self.engine_rpm),
                11: int(self.output_voltage),
                12: int(self.output_frequency * 10),
                13: self._alarm_word_1(),
                14: self._alarm_word_2(),
                15: {"TRANSFER": 0, "PARALLEL": 1, "ISLAND": 2}.get(self.transfer_mode, 0),
                16: int(self.parallel_setpoint_kw * 10),
                17: self._alarm_word_3(),
                20: 0,  # command register reads as 0
            }

    # ----- Auto-mode logic -----

    def _check_auto_mode(self, dt: float):
        if not self.auto_mode:
            self._auto_start_pending = False
            self._auto_retransfer_pending = False
            return

        # Detect utility failure
        if self._last_utility_available and not self._utility_available:
            if self.state == State.STOPPED:
                self._auto_start_pending = True
                self._auto_start_delay = random.uniform(5, 10)

        # Detect utility restore
        if not self._last_utility_available and self._utility_available:
            if self.state == State.RUNNING and self.gen_breaker:
                self._auto_retransfer_pending = True
                self._auto_retransfer_delay = random.uniform(5, 10)

        self._last_utility_available = self._utility_available

        # Auto-start sequence
        if self._auto_start_pending:
            self._auto_start_delay = max(0.0, self._auto_start_delay - dt)
            if self._auto_start_delay == 0.0:
                self._auto_start_pending = False
                if self.state == State.STOPPED:
                    self._cmd_start()

        # Auto close gen breaker once running and stable
        if (self.state == State.RUNNING and not self.gen_breaker
                and not self._utility_available and not self._auto_start_pending
                and self.engine_rpm > 1750 and self.output_voltage > 470):
            self._cmd_close_gen_breaker()

        # In PARALLEL mode with utility available and running, auto-close gen breaker for load sharing
        if (self.transfer_mode == "PARALLEL" and self.state == State.RUNNING
                and not self.gen_breaker and self._utility_available
                and not self._auto_start_pending
                and self.engine_rpm > 1750 and self.output_voltage > 470):
            self._cmd_close_gen_breaker()

        # Auto retransfer sequence
        if self._auto_retransfer_pending:
            self._auto_retransfer_delay = max(0.0, self._auto_retransfer_delay - dt)
            if self._auto_retransfer_delay == 0.0:
                self._auto_retransfer_pending = False
                if self.state == State.RUNNING:
                    self._cmd_open_gen_breaker()
                    self._cmd_close_util_breaker()
                    self._begin_cooldown()

    # ----- State update -----

    def _update_state(self, dt: float):
        # Building load drifts in all states (demand exists regardless of source)
        self.building_load_kw += random.gauss(0, 1) * dt
        min_load_kw = max(100.0, self.rated_kw * 0.2)
        max_load_kw = max(min_load_kw, self.rated_kw * 0.95)
        self.building_load_kw = max(min_load_kw, min(max_load_kw, self.building_load_kw))

        if self.state == State.STOPPED:
            self._tick_stopped(dt)
        elif self.state == State.CRANKING:
            self._tick_cranking(dt)
        elif self.state == State.RUNNING:
            self._tick_running(dt)
        elif self.state == State.COOLDOWN:
            self._tick_cooldown(dt)
        elif self.state == State.FAULT:
            self._tick_fault(dt)

        # Utility load is derived after the state tick so it reflects the latest output_kw.
        if self.utility_breaker and self.gen_breaker and self.transfer_mode == "PARALLEL" and self.state == State.RUNNING:
            self.utility_load_kw = max(0.0, self.building_load_kw - self.output_kw)
        elif self.utility_breaker:
            self.utility_load_kw = self.building_load_kw
        else:
            self.utility_load_kw = 0.0

    def _tick_stopped(self, dt: float):
        self.engine_rpm = 0.0
        self.output_kw = 0.0
        self.output_voltage = 0.0
        self.output_frequency = 0.0
        self.oil_pressure = 0.0
        self.gen_breaker = False
        # Coolant drifts to ambient 75F
        self.coolant_temp += (75.0 - self.coolant_temp) * 0.02 * dt
        # Battery slowly recovers
        self.battery_voltage += (27.6 - self.battery_voltage) * 0.05 * dt

    def _tick_cranking(self, dt: float):
        self._crank_elapsed += dt
        progress = min(self._crank_elapsed / self._crank_duration, 1.0)

        # RPM ramps up to 1800
        self.engine_rpm = 1800.0 * progress
        # Battery dips during cranking
        self.battery_voltage = 27.6 - (3.6 * (1.0 - progress))
        # Oil pressure begins to rise
        self.oil_pressure = 70.0 * progress
        # Voltage and frequency begin to appear
        self.output_voltage = 480.0 * progress
        self.output_frequency = 60.0 * progress

        # Check for overcrank fault (5% chance evaluated once at 80% through crank)
        if not self._overcrank_checked and progress >= 0.8:
            self._overcrank_checked = True
            if random.random() < 0.05:
                self._latched_fault = ALARM_OVERCRANK
                self._alarms.add(ALARM_OVERCRANK)
                self._enter_fault()
                return

        if progress >= 1.0:
            self.state = State.RUNNING
            self.engine_rpm = 1800.0
            self.output_voltage = 480.0
            self.output_frequency = 60.0
            self.oil_pressure = 70.0
            self.battery_voltage = 28.5

    def _tick_running(self, dt: float):
        # Expire old anomalies based on simulation time.
        self._anomalies = {
            alarm_id: remaining - dt
            for alarm_id, remaining in self._anomalies.items()
            if remaining - dt > 0
        }
        if self._anomaly_cooldown > 0:
            self._anomaly_cooldown = max(0.0, self._anomaly_cooldown - dt)

        # Random anomaly events — rates tuned for ~1-2 faults/day across fleet
        if self._anomaly_cooldown <= 0:
            if random.random() < 4e-7:
                self._anomalies[ALARM_HIGH_COOLANT_TEMP] = random.uniform(4, 8)
            if random.random() < 2e-7:
                self._anomalies[ALARM_LOW_OIL_PRESSURE] = random.uniform(3, 6)
            if random.random() < 2e-7:
                self._anomalies[ALARM_OVERSPEED] = random.uniform(2, 5)
            if random.random() < 1e-7:
                self._anomalies[ALARM_OVER_VOLTAGE] = random.uniform(3, 6)
            if random.random() < 1e-7:
                self._anomalies[ALARM_OVER_FREQUENCY] = random.uniform(3, 6)
            # Brief cooldown to avoid rapid re-rolls of same fault
            if len(self._anomalies) > 0:
                self._anomaly_cooldown = 5.0

        # RPM with jitter (or overspeed anomaly)
        if ALARM_OVERSPEED in self._anomalies:
            self.engine_rpm = random.uniform(1960, 2050)
        else:
            self.engine_rpm = 1800.0 + random.gauss(0, 3)

        # Voltage with jitter (or anomaly)
        if ALARM_OVER_VOLTAGE in self._anomalies:
            self.output_voltage = random.uniform(515, 540)
        elif ALARM_UNDER_VOLTAGE in self._anomalies:
            self.output_voltage = random.uniform(420, 445)
        else:
            self.output_voltage = 480.0 + random.gauss(0, 2)

        # Frequency with jitter (or anomaly)
        if ALARM_OVER_FREQUENCY in self._anomalies:
            self.output_frequency = random.uniform(63.5, 66)
        elif ALARM_UNDER_FREQUENCY in self._anomalies:
            self.output_frequency = random.uniform(54, 56.5)
        else:
            self.output_frequency = 60.0 + random.gauss(0, 0.1)

        # Oil pressure (or anomaly)
        if ALARM_LOW_OIL_PRESSURE in self._anomalies:
            self.oil_pressure = random.uniform(10, 22)
        else:
            self.oil_pressure = 70.0 + random.gauss(0, 2)

        # Battery voltage at charging level
        if ALARM_HIGH_BATTERY_V in self._anomalies:
            self.battery_voltage = random.uniform(31.5, 33)
        else:
            self.battery_voltage += (28.5 - self.battery_voltage) * 0.1 * dt

        # Coolant (or anomaly)
        if ALARM_HIGH_COOLANT_TEMP in self._anomalies:
            self.coolant_temp += (240.0 - self.coolant_temp) * 0.15 * dt
        else:
            self.coolant_temp += (190.0 - self.coolant_temp) * 0.01 * dt

        # Run hours increment any time engine is running
        self.run_hours += dt / 3600.0

        # If gen breaker is closed, gen carries load based on transfer mode
        if self.gen_breaker:
            if self.utility_breaker and self.transfer_mode == "PARALLEL":
                # Parallel: gen ramps to setpoint while utility carries the remainder.
                target_kw = min(self.parallel_setpoint_kw, self.building_load_kw)
            else:
                # Transfer or Island: gen takes all building load
                target_kw = self.building_load_kw
            ramp_rate = 50.0 * dt  # 50 kW/s ramp
            if self.output_kw < target_kw:
                self.output_kw = min(self.output_kw + ramp_rate, target_kw)
            elif self.output_kw > target_kw:
                self.output_kw = max(self.output_kw - ramp_rate, target_kw)

            # Fuel consumption: proportional to load
            load_fraction = self.output_kw / self.rated_kw if self.rated_kw else 0.0
            fuel_rate = (0.5 + 2.0 * load_fraction) * dt / 3600.0  # % per second
            self.fuel_level = max(0.0, self.fuel_level - fuel_rate)
        else:
            self.output_kw = max(0.0, self.output_kw - 50.0 * dt)

        # Check critical alarms
        self._check_critical_shutdown()

    def _tick_cooldown(self, dt: float):
        self._cooldown_elapsed += dt
        self.gen_breaker = False
        self.output_kw = max(0.0, self.output_kw - 100.0 * dt)

        # RPM ramps down
        progress = min(self._cooldown_elapsed / self._cooldown_duration, 1.0)
        self.engine_rpm = 1800.0 * (1.0 - progress) + random.gauss(0, 2) * (1.0 - progress)
        self.output_voltage = 480.0 * (1.0 - progress)
        self.output_frequency = 60.0 * (1.0 - progress)
        self.oil_pressure = 70.0 * (1.0 - progress)

        # Coolant begins to drop
        self.coolant_temp += (120.0 - self.coolant_temp) * 0.005 * dt

        if progress >= 1.0:
            self.state = State.STOPPED
            self.engine_rpm = 0.0
            self.output_voltage = 0.0
            self.output_frequency = 0.0
            self.oil_pressure = 0.0
            self.output_kw = 0.0

    def _tick_fault(self, dt: float):
        # Engine is stopped in fault
        self.engine_rpm = max(0.0, self.engine_rpm - 200.0 * dt)
        self.output_kw = max(0.0, self.output_kw - 200.0 * dt)
        self.output_voltage = max(0.0, self.output_voltage - 100.0 * dt)
        self.output_frequency = max(0.0, self.output_frequency - 15.0 * dt)
        self.oil_pressure = max(0.0, self.oil_pressure - 20.0 * dt)
        self.gen_breaker = False
        self.coolant_temp += (75.0 - self.coolant_temp) * 0.01 * dt

    # ----- Alarm logic -----

    def _update_alarms(self):
        # Status alarms (not latched)
        if self.state in (State.RUNNING, State.CRANKING):
            self._alarms.add(ALARM_ENGINE_RUNNING)
        else:
            self._alarms.discard(ALARM_ENGINE_RUNNING)

        if self.state == State.STOPPED and not self._latched_fault:
            self._alarms.add(ALARM_READY)
        else:
            self._alarms.discard(ALARM_READY)

        if self.auto_mode:
            self._alarms.add(ALARM_AUTO_MODE)
        else:
            self._alarms.discard(ALARM_AUTO_MODE)

        # Condition alarms
        if self.coolant_temp > 220:
            self._alarms.add(ALARM_HIGH_COOLANT_TEMP)
        else:
            self._alarms.discard(ALARM_HIGH_COOLANT_TEMP)

        if self.state == State.RUNNING and self.oil_pressure < 25:
            self._alarms.add(ALARM_LOW_OIL_PRESSURE)
        else:
            self._alarms.discard(ALARM_LOW_OIL_PRESSURE)

        if self.engine_rpm > 1950:
            self._alarms.add(ALARM_OVERSPEED)
        else:
            self._alarms.discard(ALARM_OVERSPEED)

        if self.fuel_level < 15:
            self._alarms.add(ALARM_LOW_FUEL)
        else:
            self._alarms.discard(ALARM_LOW_FUEL)

        if self.battery_voltage > 31:
            self._alarms.add(ALARM_HIGH_BATTERY_V)
        else:
            self._alarms.discard(ALARM_HIGH_BATTERY_V)

        if self.battery_voltage < 24 and self.state not in (State.CRANKING,):
            self._alarms.add(ALARM_LOW_BATTERY_V)
        else:
            self._alarms.discard(ALARM_LOW_BATTERY_V)

        if not self._utility_available:
            self._alarms.add(ALARM_UTILITY_PHASE_LOSS)
        elif self._latched_fault != ALARM_UTILITY_PHASE_LOSS:
            self._alarms.discard(ALARM_UTILITY_PHASE_LOSS)

        if self.state == State.RUNNING:
            if self.output_voltage > 510:
                self._alarms.add(ALARM_OVER_VOLTAGE)
            else:
                self._alarms.discard(ALARM_OVER_VOLTAGE)

            if self.output_voltage < 450 and self.output_voltage > 0:
                self._alarms.add(ALARM_UNDER_VOLTAGE)
            else:
                self._alarms.discard(ALARM_UNDER_VOLTAGE)

            if self.output_frequency > 63:
                self._alarms.add(ALARM_OVER_FREQUENCY)
            else:
                self._alarms.discard(ALARM_OVER_FREQUENCY)

            if self.output_frequency < 57 and self.output_frequency > 0:
                self._alarms.add(ALARM_UNDER_FREQUENCY)
            else:
                self._alarms.discard(ALARM_UNDER_FREQUENCY)

            if self.output_kw > self.rated_kw * 1.05:
                self._alarms.add(ALARM_OVERLOAD)
            else:
                self._alarms.discard(ALARM_OVERLOAD)
        else:
            self._alarms.discard(ALARM_OVER_VOLTAGE)
            self._alarms.discard(ALARM_UNDER_VOLTAGE)
            self._alarms.discard(ALARM_OVER_FREQUENCY)
            self._alarms.discard(ALARM_UNDER_FREQUENCY)
            self._alarms.discard(ALARM_OVERLOAD)

        # Alarm Word 3 auto-detection
        in_parallel = (
            self.state == State.RUNNING
            and self.gen_breaker
            and self.utility_breaker
            and self.transfer_mode == "PARALLEL"
        )

        if in_parallel and self.output_kw < 0:
            self._alarms.add(ALARM_REVERSE_POWER)
        elif ALARM_REVERSE_POWER not in self._anomalies:
            self._alarms.discard(ALARM_REVERSE_POWER)

        if in_parallel and self.parallel_setpoint_kw > 0:
            deviation = abs(self.output_kw - self.parallel_setpoint_kw) / self.parallel_setpoint_kw
            if deviation > 0.20:
                self._load_imbalance_timer += 1.0
            else:
                self._load_imbalance_timer = 0.0
            if self._load_imbalance_timer >= 5.0:
                self._alarms.add(ALARM_LOAD_IMBALANCE)
            elif ALARM_LOAD_IMBALANCE not in self._anomalies:
                self._alarms.discard(ALARM_LOAD_IMBALANCE)
        else:
            self._load_imbalance_timer = 0.0
            if ALARM_LOAD_IMBALANCE not in self._anomalies:
                self._alarms.discard(ALARM_LOAD_IMBALANCE)

        if self.state == State.RUNNING:
            expected_rate = (0.5 + 2.0 * (self.output_kw / self.rated_kw if self.rated_kw else 0)) / 3600.0
            actual_drop = self._last_fuel_level - self.fuel_level
            if actual_drop > expected_rate * 2.0 and actual_drop > 0.001:
                self._alarms.add(ALARM_FUEL_LEAK)
            elif ALARM_FUEL_LEAK not in self._anomalies:
                self._alarms.discard(ALARM_FUEL_LEAK)
        elif ALARM_FUEL_LEAK not in self._anomalies:
            self._alarms.discard(ALARM_FUEL_LEAK)
        self._last_fuel_level = self.fuel_level

        if in_parallel and self.parallel_setpoint_kw > 0:
            if self.output_kw < self.parallel_setpoint_kw * 0.9:
                self._setpoint_not_reached_timer += 1.0
            else:
                self._setpoint_not_reached_timer = 0.0
            if self._setpoint_not_reached_timer >= 10.0:
                self._alarms.add(ALARM_SETPOINT_NOT_REACHED)
            elif ALARM_SETPOINT_NOT_REACHED not in self._anomalies:
                self._alarms.discard(ALARM_SETPOINT_NOT_REACHED)
        else:
            self._setpoint_not_reached_timer = 0.0
            if ALARM_SETPOINT_NOT_REACHED not in self._anomalies:
                self._alarms.discard(ALARM_SETPOINT_NOT_REACHED)

        # Operator fault-injection commands must remain visible until reset,
        # even if the simulated analog condition would otherwise clear.
        self._alarms |= self._manual_injected_alarms

    def _alarm_word_1(self) -> int:
        word = 0
        for bit in range(16):
            if bit in self._alarms:
                word |= (1 << bit)
        return word

    def _alarm_word_2(self) -> int:
        word = 0
        if 16 in self._alarms:
            word |= 1
        if 17 in self._alarms:
            word |= 2
        return word

    def _alarm_word_3(self) -> int:
        word = 0
        for bit in range(12):
            if (18 + bit) in self._alarms:
                word |= 1 << bit
        return word

    def _check_critical_shutdown(self):
        for alarm in CRITICAL_ALARMS:
            if alarm in self._alarms and alarm != ALARM_ESTOP:
                self._latched_fault = alarm
                self._enter_fault()
                return

    # ----- Command handlers -----

    def _cmd_start(self):
        if self.state == State.STOPPED:
            self.state = State.CRANKING
            self._crank_duration = random.uniform(3, 8)
            self._crank_elapsed = 0.0
            self._overcrank_checked = False

    def _cmd_stop(self):
        if self.state in (State.RUNNING, State.CRANKING):
            self._begin_cooldown()

    def _cmd_close_gen_breaker(self):
        if self.state == State.RUNNING and not self.gen_breaker:
            self.gen_breaker = True
            if self.transfer_mode == "TRANSFER":
                # Transfer: close gen, open utility (mutual exclusion)
                if self.utility_breaker:
                    self.utility_breaker = False
            elif self.transfer_mode == "PARALLEL":
                # Parallel: close gen breaker WITHOUT opening utility
                pass
            elif self.transfer_mode == "ISLAND":
                # Island: close gen breaker AND ensure utility is open
                if self.utility_breaker:
                    self.utility_breaker = False

    def _cmd_open_gen_breaker(self):
        if self.gen_breaker:
            self.gen_breaker = False
            self.output_kw = 0.0
            # Re-close utility breaker if utility available
            if self._utility_available:
                self.utility_breaker = True

    def _cmd_close_util_breaker(self):
        if self._utility_available:
            if self.transfer_mode == "PARALLEL":
                # Parallel: allow closing even if gen breaker is closed
                self.utility_breaker = True
            elif not self.gen_breaker:
                # Transfer or Island: only if gen breaker not closed
                self.utility_breaker = True

    def _cmd_open_util_breaker(self):
        if not self.gen_breaker:
            self.utility_breaker = False

    def _cmd_estop(self):
        self._alarms.add(ALARM_ESTOP)
        self._latched_fault = ALARM_ESTOP
        self._enter_fault()

    def _cmd_reset_alarms(self):
        if self.state == State.FAULT:
            # Clear all non-status alarms
            keep = {ALARM_ENGINE_RUNNING, ALARM_READY, ALARM_AUTO_MODE}
            self._alarms = self._alarms & keep
            self._anomalies.clear()
            self._manual_injected_alarms.clear()
            self._latched_fault = None
            self._load_imbalance_timer = 0.0
            self._setpoint_not_reached_timer = 0.0
            self.state = State.STOPPED

    def _cmd_toggle_auto(self):
        self.auto_mode = not self.auto_mode

    def _cmd_utility_fail(self):
        self._utility_available = False
        self.utility_breaker = False

    def _cmd_restore_utility(self):
        self._utility_available = True
        # If gen breaker is not closed, restore utility breaker
        if not self.gen_breaker:
            self.utility_breaker = True

    # ----- Fault injection -----

    def _inject_fault(self, alarm_id):
        """Inject a fault alarm directly.

        Adds the alarm immediately regardless of current state.
        If the generator is RUNNING, also injects a sensor anomaly
        so the analog values reflect the fault. Multiple injections
        can be stacked without resetting.
        """
        was_running = self.state == State.RUNNING
        # Always add the alarm directly so it's visible immediately
        self._alarms.add(alarm_id)
        self._manual_injected_alarms.add(alarm_id)
        if alarm_id == ALARM_HIGH_COOLANT_TEMP:
            # Preserve injected high-coolant faults through the next tick so
            # stopped units still expose the alarm bit/register state.
            self.coolant_temp = max(self.coolant_temp, 240.0)
        elif alarm_id == ALARM_LOW_OIL_PRESSURE:
            self.oil_pressure = min(self.oil_pressure, 10.0)
        elif alarm_id == ALARM_OVERSPEED:
            self.engine_rpm = max(self.engine_rpm, 2100.0)
        elif alarm_id == ALARM_OVER_VOLTAGE:
            self.output_voltage = max(self.output_voltage, 540.0)
        elif alarm_id == ALARM_UNDER_VOLTAGE:
            self.output_voltage = 420.0
        elif alarm_id == ALARM_HIGH_BATTERY_V:
            self.battery_voltage = max(self.battery_voltage, 32.0)
        # If running, also set the anomaly for sensor value override
        if was_running:
            self._anomalies[alarm_id] = random.uniform(5, 10)
        self._latched_fault = alarm_id
        if self.state != State.FAULT:
            self._enter_fault()

    def _inject_latched_fault(self, alarm_id):
        """Inject a latched fault that goes directly to FAULT state.

        Can be stacked on top of existing faults.
        """
        self._alarms.add(alarm_id)
        self._manual_injected_alarms.add(alarm_id)
        if self.state != State.FAULT:
            self._latched_fault = alarm_id
            self._enter_fault()

    # ----- Helpers -----

    def _enter_fault(self):
        self.state = State.FAULT
        self.gen_breaker = False
        # Restore to TRANSFER mode if in PARALLEL or ISLAND
        if self.transfer_mode in ("PARALLEL", "ISLAND"):
            self.transfer_mode = "TRANSFER"
        # Restore utility if available
        if self._utility_available:
            self.utility_breaker = True

    def _begin_cooldown(self):
        self.state = State.COOLDOWN
        self._cooldown_duration = random.uniform(30, 60)
        self._cooldown_elapsed = 0.0
        self.gen_breaker = False
        # Restore to TRANSFER mode if in PARALLEL or ISLAND
        if self.transfer_mode in ("PARALLEL", "ISLAND"):
            self.transfer_mode = "TRANSFER"
        # Restore utility if available
        if self._utility_available:
            self.utility_breaker = True
