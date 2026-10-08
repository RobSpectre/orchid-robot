"""Dual-arm workflows with native position following; never opens hardware ports."""
from copy import deepcopy
import time

import pytest

from orchid_demo import motion as m
from orchid_demo.engine import Engine
from orchid_demo.devices import SimulatedArm
from orchid_demo.leader import LeaderController
from test_operator_engine import Clock, command, connect, calibrate, reject


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()


def prepare(e, control="C", *, local=True, resting=False):
    connect(e, teaching_mode="leader")
    calibrate(e)
    command(e, "calibrate", target="leader", supported=True)
    command(e, "calibration_center", supported=True)
    for _ in range(5):
        command(e, "simulate_sweep")
        command(e, "calibration_next", range_complete=True)
    command(e, "calibration_save", range_complete=True)
    if resting:
        e.arm.current["elbow_flex"] = 3100
        e.arm.current["gripper"] = 1000
    e.leader.current = dict(e.arm.current)  # Operator matches poses before engagement.
    command(e, "capture_home", supported=True)
    command(e, "control_start", control=control, supported=True)
    assert e.arm.enabled and not e.controller.engaged
    if local:
        command(e, "capture_key_clearance", path_clear=True)


def move(e, motor="wrist_flex", ticks=12):
    # Model the operator bringing the leader close during the countdown.
    e.leader.current = dict(e.arm.current)
    command(e, "leader_resume", hands_clear=True)
    command(e, "simulate_leader", motor=motor, delta=ticks)
    while e.simulated_leader_input:
        e.step()
        assert e.phase != "fault", e.error
    # Position targets persist while the leader is held still; wait for arrival
    # before capturing, just as an operator watching the arm would.
    for _ in range(100):
        if m.distance(e.controller.previous, e.controller.desired) == 0:
            break
        e.step()
        assert e.phase != "fault", e.error
    else:
        pytest.fail("Leader target did not converge")
    command(e, "leader_pause")


@pytest.mark.parametrize("control", ["C", "chord.min"])
def test_leader_press_captures_measured_follower_and_verifies_three_trials(engine, control):
    prepare(engine, control)
    assert engine.arm.enabled and not engine.leader.enabled
    assert engine.capture["path"][0]["wrist_flex"] == 2047
    move(engine, ticks=12)
    command(engine, "capture_touch")
    move(engine, ticks=12)
    command(engine, "capture_pressed")
    assert engine.draft["path"][-1]["wrist_flex"] == 2071
    command(engine, "retreat_from_press", hands_clear=True)
    assert engine.phase == "home_return" and isinstance(engine.controller, LeaderController)
    assert not engine.controller.engaged and engine.arm.enabled and not engine.leader.enabled
    assert engine.arm.current["wrist_flex"] == 2047
    command(engine, "capture_home_return", path_clear=True)
    assert engine.phase == "holding"
    assert not isinstance(engine.controller, LeaderController)
    assert engine.arm.enabled and not engine.leader.enabled
    assert engine.draft["path"][0]["wrist_flex"] == 2047
    assert engine.draft["path"][-1]["wrist_flex"] == 2071
    for _ in range(3):
        command(engine, "test", hands_clear=True)
        command(engine, "pass")
    assert {**engine.key_statuses(), **engine.statuses(engine.controls)}[control]["status"] == "registered"
    command(engine, "next", supported=True)
    assert engine.arm.enabled
    assert isinstance(engine.controller, LeaderController) and not engine.controller.engaged


@pytest.mark.parametrize("control,direction", [("voicing.cw", 12), ("voicing.ccw", -12)])
def test_leader_dial_forward_loop_and_handover(engine, control, direction):
    prepare(engine, control)
    command(engine, "dial_capture_start", fixed_pad=True, reference="C major", expected_effect="Small voicing change")
    move(engine)
    command(engine, "dial_capture_contact")
    move(engine, motor="wrist_roll", ticks=direction)
    command(engine, "dial_capture_turn", direction_verified=True)
    move(engine, ticks=-12)
    command(engine, "dial_capture_lift", rim_clear=True)
    move(engine, motor="wrist_roll", ticks=-direction)
    command(engine, "dial_capture_return", hands_clear=True, rim_clear=True)
    command(engine, "capture_home_return", path_clear=True)
    assert engine.phase == "holding" and engine.arm.enabled
    for _ in range(3):
        command(engine, "test", hands_clear=True, reference_reset=True)
        command(engine, "pass", effect_verified=True)
    assert engine.statuses(engine.controls)[control]["status"] == "registered"


def test_connection_and_calibration_are_separate_and_persisted(engine):
    connect(engine, teaching_mode="leader")
    assert not engine.arm.enabled and not engine.leader.enabled
    calibrate(engine)
    follower = deepcopy(engine.calibration)
    assert not engine.leader_calibrated
    reject(engine, "control_start", supported=True)
    command(engine, "calibrate", target="leader", supported=True)
    command(engine, "calibration_center", supported=True)
    assert engine.snapshot()["leader"]["pose_reference_ready"]
    assert engine.calibrated and engine.calibration == follower
    command(engine, "simulate_sweep")
    command(engine, "calibration_reset", target="leader", supported=True)
    assert not engine.snapshot()["leader"]["pose_reference_ready"]
    assert engine.repo.get("calibration") == follower
    command(engine, "release", supported=True)
    assert engine.calibrated and not engine.leader_calibrated
    assert engine.leader.calibration is None


def test_engagement_aligns_absolute_pose_and_pause_keeps_the_follower_still(engine):
    prepare(engine)
    before = dict(engine.arm.current)
    engine.leader.current["shoulder_pan"] += 600
    engine.step()  # Paused input is free to move; no input-jump check.
    assert engine.arm.current == before
    command(engine, "leader_resume", hands_clear=True)
    assert engine.arm.current["shoulder_pan"] == before["shoulder_pan"] + 600
    engine.leader.current["shoulder_pan"] += 4
    engine.step()
    assert engine.arm.current["shoulder_pan"] == before["shoulder_pan"] + 604
    command(engine, "leader_pause")
    at_pause = dict(engine.arm.current)
    engine.leader.current["shoulder_pan"] -= 500
    engine.step()
    assert engine.arm.current == at_pause
    command(engine, "leader_resume", hands_clear=True)
    assert engine.arm.current["shoulder_pan"] == at_pause["shoulder_pan"] - 500


@pytest.mark.parametrize("pause", ["initial", "button", "visibility"])
def test_paused_hold_keeps_one_target_despite_small_follower_drift(engine, pause):
    prepare(engine)
    if pause != "initial":
        command(engine, "leader_resume", hands_clear=True)
        engine.leader.current["elbow_flex"] += 4
        engine.step()
        # Pause discards a small tracking lag once, then holds that measurement.
        engine.arm.current["elbow_flex"] -= 1
        held = dict(engine.arm.current)
        if pause == "button":
            command(engine, "leader_pause")
        else:
            engine.leader_visible_until = time.monotonic() - 1
            engine.step()
    else:
        held = dict(engine.arm.current)
    assert not engine.controller.engaged
    assert engine.controller.previous == held
    for _ in range(4):
        engine.arm.current["elbow_flex"] += 3
        engine.leader.current["elbow_flex"] -= 4
        engine.step()
        assert engine.phase == "note_hover", engine.error
        assert engine.controller.previous == held
        assert engine.arm.current == held
    # Repeated Pause requests cannot rebase an already-paused hold either.
    engine.arm.current["elbow_flex"] += 3
    command(engine, "leader_pause")
    assert engine.arm.current == held



def test_direct_leader_pose_and_paused_reposition_do_not_command_the_gripper(engine):
    prepare(engine, local=False)
    command(engine, "leader_resume", hands_clear=True)
    before = dict(engine.arm.current)
    engine.leader.current["gripper"] = 10
    engine.leader.current["wrist_flex"] += 100
    engine.step()
    assert engine.arm.current["wrist_flex"] == before["wrist_flex"] + 100
    assert engine.arm.current["gripper"] == before["gripper"]
    command(engine, "leader_pause")
    held = dict(engine.arm.current)
    assert engine.recording_error and engine.snapshot()["recording_error"]
    rejected = reject(engine, "capture_key_clearance", path_clear=True)
    assert "Recording cannot be used" in rejected["message"]
    assert engine.phase == "home_approach"
    engine.leader.current["wrist_flex"] += 300
    engine.step()
    command(engine, "leader_resume", hands_clear=True)
    engine.step()
    assert engine.arm.current["wrist_flex"] == held["wrist_flex"] + 300
    assert engine.arm.current["gripper"] == held["gripper"]
    command(engine, "release", supported=True)
    assert engine.recording_error is None


@pytest.mark.parametrize("reason", ["invalid_encoder", "leader_unplugged", "leader_torque", "lease", "stop"])
def test_following_faults_hold_without_torque_drop_or_motion_resume(engine, reason):
    prepare(engine)
    command(engine, "leader_resume", hands_clear=True)
    if reason == "invalid_encoder":
        engine.leader.current["wrist_flex"] = -1
    elif reason == "leader_unplugged":
        engine.leader.connected = False
    elif reason == "leader_torque":
        engine.leader.enabled = True
    elif reason == "lease":
        engine.lease_until = time.monotonic() - 1
    else:
        engine.stop_event.set()
    before = dict(engine.arm.current)
    engine.step()
    assert engine.phase == "fault"
    assert engine.arm.enabled and not engine.controller.engaged
    assert engine.arm.current == before
    assert engine.leader_feedback_at is None


def test_visibility_lease_pauses_and_requires_explicit_reengagement(engine):
    prepare(engine)
    command(engine, "leader_resume", hands_clear=True)
    engine.leader_visible_until = time.monotonic() - 1
    engine.leader.current["wrist_flex"] += 4
    before = dict(engine.arm.current)
    engine.step()
    assert not engine.controller.engaged and engine.arm.enabled
    assert engine.arm.current == before and engine.phase == "note_hover"
    engine.heartbeat("operator")
    engine.step()
    assert not engine.controller.engaged  # Returning to the tab does not re-engage.


def test_capture_requires_paused_powered_hold_and_tests_keep_leader_disengaged(engine):
    prepare(engine)
    reject(engine, "leader_resume")
    command(engine, "leader_resume", hands_clear=True)
    reject(engine, "capture_touch")
    assert engine.phase == "note_hover" and len(engine.capture["path"]) == 1
    reject(engine, "calibrate", supported=True, target="leader")
    reject(engine, "leader_hold", supported=True)


def test_recorded_path_uses_measured_positions_not_sent_targets(engine):
    prepare(engine)
    command(engine, "leader_resume", hands_clear=True)
    engine.arm.jammed = True
    for _ in range(6):
        engine.leader.current["wrist_flex"] += 4
        engine.step()
    assert engine.phase == "note_hover"
    assert engine.arm.current["wrist_flex"] == 2047
    assert all(p["wrist_flex"] == 2047 for p in engine.capture["path"])


def test_unsuitable_recording_does_not_interrupt_native_following_or_become_playback(engine):
    prepare(engine)
    move(engine, ticks=12)
    command(engine, "capture_touch")
    command(engine, "leader_resume", hands_clear=True)
    for _ in range(40):
        engine.leader.current["wrist_flex"] += 4
        engine.step()
    assert engine.phase == "note_touch" and engine.controller.engaged
    assert "Contact stroke" in engine.recording_error
    assert engine.arm.current["wrist_flex"] == 2059 + 160
    command(engine, "leader_pause")
    reject(engine, "capture_pressed")
    assert engine.phase == "note_touch" and not engine.controller.engaged
    assert engine.draft is None
    command(engine, "leader_resume", hands_clear=True)
    engine.leader.current["wrist_flex"] -= 160
    engine.step()
    assert engine.arm.current["wrist_flex"] == 2059
    assert engine.recording_error  # Returning does not silently repair a rejected recording.


def test_reconnect_restores_both_references_without_powering_or_resuming(engine):
    prepare(engine)
    saved = deepcopy(engine.leader_calibration)
    command(engine, "disconnect", supported=True)
    connect(engine, teaching_mode="leader", fixture_unchanged=True)
    assert engine.leader_calibrated and engine.calibrated
    assert engine.repo.get("leader_calibration") == engine.leader.calibration == saved
    assert engine.controller is None and not engine.leader.enabled and not engine.arm.enabled
    command(engine, "disconnect", supported=True)
    connect(engine, teaching_mode="manual", fixture_unchanged=True)
    assert engine.leader is None and engine.calibration_target == "follower"


def test_leader_reload_retains_follower_calibration_and_note_identity(engine):
    prepare(engine)
    command(engine, "release", supported=True)
    follower = deepcopy(engine.calibration)
    saved = deepcopy(engine.leader_calibration)
    command(engine, "calibrate", target="leader", supported=True)
    command(engine, "calibration_center", supported=True)
    command(engine, "calibration_reload", target="leader", supported=True, calibration_unchanged=True)
    assert engine.leader_calibration == engine.leader.calibration == saved
    assert engine.calibration == engine.arm.calibration == follower
    assert engine.calibrated and engine.leader_calibrated


def test_native_read_completion_does_not_use_a_custom_quarter_second_deadline(engine):
    prepare(engine)
    command(engine, "leader_resume", hands_clear=True)
    read = engine.leader.read_raw
    def slow():
        engine.clock.sleep(0.3)
        return read()
    engine.leader.read_raw = slow
    before = dict(engine.arm.current)
    engine.step()
    assert engine.phase == "note_hover" and engine.arm.current == before and engine.arm.enabled


def test_partial_pair_connection_closes_both_without_torque_commands(tmp_path):
    opened = []
    def follower(*_):
        arm = SimulatedArm()
        arm.release = lambda: pytest.fail("Connection released torque")
        opened.append(arm)
        return arm
    def leader(*_):
        arm = follower()
        def fail():
            arm.connected = True
            arm.connection_readings = {"stage": "saved_calibration_readback",
                                       "voltage_raw": dict.fromkeys(m.MOTORS, 52)}
            raise RuntimeError("Failed to read 'Min_Position_Limit' on id_=4. [RxPacketError] Input voltage error!")
        arm.open = fail
        return arm
    arms = [{"port": "/dev/test-follower", "role": "follower", "voltage": 12, "motor_ids": list(range(1,7))},
            {"port": "/dev/test-leader", "role": "leader", "voltage": 5.2, "motor_ids": list(range(1,7))}]
    e = Engine(tmp_path, "hardware", hardware_factory=follower, leader_factory=leader,
               port_scanner=lambda **_: {"arms": arms, "warnings": []})
    try:
        command(e, "refresh_ports")
        reject(e, "connect", prepared=True, fixture="test", teaching_mode="leader", port="/dev/test-follower", leader_port="/dev/test-follower")
        assert not opened
        result = reject(e, "connect", prepared=True, fixture="test", teaching_mode="leader", port="/dev/test-follower", leader_port="/dev/test-leader")
        assert "Leader connection failed (/dev/test-leader)" in result["message"]
        assert "Input voltage error" in result["message"]
        import json
        error = next(json.loads(line) for line in e.event_log.path.read_text().splitlines()
                     if json.loads(line)["kind"] == "connection_failed")
        assert error["failed_role"] == "leader" and error["failed_port"] == "/dev/test-leader"
        assert error["connection_readings"]["voltage_raw"]["wrist_flex"] == 52
        assert len(opened) == 2 and all(not arm.connected and not arm.enabled for arm in opened)
        assert e.phase == "disconnected" and e.arm is None and e.leader is None
    finally:
        e.close()


def test_add_leader_after_follower_calibration_keeps_saved_reference_and_fixture(engine):
    connect(engine)
    calibrate(engine)
    follower, fixture = deepcopy(engine.calibration), deepcopy(engine.fixture)
    arm = engine.arm
    release = arm.release
    arm.release = lambda: pytest.fail("Adding a leader released the follower")
    arm.send = arm.arm_at_current = lambda *_: pytest.fail("Adding a leader moved the follower")
    command(engine, "refresh_leader_ports")
    command(engine, "connect_leader", prepared=True, leader_port="simulator-leader")
    assert engine.arm is arm and arm.connected and not arm.enabled
    assert engine.calibrated and not engine.leader_calibrated
    assert engine.calibration == engine.repo.get("calibration") == follower
    assert engine.fixture == fixture and engine.teaching_mode == "leader"
    reject(engine, "control_start", supported=True)
    arm.release = release
    command(engine, "calibrate", target="leader", supported=True)
    command(engine, "calibration_center", supported=True)
    for _ in range(5):
        command(engine, "simulate_sweep")
        command(engine, "calibration_next", range_complete=True)
    command(engine, "calibration_save", range_complete=True)
    assert engine.calibrated and engine.leader_calibrated
    assert engine.calibration == follower and engine.arm.calibration == follower
    assert engine.fixture == fixture and not engine.arm.enabled and not engine.leader.enabled


def test_leader_connection_requires_idle_off_follower_and_detected_separate_port(engine):
    connect(engine)
    calibrate(engine)
    reject(engine, "connect_leader", leader_port="simulator-leader")
    reject(engine, "connect_leader", prepared=True, leader_port="simulator")
    reject(engine, "connect_leader", prepared=True, leader_port="unknown")
    engine.phase = "connected"  # Connection may observe pre-existing torque; attachment must refuse it.
    engine.arm.enabled = True
    reject(engine, "connect_leader", prepared=True, leader_port="simulator-leader")
    assert engine.leader is None
    engine.arm.enabled = False
    command(engine, "calibrate", supported=True)
    reject(engine, "connect_leader", prepared=True, leader_port="simulator-leader")
    reject(engine, "refresh_leader_ports")
    command(engine, "release", supported=True)
    command(engine, "control_start", supported=True)
    reject(engine, "connect_leader", prepared=True, leader_port="simulator-leader")


def test_leader_scan_excludes_open_follower_and_failed_attach_preserves_it(tmp_path):
    seen = []
    ports = [{"port": "/dev/test-follower", "role": "follower", "voltage": 12, "motor_ids": list(range(1,7))},
             {"port": "/dev/test-leader", "role": "leader", "voltage": 5.2, "motor_ids": list(range(1,7))}]
    def scan(**kwargs):
        seen.append(kwargs.get("exclude_ports", ()))
        return {"arms": [p for p in ports if p["port"] not in seen[-1]], "warnings": []}
    failed = SimulatedArm()
    def fail():
        failed.connected = True
        raise m.SafetyError("Unplugged leader")
    failed.open = fail
    e = Engine(tmp_path, "hardware", hardware_factory=lambda _, cal: SimulatedArm(cal),
               leader_factory=lambda *_: failed, port_scanner=scan)
    try:
        command(e, "refresh_ports")
        connect(e, port="/dev/test-follower")
        command(e, "calibrate", supported=True)
        command(e, "calibration_center", supported=True)
        for motor in (name for name in m.MOTORS if name != "wrist_roll"):
            for position in (1000, 3000):
                e.arm.current[motor] = position
                e.sample()
            command(e, "calibration_next", range_complete=True)
        command(e, "calibration_save", range_complete=True)
        saved = deepcopy(e.calibration)
        follower = e.arm
        follower.close = follower.release = lambda: pytest.fail("Follower was closed or released")
        command(e, "refresh_leader_ports")
        assert seen[-1] == ("/dev/test-follower",)
        reject(e, "connect_leader", prepared=True, leader_port="/dev/test-leader")
        assert e.arm is follower and follower.connected and e.calibrated
        assert e.calibration == saved and e.leader is None and not failed.connected
        assert "Unplugged leader" in e.error
    finally:
        if e.arm:
            e.arm.close = lambda: None
        e.close()
