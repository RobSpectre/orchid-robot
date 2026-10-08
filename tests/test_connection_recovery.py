"""Lost USB recovery must work without communicating with either arm."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from orchid_demo import motion as m
from orchid_demo.devices import SimulatedArm
from orchid_demo.engine import Engine
from test_operator_engine import Clock, command, connect, calibrate, reject


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    instance = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield instance
    instance.close()


def unavailable(engine, monkeypatch):
    connect(engine)
    calibrate(engine)
    engine.leader = SimulatedArm(engine.calibration)
    engine.leader.open()
    engine.fault(m.SafetyError("USB connection lost"))
    closed = []
    for role, device in (("follower", engine.arm), ("leader", engine.leader)):
        for name in ("release", "abort_calibration", "read_raw", "torque_status", "send"):
            monkeypatch.setattr(device, name, lambda *a, **kw: pytest.fail("Motor I/O during recovery"))
        monkeypatch.setattr(device, "close", lambda role=role: closed.append(role))
    engine.controller = SimpleNamespace(enabled=True, stop=lambda: pytest.fail("Recovery must not hold"))
    return closed


def test_forget_closes_both_ports_without_motor_io_and_keeps_records(engine, monkeypatch):
    closed = unavailable(engine, monkeypatch)
    saved = deepcopy(engine.repo.get("calibration"))
    engine.repo.put("home", {"id": "saved-home"})
    engine.repo.put("calibration_backup", {"saved": saved})
    engine.calibrating = True
    engine.offsets = {"pending": 1}
    engine.mode = "hardware"  # exercise clearing stale discovery, still fake devices
    controller = engine.controller
    command(engine, "forget_connection", supported=True, motor_power_disconnected=True)
    assert closed == ["follower", "leader"]
    assert not controller.enabled
    assert engine.phase == "disconnected"
    assert engine.arm is engine.leader is engine.controller is None
    assert engine.current is engine.torque is engine.leader_torque is None
    assert engine.offsets is None and not engine.calibrating
    assert not engine.calibrated and not engine.leader_calibrated
    assert engine.discovery["ports"] == []
    assert engine.repo.get("calibration") == saved
    assert engine.repo.get("home") == {"id": "saved-home"}
    assert engine.repo.get("calibration_backup") == {"saved": saved}
    assert "no torque command was sent" in engine.message
    # Shutdown must not revisit the discarded controller or devices.
    engine.cleanup_devices()
    assert closed == ["follower", "leader"]


@pytest.mark.parametrize("args", [{}, {"supported": True}, {"motor_power_disconnected": True}])
def test_forget_requires_both_physical_confirmations(engine, monkeypatch, args):
    closed = unavailable(engine, monkeypatch)
    reject(engine, "forget_connection", **args)
    assert closed == [] and engine.arm is not None
    engine.controller = None  # test cleanup must not invoke the intentionally forbidden stop


def test_forget_is_not_available_during_normal_operation(engine):
    connect(engine)
    reject(engine, "forget_connection", supported=True, motor_power_disconnected=True)
    assert engine.phase == "connected" and engine.arm.connected


def test_close_failure_does_not_skip_other_arm_or_claim_disconnect(engine, monkeypatch):
    closed = unavailable(engine, monkeypatch)
    monkeypatch.setattr(engine.arm, "close", lambda: (_ for _ in ()).throw(OSError("close failed")))
    result = reject(engine, "forget_connection", supported=True, motor_power_disconnected=True)
    assert "restart the Python app" in result["message"]
    assert closed == ["leader"] and engine.phase == "fault"
    assert not engine.controller.enabled
    engine.controller = None


def test_release_failure_explains_unavailable_connection_recovery(engine, monkeypatch):
    connect(engine)
    monkeypatch.setattr(engine.arm, "release", lambda: (_ for _ in ()).throw(OSError(5, "Input/output error")))
    result = reject(engine, "disconnect", supported=True)
    assert engine.phase == "fault" and engine.arm is not None
    assert "Torque release could not be verified" in result["message"]
    assert "Forget unavailable connection" in result["message"]
    assert engine.torque is None
