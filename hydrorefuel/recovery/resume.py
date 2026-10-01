"""按检查点续作卸氢与加注。

函数只从最后一个**已落盘**阶段之后继续；阶段内的质量也以检查点中的
``completed_kg`` / ``meter_total_kg`` 为准。重复调用时，如果检查点已经到达
终态，直接返回同构报告，不再次驱动任何计量动作。
"""

from __future__ import annotations

from typing import Callable

from ..cool.precool import Precooler
from ..cool.temperature import TemperatureModel
from ..disp.machine import Dispenser
from ..disp.nozzle import Nozzle
from ..errors import HydroError
from ..flow.meter import MassFlowMeter
from ..flow.step import FlowStep
from ..queue.confirm import Confirmation
from ..truck.machine import TruckMachine
from ..vent.latch import VentLatch
from ..vent.valve import VentValve
from .checkpoint import (
    OP_FILL,
    OP_UNLOAD,
    POLICY_ABORT,
    Checkpoint,
)

ProgressHook = Callable[[Checkpoint], None]


def _log_step(steps: list[dict], component: str, action: str, detail: dict, stage: str | None = None) -> None:
    item = {"component": component, "action": action, "detail": detail}
    if stage is not None:
        item["stage"] = stage
    steps.append(item)


def _hydro_violation(error: HydroError, steps: list[dict]) -> str:
    payload = error.as_record()
    steps.append({"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}})
    return f"{payload['component']}:{payload['type']}"


def resume_unload(
    checkpoint: Checkpoint,
    truck: TruckMachine,
    vent: VentValve,
    latch: VentLatch,
    *,
    max_steps: int = 4000,
    on_stage: ProgressHook | None = None,
) -> dict:
    """从卸氢检查点继续；``abort`` 决策只执行泄压放散和安全拔管。"""

    if checkpoint.operation != OP_UNLOAD:
        raise ValueError("检查点不是卸氢作业")
    if checkpoint.terminal:
        return {
            "station": checkpoint.station,
            "ok": True,
            "resumed": True,
            "idempotent": True,
            "decision": POLICY_ABORT,
            "steps": [],
            "violations": [],
            "completed_kg": round(checkpoint.completed_kg, 6),
        }

    decision = checkpoint.decision
    steps: list[dict] = []
    violations: list[str] = []
    stage = checkpoint.stage_index

    try:
        if decision == POLICY_ABORT:
            checkpoint = _abort_unload(checkpoint, truck, vent, latch, steps, on_stage)
            return {
                "station": checkpoint.station,
                "ok": checkpoint.terminal,
                "resumed": True,
                "decision": decision,
                "steps": steps,
                "violations": [] if checkpoint.terminal else ["unload_isolate_incomplete"],
                "completed_kg": round(checkpoint.completed_kg, 6),
            }

        if stage <= 1:
            pressure = truck.connect(checkpoint.hose_pressure_mpa)
            checkpoint = checkpoint.advanced(checkpoint.stages[1], checkpoint.updated_ts, truck_pressure_mpa=pressure)
            _persist(checkpoint, on_stage)
            _log_step(steps, "truck", "connect", {"pressure_mpa": round(pressure, 3)}, checkpoint.stage)

        stage = checkpoint.stage_index
        if stage <= 2:
            travel = latch.engage()
            checkpoint = checkpoint.advanced(checkpoint.stages[2], checkpoint.updated_ts)
            _persist(checkpoint, on_stage)
            _log_step(steps, "vent", "latch_engage", {"travel_mm": travel}, checkpoint.stage)

        stage = checkpoint.stage_index
        if stage <= 3:
            truck.open_unload()
            checkpoint = checkpoint.advanced(checkpoint.stages[4], checkpoint.updated_ts)
            _persist(checkpoint, on_stage)
            _log_step(steps, "truck", "open_unload", {"state": truck.state}, checkpoint.stage)

        stage = checkpoint.stage_index
        if stage == 4:
            completed = checkpoint.completed_kg
            pressure = checkpoint.truck_pressure_mpa
            for _ in range(max_steps):
                if completed + 1e-9 >= checkpoint.target_kg:
                    break
                before = completed
                pressure = truck.transfer(checkpoint.dt)
                completed = truck.delivered_kg
                added = completed - before
                checkpoint = checkpoint.advanced(
                    checkpoint.stages[4],
                    checkpoint.updated_ts,
                    completed_kg=completed,
                    truck_pressure_mpa=pressure,
                    params={"truck_vents": int(checkpoint.params.get("truck_vents", 0))},
                )
                _persist(checkpoint, on_stage)
                if added <= 0.0:
                    violations.append("unload_progress_stalled")
                    break
            _log_step(
                steps,
                "truck",
                "transfer",
                {"delivered_kg": round(completed, 3), "pressure_mpa": round(pressure, 3)},
                checkpoint.stage,
            )
            if completed + 1e-9 < checkpoint.target_kg:
                violations.append("unload_incomplete")
            else:
                truck.finish_unload()
                checkpoint = checkpoint.advanced(
                    checkpoint.stages[5], checkpoint.updated_ts, truck_pressure_mpa=truck.pressure_mpa
                )
                _persist(checkpoint, on_stage)
                _log_step(steps, "truck", "finish_unload", {"state": truck.state}, checkpoint.stage)

        if not violations and checkpoint.stage == "finish_unload":
            vent.open()
            checkpoint = checkpoint.advanced(
                checkpoint.stages[6], checkpoint.updated_ts, vent_open=True, vented_mpa=vent.vented_mpa()
            )
            _persist(checkpoint, on_stage)
            _log_step(steps, "vent", "open", {"open": vent.is_open()}, checkpoint.stage)

        if not violations and checkpoint.stage == "vent_open":
            ready, vented, checkpoint = _bleed_until_ready(
                checkpoint, truck, vent, max_steps, "vent_hold", on_stage
            )
            checkpoint = checkpoint.advanced(
                "vent_hold",
                checkpoint.updated_ts,
                truck_pressure_mpa=truck.pressure_mpa,
                vent_open=vent.is_open(),
                vented_mpa=vented,
            )
            _persist(checkpoint, on_stage)
            _log_step(
                steps,
                "vent",
                "vent_hold",
                {
                    "pressure_mpa": round(truck.pressure_mpa, 3),
                    "vented_mpa": round(vented, 3),
                    "ready": ready,
                },
                checkpoint.stage,
            )
            if not ready:
                violations.append("vent_timeout")

        if not violations and checkpoint.stage == "vent_hold":
            vent.close()
            latch.release()
            checkpoint = checkpoint.advanced(
                checkpoint.stages[8], checkpoint.updated_ts, vent_open=False, vented_mpa=vent.vented_mpa()
            )
            _persist(checkpoint, on_stage)
            _log_step(steps, "vent", "close", {"latch_engaged": latch.is_engaged()}, checkpoint.stage)

        if not violations and checkpoint.stage == "vent_close":
            truck.disconnect()
            checkpoint = checkpoint.advanced(checkpoint.stages[9], checkpoint.updated_ts)
            _persist(checkpoint, on_stage)
            _log_step(steps, "truck", "disconnect", {"state": truck.state}, checkpoint.stage)
    except HydroError as error:
        violations.append(_hydro_violation(error, steps))

    return {
        "station": checkpoint.station,
        "ok": not violations and checkpoint.terminal,
        "resumed": True,
        "decision": decision,
        "steps": steps,
        "violations": violations,
        "completed_kg": round(checkpoint.completed_kg, 6),
    }


def _abort_unload(
    checkpoint: Checkpoint,
    truck: TruckMachine,
    vent: VentValve,
    latch: VentLatch,
    steps: list[dict],
    on_stage: ProgressHook | None,
) -> Checkpoint:
    """按隔离顺序安全收尾：先切断卸氢，再泄压，最后才允许拔管。"""

    if checkpoint.stage == "init":
        checkpoint = Checkpoint.from_dict({**checkpoint.as_dict(), "stage": checkpoint.stages[-1]})
        _persist(checkpoint, on_stage)
        _log_step(steps, "truck", "disconnect", {"state": "drained", "skipped": True}, checkpoint.stage)
        return checkpoint

    if truck.state == "unloading":
        truck.finish_unload()
        checkpoint = checkpoint.advanced(
            "finish_unload", checkpoint.updated_ts, truck_pressure_mpa=truck.pressure_mpa
        )
        _persist(checkpoint, on_stage)
        _log_step(steps, "truck", "finish_unload", {"state": truck.state}, checkpoint.stage)

    if checkpoint.stage in ("connect", "latch_engage", "finish_unload"):
        if checkpoint.stage == "connect":
            latch.engage()
            checkpoint = checkpoint.advanced("latch_engage", checkpoint.updated_ts)
            _persist(checkpoint, on_stage)
            _log_step(steps, "vent", "latch_engage", {"travel_mm": latch.travel()}, checkpoint.stage)
        if not vent.is_open():
            vent.open()
        checkpoint = checkpoint.advanced("vent_open", checkpoint.updated_ts, vent_open=True, vented_mpa=vent.vented_mpa())
        _persist(checkpoint, on_stage)
        _log_step(steps, "vent", "open", {"open": vent.is_open()}, checkpoint.stage)

    if checkpoint.stage in ("vent_open", "vent_hold"):
        ready, vented, checkpoint = _bleed_until_ready(
            checkpoint, truck, vent, 4000, "vent_hold", on_stage
        )
        checkpoint = checkpoint.advanced(
            "vent_hold",
            checkpoint.updated_ts,
            truck_pressure_mpa=truck.pressure_mpa,
            vent_open=vent.is_open(),
            vented_mpa=vented,
        )
        _persist(checkpoint, on_stage)
        _log_step(
            steps,
            "vent",
            "vent_hold",
            {"pressure_mpa": round(truck.pressure_mpa, 3), "ready": ready},
            checkpoint.stage,
        )
        if not ready:
            return checkpoint

    if checkpoint.stage == "vent_hold":
        vent.close()
        latch.release()
        checkpoint = checkpoint.advanced("vent_close", checkpoint.updated_ts, vent_open=False)
        _persist(checkpoint, on_stage)
        _log_step(steps, "vent", "close", {"latch_engaged": latch.is_engaged()}, checkpoint.stage)

    if checkpoint.stage == "vent_close":
        truck.disconnect()
        checkpoint = checkpoint.advanced("disconnect", checkpoint.updated_ts)
        _persist(checkpoint, on_stage)
        _log_step(steps, "truck", "disconnect", {"state": truck.state}, checkpoint.stage)
    return checkpoint


def _bleed_until_ready(
    checkpoint: Checkpoint,
    truck: TruckMachine,
    vent: VentValve,
    max_steps: int,
    target_stage: str,
    on_stage: ProgressHook | None = None,
) -> tuple[bool, float, Checkpoint]:
    vented = checkpoint.vented_mpa
    for _ in range(max_steps):
        if truck.discharge_ready():
            return True, vent.vented_mpa(), checkpoint
        bled = vent.bleed(truck.pressure_mpa, checkpoint.dt)
        truck.bleed(truck.pressure_mpa - bled)
        if checkpoint.stage != target_stage:
            checkpoint = checkpoint.advanced(
                target_stage,
                checkpoint.updated_ts,
                truck_pressure_mpa=truck.pressure_mpa,
                vent_open=vent.is_open(),
                vented_mpa=vent.vented_mpa(),
            )
        else:
            checkpoint = checkpoint.advanced(
                checkpoint.stage,
                checkpoint.updated_ts,
                truck_pressure_mpa=truck.pressure_mpa,
                vent_open=vent.is_open(),
                vented_mpa=vent.vented_mpa(),
            )
        _persist(checkpoint, on_stage)
    return truck.discharge_ready(), vent.vented_mpa(), checkpoint


def resume_fill(
    checkpoint: Checkpoint,
    dispenser: Dispenser,
    nozzle: Nozzle,
    precooler: Precooler,
    temperature: TemperatureModel,
    meter: MassFlowMeter,
    step: FlowStep,
    confirm: Confirmation,
    *,
    max_steps: int = 6000,
    on_stage: ProgressHook | None = None,
) -> dict:
    """从加注检查点继续；不满足工位安全续作条件时先隔离停机再回枪。"""

    if checkpoint.operation != OP_FILL:
        raise ValueError("检查点不是加注作业")
    if checkpoint.terminal:
        return {
            "station": checkpoint.station,
            "vehicle": checkpoint.vehicle,
            "ok": True,
            "resumed": True,
            "idempotent": True,
            "decision": POLICY_ABORT,
            "steps": [],
            "violations": [],
            "filled_kg": round(checkpoint.completed_kg, 6),
        }

    decision = checkpoint.decision
    steps: list[dict] = []
    violations: list[str] = []

    try:
        if decision == POLICY_ABORT:
            checkpoint = _abort_fill(checkpoint, dispenser, nozzle, steps, on_stage)
            return {
                "station": checkpoint.station,
                "vehicle": checkpoint.vehicle,
                "ok": checkpoint.terminal,
                "resumed": True,
                "decision": decision,
                "steps": steps,
                "violations": [] if checkpoint.terminal else ["fill_isolate_incomplete"],
                "filled_kg": round(checkpoint.completed_kg, 6),
            }

        if checkpoint.stage == "init":
            confirm.open(checkpoint.vehicle, checkpoint.updated_ts)
            accepted = confirm.confirm(checkpoint.vehicle, checkpoint.updated_ts)
            _log_step(
                steps,
                "queue",
                "confirm",
                {"vehicle": checkpoint.vehicle, "accepted": accepted},
                "init",
            )
            if not accepted:
                violations.append("queue_confirm_rejected")
            else:
                checkpoint = checkpoint.advanced("confirm", checkpoint.updated_ts)
                _persist(checkpoint, on_stage)

        if not violations and checkpoint.stage == "confirm":
            dispenser.reset()
            seated = nozzle.seat(nozzle.seat_travel_mm + 5.0)
            checkpoint = checkpoint.advanced("seat_nozzle", checkpoint.updated_ts)
            _persist(checkpoint, on_stage)
            _log_step(
                steps,
                "disp",
                "seat_nozzle",
                {"seated": seated, "latched": nozzle.is_latched(), "travel_mm": nozzle.travel_mm()},
                checkpoint.stage,
            )
            if not seated:
                violations.append("nozzle_not_seated")

        if not violations and checkpoint.stage == "seat_nozzle":
            precooler.start()
            checkpoint = checkpoint.advanced(
                "seat_nozzle",
                checkpoint.updated_ts,
                params={"temperature_c": temperature.value_c, "held_s": 0.0},
            )
            _persist(checkpoint, on_stage)
            ready = False
            for _ in range(max_steps):
                temperature.cool(checkpoint.dt)
                ready = precooler.tick(checkpoint.dt, temperature.value_c)
                if ready:
                    break
            checkpoint = checkpoint.advanced(
                "precool",
                checkpoint.updated_ts,
                params={"temperature_c": temperature.value_c, "held_s": precooler.held_s},
            )
            _persist(checkpoint, on_stage)
            _log_step(
                steps,
                "cool",
                "precool",
                {
                    "temperature_c": round(temperature.value_c, 2),
                    "held_s": round(precooler.held_s, 2),
                    "ready": ready,
                },
                checkpoint.stage,
            )
            if not ready:
                violations.append("precool_not_ready")

        if not violations and checkpoint.stage == "precool":
            dispenser.open_fill_valve()
            checkpoint = checkpoint.advanced("open_fill_valve", checkpoint.updated_ts)
            _persist(checkpoint, on_stage)
            _log_step(
                steps,
                "disp",
                "open_fill_valve",
                {"valve_open": dispenser.fill_valve_open()},
                checkpoint.stage,
            )

        if not violations and checkpoint.stage == "open_fill_valve":
            checkpoint = checkpoint.advanced(
                "transfer",
                checkpoint.updated_ts,
                meter_total_kg=checkpoint.meter_total_kg,
                completed_kg=checkpoint.completed_kg,
            )
            _persist(checkpoint, on_stage)

        if not violations and checkpoint.stage == "transfer":
            for index in range(checkpoint.step_completed, checkpoint.steps_requested):
                target_step_kg = step.target_kg
                while step.current_kg() + 1e-9 < target_step_kg:
                    before = meter.total_kg()
                    meter.commit(checkpoint.rate_kg_per_min, checkpoint.dt)
                    added = meter.total_kg() - before
                    step.add(added)
                    checkpoint = checkpoint.advanced(
                        "transfer",
                        checkpoint.updated_ts,
                        meter_total_kg=meter.total_kg(),
                        completed_kg=checkpoint.completed_kg + added,
                        accounted_kg=checkpoint.accounted_kg + added,
                        step_completed=step.completed(),
                        step_current_kg=step.current_kg(),
                    )
                    _persist(checkpoint, on_stage)
                within = step.within()
                _log_step(
                    steps,
                    "flow",
                    "step_check",
                    {
                        "index": index + 1,
                        "mass_kg": round(step.current_kg(), 3),
                        "target_kg": round(step.target_kg, 3),
                        "within": within,
                    },
                    checkpoint.stage,
                )
                if not within:
                    violations.append(f"flow_step_{index + 1}_mismatch")
                    break
                step.advance()
                checkpoint = checkpoint.advanced(
                    "transfer",
                    checkpoint.updated_ts,
                    completed_kg=checkpoint.completed_kg,
                    step_completed=step.completed(),
                    step_current_kg=0.0,
                )
                _persist(checkpoint, on_stage)

            if violations:
                checkpoint = _abort_fill(checkpoint, dispenser, nozzle, steps, on_stage)
            else:
                checkpoint = checkpoint.advanced(
                    "fill_complete",
                    checkpoint.updated_ts,
                    meter_total_kg=meter.total_kg(),
                    completed_kg=checkpoint.completed_kg,
                    accounted_kg=checkpoint.accounted_kg,
                    step_completed=step.completed(),
                    step_current_kg=0.0,
                )
                _persist(checkpoint, on_stage)
                _log_step(
                    steps,
                    "disp",
                    "fill_complete",
                    {"valve_open": dispenser.fill_valve_open(), "total_kg": round(meter.total_kg(), 3)},
                    checkpoint.stage,
                )
    except HydroError as error:
        violations.append(_hydro_violation(error, steps))
        if dispenser.fill_valve_open():
            dispenser.isolate()
            dispenser.stop()
        if nozzle.is_seated():
            nozzle.unseat()

    operation_complete = not violations and checkpoint.stage_index >= checkpoint.stages.index("fill_complete")
    return {
        "station": checkpoint.station,
        "vehicle": checkpoint.vehicle,
        "ok": operation_complete,
        "resumed": True,
        "decision": decision,
        "steps": steps,
        "violations": violations,
        "filled_kg": round(checkpoint.completed_kg, 6),
        "meter_total_kg": round(checkpoint.meter_total_kg, 6),
    }


def _abort_fill(
    checkpoint: Checkpoint,
    dispenser: Dispenser,
    nozzle: Nozzle,
    steps: list[dict],
    on_stage: ProgressHook | None,
    include_complete: bool = True,
) -> Checkpoint:
    if checkpoint.stage in ("init", "confirm") and not nozzle.is_seated():
        checkpoint = Checkpoint.from_dict({**checkpoint.as_dict(), "stage": checkpoint.stages[-1]})
        _persist(checkpoint, on_stage)
        _log_step(steps, "disp", "unseat_nozzle", {"seated": False, "skipped": True}, checkpoint.stage)
        return checkpoint

    if include_complete and checkpoint.stage == "transfer":
        checkpoint = checkpoint.advanced(
            "fill_complete", checkpoint.updated_ts, completed_kg=checkpoint.completed_kg
        )
        _persist(checkpoint, on_stage)
        _log_step(
            steps,
            "disp",
            "fill_complete",
            {"valve_open": dispenser.fill_valve_open(), "interrupted": True},
            checkpoint.stage,
        )

    if checkpoint.stage in ("fill_complete", "transfer", "open_fill_valve"):
        if dispenser.fill_valve_open():
            dispenser.isolate()
        checkpoint = checkpoint.advanced("isolated", checkpoint.updated_ts)
        _persist(checkpoint, on_stage)
        _log_step(steps, "disp", "isolate", {"valve_open": dispenser.fill_valve_open()}, checkpoint.stage)

    if checkpoint.stage == "isolated":
        if dispenser.is_filling():
            dispenser.stop()
        checkpoint = checkpoint.advanced("stopped", checkpoint.updated_ts)
        _persist(checkpoint, on_stage)
        _log_step(steps, "disp", "stop", {"state": dispenser.state}, checkpoint.stage)

    if checkpoint.stage == "stopped" and nozzle.is_seated():
        nozzle.unseat()
    if checkpoint.stage in ("stopped", "precool", "seat_nozzle", "confirm"):
        if nozzle.is_seated():
            nozzle.unseat()
        checkpoint = checkpoint.advanced("unseat_nozzle", checkpoint.updated_ts)
        _persist(checkpoint, on_stage)
        _log_step(
            steps,
            "disp",
            "unseat_nozzle",
            {"seated": nozzle.is_seated(), "travel_mm": nozzle.travel_mm()},
            checkpoint.stage,
        )
    return checkpoint


def _persist(checkpoint: Checkpoint, on_stage: ProgressHook | None) -> None:
    if on_stage is not None:
        on_stage(checkpoint)
