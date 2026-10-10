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
    assert [music.beats(x) for x in ("2bars", "1bar", "1.5bars", "16 bars", "1bar+1/2", "1/4+1/16")] == [8, 4, 6, 64, 6, 1.25]
    for bad in ("1/5", "0", "quarter", "", "1/4..", 0.5, "0bars", "1/4+", "+"):
        with pytest.raises(SafetyError, match="note value|no length|lower number"):
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
    assert teach.press_for(600, shaped, 1.0) > 590  # ten minutes of a sweeping chord: no limit on a written note


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


def test_a_held_press_stops_pushing_once_the_key_bottoms_out():
    """The taught press is 3 deg past where this key bottoms out. After settling, the arm holds where it stopped plus a
    light preload, writes that goal once for the whole hold, and lifts without pushing back down."""
    pose = dict.fromkeys(("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"), 0.0)
    points = {name: {"goal": {**pose, "shoulder_lift": lift}, "measured": {**pose, "shoulder_lift": lift}}
              for name, lift in (("home", 0.0), ("hover", 10.0), ("touch", 14.0), ("press", 17.0))}
    bottom = 14.0 + 1.0  # the key's bottom
    recording = teach.waypoint_recording(points, "C", 3.0)  # a 3 s hold
    marks, now, arm, sent = recording["marks"], [0.0], dict(pose), []

    def send(goal):
        sent.append((now[0], dict(goal)))
        arm.update(goal, shoulder_lift=min(goal["shoulder_lift"], bottom))
    session = teach.Session(lambda: dict(arm), send, clock=lambda: now[0], settle_s=0.0)
    session.play(recording, 1.0)
    while session.mode != "holding":
        now[0] += teach.PERIOD
        session.tick()
    started = session.play_began
    settled = started + marks["press"] + teach.HOLD_SETTLE_S + teach.PERIOD
    during = [goal["shoulder_lift"] for t, goal in sent if settled <= t < started + marks["lift"]]
    held = [goal for t, goal in sent if t < settled][-1]["shoulder_lift"]  # what the arm holds through the note
    assert held == pytest.approx(bottom + teach.HOLD_PUSH_DEG)  # not 17: where it stopped plus a 0.5 deg preload
    assert during == []  # nothing rewritten for the rest of the 3 s hold
    after = [goal["shoulder_lift"] for t, goal in sent if t >= started + marks["lift"]]
    assert max(after) <= bottom + teach.HOLD_PUSH_DEG + 1e-9  # lifts from the soft hold, never deeper


def test_a_held_chord_button_is_held_softly_and_let_go_without_pushing():
    pose = dict.fromkeys(("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"), 0.0)
    points = {name: {"goal": {**pose, "shoulder_lift": lift}, "measured": {**pose, "shoulder_lift": lift}}
              for name, lift in (("home", 0.0), ("hover", 10.0), ("touch", 14.0), ("press", 17.0))}
    now, arm = [0.0], dict(pose)
    session = teach.Session(lambda: dict(arm), lambda g: arm.update(g, shoulder_lift=min(g["shoulder_lift"], 15.0)),
                            clock=lambda: now[0], settle_s=0.0)
    session.play(teach.hold_recording(points, "chord.maj"), 1.0)
    while session.mode != "holding" or now[0] < 10:
        now[0] += teach.PERIOD
        session.tick()
    assert session.goal["shoulder_lift"] == pytest.approx(15.5)  # held down softly until let go
    release = teach.no_deeper(teach.return_recording(points, "chord.maj"), session.goal, points)
    assert max(f["goal"]["shoulder_lift"] for f in release["frames"]) == pytest.approx(15.5)


def test_letting_a_chord_button_go_returns_all_the_way_home():
    """A joint whose home lies past the press in the pressing direction (live performance, 2026-10-10: the Chord Arm
    stopped 5 deg short of home after Min and the interlock then refused the Keys Arm). Only the lift-off is capped."""
    pose = dict.fromkeys(("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"), 0.0)
    points = {name: {"goal": {**pose, "shoulder_lift": lift, "wrist_flex": flex}, "measured": {**pose, "shoulder_lift": lift}}
              for name, lift, flex in (("home", 25.0, 0.0), ("hover", 10.0, 0.0), ("touch", 14.0, 0.0), ("press", 17.0, 0.0))}
    soft = {**points["press"]["goal"], "shoulder_lift": 15.5}  # held softly above the taught press
    release = teach.no_deeper(teach.return_recording(points, "chord.min"), soft, points)
    lifted = release["marks"]["touch"]
    assert max(f["goal"]["shoulder_lift"] for f in release["frames"] if f["t"] <= lifted) == pytest.approx(15.5)
    assert release["frames"][-1]["goal"]["shoulder_lift"] == pytest.approx(25.0)  # home, though "deeper" than the press


def test_each_key_learns_where_it_sounds_and_strikes_land_on_the_beat(engine):  # noqa: F811
    """Orchid's notes say this key sounds 80% of the way down its stroke, not halfway: after a few plays the strike is
    timed on that point, and every timed play reports how far its note was from the beat."""
    from orchid_demo.keycheck import KeyChecker
    following(engine)
    teach_points(engine, "C")
    run(engine, 10, until=lambda: engine.phase == "teach_hold")

    def heard():
        marks = engine.teach.recording["marks"]
        anchor = engine.play_steps[0]["clock"] if engine.play_steps and "clock" in engine.play_steps[0] else \
            {"t": engine.teach.play_began, "at": 0.0}
        t = anchor["t"] + (marks["touch"] + 0.8 * (marks["press"] - marks["touch"]) - anchor["at"]) / engine.teach.speed
        return [{"type": "press", "t": t, "name": "C", "velocity": 40, "notes": [60]}]
    engine.key_checker = KeyChecker(heard, background=False)
    timing = music.timing({"bpm": 100, "beat": 0.0, "running": True, "paused": False, "t": engine.clock()})
    lates = []
    for _ in range(4):
        command(engine, "teach_play", control="C", sound_s=0.6,
                rhythm={"timing": {**timing, "grid": {"t": engine.clock(), "beat": 0.0}}, "steps": [{"offset": 0}]})
        run(engine, 20, until=lambda: engine.phase == "teach_hold")
        run(engine, 0.2)
        lates.append(engine.key_check["steps"][0]["late_ms"])
    assert engine.strike_fraction("C") == pytest.approx(0.8, abs=0.01)
    assert abs(lates[0]) > 30 and abs(lates[-1]) <= 2 * teach.PERIOD * 1000  # late at first; then on the beat
    assert engine.public["onsets"] == {"C": pytest.approx(0.8, abs=0.01)}
    engine.save_control_entry("C", {**engine.notes["C"], "saved_at": "re-taught"})  # a re-teach starts learning over
    assert engine.strike_fraction("C") == teach.STRIKE_FRACTION


def test_a_score_holds_every_note_as_written_and_moves_a_late_change_by_whole_bars():
    from orchid_demo import score
    placed = score.steps([{"bar": 1, "beat": 1, "key": "C", "chord": "chord.maj", "duration": "1"},
                          {"bar": 2, "beat": 1, "key": "A", "chord": "chord.min", "duration": "2bars"},
                          {"bar": 3, "beat": 1, "key": "F", "chord": "chord.maj", "duration": "1"},
                          {"bar": 6, "beat": 3, "key": "G", "duration": "4bars"}], "1/8")
    assert [s["offset"] for s in placed] == [0, 4, 8, 22]

    def timing(step, after=None):  # seconds at the playing speed; after: from that key's hover
        return {"lead": 0.5 if after else 0.8, "rise": 0.9, "lift": 0.4, "chord_press": 0.6, "chord_release": 0.7}
    plan = score.plan(placed, 0.5, timing, {"chord>chord": 0.2})  # 120 BPM: a bar is 2 s
    c, a, f, g = placed
    assert [s["hold_s"] for s in placed] == [2.0, 4.0, 2.0, 8.0]  # every note held as written, however long
    # C+maj -> A+min: held 2 s; the Chord Arm home 1.2 (the Keys Arm only lifts to C's hover, 0.8); the chord button 1.0
    # and the key straight from C's hover 0.5; learned 0.2: 4.9 s for a 2 s gap: 2 bars later.
    assert c["needs_s"] == pytest.approx(2.0 + 1.2 + 1.0 + 0.5 + 0.2)
    assert plan["moves"][0]["to_bar"] == 4 and plan["moves"][0]["bars"] == 2 and "to bar 4" in plan["moves"][0]["text"]
    # A+min's two bars now overrun F's bar 3 (moved to 5): F moves on past A's hold too, keeping its beat 1.
    assert f["moved_bars"] > a["moved_bars"] and plan["moves"][1]["to_bar"] == 3 + f["moved_bars"]
    assert g["moved_bars"] == f["moved_bars"]  # G at bar 6 beat 3 has room after F: no further move
    assert plan["length_bars"] == 10 + g["moved_bars"]
    with pytest.raises(SafetyError, match="Two notes at bar 1 beat 1"):
        score.steps([{"bar": 1, "beat": 1, "key": "C"}, {"bar": 1, "beat": 1, "key": "E"}], "1/8")
    with pytest.raises(SafetyError, match="A beat is 1 to 5"):
        score.steps([{"bar": 1, "beat": 5, "key": "C"}], "1/8")


def test_performances_teach_the_plan_how_long_changes_really_take(tmp_path):
    from orchid_demo import score

    def timing(step, after=None):
        return {"lead": 0.5 if after else 0.8, "rise": 0.9, "lift": 0.4, "chord_press": 0.6, "chord_release": 0.7}
    placed = score.steps([{"bar": 1, "beat": 1, "key": "C", "chord": "chord.maj", "duration": "1/4"},
                          {"bar": 3, "beat": 1, "key": "A", "chord": "chord.min"}], "1/4")
    score.plan(placed, 0.5, timing)
    model = placed[0]["needs_s"]
    strikes = [{"strike_at": 100.0}, {"strike_at": 104.0, "earliest": 100.0 + model + 2.5}]  # took 2.5 s longer
    learned = score.learn(score.load(tmp_path / "t.json"), placed, strikes, timing)
    assert learned["extra"]["chord>chord"] == pytest.approx(2.5)
    score.save(tmp_path / "t.json", learned)
    assert score.load(tmp_path / "t.json")["extra"]["chord>chord"] == pytest.approx(2.5)
    assert score.load(tmp_path / "missing.json")["extra"] == score.SEEDED  # before any performance: today's measurement


def test_a_score_starts_on_its_own_beat_of_studios_bar():
    grid = music.timing({"bpm": 120, "beat": 10.0, "running": True, "paused": False, "t": 100.0})
    plan = music.Schedule(grid, [2, 3], align=4, phase=2)  # the first note is on beat 3 of its bar
    assert plan.strike(0, 100.3) == pytest.approx(102.0)  # Studio beat 14, the next beat 3 of a bar (not beat 11)


def test_the_rigs_delay_is_learned_apart_from_each_keys_trigger_point_so_timing_holds_at_any_speed(engine):  # noqa: F811
    """C sounds 60% down its stroke and E 30%, each 60 ms after the commanded pose gets there (servo lag and MIDI).
    Heard at two speeds, the model tells the two apart, and predicts a third speed."""
    following(engine)
    for key in ("C", "E"):
        engine.notes[key] = {"saved_at": "taught"}
    steps = []
    for key, fraction in (("C", 0.6), ("E", 0.3)):
        for stroke in (0.4, 0.4, 0.2, 0.2, 0.3):  # touch -> press in seconds at 1x, 2x and 1.33x
            steps.append({"control": key, "status": "ok", "into_stroke_s": fraction * stroke + 0.06, "stroke_s": stroke})
    engine.onset_heard(steps)
    assert engine.strike_fraction("C") == pytest.approx(0.6, abs=0.02) and engine.strike_fraction("E") == pytest.approx(0.3, abs=0.02)
    assert engine.onset_delay("C") == pytest.approx(0.06, abs=0.005) and engine.onset_delay("D") == 0.0  # D: not heard yet
    engine.notes["C"] = {"saved_at": "re-taught"}  # a re-teach starts C over; E keeps its learning
    assert engine.strike_fraction("C") == teach.STRIKE_FRACTION and engine.strike_fraction("E") == pytest.approx(0.3, abs=0.02)


def test_the_conductor_fires_each_change_on_its_bar_line_and_moves_it_with_its_note():
    from orchid_demo import score
    placed = score.steps([{"bar": 1, "beat": 1, "key": "C"}, {"bar": 3, "beat": 1, "key": "E"},
                          {"bar": 5, "beat": 1, "key": "G"}], "1")
    later = score.changes([{"bar": 3, "sound": 4}, {"bar": 4, "beat": 3, "fx": {"reverb": {"mix": 10}}},
                           {"bar": 7, "sound": 9}], placed)
    assert [c["event"] for c in later] == [1, 2, 3]  # the note each change comes with (3: after the last)
    now, known, fired = [100.0], {0: 100.0}, []
    conductor = score.Conductor(later, placed, 0.5, lambda c: fired.append((c["bar"], now[0])) or ["ok"], lambda: dict(known),
                                clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    conductor.thread = type("T", (), {"start": lambda self: None, "join": lambda self, t=None: None})()
    # E is due at bar 3 (4 s later), but moved a bar later: its change goes with it.
    known[1] = 106.0
    conductor.finished.set()
    known[2] = 110.0
    conductor.run()
    assert fired[0][0] == 3 and fired[0][1] == pytest.approx(106.0 - score.CHANGE_LEAD_S, abs=0.011)
    assert fired[1][0] == 4 and fired[1][1] == pytest.approx(110.0 - 2 * 0.5 - score.CHANGE_LEAD_S, abs=0.011)  # bar 4 beat 3: 2 beats before G
    assert fired[2][0] == 7 and fired[2][1] == pytest.approx(110.0 + 8 * 0.5 - score.CHANGE_LEAD_S, abs=0.011)  # on from G
    # A note the plan already moved a bar later: counting on from the note before, its change waits for that bar too.
    placed[1]["moved_bars"] = placed[2]["moved_bars"] = 1
    conductor = score.Conductor(later[:1], placed, 0.5, lambda c: ["ok"], lambda: {0: 100.0})
    assert conductor.due(later[0], {0: 100.0}) == pytest.approx(100.0 + 4.0 + 2.0)


def test_the_voicing_dial_is_turned_until_orchid_reports_the_target(tmp_path):
    from orchid_demo import voicing
    dial = {"value": 40, "reported": False}

    def turn(control, degrees):  # this dial: clockwise lowers it, 0.25 clicks a degree; its first report has no delta
        clicks = round(abs(degrees) * 0.25) * (-1 if control == "voicing.cw" else 1)
        dial["value"] += clicks
        first, dial["reported"] = not dial["reported"], True
        return (0 if first else clicks), dial["value"]
    learned = voicing.load(tmp_path / "v.json")
    report = voicing.set_voicing(52, None, turn, learned)
    assert report["ok"] and report["reached"] == 52 and len(report["turns"]) <= voicing.MAX_TURNS
    assert learned["up"] == "voicing.ccw" and learned["clicks_per_degree"] == pytest.approx(0.25, abs=0.08)
    voicing.save(tmp_path / "v.json", learned)
    again = voicing.set_voicing(45, 52, turn, voicing.load(tmp_path / "v.json"))
    assert again["ok"] and again["turns"][0]["control"] == "voicing.cw"  # down, the learned way, first time
    stuck = voicing.set_voicing(10, 45, lambda control, degrees: (0, None), dict(voicing.DEFAULTS))
    assert not stuck["ok"] and len(stuck["turns"]) == 2 and "not catching it" in stuck["reason"]


def test_a_voicing_change_between_notes_is_planned_as_time_before_the_note():
    from orchid_demo import score
    placed = score.steps([{"bar": 1, "beat": 1, "key": "C", "duration": "1"}, {"bar": 2, "beat": 1, "key": "E"}], "1")

    def timing(step, after=None):
        return {"lead": 0.5 if after else 0.8, "rise": 0.9, "lift": 0.4, "chord_press": 0.6, "chord_release": 0.7}
    without = score.plan(placed, 0.5, timing)["moves"][0]["to_bar"]  # C held its bar: E already moves
    assert placed[0]["needs_s"] == pytest.approx(2.0 + 0.8 + 0.5)  # up to C's hover, straight to E's, down to E
    plan = score.plan(placed, 0.5, timing, before={1: 6.0})  # and 6 s of turning the dial before E, from home
    assert placed[0]["needs_s"] == pytest.approx(2.0 + 1.3 + 0.8 + 6.0) and (without, plan["moves"][0]["to_bar"]) == (3, 7)


def test_a_note_a_hair_late_is_played_now_not_a_bar_later():
    grid = music.timing({"bpm": 76, "beat": 0.0, "running": True, "paused": False, "t": 100.0})
    plan = music.Schedule(grid, [0, 4, 8], at=101.0, slip=4)
    beat = 60 / 76
    assert plan.strike(0, 101.0) == pytest.approx(101.0)
    assert plan.strike(1, 101.0 + 4 * beat + 0.03) == pytest.approx(101.0 + 4 * beat + 0.03)  # 30 ms late: played late
    assert plan.report[1]["late_ms"] == 30 and plan.report[1]["slipped_beats"] == 0
    assert plan.strike(2, 101.0 + 8 * beat + 0.2) == pytest.approx(101.0 + 12 * beat)  # 200 ms late: the next bar
    assert plan.report[2]["slipped_beats"] == 4
