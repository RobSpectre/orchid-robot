"""Full operator workflows, persistence, and failures without a serial device."""
from copy import deepcopy
import time
import uuid

import pytest

from orchid_demo import motion as m
from orchid_demo.engine import Engine, GuardedController
from orchid_demo.devices import SimulatedArm


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()


def command(e, action, **args):
    owner = "operator"
    e.heartbeat(owner)
    receipt = e.submit(owner, uuid.uuid4().hex, action, e.revision, args)
    e.step()
    result = e.receipts[receipt["id"]]
    assert result["status"] == "complete", result
    assert e.phase != "fault", e.error
    return result


def connect(e, **kwargs):
    command(e, "connect", prepared=True, fixture="Test fixture", **kwargs)


def calibrate(e):
    command(e, "calibrate", supported=True)
    command(e, "calibration_center", supported=True)
    for _ in range(5):
        command(e, "simulate_sweep")
        command(e, "calibration_next", range_complete=True)
    command(e, "calibration_save", range_complete=True)


def teach(e):
    command(e, "capture_pressed")
    command(e, "capture_touch")
    command(e, "capture_clear", supported=True)
    assert e.phase == "holding"
    assert e.arm.enabled


def accept_trial(e):
    command(e, "test", hands_clear=True)
    assert e.phase == "result"
    command(e, "pass")


def held(e):
    connect(e)
    calibrate(e)
    command(e, "note_start", key="C", supported=True)
    teach(e)


def test_complete_twelve_notes_and_resume(engine, tmp_path):
    connect(engine)
    calibrate(engine)
    command(engine, "note_start", key="C", supported=True)
    for note in m.KEYS:
        assert engine.selected == note
        teach(engine)
        for _ in range(3):
            accept_trial(engine)
        assert engine.key_statuses()[note]["status"] == "registered"
        command(engine, "next", supported=True)
        assert not engine.arm.enabled
    assert engine.phase == "ready"
    export = engine.export_snapshot()
    assert list(export["keys"]) == list(m.KEYS)
    assert all(e["verification"]["successful_trials"] == 3 for e in export["keys"].values())
    assert all(e["mode"] == "simulation" for e in export["keys"].values())
    command(engine, "disconnect", supported=True)
    reloaded = Engine(tmp_path)
    try:
        assert reloaded.phase == "disconnected"
        assert reloaded.arm is None  # Saved poses never resume powered motion.
        assert all(v["status"] == "registered" for v in reloaded.key_statuses().values())
        connect(reloaded, fixture_unchanged=True)
        assert reloaded.calibrated
    finally:
        reloaded.close()
    physical = Engine(tmp_path, "hardware", hardware_factory=lambda *_: pytest.fail("Hardware accessed"))
    try:
        assert physical.calibration is None
        assert all(v is None for v in physical.notes.values())
        assert physical.arm is None
    finally:
        physical.close()


def test_support_and_calibration_are_required(engine):
    connect(engine)
    with pytest.raises(m.SafetyError, match="weight"):
        engine.dispatch("calibrate", {})
    with pytest.raises(m.SafetyError, match="calibration"):
        engine.dispatch("note_start", {"supported": True, "key": "C"})
    assert not engine.arm.enabled


def test_calibration_rejects_empty_sweep_and_rolls_back(engine):
    connect(engine)
    calibrate(engine)
    previous = deepcopy(engine.calibration)
    command(engine, "calibrate", supported=True)
    command(engine, "calibration_center", supported=True)
    with pytest.raises(m.SafetyError, match="Not enough travel"):
        engine.dispatch("calibration_next", {"range_complete": True})
    command(engine, "release", supported=True)
    assert engine.arm.calibration == previous
    assert engine.repo.get("calibration") == previous
    assert engine.calibrated
    assert not engine.arm.enabled


def test_recalibration_and_fixture_changes_invalidate_notes(engine):
    held(engine)
    for _ in range(3):
        accept_trial(engine)
    command(engine, "release", supported=True)
    command(engine, "calibrate", supported=True)
    command(engine, "calibration_center", supported=True)
    for _ in range(5):
        command(engine, "simulate_sweep")
        if engine.range_index == 0:
            engine.arm.current["shoulder_pan"] = 3200
            engine.sample()
        command(engine, "calibration_next", range_complete=True)
    command(engine, "calibration_save", range_complete=True)
    assert engine.key_statuses()["C"]["status"] == "needs_reteach"
    command(engine, "disconnect", supported=True)
    old_fixture = engine.fixture["id"]
    connect(engine)
    assert old_fixture != engine.fixture["id"]
    assert engine.key_statuses()["C"]["status"] == "needs_reteach"


def test_duplicate_stale_and_second_operator_commands(engine):
    engine.heartbeat("first")
    with pytest.raises(m.SafetyError, match="Another browser"):
        engine.heartbeat("second")
    args = {"prepared": True, "fixture": "Test"}
    r = engine.submit("first", "same-id", "connect", 0, args)
    assert engine.submit("first", "same-id", "connect", 0, args) == r
    with pytest.raises(m.SafetyError, match="different contents"):
        engine.submit("first", "same-id", "calibrate", 0, {})
    engine.step()
    assert engine.submit("first", "same-id", "connect", 0, args)["status"] == "complete"
    with pytest.raises(m.SafetyError, match="workflow changed"):
        engine.submit("first", "other-id", "calibrate", 0, {"supported": True})


@pytest.mark.parametrize("cause", ["stop", "lease", "disconnect", "tracking", "partial_torque"])
def test_faults_stop_without_dropping_or_retracting(engine, cause):
    held(engine)
    before = dict(engine.arm.current)
    if cause == "stop":
        engine.submit("operator", "stop", "stop", -1, {})
    elif cause == "lease":
        engine.lease_until = time.monotonic() - 1
    elif cause == "disconnect":
        engine.arm.connected = False
    elif cause == "tracking":
        engine.arm.current["wrist_flex"] += 10
    else:
        engine.arm.torque_status = lambda: {**dict.fromkeys(m.MOTORS, 1), "elbow_flex": 0}
    actual_before_fault = dict(engine.arm.current)
    engine.step()
    assert engine.phase == "fault"
    assert engine.arm.enabled  # Never drop the arm on a software stop.
    assert engine.arm.current == actual_before_fault
    assert engine.torque is None
    assert engine.feedback_at is None
    if cause == "stop":
        assert engine.arm.current == before
        command(engine, "retry", supported=True)
        assert not engine.arm.enabled
        assert engine.phase == "note_ready"


def test_late_heartbeat_cannot_resume_lost_hold(engine):
    held(engine)
    engine.lease_until = time.monotonic() - 1
    engine.heartbeat("new operator")
    engine.step()
    assert engine.phase == "fault"
    assert engine.arm.enabled


def test_hands_clear_and_three_trials_before_next(engine):
    held(engine)
    with pytest.raises(m.SafetyError, match="hands"):
        engine.dispatch("test", {})
    accept_trial(engine)
    with pytest.raises(m.SafetyError, match="three trials"):
        engine.dispatch("next", {"supported": True})
    assert engine.arm.enabled
    command(engine, "test", hands_clear=True)
    command(engine, "fail")
    assert engine.trials == 0
    assert engine.key_statuses()["C"]["status"] == "testing"
    assert engine.repo.notes()["C"]["verification"]["successful_trials"] == 0


def test_jammed_motor_does_not_save_a_note(engine):
    held(engine)
    engine.arm.jammed = True
    engine.heartbeat("operator")
    receipt = engine.submit("operator", "jam-test", "test", engine.revision, {"hands_clear": True})
    engine.step()
    assert engine.receipts[receipt["id"]]["status"] == "rejected"
    assert engine.phase == "fault"
    assert engine.notes["C"] is None
    assert engine.arm.enabled


def test_large_manual_jump_stops_capture(engine):
    connect(engine)
    calibrate(engine)
    command(engine, "note_start", key="C", supported=True)
    command(engine, "capture_pressed")
    engine.arm.current["wrist_flex"] += 30
    engine.step()
    assert engine.phase == "fault"
    assert not engine.arm.enabled
    assert engine.notes["C"] is None


def test_cancel_during_handover_never_enables_torque(engine, monkeypatch):
    held(engine)
    command(engine, "retry", supported=True)
    command(engine, "capture_pressed")
    command(engine, "capture_touch")
    original = GuardedController.arm_here
    def interrupt_during_stability(controller):
        def stop(_):
            engine.stop_event.set()
        controller.sleep = stop
        return original(controller)
    monkeypatch.setattr(GuardedController, "arm_here", interrupt_during_stability)
    engine.heartbeat("operator")
    engine.submit("operator", "clear", "capture_clear", engine.revision, {"supported": True})
    engine.step()
    assert engine.phase == "fault"
    assert not engine.arm.enabled


def test_simulator_imports_no_robot_stack(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        assert not name.startswith(("lerobot", "serial", "scservo"))
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    e = Engine(tmp_path)
    try:
        connect(e)
        assert isinstance(e.arm, SimulatedArm)
    finally:
        e.close()


def test_stop_before_connect_still_allows_connection(engine):
    engine.heartbeat("operator")
    engine.submit("operator", "early-stop", "stop", -1, {})
    engine.step()
    assert engine.phase == "disconnected"
    connect(engine)
    assert engine.phase == "connected"


def test_unexpected_worker_failure_leaves_visible_fault_and_hold(engine, monkeypatch):
    held(engine)
    arm = engine.arm
    def fail():
        raise OSError("disk unavailable")
    monkeypatch.setattr(engine, "step", fail)
    engine.start()
    engine.thread.join(timeout=2)
    assert not engine.thread.is_alive()
    assert engine.snapshot()["worker_alive"] is False
    assert engine.phase == "fault"
    assert "disk unavailable" in engine.error
    assert arm.enabled
    assert not arm.connected
    with pytest.raises(m.SafetyError, match="not running"):
        engine.submit("operator", "late", "retry", engine.revision, {"supported": True})
