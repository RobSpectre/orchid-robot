"""Chords: Chord Arm holds a chord button while Keys Arm plays a key, run by orchid-robot from one API call (simulation)."""
from copy import deepcopy
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from orchid_demo import kinematics
from orchid_demo import motion as m
from orchid_demo.api import create_app
from orchid_demo.engine import Engine, WAYPOINT_FORMAT
from orchid_demo.keycheck import KeyChecker, check_step
from orchid_demo.rig import ROLES, LeaderStore


@pytest.fixture
def plate(tmp_path, monkeypatch):
    # The simulated arms' zero poses point straight at each other; the order of a chord is what these tests check.
    monkeypatch.setattr(kinematics, "CLEARANCE_MM", 0.0)
    slow = lambda s: time.sleep(min(s, 0.005))  # noqa: E731  real clock, fast simulated sweeps
    a = Engine(tmp_path, sleep=slow, owns=ROLES["a"])
    b = Engine(tmp_path / "arm-b", sleep=slow, owns=ROLES["b"], leader_store=LeaderStore(a.repo))
    calls, looper, exported = [], {"running": True, "settings": {"bars": 4}}, []

    def studio(command, **fields):  # a stand-in for Orchid Studio: what was sent, and when
        calls.append((command, fields, time.monotonic()))
        if command == "status":
            return {"status": "ok", "performance": None, "looper": looper}
        if command == "loop-export":
            return {"status": "ok", "document": {"layers": exported}}
        return {"status": "ok", **({"composed": {"applies_at_beat": 16}} if command == "loop-compose" else {})}
    with TestClient(create_app(tmp_path, engines={"a": a, "b": b}, studio=studio), base_url="http://127.0.0.1") as client:
        client.studio_calls, client.looper, client.exported = calls, looper, exported
        token = client.get("/api/session").json()["token"]
        headers = {"x-orchid-token": token, "x-orchid-operator": str(uuid4()), "origin": "http://127.0.0.1"}

        def command(engine, action, **args):
            client.post("/api/heartbeat", json={"leader_visible": True}, headers=headers)
            identity = str(uuid4())
            response = client.post("/api/commands", json={"id": identity, "action": action, "revision": engine.revision,
                                                          "args": args, "arm": engine.arm_id}, headers=headers)
            assert response.status_code == 202, response.text
            until = time.monotonic() + 10
            while time.monotonic() < until and engine.receipts[identity]["status"] == "queued":
                time.sleep(0.01)
            assert engine.receipts[identity]["status"] == "complete", engine.receipts[identity]

        for engine, controls in ((a, ("C", "D")), (b, ("chord.maj", "chord.min"))):
            command(engine, "connect", prepared=True, fixture="Test", teaching_mode="manual")
            command(engine, "calibrate", supported=True, target="follower")
            command(engine, "calibration_center", supported=True)
            for _ in range(5):
                command(engine, "simulate_sweep")
                command(engine, "calibration_next", range_complete=True)
            command(engine, "calibration_save", range_complete=True)
            joints = dict(engine.arm.teleop_read()[1])
            engine.save_teach_home({"goal": joints, "measured": joints})
            command(engine, "teach_begin", control=controls[0], pose="home")
            teach(engine, controls)
            until = time.monotonic() + 10
            while time.monotonic() < until and not (engine.phase == "teach_hold" and engine.parked_now[0]):
                time.sleep(0.01)
            command(engine, "teach_settings", speed=3.0)  # the operator's Arm speed (programs cannot set it)
        beating = threading.Event()

        def heartbeat():  # the console page keeps the lease while it is open
            while not beating.wait(0.2):
                client.post("/api/heartbeat", json={"leader_visible": True}, headers=headers)
        beat = threading.Thread(target=heartbeat, daemon=True)
        beat.start()
        yield client, a, b, command, {"x-orchid-token": token}
        beating.set()
        beat.join()


def teach(engine, controls):
    home = engine.repo.get("teach_home")["point"]

    def lowered(lift, wrist):
        return {"goal": {**home["goal"], "shoulder_lift": home["goal"]["shoulder_lift"] + lift,
                         "wrist_flex": home["goal"]["wrist_flex"] + wrist}, "measured": deepcopy(home["measured"])}
    for control in controls:
        entry = {"format": WAYPOINT_FORMAT, "control_id": control, "mode": "simulation",
                 "points": {"home": home, "hover": lowered(8, 0), "touch": lowered(8, 3), "press": lowered(8, 4)},
                 "calibration_sha256": m.fingerprint(engine.calibration), "fixture_id": engine.fixture["id"],
                 "verification": {"successful_trials": 0}}
        engine.save_control_entry(control, entry)


def test_a_chord_presses_the_button_first_and_lets_go_once_the_key_is_down(plate):
    client, a, b, command, auth = plate
    order = []  # (arm, what) as each arm's worker starts and finishes its part
    for engine in (a, b):
        original = engine.transition
        engine.transition = lambda phase, message, engine=engine, original=original: (
            order.append((engine.arm_id, phase, engine.play_t if phase != "teach_play" else None)), original(phase, message))[1]
    played = client.post("/api/chords/play", json={"key": "C", "chord": "chord.maj"}, headers=auth)
    assert played.status_code == 200, played.text
    body = played.json()
    assert body["message"].startswith("Played C + Maj; both arms home. At 120 BPM") and body["clearance_mm"] >= kinematics.CLEARANCE_MM
    moves = [(arm, phase) for arm, phase, _ in order]
    # B presses and holds, then A plays, then B lets go while A is still playing, then both hold at home.
    assert moves.index(("b", "teach_hold")) < moves.index(("a", "teach_play"))
    release = len(moves) - 1 - moves[::-1].index(("b", "teach_play"))
    assert moves.index(("a", "teach_play")) < release < moves.index(("a", "teach_hold"))
    assert a.phase == b.phase == "teach_hold" and b.chord_held is None and a.teach_played == "C"
    assert a.parked_now[0] and b.parked_now[0] and a.rig.chord is None


def test_chords_in_a_sequence_and_refusals(plate, monkeypatch):
    client, a, b, command, auth = plate
    seq = client.post("/api/sequence", json={"steps": [{"control": "C", "chord": "chord.maj"}, {"control": "D"},
                                                       {"control": "D", "chord": "chord.min"}]}, headers=auth)
    assert seq.status_code == 200, seq.text
    assert seq.json()["message"].startswith("Played C + Maj → D → D + Min; holding at home. At 120 BPM")
    untaught = client.post("/api/chords/play", json={"key": "C", "chord": "chord.sus"}, headers=auth)
    assert untaught.status_code == 409 and "Suspended has not been taught" in untaught.json()["detail"]
    wrong = client.post("/api/chords/play", json={"key": "chord.maj", "chord": "C"}, headers=auth)
    assert wrong.status_code == 409 and "Choose a key" in wrong.json()["detail"]
    monkeypatch.setattr(kinematics, "CLEARANCE_MM", 10_000)
    close = client.post("/api/chords/play", json={"key": "C", "chord": "chord.maj"}, headers=auth)
    assert close.status_code == 409 and "would bring the arms within 0 mm" in close.json()["detail"]
    assert b.phase == "teach_hold" and b.chord_held is None  # nothing moved


def test_a_failed_key_lets_the_chord_button_go(plate, monkeypatch):
    client, a, b, command, auth = plate

    motion = a.control_motion

    def refuse(control, args, speed=1.0, after=...):
        if "sound_s" not in args:  # the clearance check models the path; only the play itself fails
            return motion(control, args, speed, after)
        raise m.SafetyError(f"{control} cannot play right now.")
    monkeypatch.setattr(a, "control_motion", refuse)
    refused = client.post("/api/chords/play", json={"key": "C", "chord": "chord.maj"}, headers=auth)
    assert refused.status_code == 409 and refused.json()["detail"] == "Keys Arm: C cannot play right now."
    until = time.monotonic() + 10
    while time.monotonic() < until and not (b.phase == "teach_hold" and b.parked_now[0]):
        time.sleep(0.01)
    assert b.chord_held is None and b.parked_now[0] and a.rig.chord is None  # B let go and went home


def test_the_rig_refuses_other_moves_while_a_chord_holds_and_only_releases_a_held_button(plate):
    client, a, b, command, auth = plate
    b.api_submit("chord_press", {"control": "chord.maj"})  # B holds Maj on its own (no chord run)
    until = time.monotonic() + 10
    while time.monotonic() < until and not (b.phase == "teach_hold" and b.chord_held):
        time.sleep(0.01)
    refused = client.post("/api/controls/C/play", json={}, headers=auth)
    assert refused.status_code == 409 and "Chord Arm is away from its home" in refused.json()["detail"]
    command(b, "chord_release")
    until = time.monotonic() + 10  # back home and published as parked (the interlock reads that, a tick after the phase)
    while time.monotonic() < until and not (b.phase == "teach_hold" and b.parked_now[0]):
        time.sleep(0.01)
    assert b.chord_held is None
    assert client.post("/api/controls/C/play", json={}, headers=auth).status_code == 200


def test_the_key_check_reads_a_chord_by_its_tones():
    step = {"key": "A", "chord": "chord.min", "start": 0.0, "end": 2.0, "marks": {}}
    press = {"type": "press", "t": 1.0, "name": "E", "notes": [52, 57, 60], "velocity": 70}  # E A C: A minor, inverted
    assert check_step(step, 0.5, 1.0, [press])["status"] == "ok"
    assert check_step(step, 0.5, 1.0, [{**press, "notes": [57, 61, 64]}])["status"] == "wrong_chord"  # A major
    assert check_step(step, 0.5, 1.0, [{**press, "name": "A", "notes": [57]}])["status"] == "no_chord"
    assert check_step(step, 0.5, 1.0, [])["status"] == "missed"
    checker = KeyChecker(lambda: [press], settle_s=0, background=False)
    checker.request({"play": 1, "started": 0.5, "speed": 1.0, "steps": [step]})
    assert checker.poll()["summary"].startswith("A + Min ✓ E A C")


def test_the_model_finds_a_joint_move_that_sags_below_both_its_ends():
    calibration = {name: {"range_min": 0, "range_max": 4094} for name in m.MOTORS}
    pose = {name: 0.0 for name in m.MOTORS}
    side = {**pose, "shoulder_pan": -40.0}
    assert kinematics.dip(pose, pose, calibration, "a") == 0.0
    assert kinematics.dip(side, {**pose, "shoulder_pan": 40.0}, calibration, "a") == pytest.approx(0.0, abs=0.01)  # a turn: level
    folded = {**pose, "shoulder_pan": -27.0, "shoulder_lift": 82.0, "elbow_flex": 87.0, "wrist_flex": 62.0}
    reaching = {**pose, "shoulder_pan": 10.0, "shoulder_lift": 67.0, "elbow_flex": -62.0, "wrist_flex": -66.0}
    assert kinematics.dip(folded, reaching, calibration, "a") > kinematics.HOP_DIP_MM


def test_the_model_keeps_two_arms_apart_and_measures_them():
    calibration = {name: {"range_min": 0, "range_max": 4094} for name in m.MOTORS}
    pose = dict.fromkeys(m.MOTORS, 0.0)
    near = kinematics.clearance([pose], calibration, [pose], calibration)
    assert 0 < near < 1000  # the mounts are 400 mm apart
    assert kinematics._segment_distance((0, 0, 0), (10, 0, 0), (5, 5, 0), (5, 10, 0)) == pytest.approx(5)
    assert kinematics._segment_distance((0, 0, 0), (10, 0, 0), (20, 0, 0), (30, 0, 0)) == pytest.approx(10)


def test_a_chord_sweeps_on_for_bars(plate):
    client, a, b, command, auth = plate
    started = time.monotonic()
    played = client.post("/api/chords/play", json={"key": "C", "chord": "chord.maj", "duration": "2bars"},
                         headers=auth)
    assert played.status_code == 200, played.text
    assert played.json()["duration"] == {"beats": 8.0, "seconds": 4.0}  # two 4/4 bars at 120 BPM
    assert time.monotonic() - started >= 4.0 and b.parked_now[0]  # held the whole time; the Chord Arm let go long before


def test_a_score_of_chords_and_keys_is_planned_then_played_and_reports_each_note(plate):
    client, a, b, command, auth = plate
    events = [{"bar": 1, "beat": 1, "key": "C", "chord": "chord.maj", "duration": "1/2"},
              {"bar": 2, "beat": 1, "key": "D"},
              {"bar": 3, "beat": 1, "key": "D", "chord": "chord.min", "duration": "1"}]
    planned = client.post("/api/score", json={"events": events, "plan_only": True}, headers=auth)
    assert planned.status_code == 200, planned.text
    body = planned.json()
    assert body["status"] == "planned" and body["plan"]["events"] == 3 and body["plan"]["bpm"] == 120
    assert a.phase == b.phase == "teach_hold" and a.teach_played is None  # nothing moved
    played = client.post("/api/score", json={"events": events}, headers=auth)
    assert played.status_code == 200, played.text
    body = played.json()
    assert [(p["bar"], p["key"], p["chord"]) for p in body["played"]] == [(1, "C", "chord.maj"), (2, "D", None), (3, "D", "chord.min")]
    assert all(p["slipped_beats"] is not None for p in body["played"]) and "3 notes at 120 BPM" in body["message"]
    assert a.parked_now[0] and b.parked_now[0]
    # Between notes the Keys Arm waited over the last key and went straight on, the chord button going down meanwhile.
    plays = [e["message"] for e in a.events if e["message"].startswith("Playing ")][:3][::-1]
    assert "C's hover" not in plays[0] and "C's hover → hover" in plays[1] and "D's hover → hover" in plays[2], plays
    clash = client.post("/api/score", json={"events": events + [{"bar": 2, "beat": 1, "key": "E"}]}, headers=auth)
    assert clash.status_code == 422 and "Two notes at bar 2 beat 1" in clash.json()["detail"]


def test_a_score_sets_sound_perform_and_fx_at_the_start_and_changes_them_on_their_bar(plate):
    client, a, b, command, auth = plate
    events = [{"bar": 1, "beat": 1, "key": "C", "chord": "chord.maj", "duration": "1/2"},
              {"bar": 3, "beat": 1, "key": "D", "chord": "chord.min", "duration": "1/2"}]
    body = {"events": events, "sound": 12, "perform": {"mode": "arp"},
            "fx": {"reverb": {"mix": 30, "room": "hall"}},
            "changes": [{"bar": 3, "perform": {"mode": "harp"}, "fx": {"delay": {"mix": 20, "beats": 0.5}}}]}
    planned = client.post("/api/score", json={**body, "plan_only": True}, headers=auth).json()
    assert planned["plan"]["changes"] == [{"bar": 3, "beat": 1, "perform": {"mode": "harp"}, "fx": {"delay": {"mix": 20, "beats": 0.5}}}]
    assert client.studio_calls == []  # a plan changes nothing
    played = client.post("/api/score", json=body, headers=auth)
    assert played.status_code == 200, played.text
    result = played.json()
    sent = [(c, f) for c, f, _ in client.studio_calls]
    assert sent[:4] == [("status", {}), ("layer-preset", {"slot": 5, "preset": 12}),
                        ("perform-configure", {"settings": {"mode": "arp"}}), ("fx", {"slot": 5, "reverb": {"mix": 30, "room": "hall"}})]
    assert sent[4:] == [("perform-configure", {"settings": {"mode": "harp"}}), ("fx", {"slot": 5, "delay": {"mix": 20, "beats": 0.5}})]
    assert result["studio"]["start"] == ["sound 12 (voice 5)", "perform mode arp", "fx reverb {'mix': 30, 'room': 'hall'} (voice 5)"]
    fired = result["studio"]["fired"]
    assert len(fired) == 1 and fired[0]["bar"] == 3 and abs(fired[0]["late_ms"]) <= 60  # on the bar line it moved to
    change_at = client.studio_calls[4][2]
    second = result["rhythm"]["steps"][-1]["strike_at"]
    assert change_at == pytest.approx(second - 0.03, abs=0.06)  # D+min lands on that bar line, with its change


def test_studio_settings_need_orchid_studio(tmp_path):
    a = Engine(tmp_path, sleep=lambda s: time.sleep(min(s, 0.005)), owns=ROLES["a"])
    with TestClient(create_app(tmp_path, engines={"a": a}), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        refused = client.post("/api/score", json={"events": [{"bar": 1, "beat": 1, "key": "C"}], "sound": 3, "plan_only": True},
                              headers={"x-orchid-token": token})
        assert refused.status_code == 409 and "need Orchid Studio" in refused.json()["detail"]


def test_a_looped_score_hands_studio_this_pass_with_each_chord_on_its_written_beat(plate):
    """What Orchid sent on this pass, each chord moved onto its written beat: a slightly late arm does not matter to
    the loop. A note nothing sounded for is left out."""
    from orchid_demo.keycheck import KeyChecker
    client, a, b, command, auth = plate
    sent = {"C": ([48, 52, 55], 70), "D": ([62], 50)}  # what Orchid sends for each (C with Maj held)
    silent = set()

    def orchid():  # the press arrives 40 ms after it was due: a little late
        step = a.play_steps[0]
        if step["key"] in silent:
            return []
        notes, velocity = sent[step["key"]]
        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        return [{"type": "press", "t": step["due"] + 0.04, "name": names[notes[0] % 12], "notes": notes, "velocity": velocity}]
    a.key_checker = KeyChecker(orchid, settle_s=0, background=False)
    events = [{"bar": 1, "beat": 1, "key": "C", "chord": "chord.maj", "duration": "2bars"},
              {"bar": 3, "beat": 3, "key": "D", "duration": "1/2"}]
    played = client.post("/api/score", json={"events": events, "loop_slot": 2}, headers=auth)
    assert played.status_code == 200, played.text
    result = played.json()
    composed = [(f, t) for c, f, t in client.studio_calls if c == "loop-compose"]
    assert len(composed) == 1
    fields, sent_at = composed[0]
    assert fields == {"slot": 2, "chords": [{"beat": 0.0, "duration": 8.0, "notes": [48, 52, 55], "velocity": 70},
                                            {"beat": 10.0, "duration": 2.0, "notes": [62], "velocity": 50}]}
    assert sent_at > result["rhythm"]["steps"][-1]["strike_at"]  # after the pass: it lands on the next boundary
    loop = result["studio"]["loop"]
    assert loop["kept"] and loop["slot"] == 2 and loop["bars"] == 4 and loop["applies_at_beat"] == 16 and loop["missed"] == []
    silent.add("D")
    result = client.post("/api/score", json={"events": events, "loop_slot": 2}, headers=auth).json()
    composed = [f for c, f, t in client.studio_calls if c == "loop-compose"]
    assert composed[-1]["chords"] == [{"beat": 0.0, "duration": 8.0, "notes": [48, 52, 55], "velocity": 70}]
    assert result["studio"]["loop"]["missed"] == [{"bar": 3, "beat": 3, "key": "D", "chord": None}]
    silent.discard("D")
    client.post("/api/score", json={"events": events, "loop_slot": 2, "loop_velocity": 96}, headers=auth)
    composed = [f for c, f, t in client.studio_calls if c == "loop-compose"]
    assert [c["velocity"] for c in composed[-1]["chords"]] == [96, 96]  # Orchid's notes, at the layer's level
    assert [c["notes"] for c in composed[-1]["chords"]] == [[48, 52, 55], [62]]
    too_long = client.post("/api/score", json={"events": events + [{"bar": 5, "beat": 1, "key": "C"}], "loop_slot": 1,
                                               "plan_only": True}, headers=auth)
    assert too_long.status_code == 409 and "past the end of Studio's 4-bar loop" in too_long.json()["detail"]



def test_one_pass_feeds_several_loop_layers_each_in_its_register_hold_and_sound(plate):
    """Short, on-time arm notes; the pad layer holds each chord to the next (legato), the bass layer is the same pass two
    octaves down, an overdub adds to what the layer already plays, and each layer gets its own sound."""
    from orchid_demo.keycheck import KeyChecker
    client, a, b, command, auth = plate
    sent = {"C": ([60, 64, 67], 40), "D": ([62], 40)}
    a.key_checker = KeyChecker(lambda: [{"type": "press", "t": a.play_steps[0]["due"], "name": a.play_steps[0]["key"],
                                         "notes": sent[a.play_steps[0]["key"]][0], "velocity": 40}], settle_s=0, background=False)
    events = [{"bar": 1, "beat": 1, "key": "C", "chord": "chord.maj", "duration": "1/4"},
              {"bar": 3, "beat": 1, "key": "D", "duration": "1/4"}]
    client.exported[:] = [{"slot": 1, "chords": [{"beat": 4.0, "duration": 2.0, "note": 55, "velocity": 70},
                                                 {"beat": 4.0, "duration": 2.0, "note": 59, "velocity": 70},
                                                 {"beat": 8.0, "duration": 1.0, "note": 50, "velocity": 70}]}]
    client.looper["pending_composes"] = []
    client.looper["monitor_slot"] = 5
    calls = client.studio_calls
    body = {"events": events, "sound": 52, "loop_layers": [
        {"slot": 1, "hold": "legato", "merge": True, "velocity": 72, "perform": {"mode": "off"}},
        {"slot": 3, "transpose": -24, "hold": 2, "sound": 45, "fx": {"reverb": {"mix": 8}}}]}
    played = client.post("/api/score", json=body, headers=auth)
    assert played.status_code == 200, played.text
    composed = {f["slot"]: f for c, f, _ in calls if c == "loop-compose"}
    # the pad: the overdub keeps the layer's chord at beat 4, the pass replaces the one at beat 8, all held legato
    assert composed[1]["chords"] == [{"beat": 0.0, "duration": 4.0, "notes": [60, 64, 67], "velocity": 72},
                                     {"beat": 4.0, "duration": 4.0, "notes": [55, 59], "velocity": 70},
                                     {"beat": 8.0, "duration": 8.0, "notes": [62], "velocity": 72}]
    assert composed[1]["settings"] == {"mode": "off"}
    # the bass: two octaves down, each note held two beats, at the press velocity
    assert composed[3]["chords"] == [{"beat": 0.0, "duration": 2.0, "notes": [36, 40, 43], "velocity": 40},
                                     {"beat": 8.0, "duration": 2.0, "notes": [38], "velocity": 40}]
    sent_settings = [(c, f) for c, f, _ in calls]
    assert ("loop-monitor", {"slot": 1}) in sent_settings  # the pass is heard through the pad layer's own voice
    assert ("layer-preset", {"slot": 3, "preset": 45}) in sent_settings and ("layer-preset", {"slot": 1, "preset": 52}) in sent_settings
    assert ("layer-preset", {"slot": 3, "preset": 52}) not in sent_settings  # the bass has its own sound
    loop = played.json()["studio"]["loop"]
    assert loop["kept"] and loop["slots"] == [1, 3] and [entry["slot"] for entry in loop["layers"]] == [1, 3]
    del client.looper["pending_composes"]  # an older Studio drops all but the last composition waiting for a boundary
    refused = client.post("/api/score", json=body, headers=auth)
    assert refused.status_code == 409 and "one waiting composition" in refused.json()["detail"]
    one = client.post("/api/score", json={**body, "loop_layers": body["loop_layers"][:1]}, headers=auth)
    assert one.status_code == 200, one.text
    both = client.post("/api/score", json={**body, "loop_slot": 2}, headers=auth)
    assert both.status_code == 422


def test_loop_layer_hold_and_merge_shape_the_chords():
    from orchid_demo import score
    chords = [{"beat": 8.0, "duration": 1.0, "notes": [62], "velocity": 80}, {"beat": 0.0, "duration": 0.5, "notes": [60], "velocity": 80}]
    assert [c["duration"] for c in score.hold(chords, "legato", 32)] == [8.0, 24.0]
    assert [c["duration"] for c in score.hold(chords, 12, 32)] == [8.0, 12.0]
    assert [c["duration"] for c in score.hold(chords, "written", 32)] == [0.5, 1.0]
    assert score.merge([{"beat": 4, "duration": 2, "note": 67, "velocity": 50}, {"beat": 4, "duration": 2, "note": 64, "velocity": 60}],
                       chords) == [{"beat": 0.0, "duration": 0.5, "notes": [60], "velocity": 80},
                                   {"beat": 4.0, "duration": 2.0, "notes": [64, 67], "velocity": 60},
                                   {"beat": 8.0, "duration": 1.0, "notes": [62], "velocity": 80}]
    placed = [{"bar": 1, "beat": 1, "control": "C", "offset": 0.0, "beats": 1.0}]
    assert score.take(placed, [{"midi": [10, 60]}], transpose=-24) == ([{"beat": 0.0, "duration": 1.0, "notes": [36], "velocity": 80}], [])
    assert score.take(placed, [{"midi": [10]}], transpose=-24)[1][0]["key"] == "C"  # off MIDI's range: left out
    assert score.register([26, 29, 33], octave=3) == [50, 53, 57]  # Orchid set low: the pad still sits in octave 3
    assert score.register([62, 65, 69], octave=3) == [50, 53, 57] and score.register([62], octave=2) == [38]
    assert score.register([57, 60, 64], octave=5, transpose=-12) == [69, 72, 76]


def test_a_take_is_composed_for_the_end_of_the_pass_the_arms_played_on():
    """So the audience hears on the very next loop what it just saw the arms play, even if the arms got home late."""
    from orchid_demo import score
    grid, beat_s = {"t": 1000.0, "beat": 400.0}, 0.5  # 120 BPM: beat 400 at t=1000 s
    # the pass started at beat 416 (a 32-beat loop); its first note, written at bar 2 beat 3 (offset 6), struck at beat 422
    first = {"strike_at": 1000.0 + 22 * beat_s, "slipped_beats": 0}
    assert score.pass_end(first, 6.0, grid, beat_s, 32) == 448
    assert score.pass_end({**first, "strike_at": first["strike_at"] + 0.04}, 6.0, grid, beat_s, 32) == 448  # a hair late
    # a first note moved a bar later still belongs to the pass it was written in
    assert score.pass_end({"strike_at": 1000.0 + 26 * beat_s, "slipped_beats": 4}, 6.0, grid, beat_s, 32) == 448


def test_a_looped_scores_sound_and_fx_reach_its_loop_layers_voice_and_the_layer_gets_its_perform_mode(plate):
    """In a jam the loop layers are what plays on: sound and fx go to the layer's voice as well as the live voice."""
    from orchid_demo.keycheck import KeyChecker
    client, a, b, command, auth = plate
    a.key_checker = KeyChecker(lambda: [{"type": "press", "t": a.play_steps[0]["due"], "name": "C", "notes": [60], "velocity": 60}],
                               settle_s=0, background=False)
    body = {"events": [{"bar": 1, "beat": 1, "key": "C", "duration": "1"}], "loop_slot": 3, "sound": 21,
            "perform": {"mode": "bloom"}, "fx": {"reverb": {"mix": 30, "room": "plate"}}}
    played = client.post("/api/score", json=body, headers=auth)
    assert played.status_code == 200, played.text
    sent = [(c, f) for c, f, _ in client.studio_calls]
    assert ("layer-preset", {"slot": 5, "preset": 21}) in sent and ("layer-preset", {"slot": 3, "preset": 21}) in sent
    assert ("fx", {"slot": 5, "reverb": {"mix": 30, "room": "plate"}}) in sent and ("fx", {"slot": 3, "reverb": {"mix": 30, "room": "plate"}}) in sent
    composed = [f for c, f in sent if c == "loop-compose"]
    assert composed[-1]["slot"] == 3 and composed[-1]["settings"] == {"mode": "bloom"}


def test_a_score_sets_orchids_voicing_in_software_at_the_start_and_on_its_bar_line(plate):
    """Voicing moves the chords up and down the keyboard: Studio sets it (chord-voicing); the arms never turn the dial."""
    client, a, b, command, auth = plate
    events = [{"bar": 1, "beat": 1, "key": "C", "duration": "1"}, {"bar": 2, "beat": 1, "key": "D", "duration": "1"}]
    planned = client.post("/api/score", json={"events": events, "voicing": 20, "changes": [{"bar": 2, "voicing": 44}],
                                              "plan_only": True}, headers=auth).json()
    assert planned["plan"]["changes"] == [{"bar": 2, "beat": 1, "voicing": 44}]
    played = client.post("/api/score", json={"events": events, "voicing": 20, "changes": [{"bar": 2, "voicing": 44}]},
                         headers=auth)
    assert played.status_code == 200, played.text
    sent = [f["value"] for c, f, _ in client.studio_calls if c == "chord-voicing"]
    assert sent == [20, 44]
    for bad in (0, 61):
        assert client.post("/api/score", json={"events": events, "voicing": bad, "plan_only": True}, headers=auth).status_code == 422
    layer = client.post("/api/score", json={"events": events, "loop_layers": [{"slot": 1, "voicing": 20}], "plan_only": True},
                        headers=auth)
    assert layer.status_code == 422  # voicing is Orchid's, not a loop layer's


def test_a_program_sends_both_arms_home_and_orchid_robot_moves_them_one_at_a_time(plate):
    client, a, b, command, auth = plate
    home = client.post("/api/home", json={}, headers=auth)
    assert home.status_code == 200, home.text
    assert "Keys Arm: At home" in home.json()["message"] and "Chord Arm: At home" in home.json()["message"]
    assert a.parked_now[0] and b.parked_now[0]
    rest = client.post("/api/rest?arm=b", json={}, headers=auth)
    assert rest.status_code == 409 and "Chord Arm" in rest.json()["detail"]  # no rest taught: refused, nothing guessed
