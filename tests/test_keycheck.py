"""Checking a play against what Orchid sent (Orchid Studio's key monitor), without hardware."""
import pytest

from orchid_demo import keycheck, teach
from orchid_demo.keycheck import KeyChecker, check, check_step

STEP = {"key": "C", "start": 0.0, "end": 4.0, "marks": {"touch": 1.0, "press": 1.8, "lift": 2.1}}


def press(t, name="C", velocity=70):
    return {"type": "press", "t": t, "name": name, "velocity": velocity, "octave": 3}


def release(press_t, held):
    return {"type": "release", "t": press_t + held, "press_t": press_t, "held_s": held}


def test_the_right_key_reports_velocity_timing_and_hold():
    result = check_step(STEP, 100.0, 1.0, [press(101.5), release(101.5, 0.6), press(90.0, "D")])
    assert result["status"] == "ok" and result["velocity"] == 70 and result["held_s"] == 0.6
    assert (result["into_stroke_s"], result["stroke_s"], result["vs_bottom_s"]) == (0.5, 0.8, -0.3)
    assert result["text"] == "C ✓ velocity 70, 0.50 s into the 0.80 s press, held 0.60 s"


def test_speed_scales_the_window_and_marks():
    result = check_step(STEP, 100.0, 2.0, [press(100.75)])  # at 2x the touch is at 100.5
    assert result["into_stroke_s"] == 0.25 and result["stroke_s"] == 0.4 and "release not seen" in result["text"]
    assert check_step(STEP, 100.0, 2.0, [press(103.0)])["status"] == "missed"  # after this play's window


@pytest.mark.parametrize("events, status, text", [
    ([], "missed", "C: missed, no note"),
    ([press(101.5, "B")], "wrong", "C: wrong key, B sounded"),
    ([press(101.5), press(101.7)], "repeated", "C: sounded 2 times"),
])
def test_wrong_missed_and_repeated_notes(events, status, text):
    result = check_step(STEP, 100.0, 1.0, events)
    assert (result["status"], result["text"]) == (status, text)


def test_chord_buttons_expect_no_midi_and_dials_count_clicks():
    button = {"key": "chord.min", "start": 0.0, "end": 3.0}
    assert check_step(button, 0.0, 1.0, [])["status"] == "unchecked"
    assert check_step(button, 0.0, 1.0, [press(1.0, "E")])["status"] == "stray"
    dial = {"key": "voicing.cw", "start": 0.0, "end": 3.0}
    clicks = [{"type": "voicing", "t": 1.0, "delta": 1}, {"type": "voicing", "t": 1.1, "delta": 1}]
    assert check_step(dial, 0.0, 1.0, clicks)["clicks"] == 2
    assert check_step(dial, 0.0, 1.0, [])["text"] == "Clockwise: the dial did not click"


def test_a_sequence_checks_each_step_in_its_own_window():
    steps = [{**STEP, "key": "C"}, {"key": "E", "start": 4.0, "end": 8.0, "marks": {"touch": 5.0, "press": 5.8}}]
    result = check({"play": 3, "started": 0.0, "speed": 1.0, "steps": steps}, [press(1.4), press(5.3, "E")])
    assert result["status"] == "ok" and [s["status"] for s in result["steps"]] == ["ok", "ok"]
    result = check({"play": 3, "started": 0.0, "speed": 1.0, "steps": steps}, [press(1.4)])
    assert result["status"] == "problem" and result["summary"].endswith("E: missed, no note")


def test_an_unreachable_studio_only_marks_the_check_unavailable():
    def fetch():
        raise LookupError("Orchid Studio is not reachable")
    checker = KeyChecker(fetch, background=False)
    checker.request({"play": 1, "started": 0.0, "speed": 1.0, "steps": [STEP]})
    assert checker.poll() == {"play": 1, "status": "unavailable", "steps": [], "summary": "Orchid Studio is not reachable"}
    assert checker.poll() is None
    assert "not reachable" in str(pytest.raises(LookupError, keycheck.StudioKeys(port=1)).value)


def test_recordings_mark_the_press_stroke_and_sequences_shift_them():
    def pose(lift, wrist):
        return {"goal": {"shoulder_lift": lift, "wrist_flex": wrist}, "measured": {"shoulder_lift": lift, "wrist_flex": wrist}}
    points = {"home": pose(0, 0), "hover": pose(30, 0), "touch": pose(30, 10), "press": pose(30, 14)}
    c = teach.waypoint_recording(points, "C", press_s=0.5)
    marks = c["marks"]
    assert 0 < marks["touch"] < marks["press"] and marks["lift"] == pytest.approx(marks["press"] + 0.5)
    both = teach.sequence_recording([c, teach.waypoint_recording(points, "E")])
    first, second = both["steps"]
    assert first["marks"] == marks and second["start"] == first["end"] == c["frames"][-1]["t"]
    assert second["marks"]["touch"] == pytest.approx(second["start"] + marks["touch"])


def test_a_release_matches_its_press_despite_rounding():
    result = check_step(STEP, 100.0, 1.0, [press(101.5000004), release(101.5, 0.6)])
    assert result["held_s"] == 0.6 and "held 0.60 s" in result["text"]
