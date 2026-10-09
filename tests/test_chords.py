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
    with TestClient(create_app(tmp_path, engines={"a": a, "b": b}), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"x-orchid-token": token, "x-orchid-operator": str(uuid4())}

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
    played = client.post("/api/chords/play", json={"key": "C", "chord": "chord.maj", "speed": 3.0}, headers=auth)
    assert played.status_code == 200, played.text
    body = played.json()
    assert body["message"] == "Played C + Maj; both arms home." and body["clearance_mm"] >= kinematics.CLEARANCE_MM
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
                                                       {"control": "D", "chord": "chord.min"}], "speed": 3.0}, headers=auth)
    assert seq.status_code == 200, seq.text
    assert seq.json()["message"] == "Played C + Maj → D → D + Min; holding at home."
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

    def refuse(control, args):
        raise m.SafetyError(f"{control} cannot play right now.")
    monkeypatch.setattr(a, "control_motion", refuse)
    refused = client.post("/api/chords/play", json={"key": "C", "chord": "chord.maj", "speed": 3.0}, headers=auth)
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
    until = time.monotonic() + 10
    while time.monotonic() < until and b.phase != "teach_hold":
        time.sleep(0.01)
    assert b.chord_held is None
    assert client.post("/api/controls/C/play", json={"speed": 3.0}, headers=auth).status_code == 200


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


def test_the_model_keeps_two_arms_apart_and_measures_them():
    calibration = {name: {"range_min": 0, "range_max": 4094} for name in m.MOTORS}
    pose = dict.fromkeys(m.MOTORS, 0.0)
    near = kinematics.clearance([pose], calibration, [pose], calibration)
    assert 0 < near < 1000  # the mounts are 400 mm apart
    assert kinematics._segment_distance((0, 0, 0), (10, 0, 0), (5, 5, 0), (5, 10, 0)) == pytest.approx(5)
    assert kinematics._segment_distance((0, 0, 0), (10, 0, 0), (20, 0, 0), (30, 0, 0)) == pytest.approx(10)
