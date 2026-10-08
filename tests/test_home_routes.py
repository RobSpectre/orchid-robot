"""Home teaching, endpoint departure and full routes; no hardware access."""
from copy import deepcopy
import io
import math

import pytest

from orchid_demo import home, motion as m
from orchid_demo.engine import Engine
from orchid_demo.leader import LeaderController
from test_operator_engine import Clock, command, reject
from test_leader_teaching import prepare, move


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()


def complete_route(e):
    prepare(e, local=False, resting=True)
    for _ in range(3):
        move(e, "elbow_flex", -72)
    assert e.home["pose"]["elbow_flex"] - e.arm.current["elbow_flex"] == 216
    command(e, "capture_key_clearance", path_clear=True)
    move(e, ticks=12)
    command(e, "capture_touch")
    move(e, ticks=12)
    command(e, "capture_pressed")
    command(e, "retreat_from_press", hands_clear=True)
    assert e.phase == "home_return"
    for _ in range(3):
        move(e, "elbow_flex", 72)
    command(e, "capture_home_return", path_clear=True)


def test_resting_endpoints_support_full_home_key_home_trials_and_next_key(engine):
    complete_route(engine)
    reference = deepcopy(engine.home)
    draft = deepcopy(engine.draft)
    assert len(draft["home_motion"]["approach"]) > 2
    assert len(draft["home_motion"]["return"]) > 2
    assert draft["home_motion"]["approach"][0] == reference["pose"]
    assert draft["home_motion"]["return"][-1] == reference["pose"]
    for _ in range(3):
        command(engine, "test", hands_clear=True)
        assert engine.arm.current == reference["pose"]
        command(engine, "pass")
    assert engine.key_statuses()["C"]["status"] == "registered"
    command(engine, "next", supported=True)
    assert engine.phase == "home_approach" and engine.arm.enabled
    assert engine.arm.current == reference["pose"]
    assert engine.selected == "C#"
    assert not engine.controller.engaged
    assert engine.arm.current == reference["pose"]


def test_teach_from_saved_home_establishes_hold_without_a_torque_off_gap(engine, monkeypatch):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    home_pose = dict(engine.arm.current)
    def dropping_release():
        # Reproduce the physical consequence absent from the ordinary simulator.
        engine.arm.enabled = False
        engine.arm.current["shoulder_lift"] += 100
        pytest.fail("Teach released the follower, allowing it to fall")
    monkeypatch.setattr(engine.arm, "release", dropping_release)
    command(engine, "control_start", control="C", supported=True)
    assert engine.arm.enabled and engine.arm.current == home_pose
    assert isinstance(engine.controller, LeaderController) and not engine.controller.engaged
    assert engine.snapshot()["leader_teaching"]
    command(engine, "leader_resume", hands_clear=True)
    engine.leader.current["wrist_flex"] += 24
    engine.step()
    assert engine.arm.current["wrist_flex"] == home_pose["wrist_flex"] + 24


@pytest.mark.parametrize("action", ["control_start", "next"])
def test_next_leader_control_preserves_existing_power_and_goal(engine, monkeypatch, action):
    complete_route(engine)
    for _ in range(3):
        command(engine, "test", hands_clear=True)
        command(engine, "pass")
    previous = engine.controller
    target = dict(previous.previous)
    def forbidden(*args, **kwargs):
        pytest.fail("Selecting the next control changed motor torque or configuration")
    monkeypatch.setattr(engine.arm, "release", forbidden)
    monkeypatch.setattr(engine.arm, "arm_at_calibrated", forbidden)
    monkeypatch.setattr(engine.arm, "prepare_motion", forbidden, raising=False)
    command(engine, action, control="D", supported=True)
    assert engine.arm.enabled and not previous.enabled
    assert isinstance(engine.controller, LeaderController) and not engine.controller.engaged
    assert engine.controller.previous == target and engine.arm.current == target


def test_teach_never_releases_a_preexisting_unowned_powered_arm(engine, monkeypatch):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    engine.arm.enabled = True  # A connection may observe another controller's hold.
    def forbidden(*args, **kwargs):
        pytest.fail("Teach modified an unowned powered arm")
    monkeypatch.setattr(engine.arm, "release", forbidden)
    monkeypatch.setattr(engine.arm, "arm_at_calibrated", forbidden)
    monkeypatch.setattr(engine.arm, "teleop_send", forbidden)
    reject(engine, "control_start", control="C", supported=True)
    assert engine.arm.enabled and engine.controller is None


def test_home_capture_is_read_only_and_new_home_invalidates_routes(engine):
    complete_route(engine)
    command(engine, "test", hands_clear=True)
    command(engine, "pass")
    old_home = deepcopy(engine.home)
    saved = deepcopy(engine.repo.notes()["C"])
    command(engine, "release", supported=True)
    def forbidden(*_):
        pytest.fail("Capturing home sent a motor command")
    engine.arm.send = engine.arm.send_calibrated = forbidden
    engine.arm.arm_at_current = engine.arm.arm_at_calibrated = forbidden
    command(engine, "capture_home", supported=True)
    assert engine.home["id"] != old_home["id"]
    assert engine.repo.notes()["C"] == saved
    assert engine.key_statuses()["C"]["status"] == "needs_reteach"
    assert not engine.arm.enabled and not engine.leader.enabled


def test_home_requires_supported_capture_and_teach_holds_away_from_home(engine):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    saved = deepcopy(engine.home)
    reject(engine, "capture_home")
    engine.arm.current["elbow_flex"] = 3101
    reject(engine, "capture_home", supported=True)
    assert engine.home == saved and not engine.arm.enabled
    engine.arm.current = {**saved["pose"], "elbow_flex": saved["pose"]["elbow_flex"] + 7}
    start = dict(engine.arm.current)
    command(engine, "control_start", control="D", supported=True)
    assert engine.phase == "home_prepare" and engine.arm.enabled
    assert engine.arm.current == start and not engine.controller.engaged


def test_endpoint_departure_blocks_outward_input_and_reentry_without_faulting(engine):
    prepare(engine, local=False, resting=True)
    initial = dict(engine.arm.current)
    move(engine, "elbow_flex", 96)
    assert engine.arm.current == initial
    move(engine, "elbow_flex", -24)
    assert engine.arm.current["elbow_flex"] == 3076
    move(engine, "elbow_flex", 96)
    assert engine.arm.current["elbow_flex"] == 3100  # Full recorded physical endpoint, with no invented inset.
    assert engine.phase == "home_approach"
    assert engine.arm.current["gripper"] == 1000  # Fixed jaw may stay at its taught resting opening.


def test_near_endpoint_home_is_not_accepted_as_key_clearance(engine):
    prepare(engine, local=False, resting=True)
    rejected = reject(engine, "capture_key_clearance", path_clear=True)
    assert "working margin" in rejected["message"]
    assert engine.phase == "home_approach" and engine.capture is None
    assert engine.arm.enabled


def test_changed_fixture_or_follower_reference_requires_a_new_home(engine):
    prepare(engine)
    command(engine, "release", supported=True)
    assert engine.home_ready
    engine.fixture["id"] = "changed-fixture"
    assert not engine.home_ready
    reject(engine, "control_start", supported=True)
    engine.fixture["id"] = engine.home["fixture_id"]
    engine.calibration["elbow_flex"]["homing_offset"] += 1
    assert not engine.home_ready
    reject(engine, "control_start", supported=True)


def test_home_and_routes_persist_without_automatic_motion_on_reconnect(engine):
    complete_route(engine)
    reference = deepcopy(engine.home)
    command(engine, "disconnect", supported=True)
    command(engine, "connect", prepared=True, fixture="Test fixture", fixture_unchanged=True, teaching_mode="leader")
    assert engine.home_ready and engine.home == reference
    assert engine.export_snapshot()["home"] == reference
    assert engine.repo.export()["home"] == reference
    assert not engine.arm.enabled and not engine.leader.enabled


@pytest.mark.parametrize("corruption", ["wrong_home", "sample_gap", "outside_range", "wrong_join", "wrong_return"])
def test_replay_rejects_invalid_routes_before_motion(engine, corruption):
    complete_route(engine)
    draft = deepcopy(engine.draft)
    if corruption == "wrong_home":
        draft["home_motion"]["home_id"] = "different"
    elif corruption == "sample_gap":
        draft["home_motion"]["approach"] = [draft["home_motion"]["approach"][0], draft["home_motion"]["approach"][-1]]
    elif corruption == "outside_range":
        draft["home_motion"]["approach"][0]["elbow_flex"] = 4095
    elif corruption == "wrong_join":
        draft["home_motion"]["approach"] = [draft["home_motion"]["approach"][0]]
    else:
        draft["home_motion"]["return"] = draft["home_motion"]["return"][:-2]
    with pytest.raises(m.SafetyError):
        home.validate(draft, engine.home, engine.controller.arm)


def test_full_route_has_bounded_budget_and_stops_on_feedback_failure(engine):
    complete_route(engine)
    assert m.MAX_RUN_TIME < home.budget(engine.draft) < home.MAX_ROUTE_TIME
    engine.arm.jammed = True
    engine.controller.log = io.StringIO()
    reject(engine, "test", hands_clear=True)
    assert engine.phase == "fault" and engine.arm.enabled
    assert "Next target is too far" in engine.error
    assert engine.arm.current == engine.home["pose"]


def test_leader_can_position_first_home_and_continue_without_torque_release(engine):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    engine.home = None
    engine.arm.current["elbow_flex"] = 3100
    initial = dict(engine.arm.current)
    reject(engine, "home_start")
    assert not engine.arm.enabled
    command(engine, "home_start", supported=True, control="D")
    assert engine.phase == "home_positioning"
    assert engine.arm.enabled and not engine.leader.enabled
    assert not engine.controller.engaged and engine.arm.current == initial
    # Travel from a parked endpoint is not restricted to the local key box.
    move(engine, "elbow_flex", 96)
    assert engine.arm.current == initial
    for _ in range(6):
        move(engine, "elbow_flex", -24)
    assert engine.arm.current["elbow_flex"] == initial["elbow_flex"] - 144
    reject(engine, "capture_leader_home")
    assert engine.home is None
    command(engine, "leader_resume", hands_clear=True)
    reject(engine, "capture_leader_home", path_clear=True)
    assert engine.home is None
    command(engine, "leader_pause")
    posed = dict(engine.arm.current)
    def forbidden(*_):
        pytest.fail("Saving a leader-positioned home changed torque")
    engine.arm.release = engine.arm.arm_at_calibrated = forbidden
    command(engine, "capture_leader_home", path_clear=True)
    assert engine.home["pose"] == posed
    assert engine.home["hold_target"] == engine.controller.previous
    assert engine.phase == "home_approach" and engine.selected == "D"
    assert engine.arm.enabled and not engine.controller.engaged
    assert engine.route_approach == [posed]
    move(engine)
    command(engine, "capture_key_clearance", path_clear=True)
    assert engine.phase == "note_hover"


def test_replacing_home_starts_at_current_pose_and_cancel_keeps_old_reference(engine):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    old_home = deepcopy(engine.home)
    engine.arm.current["wrist_flex"] += 100
    initial = dict(engine.arm.current)
    command(engine, "home_start", supported=True)
    assert engine.arm.current == initial and engine.home == old_home
    move(engine)
    command(engine, "release", supported=True)
    assert not engine.arm.enabled and engine.home == old_home
    command(engine, "home_start", supported=True)
    command(engine, "capture_leader_home", path_clear=True)
    assert engine.home["id"] != old_home["id"]
    assert engine.home["pose"] == engine.arm.current


def test_home_gripper_follows_both_directions_then_locks_at_saved_opening(engine):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    initial = dict(engine.arm.current)
    command(engine, "home_start", supported=True)
    assert engine.snapshot()["leader_gripper_enabled"]
    command(engine, "leader_resume", hands_clear=True)
    assert engine.arm.current == initial  # The two initial calibrated poses match.
    engine.leader.current["gripper"] += 200
    engine.step()
    assert engine.arm.current["gripper"] == initial["gripper"] + 200
    engine.leader.current["gripper"] -= 100
    engine.step()
    assert engine.arm.current["gripper"] == initial["gripper"] + 100
    command(engine, "leader_pause")
    held = dict(engine.arm.current)
    engine.leader.current["gripper"] -= 300
    engine.step()
    command(engine, "leader_resume", hands_clear=True)
    engine.step()
    assert engine.arm.current["gripper"] == held["gripper"] - 300
    held = dict(engine.arm.current)
    command(engine, "leader_pause")
    command(engine, "capture_leader_home", path_clear=True)
    assert engine.home["pose"]["gripper"] == held["gripper"]
    assert not engine.snapshot()["leader_gripper_enabled"]
    command(engine, "leader_resume", hands_clear=True)
    engine.leader.current["gripper"] += 400
    engine.step()
    assert engine.arm.current == held
    assert engine.controller.gripper == held["gripper"]
    command(engine, "leader_pause")
    command(engine, "capture_key_clearance", path_clear=True)
    command(engine, "leader_resume", hands_clear=True)
    engine.leader.current["gripper"] -= 400
    engine.step()
    assert engine.arm.current["gripper"] == held["gripper"]


@pytest.mark.parametrize("problem", ["calibration", "range", "powered"])
def test_home_positioning_rejects_invalid_start_before_commands(engine, problem):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    old_home = deepcopy(engine.home)
    if problem == "calibration":
        engine.leader_calibrated = False
    elif problem == "range":
        engine.arm.current["elbow_flex"] = 3101
    else:
        engine.arm.enabled = True
    def forbidden(*_):
        pytest.fail("Invalid home start sent a motor command")
    engine.arm.send_calibrated = engine.arm.arm_at_calibrated = forbidden
    reject(engine, "home_start", supported=True)
    assert engine.home == old_home and engine.controller is None


def prepare_away_from_home(e):
    prepare(e, local=False)
    command(e, "release", supported=True)
    e.arm.current["elbow_flex"] += 200
    e.arm.current["gripper"] += 100
    command(e, "control_start", control="D", supported=True)
    assert e.phase == "home_prepare" and e.arm.enabled
    return dict(e.arm.current)


def test_move_home_requires_clearance_and_holds_until_measured_arrival(engine, monkeypatch):
    start = prepare_away_from_home(engine)
    def forbidden(*args, **kwargs):
        pytest.fail("Homing changed torque or reconfigured a motor")
    monkeypatch.setattr(engine.arm, "release", forbidden)
    monkeypatch.setattr(engine.arm, "arm_at_calibrated", forbidden)
    monkeypatch.setattr(engine.arm, "prepare_motion", forbidden, raising=False)
    reject(engine, "move_home")
    reject(engine, "move_home", hands_clear=True)
    reject(engine, "move_home", path_clear=True)
    reject(engine, "leader_resume", hands_clear=True)
    assert engine.arm.current == start
    sent = []
    send = engine.arm.teleop_send
    def record(action, gripper):
        sent.append(engine.arm.joint_target(action))
        return send(action, gripper)
    monkeypatch.setattr(engine.arm, "teleop_send", record)
    command(engine, "move_home", hands_clear=True, path_clear=True, duration=1)
    assert len(sent) >= 20  # The move is interpolated, not a jump to the endpoint.
    assert engine.phase == "home_arrival"
    assert engine.arm.current == engine.home["pose"]
    assert engine.route_approach is None
    assert all(engine.home["pose"]["elbow_flex"] <= p["elbow_flex"] <= start["elbow_flex"] for p in sent)
    engine.step()
    assert engine.phase == "home_arrival"
    engine.step()
    assert engine.phase == "home_approach"
    assert engine.route_approach == [engine.home["pose"]]
    assert not engine.controller.engaged and not engine.controller.follow_gripper
    assert engine.arm.enabled and not engine.leader.enabled
    # Match the leader at home, then recording starts there.
    move(engine)
    command(engine, "capture_key_clearance", path_clear=True)
    assert engine.phase == "note_hover"


def test_home_target_send_is_not_mistaken_for_measured_arrival(engine):
    start = prepare_away_from_home(engine)
    engine.arm.jammed = True
    command(engine, "move_home", hands_clear=True, path_clear=True, duration=1)
    for _ in range(10):
        engine.step()
    assert engine.phase == "home_arrival"
    assert engine.arm.current == start
    assert engine.controller.previous == engine.home["pose"]
    assert engine.route_approach is None and not engine.controller.engaged
    # Arrival is a recording condition, never a lock on native positioning.
    command(engine, "leader_resume", hands_clear=True)
    assert engine.controller.engaged
    assert engine.route_approach is None
    engine.arm.jammed = False
    engine.leader.current.update(start)
    engine.leader.current["elbow_flex"] -= 80
    engine.step()
    assert engine.arm.current["elbow_flex"] == start["elbow_flex"] - 80
    assert engine.phase == "home_arrival" and engine.error is None
    command(engine, "leader_pause")
    assert engine.arm.enabled and not engine.controller.engaged


def test_direct_home_is_default_and_has_no_timed_ramp(engine, monkeypatch):
    prepare_away_from_home(engine)
    sent = []
    original = engine.controller.tick
    def tick(*args, **kwargs):
        if kwargs.get("stage") == "home_move":
            sent.append(kwargs["home_target"])
        return original(*args, **kwargs)
    monkeypatch.setattr(engine.controller, "tick", tick)
    command(engine, "move_home", hands_clear=True, path_clear=True)
    assert sent == [engine.home["pose"]]
    assert engine.controller.previous == engine.home["pose"]
    assert not engine.controller.follow_gripper


def test_reported_28_tick_home_offset_can_be_explicitly_accepted(engine, monkeypatch):
    prepare_away_from_home(engine)
    old_home = deepcopy(engine.home)
    original = engine.arm.teleop_send
    def biased_feedback(action, gripper):
        result = original(action, gripper)
        engine.arm.current["elbow_flex"] += 28
        return result
    monkeypatch.setattr(engine.arm, "teleop_send", biased_feedback)
    command(engine, "move_home", hands_clear=True, path_clear=True)
    for _ in range(10):
        engine.step()
    assert engine.current["elbow_flex"] == old_home["pose"]["elbow_flex"] + 28
    assert engine.phase == "home_arrival" and engine.home == old_home
    assert not engine.snapshot()["at_home"]
    reject(engine, "capture_leader_home")
    command(engine, "capture_leader_home", path_clear=True)
    assert engine.phase == "home_approach"
    assert engine.home["id"] != old_home["id"]
    assert engine.home["pose"]["elbow_flex"] == old_home["pose"]["elbow_flex"] + 28
    assert engine.home["hold_target"] == old_home["pose"]
    assert engine.route_approach == [engine.home["pose"]]
    assert engine.arm.enabled and not engine.controller.engaged
    command(engine, "capture_key_clearance", path_clear=True)
    assert engine.phase == "note_hover" and engine.error is None


def test_reaching_home_during_live_takeover_does_not_change_routes_or_fault(engine):
    prepare_away_from_home(engine)
    command(engine, "move_home", hands_clear=True, path_clear=True)
    engine.leader.current.update(engine.home["pose"])
    command(engine, "leader_resume", hands_clear=True)
    for _ in range(5):
        engine.step()
    assert engine.phase == "home_arrival" and engine.controller.engaged
    assert engine.route_approach is None and engine.error is None
    command(engine, "leader_pause")
    for _ in range(3):
        engine.step()
    assert engine.phase == "home_approach" and engine.route_approach == [engine.home["pose"]]


@pytest.mark.parametrize("interruption", ["stop", "lease", "read_failure"])
def test_home_move_interruption_requests_hold_without_release(engine, monkeypatch, interruption):
    prepare_away_from_home(engine)
    original = engine.controller.tick
    ticks = 0
    def tick(*args, **kwargs):
        nonlocal ticks
        if kwargs.get("stage") == "home_move":
            ticks += 1
            if ticks == 5:
                if interruption == "stop":
                    engine.stop_event.set()
                elif interruption == "lease":
                    engine.lease_until = 0
                else:
                    engine.leader.connected = False
        return original(*args, **kwargs)
    monkeypatch.setattr(engine.controller, "tick", tick)
    reject(engine, "move_home", hands_clear=True, path_clear=True, duration=1)
    assert ticks == 5 and engine.phase == "fault"
    assert engine.arm.enabled and not engine.controller.engaged
    assert engine.arm.current != engine.home["pose"]


@pytest.mark.parametrize("duration", [-1, 0.5, 61, True, "8", float("nan"), float("inf")])
def test_home_move_rejects_invalid_duration_without_leaving_hold(engine, duration):
    start = prepare_away_from_home(engine)
    if type(duration) is float and not math.isfinite(duration):
        with pytest.raises(m.SafetyError, match="move time"):
            engine.dispatch("move_home", {"hands_clear": True, "path_clear": True, "duration": duration})
    else:
        reject(engine, "move_home", hands_clear=True, path_clear=True, duration=duration)
    assert engine.phase == "home_prepare" and engine.arm.current == start
