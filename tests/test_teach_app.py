"""Web console leader teaching by points -> replay (same loop as teach_key.py), in simulation."""
from copy import deepcopy
import time

import pytest

from orchid_demo import teach
from test_operator_engine import command, connect, calibrate, reject


def ready(e):
    connect(e, teaching_mode="leader")
    calibrate(e)
    command(e, "calibrate", target="leader", supported=True)
    command(e, "calibration_center", supported=True)
    for _ in range(5):
        command(e, "simulate_sweep")
        command(e, "calibration_next", range_complete=True)
    command(e, "calibration_save", range_complete=True)
    assert e.phase == "ready" and e.home is None


def run(e, seconds, until=None):
    """Advance the worker loop at the teach rate on the fake clock."""
    for _ in range(int(seconds / teach.PERIOD)):
        e.heartbeat("operator")
        e.clock.sleep(teach.PERIOD)
        e.step()
        assert e.phase != "fault", e.error
        if until and until():
            return


def following(e, control="C"):
    ready(e)
    command(e, "teach_begin", control=control)
    assert e.phase == "teach_hold" and e.arm.enabled and not e.leader.enabled
    command(e, "teach_follow")
    run(e, 3, until=lambda: e.teach.mode == "following")
    assert e.phase == "teach_follow" and e.teach.mode == "following"


def test_following_ramps_to_leader_instead_of_jumping(engine):
    ready(engine)
    command(engine, "teach_begin", control="C")
    engine.leader.current["elbow_flex"] += 400  # leader ~35 deg away from the follower
    start = engine.arm.current["elbow_flex"]
    command(engine, "teach_follow")
    positions = [engine.arm.current["elbow_flex"]]
    for _ in range(100):
        run(engine, teach.PERIOD)
        positions.append(engine.arm.current["elbow_flex"])
    step_limit = teach.RAMP_SPEED * teach.PERIOD * 4095 / 360 + 1  # ticks per tick
    assert all(abs(b - a) <= step_limit for a, b in zip(positions, positions[1:]))
    assert positions[-1] == start + 400  # arrived after ~1.2 s, then follows 1:1
    assert engine.phase == "teach_follow" and engine.teach.mode == "following"


def test_teach_adopts_a_follower_left_holding(engine):
    ready(engine)
    engine.arm.enabled = True  # another session left it powered (tonight's dead end)
    result = reject(engine, "teach_begin", control="C")
    assert "Support it" in result["message"]
    command(engine, "teach_begin", control="C", supported=True)
    assert engine.phase == "teach_hold" and engine.arm.enabled


def test_stop_motion_during_playback_holds_without_fault(engine):
    following(engine)
    taught_key(engine)
    command(engine, "teach_play", control="C", speed=0.5)
    run(engine, teach.SETTLE_S + 0.5)
    engine.submit("operator", "stop-1", "stop", engine.revision, {})
    engine.step()
    assert engine.phase == "teach_hold" and engine.arm.enabled
    assert engine.teach.mode == "holding"


def test_blocked_start_pose_asks_then_plays_anyway(engine):
    following(engine)
    taught_key(engine)
    engine.arm.current["shoulder_pan"] += 200  # nudged ~18 deg away while holding
    engine.arm.jammed = True
    command(engine, "teach_play", control="C")
    run(engine, 5, until=lambda: engine.phase == "teach_hold")
    assert engine.public["teach"]["warning"] and "shoulder_pan" in engine.public["teach"]["warning"]
    engine.arm.jammed = False
    command(engine, "teach_play", control="C", force=True)
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert engine.teach_played == "C"


def test_calibration_change_requires_rerecording(engine):
    following(engine)
    taught_key(engine)
    engine.calibration = {**engine.calibration, "gripper": {**engine.calibration["gripper"], "range_max": 3000}}
    engine.publish()
    assert engine.public["keys"]["C"]["status"] == "needs_reteach"
    assert "different follower calibration" in reject(engine, "teach_play", control="C")["message"]


def test_lost_browser_during_playback_faults_with_a_hold(engine):
    following(engine)
    taught_key(engine)
    command(engine, "teach_play", control="C")
    engine.lease_until = time.monotonic() - 1
    engine.clock.sleep(teach.PERIOD)
    engine.step()
    assert engine.phase == "fault" and engine.teach is None
    assert engine.arm.enabled  # held, never dropped


def test_release_ends_the_session_and_turns_torque_off(engine):
    following(engine)
    command(engine, "teach_hold")
    command(engine, "release", supported=True)
    assert engine.teach is None and not engine.arm.enabled and engine.phase == "connected"


# --- waypoints: home -> hover -> touch -> press, then the same points in reverse ---------------------


def move_leader(e, motor, ticks):
    command(e, "simulate_leader", motor=motor, delta=ticks)
    run(e, 3, until=lambda: e.simulated_leader_input is None)
    run(e, 0.2)


def capture(e, point, control="C"):
    command(e, "teach_capture", point=point, control=control)


def teach_points(e, control="C"):
    """Home where the arm is, hover above, first contact, then a press."""
    capture(e, "home", control)
    move_leader(e, "shoulder_lift", 60)
    capture(e, "hover", control)
    move_leader(e, "wrist_flex", 36)
    capture(e, "touch", control)
    move_leader(e, "wrist_flex", 12)
    capture(e, "press", control)


def taught_key(e, control="C"):
    """Teach a key by its points and wait for the return to home: the follower holds there."""
    teach_points(e, control)
    run(e, 10, until=lambda: e.phase == "teach_hold")
    assert e.phase == "teach_hold" and e.public["keys"][control]["status"] == "registered"


def spy(e):
    sent = []
    real = e.teach.send_follower
    e.teach.send_follower = lambda goal: (sent.append(dict(goal)), real(goal))[1]
    return sent


def test_capturing_the_press_returns_through_touch_and_hover_to_home(engine):
    following(engine)
    home_raw = dict(engine.arm.current)
    teach_points(engine)
    assert engine.phase == "teach_play" and engine.teach_returning
    sent = spy(engine)
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert engine.phase == "teach_hold" and "taught" in engine.message
    assert all(abs(engine.arm.current[n] - home_raw[n]) <= 1 for n in home_raw)  # back at home (±1 tick rounding)
    points = engine.notes["C"]["points"]
    wrist = [g["wrist_flex"] for g in sent]
    assert wrist[0] == pytest.approx(points["press"]["goal"]["wrist_flex"])
    assert all(b <= a + 1e-9 for a, b in zip(wrist, wrist[1:]))  # only ever lifts on the way back
    assert engine.notes["C"]["format"] == "leader_waypoints_v1"
    assert engine.public["keys"]["C"]["status"] == "registered"  # taught = done; no extra confirmation step


def test_playback_goes_home_hover_touch_press_and_back(engine):
    following(engine)
    teach_points(engine)
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    command(engine, "teach_follow")
    move_leader(engine, "shoulder_pan", 80)  # wander off somewhere else first
    command(engine, "teach_play", control="C")
    sent = spy(engine)
    run(engine, 30, until=lambda: engine.phase == "teach_hold")
    assert engine.teach_played == "C" and "plays it again" in engine.message
    points = engine.notes["C"]["points"]
    press = points["press"]["goal"]["wrist_flex"]
    assert max(g["wrist_flex"] for g in sent) == pytest.approx(press)
    pressed_at = next(i for i, g in enumerate(sent) if g["wrist_flex"] == pytest.approx(press))
    assert sent[pressed_at - 1]["shoulder_lift"] == pytest.approx(points["hover"]["goal"]["shoulder_lift"])
    assert sent[-1] == pytest.approx(points["home"]["goal"])  # ends where it started


def test_points_are_captured_in_order(engine):
    following(engine)  # Teach saved home where the arm was
    assert "hover next" in reject(engine, "teach_capture", point="press", control="C")["message"]
    capture(engine, "hover")
    assert "touch next" in reject(engine, "teach_capture", point="press", control="C")["message"]


def test_re_teaching_a_key_goes_through_every_step_again_and_keeps_the_old_motion_until_press(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    before = deepcopy(engine.notes["C"]["points"])
    command(engine, "teach_follow")
    assert "Re-teaching C from the start: hover, touch and press" in engine.message
    run(engine, 3, until=lambda: engine.teach.mode == "following")
    assert engine.public["teach"]["points"] == ["home"] and "Guide it to hover" in engine.message
    assert "hover next" in reject(engine, "teach_capture", point="touch")["message"]  # in order, as the first time
    move_leader(engine, "shoulder_lift", 60)
    move_leader(engine, "wrist_flex", 30)  # a slightly different hover above the key
    capture(engine, "hover")
    assert engine.notes["C"]["points"] == before  # nothing saved until press: the old motion still plays
    command(engine, "teach_hold")
    command(engine, "teach_follow")  # following again carries on from touch, it does not start over
    run(engine, 3, until=lambda: engine.teach.mode == "following")
    assert engine.public["teach"]["points"] == ["home", "hover"]
    move_leader(engine, "wrist_flex", 36)
    capture(engine, "touch")
    move_leader(engine, "wrist_flex", 12)
    capture(engine, "press")
    after = engine.notes["C"]["points"]
    assert all(after[n] != before[n] for n in ("hover", "touch", "press")) and engine.phase == "teach_play"


def test_home_is_shared_by_the_next_key(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    command(engine, "teach_follow")
    run(engine, 3, until=lambda: engine.teach.mode == "following")
    assert "hover next" in reject(engine, "teach_capture", point="touch", control="D")["message"]
    capture(engine, "hover", "D")
    assert engine.points_for("D")["home"] == engine.notes["C"]["points"]["home"]


def test_press_keeps_the_commanded_depth_when_the_key_stops_the_arm(engine):
    following(engine)
    capture(engine, "home")
    move_leader(engine, "shoulder_lift", 60)
    capture(engine, "hover")
    move_leader(engine, "wrist_flex", 36)
    capture(engine, "touch")
    engine.arm.jammed = True  # the key surface stops the follower
    move_leader(engine, "wrist_flex", 24)
    capture(engine, "press")
    press = engine.notes["C"]["points"]["press"]
    assert press["goal"]["wrist_flex"] > press["measured"]["wrist_flex"] + 1  # the push is what replays


def test_capture_waits_for_following(engine):
    ready(engine)
    command(engine, "teach_begin", control="C")
    engine.leader.current["elbow_flex"] += 600
    command(engine, "teach_follow")
    assert "FOLLOWING" in reject(engine, "teach_capture", point="home", control="C")["message"]


def test_one_teach_press_saves_home_and_starts_following(engine):
    ready(engine)
    resting = dict(engine.arm.current)
    command(engine, "teach_begin", control="C", follow=True)
    assert engine.phase == "teach_follow" and engine.teach.mode in ("aligning", "following")
    assert engine.public["teach"]["points"] == ["home"] and engine.public["teach"]["home_saved"]
    home = engine.points_for("C")["home"]["measured"]
    assert all(abs(engine.arm.joint_action(resting, [n])[n] - home[n]) < 0.1 for n in home if n != "gripper")
    run(engine, 3, until=lambda: engine.teach.mode == "following")
    capture(engine, "hover")  # straight on to the points
    assert engine.public["teach"]["points"] == ["home", "hover"]


def test_chord_button_teach_saves_home_too(engine):
    ready(engine)
    command(engine, "teach_begin", control="chord.maj", follow=True)
    assert engine.repo.get("teach_home") is not None and engine.phase == "teach_follow"
    assert engine.public["teach"]["points"] == ["home"]


def test_one_home_for_the_arm_moves_every_key(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    taught = engine.notes["C"]["points"]
    command(engine, "teach_follow")
    move_leader(engine, "shoulder_pan", 60)  # somewhere else entirely
    capture(engine, "home", "C")
    assert engine.public["teach"]["points"] == ["home"]  # re-teaching C from the start; its saved motion is kept
    assert engine.notes["C"]["points"]["press"] == taught["press"]
    new_home = engine.repo.get("teach_home")["point"]["goal"]
    assert new_home["shoulder_pan"] > taught["home"]["goal"]["shoulder_pan"] + 3
    command(engine, "teach_play", control="C")
    sent = spy(engine)
    run(engine, 30, until=lambda: engine.phase == "teach_hold")
    assert sent[-1] == pytest.approx(new_home)  # C now starts and ends at the new home
    assert max(g["wrist_flex"] for g in sent) == pytest.approx(taught["press"]["goal"]["wrist_flex"])


def test_home_button_sets_home_where_the_arm_is_and_goes_there(engine):
    following(engine)
    old = engine.repo.get("teach_home")["point"]["goal"]
    move_leader(engine, "shoulder_pan", 60)
    command(engine, "teach_set_home")
    new = engine.repo.get("teach_home")["point"]["goal"]
    assert new["shoulder_pan"] > old["shoulder_pan"] + 3 and "Home set here" in engine.message
    assert engine.public["teach"]["home_saved_at"]
    move_leader(engine, "shoulder_pan", -96)  # wander away again
    command(engine, "teach_go_home")
    assert engine.phase == "teach_play" and engine.teach_going_home
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert engine.message == "At home, holding." and engine.teach_played is None
    assert engine.teach.goal == pytest.approx(new)


def follower_only(e):
    """Teach C with the leader, then reconnect guiding by hand: only the follower is connected."""
    following(e)
    teach_points(e, "C")
    run(e, 10, until=lambda: e.phase == "teach_hold")
    taught = deepcopy(e.notes["C"]["points"])
    command(e, "disconnect", supported=True)
    connect(e)
    assert e.leader is None and e.teaching_mode == "manual" and e.calibrated
    return taught


def test_a_taught_control_plays_with_only_the_follower(engine):
    taught = follower_only(engine)
    assert engine.public["keys"]["C"]["recorded"]
    command(engine, "teach_begin", control="C")
    assert engine.phase == "teach_hold" and engine.arm.enabled and not engine.public["teach"]["leader"]
    command(engine, "teach_play", control="C")
    sent = spy(engine)
    run(engine, 30, until=lambda: engine.phase == "teach_hold")
    assert engine.teach_played == "C"
    assert max(g["wrist_flex"] for g in sent) == pytest.approx(taught["press"]["goal"]["wrist_flex"])
    assert sent[-1] == pytest.approx(engine.repo.get("teach_home")["point"]["goal"])


def test_the_saved_home_is_reachable_with_only_the_follower(engine):
    follower_only(engine)
    assert engine.public["poses_saved"] == {"home": True, "rest": False}
    assert "No rest" in reject(engine, "teach_begin", control="C", pose="rest")["message"]
    assert not engine.arm.enabled
    command(engine, "teach_begin", control="C", pose="home")
    assert engine.phase == "teach_hold" and engine.repo.get("teach_home") is not None
    command(engine, "teach_go_home")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert engine.message == "At home, holding."
    assert engine.teach.goal == pytest.approx(engine.repo.get("teach_home")["point"]["goal"])


def test_teaching_still_needs_the_leader(engine):
    follower_only(engine)
    home = engine.repo.get("teach_home")
    assert "leader" in reject(engine, "teach_begin", control="C", follow=True)["message"]
    assert "not been taught" in reject(engine, "teach_begin", control="D")["message"]
    assert engine.phase == "connected" and not engine.arm.enabled  # refused before powering
    command(engine, "teach_begin", control="C")
    for action, args in (("teach_follow", {}), ("teach_capture", {"point": "hover", "control": "C"}), ("teach_set_home", {})):
        assert "leader" in reject(engine, action, **args)["message"]
    assert engine.phase == "teach_hold" and engine.repo.get("teach_home") == home


def test_go_home_needs_a_home(engine):
    ready(engine)
    command(engine, "teach_begin", control="C")
    engine.teach_home_doc = None  # e.g. after a recalibration invalidated it
    assert "No home" in reject(engine, "teach_go_home")["message"]


def test_playback_waits_at_home_even_with_a_rest_pose(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    command(engine, "teach_follow")
    move_leader(engine, "elbow_flex", 60)
    command(engine, "teach_set_rest")
    command(engine, "teach_go_rest")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    command(engine, "teach_play", control="C")
    sent = spy(engine)
    run(engine, 40, until=lambda: engine.phase == "teach_hold")
    home = engine.repo.get("teach_home")["point"]["goal"]
    assert sent[-1] == pytest.approx(home)  # rest -> home -> key -> home, and it stays at home
    assert "holding at home" in engine.message and engine.teach_played == "C"


def test_go_to_rest_and_no_rest_means_playback_ends_at_home(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert "No rest" in reject(engine, "teach_go_rest")["message"]
    command(engine, "teach_play", control="C")
    run(engine, 30, until=lambda: engine.phase == "teach_hold")
    assert "holding at home" in engine.message
    command(engine, "teach_follow")
    move_leader(engine, "elbow_flex", 48)
    command(engine, "teach_set_rest")
    move_leader(engine, "elbow_flex", -96)
    command(engine, "teach_go_rest")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert engine.message == "At rest, holding."
    assert engine.teach.goal == pytest.approx(engine.repo.get("teach_rest")["point"]["goal"])


def test_a_touch_far_from_hover_is_refused(engine):
    following(engine)  # hover never moved off home: captured at the folded pose
    capture(engine, "hover")
    move_leader(engine, "shoulder_lift", 96)
    move_leader(engine, "shoulder_lift", 96)
    move_leader(engine, "shoulder_lift", 96)  # ~25 deg... and more
    move_leader(engine, "shoulder_lift", 96)
    result = reject(engine, "teach_capture", point="touch", control="C")
    assert "from hover" in result["message"] and "retrain it" in result["message"]
    assert engine.public["teach"]["points"] == ["home", "hover"]
    capture(engine, "hover")  # re-capture hover above the key, then touch is accepted
    move_leader(engine, "wrist_flex", 24)
    capture(engine, "touch")
    assert engine.public["teach"]["points"] == ["home", "hover", "touch"]


def test_a_saved_key_with_a_misplaced_hover_is_flagged_and_not_played(engine):
    following(engine)
    teach_points(engine, "D")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    entry = engine.notes["D"]
    bad = {**entry, "points": {**entry["points"], "hover": entry["points"]["home"]}}
    bad["points"]["hover"] = {"goal": {**entry["points"]["home"]["goal"], "shoulder_lift": entry["points"]["touch"]["goal"]["shoulder_lift"] - 120}, "measured": entry["points"]["home"]["measured"]}
    engine.repo.save_note("D", bad)
    engine.notes = engine.repo.notes()
    engine.publish()
    assert engine.public["keys"]["D"]["status"] == "needs_reteach"
    assert "needs re-teaching" in reject(engine, "teach_play", control="D")["message"]


def test_follow_teaches_the_key_selected_on_the_map(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert engine.selected == "C"
    command(engine, "teach_follow", control="D")  # D picked on the map, then Follow the leader
    assert engine.selected == "D" and engine.public["selected"] == "D"
    run(engine, 3, until=lambda: engine.teach.mode == "following")
    capture(engine, "hover", "D")
    assert engine.public["teach"]["points_for"] == "D"



# --- wrist roll ------------------------------------------------------------------------------------


def test_a_turned_wrist_is_captured_in_the_taught_point(engine):
    following(engine)
    move_leader(engine, "wrist_roll", 96)  # turned on purpose: the follower turns with it
    capture(engine, "hover")
    points = engine.points_for("C")
    assert points["hover"]["goal"]["wrist_roll"] > points["home"]["goal"]["wrist_roll"] + 5


def test_recentering_the_wrist_roll_keeps_every_taught_motion_on_the_same_physical_poses(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    command(engine, "teach_follow")
    move_leader(engine, "elbow_flex", 48)
    command(engine, "teach_set_rest")
    command(engine, "teach_hold")
    # Physical poses (raw ticks relative to the old reference) before the change
    before_cal = deepcopy(engine.calibration)
    old = engine.notes["C"]["points"]
    command(engine, "release", supported=True)
    command(engine, "recenter_wrist_roll", supported=True)
    assert "re-centred" in engine.message and engine.phase == "ready"
    assert engine.calibration["wrist_roll"]["homing_offset"] != before_cal["wrist_roll"]["homing_offset"]
    new = engine.notes["C"]["points"]
    shift = (before_cal["wrist_roll"]["homing_offset"] - engine.calibration["wrist_roll"]["homing_offset"]) % 4096
    assert shift in (2047, 2048)  # half a turn (2047 only when 2048 does not fit the offset register)
    for name in old:
        delta = (new[name]["goal"]["wrist_roll"] - old[name]["goal"]["wrist_roll"]) % (4096 * 360 / 4095)
        assert delta == pytest.approx(shift * 360 / 4095, abs=0.01)  # the same half turn as the encoder
        assert {k: v for k, v in new[name]["goal"].items() if k != "wrist_roll"} == \
            {k: v for k, v in old[name]["goal"].items() if k != "wrist_roll"}
    assert engine.public["keys"]["C"]["status"] == "registered"  # still valid: nothing to re-teach
    assert engine.repo.get("teach_rest")["calibration_sha256"] == engine.notes["C"]["calibration_sha256"]
    # Playing C still reaches the same physical press (raw ticks shifted by the same half turn)
    command(engine, "teach_begin", control="C")
    command(engine, "teach_play", control="C")
    run(engine, 30, until=lambda: engine.phase == "teach_hold")
    assert engine.teach_played == "C"


def test_recentering_needs_both_arms_resting(engine):
    following(engine)
    assert reject(engine, "recenter_wrist_roll", supported=True)["message"].startswith(
        "Cannot recenter wrist roll while the arm is following the leader. Release torque first")


def test_the_wrist_rotates_with_the_leader_for_keys_and_chord_buttons(engine):
    for control in ("C", "chord.maj"):
        following(engine, control) if control == "C" else command(engine, "teach_follow", control=control)
        run(engine, 3, until=lambda: engine.teach.mode == "following")
        roll = engine.arm.current["wrist_roll"]
        move_leader(engine, "wrist_roll", 96)
        assert engine.arm.current["wrist_roll"] == pytest.approx(roll + 96, abs=2)
        command(engine, "teach_hold")


def test_the_wrist_follows_through_the_leaders_wrap_and_waits_at_its_own_end(engine):
    """The leader's wrist turned past +180 deg reads -179: the follower's wrist goes to its end (+168 deg) and waits,
    never spinning to -179, then follows again as soon as the leader comes back, whichever reading it shows."""
    following(engine)
    limit = round(teach.ROLL_LIMIT_DEG * 4095 / 360 + 2047.5)  # +168 deg in encoder steps (wrist roll spans 0..4095)

    def leader_at(raw, seconds=2):
        for step in range(int(seconds / teach.PERIOD)):  # turned smoothly, as by hand
            current = engine.leader.current["wrist_roll"]
            engine.leader.current["wrist_roll"] = current + max(-40, min(40, raw - current))
            run(engine, teach.PERIOD)
    leader_at(4060)  # +178.5 deg: past the follower's end
    assert engine.teach.roll_guard and engine.arm.current["wrist_roll"] == pytest.approx(limit, abs=3)
    engine.leader.current["wrist_roll"] = 4094  # through +180 ...
    run(engine, teach.PERIOD)
    engine.leader.current["wrist_roll"] = 30  # ... to -177 deg: the same turn, read across the edge
    run(engine, 0.5)
    assert engine.teach.roll_guard and engine.arm.current["wrist_roll"] == pytest.approx(limit, abs=3)  # not spun round
    move_leader(engine, "elbow_flex", 24)  # the other joints keep following
    engine.leader.current["wrist_roll"] = 4094  # back across the edge ...
    run(engine, teach.PERIOD)
    leader_at(3700)  # ... and inside its reach: +145 deg
    assert not engine.teach.roll_guard and engine.arm.current["wrist_roll"] == pytest.approx(3700, abs=3)
    engine.leader.current["wrist_roll"] = 1000  # a glitch: 92 deg in one tick is not a real turn
    run(engine, teach.PERIOD)
    assert engine.arm.current["wrist_roll"] == pytest.approx(3700, abs=3)


# --- press length, speed, sequences, API --------------------------------------------------------------


def taught_keys(engine, keys=("C",)):
    following(engine)
    for key in keys:
        if key != "C":
            command(engine, "teach_follow", control=key)
            run(engine, 3, until=lambda: engine.teach.mode == "following")
        teach_points(engine, key)
        run(engine, 10, until=lambda: engine.phase == "teach_hold")


def played_frames(engine, **args):
    command(engine, "teach_play", **args)
    frames = engine.teach.recording["frames"]
    run(engine, 60, until=lambda: engine.phase == "teach_hold")
    return frames


def test_press_length_holds_the_press_and_becomes_the_keys_default(engine):
    taught_keys(engine)
    press = None
    for press_s in (0.3, 2.0):
        frames = played_frames(engine, control="C", press_s=press_s)
        press = engine.notes["C"]["points"]["press"]["goal"]
        held = [f["t"] for f in frames if f["goal"] == pytest.approx(press)]
        assert max(held) - min(held) == pytest.approx(press_s, abs=0.05)
    assert engine.notes["C"]["press_s"] == 2.0
    assert engine.public["keys"]["C"]["press_s"] == 2.0  # used next time without being given
    assert "press length" in reject(engine, "teach_play", control="C", press_s=61)["message"]  # up to a minute


def test_speed_setting_shortens_playback_and_is_remembered(engine):
    taught_keys(engine)
    command(engine, "teach_play", control="C")
    started = engine.clock()
    run(engine, 60, until=lambda: engine.phase == "teach_hold")
    normal = engine.clock() - started
    command(engine, "teach_settings", speed=3.0)
    assert engine.teach_settings["speed"] == 3.0 and engine.repo.get("teach_settings")["speed"] == 3.0
    command(engine, "teach_play", control="C")
    started = engine.clock()
    run(engine, 60, until=lambda: engine.phase == "teach_hold")
    assert engine.clock() - started < normal / 2
    assert "speed" in reject(engine, "teach_play", control="C", speed=5)["message"]


def test_press_hardness_slows_only_the_press_stroke():
    def pose(lift, wrist):
        return {"goal": {"shoulder_lift": lift, "wrist_flex": wrist}, "measured": {"shoulder_lift": lift, "wrist_flex": wrist}}
    points = {"home": pose(0, 0), "hover": pose(30, 0), "touch": pose(30, 10), "press": pose(30, 14)}
    press_stroke, _ = teach._segment(points["touch"]["goal"], points["press"]["goal"], teach.STROKE_SPEED)
    hard, gentle = (teach.waypoint_recording(points, "C", hardness=h)["frames"] for h in (1.0, 0.5))
    assert gentle[-1]["t"] - hard[-1]["t"] == pytest.approx(press_stroke)  # twice as long; nothing else changes
    def pressed(frames):
        return [f["t"] for f in frames if f["goal"]["wrist_flex"] >= 14 - 1e-9]

    def first_press(frames):
        return pressed(frames)[0]

    def leaves_press(frames):
        return pressed(frames)[-1]

    assert first_press(gentle) - first_press(hard) == pytest.approx(press_stroke)
    assert (gentle[-1]["t"] - leaves_press(gentle)) == pytest.approx(hard[-1]["t"] - leaves_press(hard))  # release unchanged
    with pytest.raises(ValueError):
        teach.waypoint_recording(points, "C", hardness=0.05)


def test_press_hardness_is_a_remembered_global_setting(engine):
    assert engine.public["teach_settings"]["press_hardness"] == teach.PRESS_HARDNESS
    taught_keys(engine)
    durations = {}
    for hardness in (1.0, 0.25):
        command(engine, "teach_settings", press_hardness=hardness)
        command(engine, "teach_play", control="C")
        assert f"{hardness:.0%} hardness" in engine.message
        started = engine.clock()
        run(engine, 60, until=lambda: engine.phase == "teach_hold")
        durations[hardness] = engine.clock() - started
    assert durations[0.25] > durations[1.0] + 3 * teach.MIN_SEGMENT_S - 0.1
    assert engine.repo.get("teach_settings")["press_hardness"] == 0.25
    assert "hardness" in reject(engine, "teach_settings", press_hardness=1.5)["message"]


def test_speed_is_a_global_setting_available_in_any_phase(engine):
    command(engine, "teach_settings", speed=2.0)  # before anything is connected
    assert engine.public["teach_settings"]["speed"] == 2.0
    taught_keys(engine)
    command(engine, "teach_play", control="C")
    assert engine.phase == "teach_play"
    command(engine, "teach_settings", speed=0.5)  # while playing: used from the next play
    run(engine, 60, until=lambda: engine.phase == "teach_hold")
    assert engine.teach_settings["speed"] == 0.5
    assert "speed" in reject(engine, "teach_settings", speed=3.5)["message"]


def spread_keys(engine, keys=("C", "D")):
    """Taught keys as on Orchid: well away from home, side by side (taught_keys puts them a few degrees apart)."""
    home = dict(engine.teach.goal)
    engine.save_teach_home({"goal": home, "measured": home})
    for n, key in enumerate(keys):
        hover = {**home, "shoulder_pan": home["shoulder_pan"] + 20 + 8 * n, "shoulder_lift": home["shoulder_lift"] + 25}
        poses = {"hover": hover, "touch": {**hover, "wrist_flex": hover["wrist_flex"] + 6},
                 "press": {**hover, "wrist_flex": hover["wrist_flex"] + 9}}
        points = {**engine.notes[key]["points"], **{k: {"goal": v, "measured": v} for k, v in poses.items()}}
        engine.save_control_entry(key, {**engine.notes[key], "points": points})


def test_a_sequence_plays_each_key_in_order_from_hover_to_hover_and_ends_at_home(engine):
    taught_keys(engine, ("C", "D"))
    spread_keys(engine)
    command(engine, "teach_sequence", steps=[{"control": "C"}, {"control": "D", "press_s": 1.0}, {"control": "C"}])
    assert engine.phase == "teach_play" and "C → D → C" in engine.message
    sent = spy(engine)
    run(engine, 120, until=lambda: engine.phase == "teach_hold")
    assert engine.message.startswith("Played C → D → C")
    presses = [engine.notes[k]["points"]["press"]["goal"] for k in ("C", "D", "C")]
    hits = [i for i, g in enumerate(sent) for p in presses if g == pytest.approx(p)]
    order = [next(k for k in ("C", "D") if sent[i] == pytest.approx(engine.notes[k]["points"]["press"]["goal"])) for i in hits]
    collapsed = [k for i, k in enumerate(order) if i == 0 or k != order[i - 1]]
    assert collapsed == ["C", "D", "C"]
    home = engine.repo.get("teach_home")["point"]["goal"]
    assert not any(g == pytest.approx(home, abs=0.5) for g in sent[hits[0]:hits[-1]])  # never home between keys
    assert sent[-1] == pytest.approx(home)
    assert "no taught steps" in reject(engine, "teach_sequence", steps=[{"control": "E"}])["message"]


def test_a_key_can_stay_at_its_hover_and_the_next_one_starts_there_unless_that_move_would_sag(engine, monkeypatch):
    from orchid_demo import kinematics
    taught_keys(engine, ("C", "D"))
    spread_keys(engine)
    hover = {k: engine.notes[k]["points"]["hover"]["goal"] for k in ("C", "D")}
    home = engine.repo.get("teach_home")["point"]["goal"]
    frames = played_frames(engine, control="C", stay=True)
    assert frames[-1]["goal"] == pytest.approx(hover["C"]) and engine.over_key() == "C"
    assert "holding over its hover" in engine.message and not engine.parked()[0]
    frames = played_frames(engine, control="D")  # straight from C's hover, then home
    assert frames[0]["goal"] == pytest.approx(hover["C"]) and frames[-1]["goal"] == pytest.approx(home)
    assert not any(f["goal"] == pytest.approx(home, abs=0.5) for f in frames[:len(frames) // 2])
    assert engine.over_key() is None and engine.parked()[0]
    played_frames(engine, control="C", stay=True)
    monkeypatch.setattr(kinematics, "dip", lambda *a, **k: kinematics.HOP_DIP_MM + 1)  # the direct move would sag toward Orchid
    frames = played_frames(engine, control="D", stay=True)
    assert frames[0]["goal"] == pytest.approx(hover["C"]) and any(f["goal"] == pytest.approx(home) for f in frames)
    assert engine.over_key() == "D"


def test_api_acts_only_while_the_console_is_in_control(engine):
    taught_keys(engine)
    engine.api_submit("teach_play", {"control": "C"})
    engine.step()
    assert engine.phase == "teach_play"
    run(engine, 60, until=lambda: engine.phase == "teach_hold")
    engine.lease_until = time.monotonic() - 1  # console closed
    with pytest.raises(Exception, match="operator console"):
        engine.api_submit("teach_play", {"control": "C"})
    engine.heartbeat("operator")
    with pytest.raises(Exception, match="not available"):
        engine.api_submit("release", {"supported": True})


def test_each_play_is_checked_against_what_orchid_sent(engine):
    from orchid_demo.keycheck import KeyChecker
    heard = []
    engine.key_checker = KeyChecker(lambda: heard, background=False)
    taught_keys(engine, ("C", "E"))
    command(engine, "teach_play", control="C")
    started = engine.clock()
    heard.append({"type": "press", "t": started + 2.0, "name": "C", "velocity": 64, "octave": 3})
    run(engine, 60, until=lambda: engine.phase == "teach_hold")
    run(engine, teach.PERIOD)  # the result is collected on the next control tick
    assert engine.public["key_check"]["status"] == "ok" and engine.key_check["play"] == 1
    assert engine.key_check["summary"].startswith("C ✓ velocity 64") and "into the" in engine.key_check["summary"]
    assert any(e["kind"] == "key_check" for e in engine.repo.events())

    command(engine, "teach_sequence", steps=[{"control": "C"}, {"control": "E"}])
    run(engine, 60, until=lambda: engine.phase == "teach_hold")
    run(engine, teach.PERIOD)
    assert engine.key_check["status"] == "problem" and engine.key_check["play"] == 2
    assert [s["status"] for s in engine.key_check["steps"]] == ["missed", "missed"]  # nothing sounded this time

    command(engine, "teach_go_home")
    run(engine, 30, until=lambda: engine.phase == "teach_hold")
    assert engine.key_check["play"] == 2  # moving home is not a play to check


def test_holding_writes_the_goal_once_not_every_tick():
    pose = {"shoulder_pan": 1.0, "shoulder_lift": 2.0, "elbow_flex": 3.0, "wrist_flex": 4.0, "wrist_roll": 5.0, "gripper": 6.0}
    sent = []
    session = teach.Session(lambda: dict(pose), sent.append, None)
    session.hold()
    for _ in range(30):  # a second of holding still
        session.tick()
    assert len(sent) == 1  # rewriting an unchanged goal restarts the servos' motion and shakes a joint at its stop
    session.goal = {**session.goal, "elbow_flex": 3.5}  # a new goal is written at once
    session.tick()
    assert len(sent) == 2


def test_a_rest_at_a_joints_end_of_travel_is_saved_and_held_just_inside_it(engine):
    following(engine)
    engine.arm.current["elbow_flex"] = engine.calibration["elbow_flex"]["range_max"] - 2
    command(engine, "teach_hold")  # held right there, folded against the elbow's stop
    command(engine, "teach_set_rest")
    assert "elbow flex is at the end of travel" in engine.message and "about 4° inside that stop" in engine.message
    held = engine.arm.joint_target(engine.shared_teach_rest()["goal"])
    assert held["elbow_flex"] == engine.calibration["elbow_flex"]["range_max"] - engine.LIMIT_MARGIN_TICKS


def test_a_rest_saved_at_a_stop_is_held_just_inside_it(engine):
    """A rest saved folded against the elbow's stop (before that was refused) is held 4° off the stop, not into it."""
    following(engine)
    command(engine, "teach_set_rest")
    cal, doc = engine.calibration, engine.repo.get("teach_rest")
    beyond = (cal["elbow_flex"]["range_max"] + 6 - (cal["elbow_flex"]["range_min"] + cal["elbow_flex"]["range_max"]) / 2) * 360 / 4095
    doc["point"]["goal"] = {**doc["point"]["goal"], "elbow_flex": beyond, "gripper": 0.0}  # past the stop, jaws shut
    engine.repo.put("teach_rest", doc)
    engine.teach_rest_doc = doc
    rest = engine.shared_teach_rest()["goal"]
    held = engine.arm.joint_target(rest)
    assert held["elbow_flex"] == cal["elbow_flex"]["range_max"] - engine.LIMIT_MARGIN_TICKS
    assert held["gripper"] - cal["gripper"]["range_min"] >= engine.LIMIT_MARGIN_TICKS - 1
    assert rest["wrist_roll"] == doc["point"]["goal"]["wrist_roll"] and engine.repo.get("teach_rest") == doc  # stored as taught
    moved = engine.shared_teach_rest()["measured"]["elbow_flex"] - doc["point"]["measured"]["elbow_flex"]
    assert moved == pytest.approx(rest["elbow_flex"] - beyond)  # parked is judged where the arm will be held


def test_a_pose_the_wrist_could_not_turn_to_is_refused_and_an_old_one_is_held_where_it_reached(engine):
    following(engine)
    command(engine, "teach_hold")
    engine.arm.jammed = True  # the wrist stops short, as it does near +-180 deg
    engine.teach.goal = {**engine.teach.goal, "wrist_roll": engine.teach.measured["wrist_roll"] + 11.25}
    refused = reject(engine, "teach_set_rest")["message"]
    assert "cannot turn that far" in refused and "straining" in refused
    engine.arm.jammed = False
    engine.teach.hold()  # back where the wrist actually is
    command(engine, "teach_set_rest")
    doc = engine.repo.get("teach_rest")
    doc["point"]["goal"] = {**doc["point"]["goal"], "wrist_roll": 180.0}  # saved before the guard: told 180, reached 168.75
    doc["point"]["measured"] = {**doc["point"]["measured"], "wrist_roll": 168.75}
    engine.repo.put("teach_rest", doc)
    engine.teach_rest_doc = doc
    assert engine.shared_teach_rest()["goal"]["wrist_roll"] == 168.75




def test_cancelling_a_re_teach_keeps_the_saved_motion(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    saved = deepcopy(engine.notes["C"])
    command(engine, "teach_follow")
    run(engine, 3, until=lambda: engine.teach.mode == "following")
    assert engine.public["teach"]["reteaching"] == "C"
    move_leader(engine, "shoulder_lift", 60)
    capture(engine, "hover")
    command(engine, "teach_cancel_reteach")
    assert "cancelled" in engine.message and engine.phase == "teach_follow"  # still following
    run(engine, teach.PERIOD)  # the console's snapshot is published on the next tick
    assert engine.notes["C"] == saved
    assert engine.public["teach"]["points"] == ["home", "hover", "touch", "press"]
    assert engine.public["teach"]["reteaching"] is None
    assert "Nothing is being re-taught" in reject(engine, "teach_cancel_reteach")["message"]
    command(engine, "teach_hold")
    command(engine, "teach_follow")  # a fresh re-teach starts again from hover
    run(engine, teach.PERIOD)
    assert engine.public["teach"]["points"] == ["home"]
    command(engine, "teach_cancel_reteach")


def test_the_path_test_plays_every_key_hover_to_hover_and_reports_each_move(engine, monkeypatch):
    from orchid_demo import kinematics
    taught_keys(engine, ("C", "D"))
    spread_keys(engine)
    command(engine, "teach_settings", speed=2.0)
    assert engine.public["teach"]["path_untested"] == ["C", "D"]
    command(engine, "teach_sequence", steps=[{"control": "C"}, {"control": "D"}, {"control": "C"}], path_test=True)
    assert engine.teach.speed == engine.PATH_FIRST_SPEED  # new moves: the first run is very slow, whatever the Arm speed
    run(engine, 400, until=lambda: engine.phase == "teach_hold")
    test = engine.public["teach"]["path_test"]
    assert test["keys"] == ["C", "D", "C"] and [(h["from"], h["to"], h["route"]) for h in test["hops"]] == [("C", "D", "direct"), ("D", "C", "direct")]
    assert test["first"] == ["C", "D"] and engine.public["teach"]["path_untested"] == []
    command(engine, "teach_sequence", steps=[{"control": "C"}, {"control": "D"}], path_test=True)
    assert engine.teach.speed == 2.0  # played through once: the Arm speed from now on
    run(engine, 120, until=lambda: engine.phase == "teach_hold")
    engine.save_control_entry("D", {**engine.notes["D"], "saved_at": "re-taught"})  # a new hover for D
    assert engine.path_untested() == ["D"]
    monkeypatch.setattr(kinematics, "dip", lambda *a, **k: 12.5)  # every straight move would sag toward Orchid
    command(engine, "teach_sequence", steps=[{"control": "C"}, {"control": "D"}], path_test=True)
    assert "by way of home: C → D" in engine.message
    run(engine, 120, until=lambda: engine.phase == "teach_hold")
    assert engine.public["teach"]["path_test"]["hops"] == [{"from": "C", "to": "D", "route": "home", "dip_mm": 12.5}]
    command(engine, "teach_sequence", steps=[{"control": "C"}, {"control": "D"}])  # an ordinary phrase is not a path test
    assert engine.public["teach"]["path_test"] is None
