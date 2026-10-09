"""HTTP control API (list, configure, play, sequence) against the running worker, in simulation."""
from copy import deepcopy
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from orchid_demo import motion as m
from orchid_demo.api import create_app
from orchid_demo.engine import Engine, WAYPOINT_FORMAT


@pytest.fixture
def console(tmp_path):
    engine = Engine(tmp_path, sleep=lambda s: time.sleep(min(s, 0.005)))  # real clock, fast simulated sweeps
    with TestClient(create_app(tmp_path, engine=engine), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"x-orchid-token": token, "x-orchid-operator": str(uuid4())}

        def command(action, **args):
            client.post("/api/heartbeat", json={"leader_visible": True}, headers=headers)
            revision = client.get("/api/state").json()["revision"]
            identity = str(uuid4())
            response = client.post("/api/commands", json={"id": identity, "action": action, "revision": revision, "args": args},
                                   headers=headers)
            assert response.status_code == 202, response.text
            until = time.monotonic() + 10
            while time.monotonic() < until and engine.receipts[identity]["status"] == "queued":
                time.sleep(0.01)
            assert engine.receipts[identity]["status"] == "complete", engine.receipts[identity]
            client.post("/api/heartbeat", json={"leader_visible": True}, headers=headers)

        beating = threading.Event()

        def heartbeat():  # the console page keeps the lease while it is open, however long a play takes
            while not beating.wait(0.2):
                client.post("/api/heartbeat", json={"leader_visible": True}, headers=headers)
        beat = threading.Thread(target=heartbeat, daemon=True)
        beat.start()
        client.close_console = lambda: (beating.set(), beat.join())
        yield client, engine, command, {"x-orchid-token": token}
        beating.set()
        beat.join()


def taught(engine, command):
    command("connect", prepared=True, fixture="Test", teaching_mode="leader")
    for target in ("follower", "leader"):
        command("calibrate", supported=True, target=target)
        command("calibration_center", supported=True)
        for _ in range(5):
            command("simulate_sweep")
            command("calibration_next", range_complete=True)
        command("calibration_save", range_complete=True)
    command("teach_begin", control="C")  # holding; home saved here
    home = engine.repo.get("teach_home")["point"]
    def lowered(lift, wrist):
        return {"goal": {**home["goal"], "shoulder_lift": home["goal"]["shoulder_lift"] + lift,
                         "wrist_flex": home["goal"]["wrist_flex"] + wrist}, "measured": deepcopy(home["measured"])}
    for key in ("C", "D"):
        engine.repo.save_note(key, {"format": WAYPOINT_FORMAT, "control_id": key, "mode": "simulation",
                                    "points": {"home": home, "hover": lowered(8, 0), "touch": lowered(8, 3), "press": lowered(8, 4)},
                                    "calibration_sha256": m.fingerprint(engine.calibration), "fixture_id": engine.fixture["id"],
                                    "verification": {"successful_trials": 0}})
    engine.notes = engine.repo.notes()
    until = time.monotonic() + 3  # the worker republishes the snapshot on its next tick
    while time.monotonic() < until and engine.snapshot()["keys"]["D"]["status"] != "registered":
        time.sleep(0.01)


def test_list_play_and_configure_through_the_api(console):
    client, engine, command, auth = console
    taught(engine, command)
    listing = client.get("/api/controls").json()
    assert listing["ready_to_play"] is True and listing["settings"] == {"speed": 1.0, "press_s": 0.3, "press_hardness": 0.5}
    c = next(x for x in listing["controls"] if x["id"] == "C")
    assert c["status"] == "registered" and c["kind"] == "key"

    played = client.post("/api/controls/C/play", json={"speed": 3.0, "press_s": 0.5}, headers=auth)
    assert played.status_code == 200, played.text
    body = played.json()
    assert body["phase"] == "teach_hold" and body["message"].startswith("Played C")  # waited until done
    assert engine.notes["C"]["press_s"] == 0.5

    assert client.post("/api/controls/C", json={"press_s": 1.2}, headers=auth).status_code == 200
    assert engine.notes["C"]["press_s"] == 1.2
    assert client.post("/api/settings", json={"speed": 2.5}, headers=auth).json()["status"] == "complete"
    assert client.get("/api/controls").json()["settings"]["speed"] == 2.5
    assert client.post("/api/settings", json={"press_hardness": 0.3}, headers=auth).json()["status"] == "complete"
    assert client.get("/api/controls").json()["settings"]["press_hardness"] == 0.3
    assert client.post("/api/settings", json={"press_hardness": 0.05}, headers=auth).status_code == 422

    seq = client.post("/api/sequence", json={"steps": [{"control": "C"}, {"control": "D", "press_s": 0}], "speed": 3.0},
                      headers=auth).json()
    assert seq["message"].startswith("Played C → D")


def test_api_refusals(console):
    client, engine, command, auth = console
    taught(engine, command)
    assert client.post("/api/controls/E/play", json={}, headers=auth).status_code == 409  # not taught
    assert client.post("/api/controls/C/play", json={"speed": 9}, headers=auth).status_code == 422  # out of range
    assert client.post("/api/controls/C/play", json={}).status_code == 403  # no token
    client.close_console()
    engine.lease_until = time.monotonic() - 1  # the console closed
    refused = client.post("/api/controls/C/play", json={}, headers=auth)
    assert refused.status_code == 409 and "operator console" in refused.json()["detail"]


def test_api_says_how_to_get_the_arm_holding(console):
    client, engine, command, auth = console
    taught(engine, command)
    command("release", supported=True)
    refused = client.post("/api/controls/C/play", json={}, headers=auth)
    assert refused.status_code == 409 and "not holding" in refused.json()["detail"] and "Play" in refused.json()["detail"]
    assert client.post("/api/stop", json={}, headers=auth).status_code == 200  # Stop while not holding stops the arm
    until = time.monotonic() + 3
    while time.monotonic() < until and engine.phase != "fault":
        time.sleep(0.01)
    refused = client.post("/api/controls/C/play", json={}, headers=auth)
    assert refused.status_code == 409 and "Hold & keep playing" in refused.json()["detail"]


def test_play_reports_what_orchid_sent(console):
    from orchid_demo.keycheck import KeyChecker, StudioKeys
    client, engine, command, auth = console
    taught(engine, command)
    engine.key_checker = KeyChecker(lambda: [{"type": "press", "t": engine.teach.play_started + 0.3, "name": "C",
                                              "velocity": 50, "octave": 3}], settle_s=0)
    body = client.post("/api/controls/C/play", json={"speed": 3.0}, headers=auth).json()
    assert body["key_check"]["status"] == "ok" and body["key_check"]["summary"].startswith("C ✓ velocity 50")
    engine.key_checker = KeyChecker(StudioKeys(port=1), settle_s=0)  # Orchid Studio not running
    body = client.post("/api/controls/C/play", json={"speed": 3.0}, headers=auth).json()
    assert body["phase"] == "teach_hold" and body["key_check"]["status"] == "unavailable"
    assert "key_check" not in client.post("/api/controls/C", json={"press_s": 0.4}, headers=auth).json()


def test_cli_names_and_steps():
    import importlib.util
    spec = importlib.util.spec_from_file_location("orchid_cli", "scripts/orchid.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert [cli.control_id(x) for x in ("c", "c#", "C#", "cw", "ccw", "min", "M7", "m7")] == \
        ["C", "C#", "C#", "voicing.cw", "voicing.ccw", "chord.min", "chord.M7", "chord.m7"]


def test_two_followers_share_one_console(tmp_path):
    from orchid_demo.rig import ROLES, LeaderStore
    a = Engine(tmp_path, sleep=lambda s: time.sleep(min(s, 0.005)), owns=ROLES["a"])
    b = Engine(tmp_path / "arm-b", sleep=lambda s: time.sleep(min(s, 0.005)), owns=ROLES["b"], leader_store=LeaderStore(a.repo))
    with TestClient(create_app(tmp_path, engines={"a": a, "b": b}), base_url="http://127.0.0.1") as client:
        session = client.get("/api/session?arm=b").json()
        assert session["state"]["arm"] == "b" and set(session["arms"]) == {"a", "b"}
        assert session["arms"]["a"]["owns"] == list(ROLES["a"]) and session["arms"]["b"]["parked"] is True
        assert client.get("/api/session?arm=c").status_code == 404
        auth = {"x-orchid-token": session["token"], "x-orchid-operator": str(uuid4())}
        assert client.post("/api/heartbeat", json={}, headers=auth).status_code == 200
        assert a.owner == b.owner  # one console operates both followers
        mixed = client.post("/api/sequence", json={"steps": [{"control": "C"}, {"control": "chord.maj"}]}, headers=auth)
        assert mixed.status_code == 409 and "No arm is connected" in mixed.json()["detail"]  # played step by step, Keys Arm first
        refused = client.post("/api/controls/chord.maj/play", json={}, headers=auth)
        assert refused.status_code == 409 and "No arm is connected" in refused.json()["detail"]  # routed to Chord Arm
        listing = client.get("/api/controls").json()
        assert {c["id"]: c["arm"] for c in listing["controls"]}["voicing.cw"] == "b" and set(listing["ready_to_play"]) == {"a", "b"}
        command_id = str(uuid4())
        stop = client.post("/api/commands", json={"id": command_id, "action": "stop", "revision": 0, "args": {}, "arm": "b"}, headers=auth)
        assert stop.status_code == 202 and all(command_id in member.receipts for member in (a, b))  # Stop reaches both
