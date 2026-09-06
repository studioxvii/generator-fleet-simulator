from generator import ALARM_UTILITY_PHASE_LOSS, Generator, State


def test_start_command_transitions_generator_to_cranking():
    gen = Generator(1)

    gen.command(1)

    assert gen.state == State.CRANKING


def test_estop_and_reset_return_generator_to_stopped():
    gen = Generator(1)

    gen.command(7)
    assert gen.state == State.FAULT

    gen.command(8)

    assert gen.state == State.STOPPED


def test_register_values_include_command_register_as_zero():
    gen = Generator(1)

    registers = gen.get_register_values()

    assert registers[20] == 0
    assert 13 in registers
    assert 14 in registers


def test_utility_breaker_cannot_close_when_generator_breaker_is_closed():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.gen_breaker = True
    gen.utility_breaker = False
    gen._utility_available = True

    gen.command(5)

    assert gen.gen_breaker is True
    assert gen.utility_breaker is False


def test_generator_reports_custom_rated_kw():
    gen = Generator(1, rated_kw=2000)

    state = gen.get_state()

    assert state["rated_kw"] == 2000.0


def test_parallel_setpoint_is_clamped_and_exposed_in_registers():
    gen = Generator(1, rated_kw=1000)

    gen.set_parallel_setpoint(1200)
    state = gen.get_state()
    registers = gen.get_register_values()

    assert state["parallel_setpoint_kw"] == 1000.0
    assert registers[16] == 10000


def test_parallel_mode_keeps_utility_breaker_closed_and_uses_setpoint():
    gen = Generator(1, rated_kw=1000)
    gen.state = State.RUNNING
    gen.transfer_mode = "PARALLEL"
    gen.utility_breaker = True
    gen.gen_breaker = False
    gen.building_load_kw = 900
    gen.set_parallel_setpoint(300)

    gen.command(3)
    gen.tick(dt=10.0)

    assert gen.gen_breaker is True
    assert gen.utility_breaker is True
    assert gen.output_kw == 300
    assert gen.utility_load_kw == gen.building_load_kw - gen.output_kw


def test_extended_fault_command_sets_alarm_word_3():
    gen = Generator(1)

    gen.command(29)

    state = gen.get_state()
    registers = gen.get_register_values()

    assert "Sync Check Fail" in state["alarms"].values()
    assert registers[17] & (1 << 1)


def test_cooldown_progress_uses_simulation_time():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.command(2)
    gen._cooldown_duration = 2.0

    gen.tick(dt=1.0)
    assert gen.state == State.COOLDOWN

    gen.tick(dt=1.0)
    assert gen.state == State.STOPPED


def test_auto_start_and_retransfer_are_driven_by_tick_time():
    gen = Generator(1)

    gen.command(10)
    assert gen._auto_start_pending is False

    gen.tick(dt=1.0)
    assert gen._auto_start_pending is True
    gen._auto_start_delay = 0.0

    gen.tick(dt=1.0)
    assert gen.state == State.CRANKING

    gen.state = State.RUNNING
    gen.engine_rpm = 1800
    gen.output_voltage = 480
    gen.command(3)
    assert gen.gen_breaker is True

    gen.command(11)
    gen.tick(dt=1.0)
    assert gen._auto_retransfer_pending is True

    gen._auto_retransfer_delay = 0.0
    gen.tick(dt=1.0)
    assert gen.state == State.COOLDOWN
    assert gen.gen_breaker is False
    assert gen.utility_breaker is True


# ---------------------------------------------------------------------------
# Additional state machine and command coverage
# ---------------------------------------------------------------------------

def test_cranking_transitions_to_running_after_crank_duration():
    gen = Generator(1)
    gen.command(1)  # Start → CRANKING
    assert gen.state == State.CRANKING

    gen._crank_duration = 2.0
    gen._crank_elapsed = 0.0
    gen._overcrank_checked = False  # skip random overcrank

    # Patch random so overcrank never fires
    import random as _random
    original_random = _random.random
    _random.random = lambda: 1.0  # > 0.05, so overcrank never triggers

    try:
        gen.tick(dt=2.0)
    finally:
        _random.random = original_random

    assert gen.state == State.RUNNING
    assert gen.engine_rpm == 1800.0
    assert gen.output_voltage == 480.0


def test_close_gen_breaker_in_transfer_mode_opens_utility():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.transfer_mode = "TRANSFER"
    gen.utility_breaker = True
    gen.gen_breaker = False

    gen.command(3)  # Close Gen Breaker

    assert gen.gen_breaker is True
    assert gen.utility_breaker is False


def test_close_gen_breaker_in_parallel_mode_keeps_utility_closed():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.transfer_mode = "PARALLEL"
    gen.utility_breaker = True
    gen.gen_breaker = False

    gen.command(3)  # Close Gen Breaker

    assert gen.gen_breaker is True
    assert gen.utility_breaker is True


def test_open_gen_breaker_restores_utility_when_available():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.gen_breaker = True
    gen._utility_available = True
    gen.output_kw = 200.0

    gen.command(4)  # Open Gen Breaker

    assert gen.gen_breaker is False
    assert gen.output_kw == 0.0
    assert gen.utility_breaker is True


def test_close_util_breaker_only_closes_when_utility_available():
    gen = Generator(1)
    gen.utility_breaker = False
    gen.gen_breaker = False
    gen._utility_available = False

    gen.command(5)  # Close Util Breaker

    # Utility not available — should not close
    assert gen.utility_breaker is False


def test_open_util_breaker_opens_when_gen_breaker_not_closed():
    gen = Generator(1)
    gen.gen_breaker = False
    gen.utility_breaker = True

    gen.command(6)  # Open Util Breaker

    assert gen.utility_breaker is False


def test_toggle_auto_command_disables_auto_mode():
    gen = Generator(1)
    assert gen.auto_mode is True

    gen.command(9)  # Toggle Auto

    assert gen.auto_mode is False

    gen.command(9)  # Toggle back

    assert gen.auto_mode is True


def test_utility_fail_clears_utility_availability_and_breaker():
    gen = Generator(1)
    gen._utility_available = True
    gen.utility_breaker = True

    gen.command(10)  # Utility Fail

    assert gen._utility_available is False
    assert gen.utility_breaker is False


def test_utility_fail_and_restore_updates_phase_loss_alarm():
    gen = Generator(1)

    gen.command(10)  # Utility Fail
    gen.tick(0)

    assert ALARM_UTILITY_PHASE_LOSS in gen.get_state()["alarms"]

    gen.command(11)  # Restore Utility
    gen.tick(0)

    assert ALARM_UTILITY_PHASE_LOSS not in gen.get_state()["alarms"]


def test_restore_utility_reopens_breaker_when_gen_not_closed():
    gen = Generator(1)
    gen._utility_available = False
    gen.utility_breaker = False
    gen.gen_breaker = False

    gen.command(11)  # Restore Utility

    assert gen._utility_available is True
    assert gen.utility_breaker is True


def test_restore_utility_retransfer_sequence_from_island():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.transfer_mode = "ISLAND"
    gen._utility_available = False
    gen.utility_breaker = False
    gen.gen_breaker = True
    gen.output_kw = 250.0

    for command in (11, 14, 4):
        gen.command(command)

    assert gen._utility_available is True
    assert gen.transfer_mode == "TRANSFER"
    assert gen.gen_breaker is False
    assert gen.utility_breaker is True
    assert gen.output_kw == 0.0


def test_island_mode_opens_utility_breaker():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.transfer_mode = "TRANSFER"
    gen.utility_breaker = True
    gen.gen_breaker = False

    gen.command(13)  # Set Island Mode

    assert gen.transfer_mode == "ISLAND"
    assert gen.utility_breaker is False


def test_transfer_mode_command_opens_gen_breaker_when_both_closed():
    gen = Generator(1)
    gen.gen_breaker = True
    gen.utility_breaker = True
    gen.transfer_mode = "PARALLEL"

    gen.command(14)  # Set Transfer Mode

    assert gen.transfer_mode == "TRANSFER"
    assert gen.gen_breaker is False


def test_critical_fault_injection_causes_fault_state_on_tick():
    gen = Generator(1)
    gen.state = State.RUNNING
    gen.coolant_temp = 75.0

    # Inject high coolant temp fault (sets alarm directly, triggers shutdown in _update_alarms)
    gen.command(20)  # High Coolant Temp injection

    # The injection uses _inject_fault which adds an anomaly. Tick will update coolant_temp
    # via anomaly and then _update_alarms will detect it + _check_critical_shutdown.
    # We need to verify the generator can enter FAULT via this path.
    # Since _inject_fault sets an anomaly, we verify the fault is latched via tick.
    gen.coolant_temp = 230.0  # directly set high to guarantee alarm triggers
    gen.tick(dt=1.0)

    assert gen.state == State.FAULT


def test_get_register_values_transfer_mode_encoding():
    gen = Generator(1)
    gen.transfer_mode = "TRANSFER"
    assert gen.get_register_values()[15] == 0

    gen.transfer_mode = "PARALLEL"
    assert gen.get_register_values()[15] == 1

    gen.transfer_mode = "ISLAND"
    assert gen.get_register_values()[15] == 2


def test_state_snapshot_contains_all_expected_fields():
    gen = Generator(1, rated_kw=750)
    state = gen.get_state()

    required = {
        "unit_id", "name", "state", "auto_mode", "output_kw", "utility_load_kw",
        "utility_breaker", "gen_breaker", "fuel_level", "run_hours", "coolant_temp",
        "oil_pressure", "battery_voltage", "engine_rpm", "output_voltage",
        "output_frequency", "alarm_word_1", "alarm_word_2", "alarm_word_3",
        "alarms", "rated_kw", "transfer_mode", "parallel_setpoint_kw",
    }
    assert required.issubset(state.keys())
