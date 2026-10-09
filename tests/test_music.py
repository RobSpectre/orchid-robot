"""Musical time: note values, Orchid Studio's beat, and strikes that land on it."""
import pytest

from orchid_demo import music, teach
from orchid_demo.keycheck import check_step
from orchid_demo.motion import SafetyError
from test_operator_engine import command
from test_teach_app import engine, following, run, teach_points  # noqa: F401  (engine is a fixture)


def test_note_values_are_beats():
    assert [music.beats(x) for x in ("1", "1/2", "1/4", "1/8", "1/16", "1/8.", "1/4t", "3/8", "q", "e.", "w", "st")] == \
        pytest.approx([4, 2, 1, 0.5, 0.25, 0.75, 2 / 3, 1.5, 1, 0.75, 4, 1 / 6])
    for bad in ("1/5", "0", "quarter", "", "1/4..", 0.5, "61"):
        with pytest.raises(SafetyError, match="note value|out of range"):
            music.beats(bad)


def test_the_first_strike_lands_on_the_next_beat_and_late_notes_slip_by_whole_beats():
    grid = music.timing({"bpm": 120, "beat": 10.0, "running": True, "paused": False, "t": 100.0})
    plan = music.Schedule(grid, [0, 1, 1.5, 3])
    assert plan.strike(0, 100.3) == pytest.approx(100.5)  # beat 11
    assert plan.strike(1, 100.6) == pytest.approx(101.0)  # one beat later, as written
    assert plan.strike(2, 102.0) == pytest.approx(102.25)  # due 101.25, 0.75 s late: slips two beats
    assert plan.strike(3, 102.0) == pytest.approx(103.0)  # and the rest of the phrase slips with it
    assert [s["slipped_beats"] for s in plan.report] == [0, 0, 2, 0]
    stopped = music.timing({"bpm": 90, "beat": 0.0, "running": False, "t": 5.0})
    assert stopped["grid"] is None and music.Schedule(stopped, [0]).strike(0, 7.3) == 7.3  # no grid: its own time
    assert music.Schedule(grid, [0], at=104.0).strike(0, 103.0) == 104.0  # due at a given time


def test_a_timed_play_waits_above_the_key_then_strikes_on_time():
    pose = dict.fromkeys(("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"), 0.0)
    points = {name: {"goal": {**pose, "shoulder_lift": lift}, "measured": {**pose, "shoulder_lift": lift}}
              for name, lift in (("home", 0.0), ("hover", 10.0), ("touch", 14.0), ("press", 16.0))}
    recording = teach.waypoint_recording(points, "C", 0.3)
    marks = recording["marks"]
    assert marks["hover"] < marks["touch"] < marks["strike"] < marks["press"] < marks["lift"] < marks["release"]
    now = [0.0]
    measured = dict(pose)
    session = teach.Session(lambda: dict(measured), lambda goal: measured.update(goal), clock=lambda: now[0], settle_s=0.0)
    strike_at = 5.0
    lead = marks["strike"] - marks["hover"]
    session.play(recording, 1.0, gates=[marks["hover"]], timer=lambda i, t: strike_at - lead)
    struck = None
    while session.mode != "holding" or struck is None:
        now[0] += teach.PERIOD
        session.tick()
        frame_t = recording["frames"][session.index]["t"]
        if struck is None and session.mode == "playing" and frame_t >= marks["strike"]:
            struck = now[0]
        assert now[0] < 30
    assert struck == pytest.approx(strike_at, abs=2 * teach.PERIOD)  # it waited at the hover for the beat


def test_a_note_sounds_for_its_length():
    pose = dict.fromkeys(("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"), 0.0)
    points = {name: {"goal": {**pose, "shoulder_lift": lift}, "measured": pose}
              for name, lift in (("home", 0.0), ("hover", 10.0), ("touch", 14.0), ("press", 16.0))}
    shaped = teach.waypoint_recording(points, "C", 0.3)
    for speed in (1.0, 2.0):
        press_s = teach.press_for(0.75, shaped, speed)
        marks = teach.waypoint_recording(points, "C", press_s)["marks"]
        assert (marks["release"] - marks["strike"]) / speed == pytest.approx(0.75, abs=1e-3)
    assert teach.press_for(0.01, shaped, 1.0) == 0.0  # shorter than the strokes: as short as it goes
    assert teach.press_for(90, shaped, 3.0) == teach.MAX_PRESS_S * 3  # held at most a minute, whatever the speed


def test_the_engine_lands_a_key_on_the_grid_and_reports_it(engine):  # noqa: F811
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")
    now = engine.clock()
    timing = music.timing({"bpm": 100, "beat": 3.3, "running": True, "paused": False, "t": now})
    command(engine, "teach_play", control="C", sound_s=0.6, rhythm={"timing": timing, "steps": [{"offset": 0}]})
    struck = None
    marks = engine.teach.recording["marks"]
    for _ in range(int(20 / teach.PERIOD)):
        engine.heartbeat("operator")
        engine.clock.sleep(teach.PERIOD)
        engine.step()
        if struck is None and engine.play_t is not None and engine.play_t >= marks["strike"]:
            struck = engine.clock()
        if engine.phase == "teach_hold":
            break
    due = engine.public["teach"]["rhythm"]["steps"][0]["strike_at"]
    beat = 3.3 + (due - now) * 100 / 60
    assert beat == pytest.approx(round(beat), abs=1e-6) and struck == pytest.approx(due, abs=2 * teach.PERIOD)
    assert (marks["release"] - marks["strike"]) == pytest.approx(0.6, abs=1e-3)  # sounds 0.6 s at 1x


def test_the_key_check_times_each_note_from_where_it_waited():
    step = {"key": "C", "start": 0.0, "end": 3.0, "marks": {"touch": 1.0, "press": 1.2},
            "clock": {"t": 50.0, "at": 0.8}}  # waited at 0.8 s into the recording until clock 50.0
    note = {"type": "press", "t": 50.35, "name": "C", "velocity": 60, "notes": [60]}
    result = check_step(step, 10.0, 1.0, [note])  # without the gate the window would start at 10.0 and miss it
    assert result["status"] == "ok" and result["into_stroke_s"] == pytest.approx(0.15)
