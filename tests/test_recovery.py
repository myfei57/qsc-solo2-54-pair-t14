"""断电与手动停止后的工位级恢复。"""

from __future__ import annotations

import dataclasses

from hydrorefuel.config import default_config
from hydrorefuel.app import build_platform
from hydrorefuel.clock import ManualClock


def _start_loaded(platform, charge_kg: float = 12.0) -> None:
    platform.start()
    unload = platform.unload(target_kg=20.0)
    assert unload["ok"], unload["violations"]
    pressurize = platform.pressurize(charge_kg=charge_kg)
    assert pressurize["ok"], pressurize["violations"]


def _restart(tmp_path, config, clock=None):
    return build_platform(config, clock=clock or ManualClock(), storage_root=str(tmp_path / "state"))


def test_unload_resume_counts_only_remaining_amount(tmp_path):
    config = default_config()
    first = build_platform(config, clock=ManualClock(), storage_root=str(tmp_path / "state"))
    first.start()
    first.recovery.start(
        op_id="unload-interrupted",
        operation="unload",
        stage="transfer",
        target_kg=10.0,
        dt=1.0,
        hose_pressure_mpa=20.0,
        completed_kg=5.0,
        truck_pressure_mpa=19.55,
    )

    second = _restart(tmp_path, config)
    decision = second.recovery_status()
    assert decision["scope"] == "station"
    assert decision["decision"] == "resume"
    assert decision["completed_kg"] == 5.0

    report = second.resume_operation()
    assert report["ok"], report["violations"]
    assert abs(report["completed_kg"] - 10.0) < 1e-6
    assert abs(second.truck.delivered_kg - 10.0) < 1e-6
    assert second.truck.discharge_ready()
    assert second.truck.state == "drained"

    repeated = second.resume_operation()
    assert repeated["idempotent"] is True
    assert abs(second.truck.delivered_kg - 10.0) < 1e-6


def test_fill_resume_does_not_double_count_completed_mass(tmp_path):
    config = default_config()
    first = build_platform(config, clock=ManualClock(), storage_root=str(tmp_path / "state"))
    _start_loaded(first)
    first.recovery.start(
        op_id="fill-interrupted",
        operation="fill",
        stage="transfer",
        vehicle="vehicle01",
        target_kg=3.6,
        steps_requested=2,
        rate_kg_per_min=3.0,
        dt=36.0,
        meter_total_kg=1.8,
        completed_kg=1.8,
        accounted_kg=1.8,
        step_completed=1,
        step_current_kg=0.0,
        params={
            "temperature_c": -40.0,
            "held_s": 20.0,
            "buffer_pressure_mpa": first.bank.pressure_mpa * (10.2 / 12.0),
            "buffer_stored_kg": 10.2,
        },
    )

    second = _restart(tmp_path, config)
    assert second.meter.total_kg() == 1.8
    resumed = second.resume_operation()
    assert resumed["ok"], resumed["violations"]
    assert second.meter.total_kg() == 3.6
    # 前 1.8kg 在断电前已扣账，恢复后只能再扣 1.8kg。
    assert abs(second.bank.stored_kg - 8.4) < 1e-9

    stop = second.stop_fill()
    assert stop["ok"], stop["violations"]
    assert [step["action"] for step in stop["steps"]] == ["isolate", "stop", "unseat_nozzle"]
    assert second.meter.total_kg() == 3.6
    assert abs(second.bank.stored_kg - 8.4) < 1e-9


def test_abort_station_isolates_fill_and_keeps_completed_amount(tmp_path):
    config = dataclasses.replace(default_config(), recovery_policy="abort")
    first = build_platform(config, clock=ManualClock(), storage_root=str(tmp_path / "state"))
    _start_loaded(first)
    first.recovery.start(
        op_id="fill-abort",
        operation="fill",
        stage="transfer",
        vehicle="vehicle01",
        target_kg=3.6,
        steps_requested=2,
        rate_kg_per_min=3.0,
        dt=36.0,
        meter_total_kg=1.8,
        completed_kg=1.8,
        accounted_kg=1.8,
        step_completed=1,
        step_current_kg=0.0,
        params={
            "temperature_c": -40.0,
            "held_s": 20.0,
            "buffer_pressure_mpa": first.bank.pressure_mpa * (10.2 / 12.0),
            "buffer_stored_kg": 10.2,
        },
    )

    second = _restart(tmp_path, config)
    report = second.resume_operation()
    assert report["decision"] == "abort"
    assert report["ok"]
    assert [step["action"] for step in report["steps"]] == [
        "fill_complete",
        "isolate",
        "stop",
        "unseat_nozzle",
    ]
    assert second.meter.total_kg() == 1.8
    assert second.bank.stored_kg == 10.2
    assert not second.dispenser.fill_valve_open()
    assert not second.nozzle.is_seated()


def test_recovery_decision_is_visible_and_audited(tmp_path):
    config = default_config()
    first = build_platform(config, clock=ManualClock(), storage_root=str(tmp_path / "state"))
    first.start()
    first.recovery.start(
        op_id="audited-unload",
        operation="unload",
        stage="transfer",
        target_kg=10.0,
        dt=1.0,
        hose_pressure_mpa=20.0,
        completed_kg=4.0,
        truck_pressure_mpa=19.64,
    )

    second = _restart(tmp_path, config)
    status = second.recovery_status()
    assert status["scope"] == "station"
    assert status["operation"] == "unload"
    entries = second.audit.entries()
    decision_entries = [entry for entry in entries if entry["action"] == "recovery_decision"]
    assert decision_entries
    assert decision_entries[0]["payload"]["scope"] == "station"
    assert decision_entries[0]["payload"]["completed_kg"] == 4.0


def test_stop_unload_vents_before_disconnect(tmp_path):
    config = default_config()
    first = build_platform(config, clock=ManualClock(), storage_root=str(tmp_path / "state"))
    first.start()
    first.recovery.start(
        op_id="manual-stop-unload",
        operation="unload",
        stage="transfer",
        target_kg=10.0,
        dt=1.0,
        hose_pressure_mpa=20.0,
        completed_kg=4.0,
        truck_pressure_mpa=19.64,
    )

    second = _restart(tmp_path, config)
    report = second.stop_unload()
    assert report["ok"], report["violations"]
    actions = [step["action"] for step in report["steps"]]
    assert actions.index("vent_hold") < actions.index("disconnect")
    assert second.truck.discharge_ready()
    assert second.truck.state == "drained"
