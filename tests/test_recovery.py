"""加注/卸氢断点恢复与工位级恢复策略。"""

from __future__ import annotations

import dataclasses

from hydrorefuel.config import RECOVERY_ISOLATE
from hydrorefuel.app import build_platform
from support import load_station


def _restart(platform, storage_root: str):
    return build_platform(platform.config, clock=platform.clock, storage_root=storage_root)


def test_interrupted_fill_resumes_without_double_metering(platform, tmp_path):
    platform.start()
    load_station(platform)
    first = platform.fill(steps=3, interrupt_after="flow_step")
    assert first["interrupted"] is True
    meter_before = platform.meter.total_kg()
    decision = platform.recovery_status()
    assert decision["scope"] == "station"
    assert decision["action"] == "resume"
    assert decision["measured_kg"] == 1.8

    restored = _restart(platform, str(tmp_path / "state"))
    assert restored.meter.total_kg() == meter_before
    recovered = restored.recover_operation()
    assert recovered["ok"] is True
    assert recovered["resumed"] is True
    assert restored.recovery_status()["stage"] == "filled"
    assert restored.meter.total_kg() == 5.4
    assert restored.bank.stored_kg == 6.6

    isolated = restored.recover_operation()
    assert isolated["action"] == "isolate"
    assert not restored.dispenser.fill_valve_open()
    assert not restored.nozzle.is_seated()
    assert restored.meter.total_kg() == 5.4

    repeated = restored.recover_operation()
    assert repeated["action"] == "none"
    assert restored.meter.total_kg() == 5.4


def test_stop_request_during_fill_isolates_and_accounts_completed_mass(platform):
    platform.start()
    load_station(platform)
    platform.fill(steps=3, interrupt_after="flow_step")
    result = platform.stop_fill()
    assert result["ok"] is True
    assert result["action"] == "isolate"
    assert platform.dispenser.fill_valve_open() is False
    assert platform.dispenser.state == "stopped"
    assert not platform.nozzle.is_seated()
    assert platform.meter.total_kg() == 1.8
    assert platform.bank.stored_kg == 10.2
    assert platform.recovery_status()["pending"] is False


def test_isolate_policy_recovers_interrupted_fill_by_isolation(platform, tmp_path):
    platform.config = dataclasses.replace(platform.config, recovery_policy=RECOVERY_ISOLATE)
    platform.recovery.current = None
    platform.start()
    load_station(platform)
    platform.fill(steps=3, interrupt_after="flow_step")

    restored = _restart(platform, str(tmp_path / "state"))
    decision = restored.recovery_status()
    assert decision["safe_to_resume"] is True
    assert decision["action"] == "isolate"
    result = restored.recover_operation()
    assert result["ok"] is True
    assert result["action"] == "isolate"
    assert restored.dispenser.fill_valve_open() is False
    assert not restored.nozzle.is_seated()
    assert restored.bank.stored_kg == 10.2


def test_unload_resume_never_disconnects_under_pressure(platform, tmp_path):
    platform.start()
    interrupted = platform.unload(target_kg=5.0, dt=1.0, interrupt_after="unloading")
    assert interrupted["interrupted"] is True
    assert platform.truck.state == "unloading"
    assert platform.truck.delivered_kg >= 0.0

    restored = _restart(platform, str(tmp_path / "state"))
    result = restored.recover_operation()
    assert result["ok"] is True
    assert restored.truck.state == "drained"
    assert restored.truck.discharge_ready() is True
    assert not restored.vent.is_open()
    assert not restored.vent_latch.is_engaged()
    assert restored.truck.delivered_kg + 1e-9 >= 5.0


def test_recovery_scope_is_fixed_to_station(platform):
    status = platform.recovery_status()
    assert status["scope"] == "station"
    assert set(status) >= {
        "station",
        "pending",
        "action",
        "safe_to_resume",
        "operation",
        "stage",
        "measured_kg",
        "accounted_kg",
        "remaining_kg",
        "reasons",
    }


def test_torn_write_between_meter_and_checkpoint_forces_isolation(platform, tmp_path):
    import json

    platform.start()
    load_station(platform)
    platform.fill(steps=3, interrupt_after="flow_step")

    runtime_path = tmp_path / "state" / "runtime_state.json"
    raw = json.loads(runtime_path.read_text(encoding="utf-8"))
    raw["devices"]["meter"]["total_kg"] = 3.6
    raw["devices"]["flow_step"]["steps"] = 2
    runtime_path.write_text(json.dumps(raw), encoding="utf-8")

    restored = build_platform(platform.config, clock=platform.clock, storage_root=str(tmp_path / "state"))
    decision = restored.recovery_status()
    assert decision["safe_to_resume"] is False
    assert decision["action"] == "isolate"

    result = restored.recover_operation()
    assert result["ok"] is True
    assert result["action"] == "isolate"
    assert restored.meter.total_kg() == 3.6
    assert restored.bank.stored_kg == 8.4
    assert not restored.dispenser.fill_valve_open()
    assert not restored.nozzle.is_seated()


def test_recovery_decision_is_audited(platform):
    platform.start()
    load_station(platform)
    platform.fill(steps=1, interrupt_after="flow_step")
    platform.recover_operation()
    actions = [event.action for event in platform.bus.history("recovery")]
    assert "recovery_decision" in actions
    assert "recovery_complete" in [event.action for event in platform.bus.history("audit")]
