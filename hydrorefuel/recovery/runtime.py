"""工位级断点续作执行器。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..config import RECOVERY_ISOLATE, RECOVERY_RESUME
from ..errors import HydroError, InterlockError
from .checkpoint import (
    FILL_STAGE_CONFIRMED,
    FILL_STAGE_FILLED,
    FILL_STAGE_FLOW_STEP,
    FILL_STAGE_PRECOOLED,
    FILL_STAGE_SEATED,
    OP_FILL,
    OP_UNLOAD,
    STATUS_FINALIZED,
    STATUS_INTERRUPTED,
    UNLOAD_STAGE_CONNECTED,
    UNLOAD_STAGE_DRAINED,
    UNLOAD_STAGE_LATCHED,
    UNLOAD_STAGE_UNLOADING,
    UNLOAD_STAGE_VENTING,
    CheckpointStore,
    OperationCheckpoint,
    decide,
)

if TYPE_CHECKING:
    from ..app import Platform


class OperationInterrupted(RuntimeError):
    """操作在稳定检查点被暂停。"""


class StepMismatch(RuntimeError):
    """流量阶梯核对未过。"""

    def __init__(self, index: int) -> None:
        super().__init__(f"flow_step_{index}_mismatch")
        self.violation = str(self)


class RecoveryRuntime:
    """按检查点执行加注/卸氢，并在恢复时保证动作幂等。"""

    def __init__(self, platform: "Platform") -> None:
        self.platform = platform
        self.checkpoints = CheckpointStore(platform.store)
        self.current: OperationCheckpoint | None = None
        self._stop_requested = False

    def restore(self) -> OperationCheckpoint | None:
        checkpoint = self.checkpoints.load(self.platform.unit)
        runtime_devices = self.checkpoints.load_runtime(self.platform.unit)
        if checkpoint is None:
            if runtime_devices:
                self._restore_devices_dict(runtime_devices)
            self.current = None
            return None

        # 检查点与运行台账每次一起落盘，但两文件之间仍可能掉电。恢复时
        # 同时读两份：物理台账以最后落盘的运行态为准，计量是否对得上决定
        # 能否安全续作。
        reference = runtime_devices or checkpoint.devices
        checkpoint.safe_to_resume = self._validate_checkpoint(checkpoint, reference)
        self._restore_devices_dict(reference)
        self.checkpoints.save(checkpoint)
        self.current = checkpoint
        return checkpoint

    def _validate_checkpoint(self, checkpoint: OperationCheckpoint, runtime_devices: dict[str, Any]) -> bool:
        """用运行台账核对检查点；任何计量/阀位不一致都禁止安全续作。

        计量台账与检查点是两份落盘文档，掉电可能落在两次写入之间。运行台账
        多于检查点的量按"已计量未入账"处理：禁止续作，交隔离收尾按实际量
        结算，绝不重放步骤补账。
        """

        if not checkpoint.devices or not runtime_devices:
            return False
        if checkpoint.operation == OP_FILL:
            meter_total = float(runtime_devices.get("meter", {}).get("total_kg", 0.0))
            meter_start = float(checkpoint.params.get("meter_start_kg", 0.0))
            actual_kg = max(0.0, meter_total - meter_start)
            # 运行台账与检查点必须严格一致：少了说明台账丢失，多了说明掉电
            # 落在计量之后、检查点之前。两种情况都不能续作，避免重放补账
            # 或重复计量。
            if abs(actual_kg - checkpoint.measured_kg) > 1e-6:
                return False
            total_steps = int(checkpoint.params.get("steps", 0))
            completed = int(runtime_devices.get("flow_step", {}).get("steps", 0))
            if completed != checkpoint.flow_steps_completed or completed > total_steps:
                return False
            target = float(checkpoint.params.get("target_kg", 0.0))
            if actual_kg > target + 1e-6:
                return False
            dispenser = runtime_devices.get("dispenser", {})
            nozzle = runtime_devices.get("nozzle", {})
            if checkpoint.stage in (FILL_STAGE_SEATED, FILL_STAGE_PRECOOLED, FILL_STAGE_FLOW_STEP):
                if not nozzle.get("seated") or not nozzle.get("latched"):
                    return False
            if checkpoint.stage == FILL_STAGE_FLOW_STEP and not dispenser.get("valve_open"):
                return False
            return True
        if checkpoint.operation == OP_UNLOAD:
            truck = runtime_devices.get("truck", {})
            if checkpoint.stage in (UNLOAD_STAGE_CONNECTED, UNLOAD_STAGE_LATCHED, UNLOAD_STAGE_UNLOADING):
                if truck.get("state") not in ("connected", "unloading"):
                    return False
            delivered = float(truck.get("delivered_kg", 0.0))
            if abs(delivered - checkpoint.delivered_kg) > 1e-6:
                return False
            if checkpoint.stage == UNLOAD_STAGE_VENTING and not runtime_devices.get("vent", {}).get("open"):
                return False
            return True
        return False

    def request_stop(self) -> bool:
        """请求在当前或下一个稳定检查点暂停。"""

        if self.current is None or self.current.terminal():
            return False
        self._stop_requested = True
        if self.current is not None and not self.current.terminal():
            self.current.stop_requested = True
            self._save(self.current)
        return True

    def decision(self):
        return decide(self.current)

    def persist(self) -> None:
        """把当前设备运行态落盘，供非恢复操作后的重启使用。"""

        devices = self._snapshot_devices()
        self.checkpoints.save_runtime(self.platform.unit, devices)

    def fill(
        self,
        *,
        op_id: str,
        vehicle: str,
        rate_kg_per_min: float,
        steps: int,
        dt: float,
        interrupt_after: str | None = None,
    ) -> dict[str, Any]:
        if self.current is not None and not self.current.terminal():
            raise InterlockError("工位存在未恢复操作，必须先恢复或隔离收尾", component="recovery")

        platform = self.platform
        log: list[dict[str, Any]] = []
        violations: list[str] = []
        interrupted = False
        platform.dispenser.check_line_pressure(platform.bank.pressure_mpa)
        platform.confirm.open(vehicle, platform.clock.now())
        accepted = platform.confirm.confirm(vehicle, platform.clock.now() + 0.5)
        log.append({"component": "queue", "action": "confirm", "detail": {"vehicle": vehicle, "accepted": accepted}})
        if not accepted:
            return self._report(OP_FILL, vehicle, log, ["queue_confirm_rejected"], False)

        meter_start = platform.meter.total_kg()
        target_kg = rate_kg_per_min * dt / 60.0 * steps
        platform.dispenser.reset()
        platform.flow_step.restore(0.0, 0)
        if platform.nozzle.is_seated():
            platform.nozzle.unseat()

        checkpoint = OperationCheckpoint(
            station=platform.unit,
            op_id=op_id,
            operation=OP_FILL,
            stage=FILL_STAGE_CONFIRMED,
            status=STATUS_INTERRUPTED,
            recovery_policy=platform.config.recovery_policy,
            safe_to_resume=True,
            params={
                "vehicle": vehicle,
                "rate_kg_per_min": rate_kg_per_min,
                "steps": steps,
                "dt": dt,
                "meter_start_kg": meter_start,
                "target_kg": target_kg,
            },
            remaining_kg=target_kg,
            pressure_mpa=platform.bank.pressure_mpa,
        )
        self.current = checkpoint
        self._stop_requested = False

        try:
            self._checkpoint(checkpoint, FILL_STAGE_CONFIRMED, interrupt_after)

            seated = platform.nozzle.seat(platform.nozzle.seat_travel_mm + 5.0)
            log.append(
                {
                    "component": "disp",
                    "action": "seat_nozzle",
                    "detail": {
                        "seated": seated,
                        "latched": platform.nozzle.is_latched(),
                        "travel_mm": platform.nozzle.travel_mm(),
                    },
                }
            )
            if not seated:
                self._abandon_current()
                return self._report(OP_FILL, vehicle, log, ["nozzle_not_seated"], False)
            self._checkpoint(checkpoint, FILL_STAGE_SEATED, interrupt_after)

            platform.temperature.reset()
            platform.precooler.start()
            ready = self._run_precool(dt)
            log.append(
                {
                    "component": "cool",
                    "action": "precool",
                    "detail": {
                        "temperature_c": round(platform.temperature.value_c, 2),
                        "held_s": round(platform.precooler.held_s, 2),
                        "ready": ready,
                    },
                }
            )
            if not ready:
                if platform.nozzle.is_seated():
                    platform.nozzle.unseat()
                self._abandon_current()
                return self._report(OP_FILL, vehicle, log, ["precool_not_ready"], False)
            self._checkpoint(checkpoint, FILL_STAGE_PRECOOLED, interrupt_after)

            self._ensure_fill_valve_open(checkpoint)
            log.append(
                {
                    "component": "disp",
                    "action": "open_fill_valve",
                    "detail": {"valve_open": platform.dispenser.fill_valve_open()},
                }
            )
            interrupted = self._run_fill_steps(checkpoint, log, interrupt_after)
            if interrupted:
                return self._report(OP_FILL, vehicle, log, [], False, interrupted=True)

            violations.extend(self._account_fill(checkpoint))
            log.append(
                {
                    "component": "disp",
                    "action": "fill_complete",
                    "detail": {
                        "valve_open": platform.dispenser.fill_valve_open(),
                        "total_kg": round(checkpoint.measured_kg, 3),
                    },
                }
            )
            # 计量已完成但尚未隔离停机：保留 filled 检查点。它不在可续作
            # 集合里，重启后一律按隔离顺序收尾，绝不让开阀状态悬空。
            checkpoint.stage = FILL_STAGE_FILLED
            checkpoint.safe_to_resume = True
            checkpoint.status = STATUS_INTERRUPTED
            self._save(checkpoint)
        except OperationInterrupted:
            return self._report(OP_FILL, vehicle, log, [], False, interrupted=True)
        except StepMismatch as error:
            violations.append(error.violation)
            log.append({"component": "flow", "action": "rejected", "detail": {"message": error.violation}})
            self._safe_isolate_fill(checkpoint)
            self._account_fill(checkpoint)
            self._abandon_current()
        except HydroError as error:
            payload = error.as_record()
            violations.append(f"{payload['component']}:{payload['type']}")
            log.append(
                {
                    "component": payload["component"],
                    "action": "rejected",
                    "detail": {"message": payload["message"]},
                }
            )
            self._safe_isolate_fill(checkpoint)
            self._account_fill(checkpoint)
            self._abandon_current()
        return self._report(OP_FILL, vehicle, log, violations, not violations)

    def recover(self, op_id: str) -> dict[str, Any]:
        platform = self.platform
        checkpoint = self.current
        decision = decide(checkpoint)
        log: list[dict[str, Any]] = []
        if checkpoint is None or checkpoint.terminal():
            self._clear_current()
            return {
                "station": platform.unit,
                "op_id": op_id,
                "resumed_op_id": None,
                "ok": True,
                "resumed": False,
                "action": "none",
                "steps": log,
                "violations": [],
                "decision": decision.as_dict(),
            }

        checkpoint.attempts += 1
        checkpoint.status = "resuming"
        self._save(checkpoint)
        vehicle = str(checkpoint.params.get("vehicle", "vehicle01"))

        try:
            if decision.action == RECOVERY_ISOLATE:
                if checkpoint.operation == OP_FILL:
                    self._recover_fill_isolation(checkpoint, log)
                else:
                    self._recover_unload_isolation(checkpoint, log)
                checkpoint.status = STATUS_FINALIZED
                self._clear_current()
                action = RECOVERY_ISOLATE
                ok = True
            else:
                if checkpoint.operation == OP_FILL:
                    ok = self._resume_fill(checkpoint, log)
                else:
                    ok = self._resume_unload(checkpoint, log)
                action = RECOVERY_RESUME
        except StepMismatch as error:
            checkpoint.safe_to_resume = False
            checkpoint.status = STATUS_INTERRUPTED
            self._reconcile_fill_mass(checkpoint)
            self._save(checkpoint)
            return {
                "station": platform.unit,
                "vehicle": vehicle,
                "op_id": op_id,
                "resumed_op_id": checkpoint.op_id,
                "ok": False,
                "resumed": False,
                "action": decision.action,
                "steps": log
                + [{"component": "flow", "action": "rejected", "detail": {"message": error.violation}}],
                "violations": [error.violation],
                "decision": decision.as_dict(),
            }
        except HydroError as error:
            payload = error.as_record()
            checkpoint.safe_to_resume = False
            checkpoint.status = STATUS_INTERRUPTED
            self._save(checkpoint)
            return {
                "station": platform.unit,
                "vehicle": vehicle,
                "op_id": op_id,
                "resumed_op_id": checkpoint.op_id,
                "ok": False,
                "resumed": False,
                "action": decision.action,
                "steps": log
                + [{"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}}],
                "violations": [f"{payload['component']}:{payload['type']}"],
                "decision": decision.as_dict(),
            }

        return {
            "station": platform.unit,
            "vehicle": vehicle,
            "op_id": op_id,
            "resumed_op_id": checkpoint.op_id,
            "ok": ok,
            "resumed": action == RECOVERY_RESUME and ok,
            "action": action,
            "steps": log,
            "violations": [],
            "decision": decision.as_dict(),
        }

    def unload(
        self,
        *,
        op_id: str,
        hose_pressure_mpa: float,
        target_kg: float,
        dt: float,
        interrupt_after: str | None = None,
    ) -> dict[str, Any]:
        if self.current is not None and not self.current.terminal():
            raise InterlockError("工位存在未恢复操作，必须先恢复或隔离收尾", component="recovery")

        platform = self.platform
        steps_log: list[dict[str, Any]] = []
        violations: list[str] = []
        platform.truck.reset()
        platform.vent._open = False
        platform.vent_latch._engaged = False
        platform.vent_latch._traveled_mm = 0.0

        pressure = platform.truck.connect(hose_pressure_mpa)
        steps_log.append({"component": "truck", "action": "connect", "detail": {"pressure_mpa": round(pressure, 3)}})
        checkpoint = OperationCheckpoint(
            station=platform.unit,
            op_id=op_id,
            operation=OP_UNLOAD,
            stage=UNLOAD_STAGE_CONNECTED,
            status=STATUS_INTERRUPTED,
            recovery_policy=platform.config.recovery_policy,
            safe_to_resume=True,
            params={"hose_pressure_mpa": hose_pressure_mpa, "target_kg": target_kg, "dt": dt},
            remaining_kg=target_kg,
            pressure_mpa=pressure,
        )
        self.current = checkpoint
        self._stop_requested = False

        try:
            self._checkpoint(checkpoint, UNLOAD_STAGE_CONNECTED, interrupt_after)
            self._once(checkpoint, "latch_engage", lambda: platform.vent_latch.engage())
            steps_log.append(
                {"component": "vent", "action": "latch_engage", "detail": {"travel_mm": platform.vent_latch.travel()}}
            )
            self._checkpoint(checkpoint, UNLOAD_STAGE_LATCHED, interrupt_after)

            self._once(checkpoint, "open_unload", lambda: platform.truck.open_unload())
            steps_log.append({"component": "truck", "action": "open_unload", "detail": {"state": platform.truck.state}})
            checkpoint.stage = UNLOAD_STAGE_UNLOADING
            self._save(checkpoint)
            self._checkpoint(checkpoint, UNLOAD_STAGE_UNLOADING, interrupt_after)

            self._transfer_unload(checkpoint, steps_log)
            if checkpoint.stop_requested:
                return self._report(OP_UNLOAD, "", steps_log, [], False, interrupted=True)

            self._finish_transfer(checkpoint, steps_log)
            self._vent_and_disconnect(checkpoint, steps_log, interrupt_after=interrupt_after)
            if checkpoint.stage == UNLOAD_STAGE_VENTING and checkpoint.stop_requested:
                return self._report(OP_UNLOAD, "", steps_log, [], False, interrupted=True)
            self._clear_current()
        except OperationInterrupted:
            return self._report(OP_UNLOAD, "", steps_log, [], False, interrupted=True)
        except HydroError as error:
            payload = error.as_record()
            violations.append(f"{payload['component']}:{payload['type']}")
            steps_log.append(
                {"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}}
            )
            self._recover_unload_isolation(checkpoint, steps_log)
            self._abandon_current()
        return self._report(OP_UNLOAD, "", steps_log, violations, not violations)

    def _run_precool(self, dt: float) -> bool:
        platform = self.platform
        ready = False
        for _ in range(6000):
            platform.temperature.cool(dt)
            ready = platform.precooler.tick(dt, platform.temperature.value_c)
            if ready:
                break
        return ready

    def _run_fill_steps(
        self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]], interrupt_after: str | None
    ) -> bool:
        platform = self.platform
        total_steps = int(checkpoint.params["steps"])
        dt = float(checkpoint.params["dt"])
        rate = float(checkpoint.params["rate_kg_per_min"])
        for index in range(total_steps):
            action_id = f"flow:{index + 1}"
            if not checkpoint.action_applied(action_id):
                platform.meter.commit(rate, dt)
                delta = platform.meter.total_kg() - float(checkpoint.params["meter_start_kg"])
                step_kg = delta - platform.flow_step.target_kg * index
                platform.flow_step.restore(step_kg, index)
                within = platform.flow_step.within()
                platform.flow_step.advance()
                checkpoint.measured_kg = delta
                checkpoint.remaining_kg = max(0.0, float(checkpoint.params["target_kg"]) - delta)
                checkpoint.flow_steps_completed = index + 1
                checkpoint.flow_step_kg = 0.0
                checkpoint.mark_action(action_id)
                checkpoint.stage = FILL_STAGE_FLOW_STEP
                self._save(checkpoint)
            else:
                within = True
            log.append(
                {
                    "component": "flow",
                    "action": "step_check",
                    "detail": {
                        "index": index + 1,
                        "mass_kg": round(platform.flow_step.target_kg, 3),
                        "target_kg": round(platform.flow_step.target_kg, 3),
                        "within": within,
                        "resumed": index + 1 <= checkpoint.flow_steps_completed,
                    },
                }
            )
            if not within:
                raise StepMismatch(index + 1)
            self._checkpoint(checkpoint, FILL_STAGE_FLOW_STEP, interrupt_after)
        return checkpoint.stop_requested

    def clear_operation(self) -> None:
        """物理收尾已由调用方完成后，清掉工位检查点。"""

        self._clear_current()

    def _resume_fill(self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]]) -> bool:
        self._restore_devices_dict(checkpoint.devices)
        if checkpoint.stage == FILL_STAGE_CONFIRMED:
            seated = self.platform.nozzle.seat(self.platform.nozzle.seat_travel_mm + 5.0)
            log.append({"component": "disp", "action": "resume_seat_nozzle", "detail": {"seated": seated}})
            checkpoint.stage = FILL_STAGE_SEATED
            self._save(checkpoint)
        if checkpoint.stage == FILL_STAGE_SEATED:
            self.platform.temperature.reset()
            self.platform.precooler.start()
            ready = self._run_precool(float(checkpoint.params["dt"]))
            log.append({"component": "cool", "action": "resume_precool", "detail": {"ready": ready}})
            if not ready:
                return False
            checkpoint.stage = FILL_STAGE_PRECOOLED
            self._save(checkpoint)
        if checkpoint.stage in (FILL_STAGE_PRECOOLED, FILL_STAGE_FLOW_STEP):
            self._ensure_fill_valve_open(checkpoint)
            interrupted = self._run_fill_steps(checkpoint, log, None)
            if interrupted:
                return False
        self._reconcile_fill_mass(checkpoint)
        self._account_fill(checkpoint)
        log.append(
            {
                "component": "recovery",
                "action": "resume_complete",
                "detail": {"measured_kg": round(checkpoint.measured_kg, 3), "accounted_kg": round(checkpoint.accounted_kg, 3)},
            }
        )
        checkpoint.stage = FILL_STAGE_FILLED
        checkpoint.safe_to_resume = True
        checkpoint.status = STATUS_INTERRUPTED
        self._save(checkpoint)
        return True

    def _recover_fill_isolation(self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]]) -> None:
        self._restore_devices_dict(checkpoint.devices)
        self._safe_isolate_fill(checkpoint, log, prefix="recovery_")
        self._account_fill(checkpoint)
        log.append(
            {
                "component": "recovery",
                "action": "recovery_isolated",
                "detail": {"measured_kg": round(checkpoint.measured_kg, 3), "accounted_kg": round(checkpoint.accounted_kg, 3)},
            }
        )

    def _safe_isolate_fill(
        self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]] | None = None, prefix: str = ""
    ) -> None:
        platform = self.platform
        if platform.dispenser.fill_valve_open():
            platform.dispenser.isolate()
            if log is not None:
                log.append({"component": "disp", "action": f"{prefix}isolate", "detail": {"valve_open": False}})
        if platform.dispenser.is_filling():
            platform.dispenser.stop()
            if log is not None:
                log.append({"component": "disp", "action": f"{prefix}stop", "detail": {"state": platform.dispenser.state}})
        if platform.nozzle.is_seated():
            platform.nozzle.unseat()
            if log is not None:
                log.append({"component": "disp", "action": f"{prefix}unseat_nozzle", "detail": {"seated": False}})

    def _account_fill(self, checkpoint: OperationCheckpoint) -> list[str]:
        self._reconcile_fill_mass(checkpoint)
        already = checkpoint.accounted_kg
        measured = checkpoint.measured_kg
        delta = max(0.0, measured - already)
        if delta > 1e-9:
            try:
                self.platform.bank.draw(delta)
                checkpoint.accounted_kg = measured
                self._save(checkpoint)
                return []
            except HydroError:
                return ["fill_accounting_failed"]
        return []

    def _reconcile_fill_mass(self, checkpoint: OperationCheckpoint) -> None:
        """以流量计为准核对已加注量，防止漏账（不重复计量）。"""

        meter_start = float(checkpoint.params.get("meter_start_kg", 0.0))
        actual = max(0.0, self.platform.meter.total_kg() - meter_start)
        checkpoint.measured_kg = max(checkpoint.measured_kg, actual)
        target = float(checkpoint.params.get("target_kg", 0.0))
        checkpoint.remaining_kg = max(0.0, target - checkpoint.measured_kg)

    def _transfer_unload(self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]]) -> None:
        platform = self.platform
        dt = float(checkpoint.params["dt"])
        target = float(checkpoint.params["target_kg"])
        index = 0
        while platform.truck.delivered_kg + 1e-9 < target:
            index += 1
            action_id = f"transfer:{index}"
            if not checkpoint.action_applied(action_id):
                if platform.truck.state != "unloading":
                    platform.truck.open_unload()
                platform.truck.transfer(dt)
                checkpoint.delivered_kg = platform.truck.delivered_kg
                checkpoint.remaining_kg = max(0.0, target - checkpoint.delivered_kg)
                checkpoint.pressure_mpa = platform.truck.pressure_mpa
                checkpoint.mark_action(action_id)
                checkpoint.stage = UNLOAD_STAGE_UNLOADING
                self._save(checkpoint)
            if self._should_stop(checkpoint, UNLOAD_STAGE_UNLOADING):
                break
        log.append(
            {
                "component": "truck",
                "action": "transfer",
                "detail": {
                    "delivered_kg": round(platform.truck.delivered_kg, 3),
                    "pressure_mpa": round(platform.truck.pressure_mpa, 3),
                },
            }
        )

    def _finish_transfer(self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]]) -> None:
        platform = self.platform
        if platform.truck.state == "unloading":
            platform.truck.finish_unload()
            checkpoint.mark_action("finish_unload")
            self._save(checkpoint)
            log.append({"component": "truck", "action": "finish_unload", "detail": {"state": platform.truck.state}})

    def _vent_and_disconnect(
        self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]], interrupt_after: str | None = None
    ) -> None:
        platform = self.platform
        dt = float(checkpoint.params["dt"])
        if not platform.vent.is_open():
            platform.vent.open()
            checkpoint.mark_action("vent_open")
        checkpoint.stage = UNLOAD_STAGE_VENTING
        checkpoint.pressure_mpa = platform.truck.pressure_mpa
        self._save(checkpoint)
        log.append({"component": "vent", "action": "open", "detail": {"open": True}})
        self._checkpoint(checkpoint, UNLOAD_STAGE_VENTING, interrupt_after)

        index = 0
        while not platform.truck.discharge_ready():
            index += 1
            action_id = f"vent:{index}"
            if not checkpoint.action_applied(action_id):
                if not platform.vent.is_open():
                    platform.vent.open()
                bled = platform.vent.bleed(platform.truck.pressure_mpa, dt)
                platform.truck.bleed(platform.truck.pressure_mpa - (platform.truck.pressure_mpa - bled))
                checkpoint.pressure_mpa = platform.truck.pressure_mpa
                checkpoint.mark_action(action_id)
                checkpoint.stage = UNLOAD_STAGE_VENTING
                self._save(checkpoint)
            if self._should_stop(checkpoint, UNLOAD_STAGE_VENTING):
                return
        if platform.vent.is_open():
            platform.vent.close()
        if platform.vent_latch.is_engaged():
            platform.vent_latch.release()
        if platform.truck.state != UNLOAD_STAGE_DRAINED:
            platform.truck.disconnect()
        checkpoint.stage = UNLOAD_STAGE_DRAINED
        checkpoint.remaining_kg = 0.0
        self._save(checkpoint)
        log.append(
            {
                "component": "vent",
                "action": "vent_hold",
                "detail": {"pressure_mpa": round(platform.truck.pressure_mpa, 3), "ready": True},
            }
        )
        log.append({"component": "truck", "action": "disconnect", "detail": {"state": platform.truck.state}})

    def _resume_unload(self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]]) -> bool:
        self._restore_devices_dict(checkpoint.devices)
        if checkpoint.stage == UNLOAD_STAGE_CONNECTED:
            self.platform.vent_latch.engage()
            checkpoint.stage = UNLOAD_STAGE_LATCHED
            self._save(checkpoint)
        if checkpoint.stage == UNLOAD_STAGE_LATCHED:
            self.platform.truck.open_unload()
            checkpoint.stage = UNLOAD_STAGE_UNLOADING
            self._save(checkpoint)
        if checkpoint.stage == UNLOAD_STAGE_UNLOADING:
            self._transfer_unload(checkpoint, log)
            if checkpoint.stop_requested:
                return False
            self._finish_transfer(checkpoint, log)
        if checkpoint.stage in (UNLOAD_STAGE_UNLOADING, UNLOAD_STAGE_VENTING):
            checkpoint.stage = UNLOAD_STAGE_VENTING
            self._vent_and_disconnect(checkpoint, log)
        log.append({"component": "recovery", "action": "resume_complete", "detail": {"delivered_kg": checkpoint.delivered_kg}})
        self._clear_current()
        return True

    def _recover_unload_isolation(self, checkpoint: OperationCheckpoint, log: list[dict[str, Any]]) -> None:
        self._restore_devices_dict(checkpoint.devices)
        platform = self.platform
        checkpoint.delivered_kg = max(checkpoint.delivered_kg, platform.truck.delivered_kg)
        if platform.truck.state == "unloading":
            platform.truck.finish_unload()
            log.append({"component": "truck", "action": "recovery_finish_unload", "detail": {"state": platform.truck.state}})
        if not platform.vent.is_open() and platform.truck.state in ("connected", "unloading"):
            platform.vent.open()
            log.append({"component": "vent", "action": "recovery_open", "detail": {"open": True}})
        dt = float(checkpoint.params.get("dt", 1.0))
        for _ in range(4000):
            if platform.truck.discharge_ready():
                break
            if not platform.vent.is_open():
                platform.vent.open()
            bled = platform.vent.bleed(platform.truck.pressure_mpa, dt)
            platform.truck.bleed(platform.truck.pressure_mpa - (platform.truck.pressure_mpa - bled))
        if not platform.truck.discharge_ready():
            raise HydroError("vent_timeout", component="vent")
        if platform.vent.is_open():
            platform.vent.close()
        if platform.vent_latch.is_engaged():
            platform.vent_latch.release()
        platform.truck.disconnect()
        checkpoint.stage = UNLOAD_STAGE_DRAINED
        log.append(
            {
                "component": "recovery",
                "action": "recovery_isolated",
                "detail": {"pressure_mpa": round(platform.truck.pressure_mpa, 3), "delivered_kg": checkpoint.delivered_kg},
            }
        )

    def _ensure_fill_valve_open(self, checkpoint: OperationCheckpoint) -> None:
        if not self.platform.dispenser.fill_valve_open():
            self.platform.dispenser.open_fill_valve()
            checkpoint.mark_action("open_fill_valve")
            self._save(checkpoint)

    def _once(self, checkpoint: OperationCheckpoint, action_id: str, action) -> None:
        if not checkpoint.action_applied(action_id):
            action()
            checkpoint.mark_action(action_id)
            self._save(checkpoint)

    def _checkpoint(self, checkpoint: OperationCheckpoint, stage: str, interrupt_after: str | None) -> None:
        checkpoint.stage = stage
        checkpoint.stop_requested = self._stop_requested or checkpoint.stop_requested
        self._save(checkpoint)
        if interrupt_after == stage:
            raise OperationInterrupted(stage)
        if self._stop_requested:
            checkpoint.stop_requested = True
            self._save(checkpoint)
            raise OperationInterrupted(stage)

    def _should_stop(self, checkpoint: OperationCheckpoint, stage: str) -> bool:
        checkpoint.stage = stage
        self._save(checkpoint)
        if self._stop_requested:
            checkpoint.stop_requested = True
            self._save(checkpoint)
            return True
        return False

    def _save(self, checkpoint: OperationCheckpoint) -> None:
        checkpoint.updated_ts = self.platform.clock.now()
        checkpoint.devices = self._snapshot_devices()
        self.checkpoints.save_runtime(checkpoint.station, checkpoint.devices)
        self.checkpoints.save(checkpoint)

    def _clear_current(self) -> None:
        devices = self._snapshot_devices()
        self.checkpoints.clear(self.platform.unit)
        self.checkpoints.save_runtime(self.platform.unit, devices)
        self.current = None
        self._stop_requested = False

    def _abandon_current(self) -> None:
        self._clear_current()

    def _snapshot_devices(self) -> dict[str, Any]:
        p = self.platform
        return {
            "truck": {
                "state": p.truck._state,
                "pressure_mpa": p.truck._pressure_mpa,
                "delivered_kg": p.truck._delivered_kg,
                "vents": p.truck._vents,
            },
            "vent": {"open": p.vent._open, "bursts": p.vent._bursts, "vented_mpa": p.vent._vented_mpa},
            "vent_latch": {
                "engaged": p.vent_latch._engaged,
                "traveled_mm": p.vent_latch._traveled_mm,
                "cycles": p.vent_latch._cycles,
                "resets": p.vent_latch._resets,
            },
            "precooler": {"state": p.precooler._state, "held_s": p.precooler._held_s},
            "temperature": {"value_c": p.temperature._value_c},
            "meter": {"rate": p.meter._rate_kg_per_min, "total_kg": p.meter._total_kg},
            "flow_step": {"current_kg": p.flow_step._current_kg, "steps": p.flow_step._steps},
            "nozzle": {
                "travel_mm": p.nozzle._travel_mm,
                "seated": p.nozzle._seated,
                "latched": p.nozzle._latch_engaged,
                "cycles": p.nozzle._cycles,
            },
            "dispenser": {"state": p.dispenser._state, "valve_open": p.dispenser._valve_open},
            "bank": {"pressure_mpa": p.bank._pressure_mpa, "stored_kg": p.bank._stored_kg},
            "compressor": {
                "state": p.compressor._state,
                "pressure_mpa": p.compressor._pressure_mpa,
                "starts": p.compressor._starts,
                "stops": p.compressor._stops,
            },
        }

    def _restore_devices_dict(self, data: dict[str, Any]) -> None:
        if not data:
            return
        p = self.platform
        truck = data.get("truck", {})
        p.truck._state = truck.get("state", "idle")
        p.truck._pressure_mpa = float(truck.get("pressure_mpa", 0.0))
        p.truck._delivered_kg = float(truck.get("delivered_kg", 0.0))
        p.truck._vents = int(truck.get("vents", 0))

        vent = data.get("vent", {})
        p.vent._open = bool(vent.get("open", False))
        p.vent._bursts = int(vent.get("bursts", 0))
        p.vent._vented_mpa = float(vent.get("vented_mpa", 0.0))

        latch = data.get("vent_latch", {})
        p.vent_latch._engaged = bool(latch.get("engaged", False))
        p.vent_latch._traveled_mm = float(latch.get("traveled_mm", 0.0))
        p.vent_latch._cycles = int(latch.get("cycles", 0))
        p.vent_latch._resets = int(latch.get("resets", 0))

        cool = data.get("precooler", {})
        p.precooler._state = cool.get("state", "idle")
        p.precooler._held_s = float(cool.get("held_s", 0.0))
        temperature = data.get("temperature", {})
        p.temperature._value_c = float(temperature.get("value_c", 25.0))

        meter = data.get("meter", {})
        p.meter._rate_kg_per_min = float(meter.get("rate", 0.0))
        p.meter._total_kg = float(meter.get("total_kg", 0.0))
        step = data.get("flow_step", {})
        p.flow_step.restore(float(step.get("current_kg", 0.0)), int(step.get("steps", 0)))

        nozzle = data.get("nozzle", {})
        p.nozzle._travel_mm = float(nozzle.get("travel_mm", 0.0))
        p.nozzle._seated = bool(nozzle.get("seated", False))
        p.nozzle._latch_engaged = bool(nozzle.get("latched", False))
        p.nozzle._cycles = int(nozzle.get("cycles", 0))

        dispenser = data.get("dispenser", {})
        p.dispenser._state = dispenser.get("state", "idle")
        p.dispenser._valve_open = bool(dispenser.get("valve_open", False))

        bank = data.get("bank", {})
        p.bank._pressure_mpa = float(bank.get("pressure_mpa", 0.0))
        p.bank._stored_kg = float(bank.get("stored_kg", 0.0))

        comp = data.get("compressor", {})
        p.compressor._state = comp.get("state", "stopped")
        p.compressor._pressure_mpa = float(comp.get("pressure_mpa", 0.0))
        p.compressor._starts = int(comp.get("starts", 0))
        p.compressor._stops = int(comp.get("stops", 0))

    def _report(
        self,
        operation: str,
        vehicle: str,
        steps: list[dict[str, Any]],
        violations: list[str],
        ok: bool,
        *,
        interrupted: bool = False,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "station": self.platform.unit,
            "ok": ok,
            "steps": steps,
            "violations": list(violations),
            "interrupted": interrupted,
        }
        if operation == OP_FILL:
            report["vehicle"] = vehicle
        return report
