"""Key calibration regime: path geometry, and the engine's find -> set -> verify loop with a stand-in for Orchid Studio."""
from copy import deepcopy

import pytest

from orchid_demo import teach, tune
from orchid_demo.engine import Engine
from orchid_demo.keycheck import KeyChecker
from test_operator_engine import Clock, command, reject
from test_teach_app import following, run, taught_keys, teach_points


def pose(lift, wrist):
    goal = {"shoulder_pan": 10.0, "shoulder_lift": lift, "elbow_flex": -20.0, "wrist_flex": wrist, "wrist_roll": -170.0, "gripper": 1.0}
    return {"goal": goal, "measured": dict(goal)}


def straight(start, end, seconds=4.0, steps=40):
    """A commanded path from start to end wrist angle, then back: like a press stroke and its return."""
    down = [{"t": seconds * i / steps, "goal": pose(30, start + (end - start) * i / steps)["goal"]} for i in range(steps + 1)]
    return down + [{"t": seconds + 1, "goal": pose(30, start)["goal"]}]


def test_goal_at_interpolates_the_commanded_path():
    frames = straight(90, 100)
    assert tune.goal_at(frames, 1.0)["wrist_flex"] == pytest.approx(92.5)
    assert tune.goal_at(frames, -1)["wrist_flex"] == 90 and tune.goal_at(frames, 99)["wrist_flex"] == 90


def test_touch_and_press_are_placed_around_the_trigger_on_the_path():
    frames = straight(90, 100)
    trigger, touch, press = tune.around(frames, 2.0, 4.0, 1.0, 0.75)  # triggers at 95 deg
    assert trigger["wrist_flex"] == pytest.approx(95)
    assert touch["wrist_flex"] == pytest.approx(94, abs=0.26) and touch["wrist_flex"] <= 94 + 1e-9
    assert press["wrist_flex"] == pytest.approx(95.75, abs=0.26) and press["wrist_flex"] >= 95.75 - 1e-9
    _, _, past = tune.around(frames, 3.9, 4.0, 1.0, 0.75)  # the taught press is only 0.25 deg further: extend
    assert past["wrist_flex"] == pytest.approx(99.75 + 0.75, abs=0.01)


def test_finding_presses_deeper_only_up_to_the_limit():
    points = {"home": pose(0, 80), "hover": pose(30, 90), "touch": pose(30, 96), "press": pose(30, 98)}
    taught, steps = points["press"], 0
    while True:
        new, why = tune.deeper(points, taught)
        if new is None:
            break
        points, steps = new, steps + 1
    assert steps == int(tune.LIMIT_DEG / tune.STEP_DEG) and "3°" in why


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()


def at(e, fraction, name=None, velocity=20, offset=0.0):
    """A note-on at fraction of the way from touch to press on the current play, on the engine clock."""
    marks = e.teach.recording["marks"]
    t = e.teach.play_started + (marks["touch"] + fraction * (marks["press"] - marks["touch"])) / e.teach.speed + offset
    return {"type": "press", "t": t, "name": name or e.selected, "velocity": velocity, "octave": 3}


def taught_c(e, heard):
    following(e)
    teach_points(e, "C")
    run(e, 10, until=lambda: e.phase == "teach_hold")
    e.key_checker = KeyChecker(lambda: heard(e), background=False)


def calibrated(e, **args):
    command(e, "tune_start", control="C", beside_arm=True, **args)
    run(e, 400, until=lambda: not e.tuning)
    return e.tune


def test_a_key_is_found_set_around_its_trigger_and_verified_at_playing_speed(engine):
    taught_c(engine, lambda e: [at(e, 0.3)] if e.tune["phase"] == "find" else [at(e, 0.5)])
    command(engine, "teach_settings", speed=2.0)
    taught = deepcopy(engine.notes["C"]["points"])
    result = calibrated(engine)
    assert result["status"] == "done", result["message"]
    assert [(e["phase"], e["outcome"]) for e in result["log"]] == [("find", "pass"), ("find", "pass"), ("verify", "pass"), ("verify", "pass")]
    saved = engine.notes["C"]
    trigger = saved["calibration"]["trigger"]
    assert tune.gap(saved["points"]["touch"]["goal"], trigger) == pytest.approx(tune.TOUCH_MARGIN, abs=0.1)
    assert tune.gap(saved["points"]["press"]["goal"], trigger) == pytest.approx(tune.PRESS_DEPTH, abs=0.1)
    assert saved["calibration"]["verified_speed"] == 2.0 and saved["taught_points"]["touch"] == taught["touch"]
    assert saved["points"]["touch"]["goal"]["gripper"] == taught["touch"]["goal"]["gripper"]
    assert engine.public["keys"]["C"]["status"] == "registered" and "calibrated" in engine.message


def test_finds_play_gently_and_verify_plays_at_the_arm_speed(engine):
    speeds = []
    def heard(e):
        speeds.append((e.tune["phase"], e.teach.speed))
        return [at(e, 0.4)]
    taught_c(engine, heard)
    command(engine, "teach_settings", speed=2.5)
    calibrated(engine)
    assert speeds == [("find", tune.FIND_SPEED)] * 2 + [("verify", 2.5)] * 2


def test_a_double_press_at_speed_leaves_more_room_above_the_trigger(engine):
    verify_tries = []
    def heard(e):
        if e.tune["phase"] == "find":
            return [at(e, 0.3)]
        verify_tries.append(1)
        return [at(e, 0.2), at(e, 0.2, offset=0.08)] if len(verify_tries) == 1 else [at(e, 0.5)]
    taught_c(engine, heard)
    result = calibrated(engine)
    assert result["status"] == "done" and result["margin"] == tune.TOUCH_MARGIN + tune.MARGIN_STEP
    saved = engine.notes["C"]
    assert tune.gap(saved["points"]["touch"]["goal"], saved["calibration"]["trigger"]) == pytest.approx(1.5, abs=0.1)


def test_a_silent_key_is_pressed_deeper_while_finding(engine):
    finds = []
    def heard(e):
        if e.tune["phase"] == "verify":
            return [at(e, 0.5)]
        finds.append(1)
        return [] if len(finds) == 1 else [at(e, 0.9)]
    taught_c(engine, heard)
    result = calibrated(engine)
    assert result["status"] == "done"
    assert [e["outcome"] for e in result["log"]][:3] == ["adjust", "pass", "pass"] and "deeper" in result["log"][0]["text"]


def test_finds_that_disagree_or_a_wrong_key_stop_without_saving(engine):
    positions = iter([0.1, 0.9, 0.1, 0.9, 0.1, 0.9])
    taught_c(engine, lambda e: [at(e, next(positions))])
    before = deepcopy(engine.notes["C"])
    result = calibrated(engine)
    assert result["status"] == "failed" and "different point" in result["message"]
    assert engine.notes["C"] == before
    engine.key_checker = KeyChecker(lambda: [at(engine, 0.3, "C#")], background=False)
    result = calibrated(engine)
    assert result["status"] == "failed" and "C# sounded" in result["message"] and engine.notes["C"] == before


def test_stop_motion_ends_calibration_and_other_commands_wait(engine):
    taught_c(engine, lambda e: [at(e, 0.3)])
    command(engine, "tune_start", control="C", beside_arm=True)
    run(engine, 5, until=lambda: engine.phase == "teach_play")
    assert "tune-up is running" in reject(engine, "teach_play", control="C")["message"]
    engine.stop_event.set()
    run(engine, teach.PERIOD * 2)
    assert engine.tune["status"] == "stopped" and engine.phase == "teach_hold"
    run(engine, 5)
    assert engine.phase == "teach_hold"


def test_calibration_needs_confirmation_the_note_check_and_a_taught_key(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert "note check" in reject(engine, "tune_start", control="C", beside_arm=True)["message"]
    engine.key_checker = KeyChecker(lambda: [], background=False)
    assert "beside the arm" in reject(engine, "tune_start", control="C")["message"]
    assert "Only keyboard keys" in reject(engine, "tune_start", control="chord.maj", beside_arm=True)["message"]
    assert "not been taught" in reject(engine, "tune_start", control="D", beside_arm=True)["message"]
    assert engine.tune is None


def test_a_list_is_calibrated_one_after_another_and_stop_ends_the_list(engine):
    taught_keys(engine, ("C", "E"))
    engine.key_checker = KeyChecker(lambda: [at(engine, 0.3, "C" if engine.selected == "C" else "F")], background=False)
    assert "not been taught" in reject(engine, "tune_start", controls=["C", "D"], beside_arm=True)["message"]
    command(engine, "tune_start", controls=["C", "E"], beside_arm=True)
    assert engine.public["tune_queue"] == ["E"]
    run(engine, 800, until=lambda: not engine.tuning)
    results = engine.public["tune_results"]
    assert results["C"]["status"] == "done" and results["E"]["status"] == "failed" and "F sounded" in results["E"]["message"]
    command(engine, "tune_start", controls=["C", "E"], beside_arm=True)
    run(engine, 400, until=lambda: engine.tune["status"] == "done")
    command(engine, "tune_stop")
    assert not engine.tuning and engine.tune["control"] == "C"
