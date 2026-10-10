"""Correct keys: path geometry, the keyboard model, and the engine's test -> move / depth -> test loop with a stand-in for Orchid Studio."""
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



NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def presses(e):
    """The presses of the current play: each with its marks (one for a find, TEST_PRESSES for a test)."""
    return e.teach.recording.get("steps") or [{"marks": e.teach.recording["marks"]}]


def orchid(e, decide):
    """A stand-in for Orchid Studio's key events on engine e. decide(e, i) says what press i of the current play sounded:
    None nothing, "C" that key, ("C", "C#") both at once, ["C", "C"] the key twice. Notes sound 30% into the stroke."""
    def heard():
        events = []
        for i, step in enumerate(presses(e)):
            marks = step["marks"]
            t = e.teach.play_started + (marks["touch"] + 0.3 * (marks["press"] - marks["touch"])) / e.teach.speed
            what = decide(e, i)
            if what is None:
                continue
            for n, keys in enumerate([what] if not isinstance(what, list) else what):
                keys = keys if isinstance(keys, tuple) else (keys,)
                events.append({"type": "press", "t": t + n * 0.08, "name": keys[0], "velocity": 20, "octave": 3,
                               "notes": [60 + NAMES.index(k) for k in keys]})
        return events
    return heard


def at_trigger(e, i):
    """Finding: the key sounds 30% into the stroke. Testing: decided by the test's own function."""
    return e.selected


def taught_c(e, decide):
    following(e)
    teach_points(e, "C")
    run(e, 10, until=lambda: e.phase == "teach_hold")
    e.key_checker = KeyChecker(orchid(e, decide), background=False)


def corrected(e, **args):
    command(e, "tune_start", control="C", beside_arm=True, **args)
    run(e, 600, until=lambda: not e.tuning)
    return e.tune


def rounds(e):
    return len(e.tune["tests"])


def test_tally_counts_each_press_and_how_often_each_neighbour_sounded():
    steps = [{"status": "ok"}, {"status": "wrong", "heard": ["C#"]}, {"status": "double", "heard": ["C", "C#"]},
             {"status": "missed"}, {"status": "repeated"}]
    tally = tune.tally(steps, "C")
    assert tally == {"clean": 1, "missed": 1, "repeated": 1, "wrong": 1, "double": 1, "neighbours": {"C#": 0.3}, "presses": 5}
    assert tune.describe(tally, "C") == "C: 1/5 clean, C# 1.5×, 1 missed, 1 sounded twice"


def test_a_key_that_presses_right_five_times_is_left_as_it_is(engine):
    taught_c(engine, lambda e, i: "C")
    before = deepcopy(engine.notes["C"]["points"])
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    assert [(x["phase"], x["outcome"]) for x in result["log"]] == [("test", "pass")]
    assert engine.notes["C"]["points"] == before and "already right, nothing changed" in engine.message
    assert engine.notes["C"]["calibration"]["tests"][0]["clean"] == tune.TEST_PRESSES


def test_a_test_presses_five_times_at_the_arm_speed_and_finding_presses_gently(engine):
    seen = []

    def decide(e, i):
        if i == 0:
            seen.append((e.tune["phase"], e.teach.speed, len(presses(e))))
        return None if e.tune["phase"] == "test" and len(e.tune["tests"]) == 0 and i == 0 else "C"
    taught_c(engine, decide)
    command(engine, "teach_settings", speed=2.5)
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    assert seen[0] == ("test", 2.5, tune.TEST_PRESSES)  # five presses as played
    assert ("find", tune.FIND_SPEED, 1) in seen and seen[-1] == ("test", 2.5, tune.TEST_PRESSES)


def test_a_white_key_that_sounds_its_black_neighbour_moves_forward_by_how_often(engine):
    """Two presses of five hit C#: the finger is too far back, onto the black keys' row. The stroke moves forward,
    onto C's wide front, by 2/5 of half the way between the rows, then tests clean."""
    taught_c(engine, lambda e, i: "C#" if rounds(e) == 0 and i < 2 else "C")
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    assert [x["phase"] for x in result["log"]] == ["test", "test"] and "C# 2×" in result["log"][0]["text"]
    board = engine.keyboard()
    way, between = board.correction("C", "C#")
    expected = 0.4 * between / 2
    saved = engine.notes["C"]
    hover, taught_hover = (engine.finger(saved[k]["hover"]["goal"]) for k in ("points", "taught_points"))
    moved = [hover[i] - taught_hover[i] for i in range(3)]
    assert sum(moved[i] * way[i] for i in range(3)) == pytest.approx(expected, abs=0.05)  # forward, toward the player
    assert way == pytest.approx((-board.back[0], -board.back[1], 0))
    assert saved["calibration"]["side_shift_mm"] == pytest.approx(expected, abs=0.05)
    assert "moved" in engine.message and len(saved["calibration"]["tests"]) == 2


def test_a_black_key_that_sounds_its_white_neighbour_moves_along_the_row_toward_itself():
    """D# hitting E: the finger is beside D#, on E's back part. It moves along the row toward D#, not back."""
    from orchid_demo.keyboard import SLOTS, Keyboard
    board = Keyboard({k: (10 + 17 * slot, 500 + 35 * row, 5.0) for k, (slot, row) in SLOTS.items()})
    way, between = board.correction("D#", "E")
    assert way == pytest.approx((-1, 0, 0), abs=1e-6) and between == pytest.approx(8.5, abs=0.01)  # toward D, half a slot
    assert board.correction("A#", "A")[0] == pytest.approx((1, 0, 0), abs=1e-6)
    assert board.correction("D", "E") == (pytest.approx((-1, 0, 0), abs=1e-6), pytest.approx(17, abs=0.01))
    assert board.correction("C", "E") is None


def test_two_keys_at_once_move_half_as_far(engine):
    taught_c(engine, lambda e, i: ("C", "C#") if rounds(e) == 0 else "C")
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    assert engine.notes["C"]["calibration"]["side_shift_mm"] == pytest.approx(min(0.5 * engine.keyboard().correction("C", "C#")[1] / 2, engine.keyboard().pitch * tune.MAX_STEP_KEYS), abs=0.05)


def test_a_key_that_keeps_sounding_its_neighbour_stops_at_a_keys_width(engine):
    taught_c(engine, lambda e, i: "D")
    before = deepcopy(engine.notes["C"])
    result = corrected(engine)
    assert result["status"] == "failed" and "would have to move over" in result["message"] and engine.notes["C"] == before
    taught_far = KeyChecker(orchid(engine, lambda e, i: "G"), background=False)  # not a neighbour: too far off to move
    engine.key_checker = taught_far
    result = corrected(engine)
    assert result["status"] == "failed" and "more than a key off" in result["message"]


def test_the_right_key_but_missed_finds_the_trigger_then_sets_the_depth(engine):
    taught_c(engine, lambda e, i: None if rounds(e) == 0 and i < 2 else "C")
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    phases = [x["phase"] for x in result["log"]]
    assert phases[0] == "test" and phases[1:3] == ["find", "find"] and phases[-1] == "test"
    saved = engine.notes["C"]
    trigger = saved["calibration"]["trigger"]
    assert tune.gap(saved["points"]["touch"]["goal"], trigger) == pytest.approx(tune.TOUCH_MARGIN, abs=0.1)
    assert tune.gap(saved["points"]["press"]["goal"], trigger) == pytest.approx(tune.PRESS_DEPTH, abs=0.1)
    assert saved["taught_points"]["press"] != saved["points"]["press"] and "trigger point" in engine.message


def test_a_double_trigger_after_the_depth_is_set_leaves_more_room_above_the_trigger(engine):
    def decide(e, i):
        if rounds(e) == 0:
            return None if i == 0 else "C"
        return ["C", "C"] if rounds(e) == 1 and i == 0 else "C"
    taught_c(engine, decide)
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    assert result["margin"] == tune.TOUCH_MARGIN + tune.MARGIN_STEP and rounds(engine) == 3


def test_a_key_that_never_tests_clean_stops_after_the_last_round_without_saving(engine):
    taught_c(engine, lambda e, i: None if e.tune["phase"] == "test" and i == 0 else "C")  # the first press always misses
    before = deepcopy(engine.notes["C"])
    result = corrected(engine)
    assert result["status"] == "failed" and "re-teach it with the leader" in result["message"]
    assert "at the deepest press" in result["message"] or f"after {tune.MAX_ROUNDS} tests" in result["message"]
    assert engine.notes["C"] == before


def test_stop_motion_ends_correcting_and_other_commands_wait(engine):
    taught_c(engine, lambda e, i: "C")
    command(engine, "tune_start", control="C", beside_arm=True)
    run(engine, 5, until=lambda: engine.phase == "teach_play")
    assert "being corrected" in reject(engine, "teach_play", control="C")["message"]
    engine.stop_event.set()
    run(engine, teach.PERIOD * 2)
    assert engine.tune["status"] == "stopped" and engine.phase == "teach_hold"
    run(engine, 5)
    assert engine.phase == "teach_hold"


def test_correcting_needs_confirmation_the_note_check_and_a_taught_key(engine):
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    assert "note check" in reject(engine, "tune_start", control="C", beside_arm=True)["message"]
    engine.key_checker = KeyChecker(lambda: [], background=False)
    assert "beside the arm" in reject(engine, "tune_start", control="C")["message"]
    assert "Only keyboard keys" in reject(engine, "tune_start", control="chord.maj", beside_arm=True)["message"]
    assert "not been taught" in reject(engine, "tune_start", control="D", beside_arm=True)["message"]
    assert engine.tune is None


def test_a_list_is_corrected_one_after_another_and_stop_ends_the_list(engine):
    taught_keys(engine, ("C", "E"))
    engine.key_checker = KeyChecker(orchid(engine, lambda e, i: "C" if e.selected == "C" else "A"), background=False)
    assert "not been taught" in reject(engine, "tune_start", controls=["C", "D"], beside_arm=True)["message"]
    command(engine, "tune_start", controls=["C", "E"], beside_arm=True)
    assert engine.public["tune_queue"] == ["E"]
    run(engine, 800, until=lambda: not engine.tuning)
    results = engine.public["tune_results"]
    assert results["C"]["status"] == "done" and results["E"]["status"] == "failed" and "A sounded" in results["E"]["message"]
    command(engine, "tune_start", controls=["C", "E"], beside_arm=True)
    run(engine, 400, until=lambda: engine.tune["status"] == "done")
    command(engine, "tune_stop")
    assert not engine.tuning and engine.tune["control"] == "C"


def test_a_stroke_that_comes_down_to_the_side_is_straightened(engine):
    taught_c(engine, lambda e, i: "C")
    entry = deepcopy(engine.notes["C"])
    along = engine.keyboard().along3()
    touch = engine.finger(entry["points"]["touch"]["goal"])
    entry["points"]["touch"] = engine.moved(entry["points"], "touch", tuple(touch[i] + 3.0 * along[i] for i in range(3)))
    engine.save_control_entry("C", entry)
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    assert result["log"][0]["phase"] == "straighten" and "approach straightened" in result["log"][0]["text"]
    saved = engine.notes["C"]
    t, p = engine.finger(saved["points"]["touch"]["goal"]), engine.finger(saved["points"]["press"]["goal"])
    assert abs(sum((t[i] - p[i]) * along[i] for i in range(2))) < 0.3  # straight down onto the press, along the row
    assert saved["calibration"]["straightened"] is True and saved["taught_points"]["touch"] == entry["points"]["touch"]
    assert "approach straightened" in engine.message


def test_two_keys_at_once_count_as_a_neighbour_sounding():
    from orchid_demo.keycheck import check_step
    step = {"key": "C", "start": 0, "end": 3, "marks": {}}
    both = {"type": "press", "t": 1.0, "name": "C", "notes": [60, 61], "velocity": 40}
    result = check_step(step, 0.0, 1.0, [both])
    assert result["status"] == "double" and result["heard"] == ["C", "C#"] and "C and C# sounded together" in result["text"]




def test_the_keyboard_pattern_flags_a_key_taught_off_centre():
    from orchid_demo.keyboard import SLOTS, Keyboard
    tips = {k: (10 + 17 * slot, 500 + 35 * row, 5.0) for k, (slot, row) in SLOTS.items()}
    tips["D"] = (tips["D"][0] + 4.5, tips["D"][1], 5.0)  # 4.5 mm toward D#
    board = Keyboard(tips)
    assert board.pitch == pytest.approx(17, abs=0.8) and board.along == pytest.approx((1, 0), abs=1e-3)
    report = board.report()
    assert set(report["keys"]) == {"D"} and report["keys"]["D"]["text"] == "D is 4 mm toward D#"
    assert board.correction("C", "C#")[0] == pytest.approx((0, -1, 0), abs=0.01)  # a white key off a black one: forward
    assert board.correction("C", "E") is None and Keyboard({"C": tips["C"]}).report()["fitted"] is False


def test_testing_all_keys_reports_each_and_changes_nothing(engine):
    taught_keys(engine, ("C", "E"))
    engine.key_checker = KeyChecker(orchid(engine, lambda e, i: "C" if e.selected == "C" else ("F" if i < 2 else "E")),
                                    background=False)
    before = deepcopy(engine.notes)
    command(engine, "tune_start", controls=["C", "E"], beside_arm=True, test_only=True)
    assert engine.message.startswith("Testing C: pressing it 5 times")
    run(engine, 400, until=lambda: not engine.tuning)
    results = engine.public["tune_results"]
    assert results["C"]["kind"] == "test" and results["C"]["status"] == "done" and results["C"]["clean"] == 5
    assert results["E"]["status"] == "failed" and results["E"]["clean"] == 3 and "F 2×" in results["E"]["message"]
    assert engine.notes == before  # a test moves and saves nothing
    assert [x["phase"] for x in engine.tune["log"]] == ["test"]  # one test of five presses, no corrections


def test_a_neighbour_every_time_moves_half_a_key_a_round_until_it_is_right(engine):
    """A# style: the neighbour sounds on all five presses. One round moves half a key's width, not a whole one."""
    taught_c(engine, lambda e, i: "D" if rounds(e) == 0 else "C")
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    pitch = engine.keyboard().pitch
    assert engine.notes["C"]["calibration"]["side_shift_mm"] == pytest.approx(pitch * tune.MAX_STEP_KEYS, abs=0.05)
    assert f"moved {pitch * tune.MAX_STEP_KEYS:.1f} mm away from D" in result["log"][0]["text"]


def test_a_neighbour_sounding_while_finding_the_depth_moves_away_from_it_and_tests_again(engine):
    """F# style: every press missed, then a gentle deeper press sounded the neighbour."""
    def decide(e, i):
        if e.tune["phase"] == "find":
            return "C#"
        return None if rounds(e) == 0 else "C"
    taught_c(engine, decide)
    result = corrected(engine)
    assert result["status"] == "done", result["message"]
    phases = [x["phase"] for x in result["log"]]
    assert phases == ["test", "find", "test"] and "away from C#" in result["log"][1]["text"]
    assert engine.notes["C"]["calibration"]["side_shift_mm"] > 0
