"""Two followers on one plate: who plays what, the leader handed between them, and the interlock that keeps them apart."""
from copy import deepcopy

import pytest

from orchid_demo import teach
from orchid_demo.engine import Engine
from orchid_demo.motion import SafetyError
from orchid_demo.rig import ROLES, LeaderStore, Rig
from test_operator_engine import Clock, calibrate, command, connect, reject
from test_teach_app import following, run, teach_points


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    a = Engine(tmp_path, clock=clock, sleep=clock.sleep, owns=ROLES["a"])
    b = Engine(tmp_path / "arm-b", clock=clock, sleep=clock.sleep, owns=ROLES["b"], leader_store=LeaderStore(a.repo))
    r = Rig()
    r.add("a", a)
    r.add("b", b)
    yield r, a, b
    a.close()
    b.close()


def tick(*engines, seconds=0.2):
    for _ in range(int(seconds / teach.PERIOD)):
        for e in engines:
            e.heartbeat("operator")
        engines[0].clock.sleep(teach.PERIOD)
        for e in engines:
            e.step()


def a_teaches_c_then_hands_over_the_leader(a, b):
    """A: leader-taught C, holding at home. B: connected, calibrated, then given the same leader."""
    following(a)
    teach_points(a, "C")
    run(a, 10, until=lambda: a.phase == "teach_hold")
    connect(b)
    calibrate(b)
    tick(a, b)
    command(a, "release", supported=True)
    command(a, "leader_detach")
    assert a.leader is None and a.teaching_mode == "manual"
    command(b, "refresh_leader_ports")
    command(b, "connect_leader", prepared=True, leader_port="simulator-leader")
    assert b.leader_calibrated  # the leader calibration A saved is shared


def test_each_arm_plays_only_its_own_controls(rig):
    r, a, b = rig
    assert Rig.control_arm("C") == "a" and Rig.control_arm("chord.maj") == "b" and Rig.control_arm("voicing.cw") == "b"
    with pytest.raises(SafetyError, match="played by the other arm"):
        a.teach_control({"control": "chord.maj"})
    with pytest.raises(SafetyError, match="played by the other arm"):
        b.teach_control({"control": "C"})


def test_the_shared_leader_moves_between_followers_and_b_sets_its_own_home(rig):
    r, a, b = rig
    a_teaches_c_then_hands_over_the_leader(a, b)
    command(b, "teach_begin", control="chord.maj", follow=True)
    tick(a, b, seconds=3)
    command(b, "teach_set_home")
    command(b, "teach_hold")
    tick(a, b)
    assert b.repo.get("teach_home") and a.repo.get("teach_home") != b.repo.get("teach_home")
    assert b.parked_now == (True, "at home")


def test_an_arm_moves_only_while_the_other_is_parked_at_home(rig):
    r, a, b = rig
    a_teaches_c_then_hands_over_the_leader(a, b)
    command(a, "teach_begin", control="C", supported=True)  # a hold only: allowed anywhere
    tick(a, b)
    assert b.parked_now[0] is False and "no home" in b.parked_now[1]
    assert "Chord Arm is not parked at its home" in reject(a, "teach_play", control="C")["message"]

    command(b, "teach_begin", control="chord.maj", follow=True)  # B leaves home: A is parked
    tick(a, b, seconds=3)
    command(b, "teach_set_home")
    command(b, "teach_hold")
    tick(a, b)
    assert b.parked_now[0] and r.holder is None

    command(a, "teach_play", control="C")  # A leaves home and holds the claim while it is away
    tick(a, b)
    assert r.holder == "a" and a.parked_now == (False, "moving")
    assert "Keys Arm" in reject(b, "teach_follow", control="chord.maj")["message"]
    run(a, 30, until=lambda: a.phase == "teach_hold")
    tick(a, b)
    assert r.holder is None and a.parked_now[0]
    command(b, "teach_follow", control="chord.maj")  # A is home again: B may move


def test_going_home_needs_only_the_other_arm_to_hold_still(rig):
    r, a, b = rig
    a_teaches_c_then_hands_over_the_leader(a, b)
    command(a, "teach_begin", control="C", supported=True)
    tick(a, b)
    assert not b.parked_now[0]  # B has no home yet, but it is still: A may go home
    command(b, "teach_begin", control="chord.maj", follow=True)  # A is parked, so B may move
    tick(a, b, seconds=0.5)
    assert b.parked_now == (False, "moving")
    assert "Chord Arm is away from its home (moving)" in reject(a, "teach_go_home")["message"]


def test_recentring_one_follower_leaves_the_shared_leader_and_follows_it_half_a_turn_round(rig):
    """The Keys Arm's wrist works at the end of its rotation: re-centre it alone. The leader's calibration (shared with
    the Chord Arm) is unchanged; the Keys Arm adds half a turn to the leader's wrist reading, so it follows the same
    physical turn, now mid-range, and its taught key keeps the same physical pose."""
    r, a, b = rig
    following(a)
    teach_points(a, "C")
    run(a, 10, until=lambda: a.phase == "teach_hold")
    before = {"leader": deepcopy(a.leader_calibration), "roll": a.calibration["wrist_roll"]["homing_offset"],
              "C": deepcopy(a.notes["C"]["points"])}
    command(a, "release", supported=True)
    command(a, "recenter_wrist_roll", supported=True)
    assert "this follower (the shared leader is unchanged)" in a.message
    assert a.leader_calibration == before["leader"] and a.repo.get("leader_calibration") == before["leader"]
    assert a.calibration["wrist_roll"]["homing_offset"] != before["roll"]
    assert a.leader_roll_offset == pytest.approx(180.0, abs=0.1) or a.leader_roll_offset == pytest.approx(-180.0, abs=0.1)
    new = a.notes["C"]["points"]
    for name in ("hover", "touch", "press"):
        turned = (new[name]["goal"]["wrist_roll"] - before["C"][name]["goal"]["wrist_roll"]) % 360
        assert turned == pytest.approx(180.0, abs=0.1)
    leader_roll = a.leader.teleop_read()[1]["wrist_roll"]
    assert ((a.read_leader_joints()["wrist_roll"] - leader_roll) % 360) == pytest.approx(180.0, abs=0.1)
    assert b.leader_roll_offset == 0.0  # the Chord Arm follows the leader as before


@pytest.fixture
def dynamic(tmp_path):
    """Both followers as the app builds them: no fixed roles, Chord Arm's leader calibration shared from Keys Arm."""
    clock = Clock()
    a = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    b = Engine(tmp_path / "arm-b", clock=clock, sleep=clock.sleep, leader_store=LeaderStore(a.repo))
    r = Rig()
    r.add("a", a)
    r.add("b", b)
    yield r, a, b
    a.close()
    b.close()


def test_each_arm_is_only_ever_taught_its_own_controls(dynamic):
    r, a, b = dynamic
    assert a.owns == ROLES["a"] and b.owns == ROLES["b"]  # with or without the other arm connected
    connect(a)
    assert a.public["selected"] in ROLES["a"]
    for action, args in (("teach_begin", {"control": "chord.maj", "follow": True}), ("control_start", {"control": "voicing.cw"}),
                         ("teach_sequence", {"steps": [{"control": "C"}, {"control": "chord.min"}]}),
                         ("tune_start", {"controls": ["C", "chord.dim"], "beside_arm": True}),
                         ("teach_configure", {"control": "voicing.ccw", "turn_degrees": 10})):
        assert "played by the other arm" in reject(a, action, **args)["message"], action
    connect(b, port="simulator-2")
    assert "C is played by the other arm" in reject(b, "teach_play", control="C")["message"]
    assert r.split() and a.owns == ROLES["a"]
    command(b, "disconnect", supported=True)
    assert a.owns == ROLES["a"]  # still only the keys


def test_a_follower_carrying_arm_a_calibration_is_refused_as_arm_b(dynamic):
    r, a, b = dynamic
    a.calibration = {f"m{i}": {"id": i, "range_min": 1000 + i, "range_max": 3000 + i} for i in range(1, 7)}
    scanned = {"port": "/dev/ttyACM1", "role": "follower", "motor_ids": list(range(1, 7)), "voltage": 12.1,
               "limits": {str(i): [1000 + i, 3000 + i] for i in range(1, 7)}}
    as_b, as_a = b.port_entry(scanned), a.port_entry(scanned)
    assert as_b["arm"] == "a" and not as_b["connectable"] and "Connect it as the Keys Arm" in as_b["problem"]
    assert as_a["arm"] == "a" and as_a["connectable"]
    unknown = b.port_entry({**scanned, "limits": None})
    assert unknown["arm"] is None and unknown["connectable"]


def test_scans_skip_the_other_arms_ports_and_arm_b_appears_once_a_second_follower_is_seen(tmp_path):
    clock = Clock()
    seen = []
    def scanner(guard, exclude_ports=()):
        seen.append(tuple(exclude_ports))
        return {"arms": [{"port": "/dev/ttyACM1", "role": "follower", "motor_ids": list(range(1, 7)), "voltage": 12.0}],
                "warnings": []}
    a = Engine(tmp_path, "hardware", clock=clock, sleep=clock.sleep, port_scanner=scanner)
    b = Engine(tmp_path / "arm-b", "hardware", clock=clock, sleep=clock.sleep, port_scanner=scanner)
    r = Rig()
    r.add("a", a)
    r.add("b", b)
    try:
        assert not r.available("b", b)  # nothing seen yet: the console looks like one arm
        a.arm, a.follower_port = object(), "/dev/ttyACM0"  # Keys Arm connected on another port...
        a.leader, a.leader_port = object(), "/dev/ttyACM2"  # ...with the leader
        command(b, "refresh_ports")
        assert seen[-1] == ("/dev/ttyACM0", "/dev/ttyACM2")  # neither is probed
        assert r.available("b", b) and r.summary()["b"]["available"]
    finally:
        a.arm = a.leader = a.follower_port = None
        a.close()
        b.close()


def follower(path, arm=None, voltage=12.2):
    return {"port": path, "path": path, "role": "follower", "motor_ids": list(range(1, 7)), "voltage": voltage, "arm": arm}


def test_the_plan_sends_each_recognised_follower_home_and_a_new_one_to_the_free_arm(dynamic):
    r, a, b = dynamic
    leader = {"port": "/dev/L", "path": "/dev/L", "role": "leader", "motor_ids": list(range(1, 7)), "voltage": 5.0,
              "leader_connectable": True}
    r.scan = [follower("/dev/new"), follower("/dev/orig", "a"), leader]
    plan = r.plan()
    assert plan == {"a": {"port": "/dev/orig", "recognised": True, "teaching_mode": "leader", "leader_port": "/dev/L"},
                    "b": {"port": "/dev/new", "recognised": False, "teaching_mode": "manual", "leader_port": None}}
    assert r.plan("manual")["a"]["teaching_mode"] == "manual"
    r.scan = [follower("/dev/x", "b"), follower("/dev/y")]  # Chord Arm recognised; the unknown one fills Keys Arm
    assert {arm: step["port"] for arm, step in r.plan().items()} == {"a": "/dev/y", "b": "/dev/x"}
    r.scan = [{**follower("/dev/short"), "motor_ids": [1, 2, 3]}]  # a follower that is not ready is never planned
    assert r.plan() == {}


def test_the_plan_skips_connected_arms_and_ports_in_use(dynamic):
    r, a, b = dynamic
    connect(a, port="simulator")
    plan = r.plan()
    assert list(plan) == ["b"] and plan["b"]["port"] == "simulator-2"


def test_connect_all_connects_every_detected_arm_with_one_confirmation(tmp_path):
    from fastapi.testclient import TestClient
    from uuid import uuid4
    from orchid_demo.api import create_app
    import time as clock
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        session = client.get("/api/session").json()
        assert set(session["connect_plan"]) == {"a", "b"} and session["connect_plan"]["a"]["leader_port"] == "simulator-leader"
        auth = {"x-orchid-token": session["token"], "x-orchid-operator": str(uuid4())}
        client.post("/api/heartbeat", json={}, headers=auth)
        refused = client.post("/api/connect-all", json={"prepared": False, "fixture": "Plate"}, headers=auth)
        assert refused.status_code == 200  # queued; each arm's worker refuses an unconfirmed connection
        deadline = clock.monotonic() + 5
        while clock.monotonic() < deadline and client.get("/api/state").json()["pending"]:
            clock.sleep(0.02)
        assert client.get("/api/state?arm=a").json()["phase"] == "disconnected"
        client.post("/api/heartbeat", json={}, headers=auth)
        done = client.post("/api/connect-all", json={"prepared": True, "fixture": "Plate"}, headers=auth).json()
        assert set(done["plan"]) == {"a", "b"}
        deadline = clock.monotonic() + 5
        while clock.monotonic() < deadline and not all(client.get(f"/api/state?arm={x}").json()["connected"] for x in "ab"):
            client.post("/api/heartbeat", json={}, headers=auth)
            clock.sleep(0.02)
        a, b = (client.get(f"/api/state?arm={x}").json() for x in "ab")
        assert a["connected"] and a["leader"]["connected"] and b["connected"] and not b["leader"]["connected"]
        assert client.get("/api/session").json()["connect_plan"] == {}
        assert client.post("/api/connect-all", json={"prepared": True}, headers=auth).status_code == 409


def test_calibrating_a_follower_without_this_arms_calibration_needs_explicit_replacement(rig):
    r, a, b = rig
    following(a)
    teach_points(a, "C")
    run(a, 10, until=lambda: a.phase == "teach_hold")
    command(a, "disconnect", supported=True)
    connect(a)
    a.calibration_foreign = True  # as when another arm's motors are connected here
    a.publish()
    assert a.public["calibration_foreign"] and a.public["calibration_dependents"] == 1
    refused = reject(a, "calibrate", supported=True)["message"]
    assert "may be a different arm" in refused and "1 taught controls" in refused
    assert a.public["keys"]["C"]["status"] == "registered"  # nothing changed
    command(a, "calibrate", supported=True, replace_calibration=True)
    assert a.phase == "calibration_midpoint"


def test_one_press_finds_the_arms_then_connects_each_to_its_place(tmp_path):
    """Hardware engines with a fake USB scan: the endpoint scans on a disconnected follower, then submits the
    connections from the plan (the fake arms then fail to open, which is fine: the plan and the scan are the point)."""
    from fastapi.testclient import TestClient
    from uuid import uuid4
    from orchid_demo.api import create_app
    scans = []
    def scanner(guard, exclude_ports=()):
        scans.append(exclude_ports)
        six = list(range(1, 7))
        return {"arms": [{"port": "/dev/A", "role": "follower", "motor_ids": six, "voltage": 12.1, "limits": None},
                         {"port": "/dev/L", "role": "leader", "motor_ids": six, "voltage": 5.0, "limits": None}],
                "warnings": []}
    a = Engine(tmp_path, "hardware", port_scanner=scanner)
    b = Engine(tmp_path / "arm-b", "hardware", port_scanner=scanner, leader_store=LeaderStore(a.repo))
    with TestClient(create_app(tmp_path, "hardware", engines={"a": a, "b": b}), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        auth = {"x-orchid-token": token, "x-orchid-operator": str(uuid4())}
        client.post("/api/heartbeat", json={}, headers=auth)
        reply = client.post("/api/connect-all", json={"prepared": True, "scan": True}, headers=auth)
        assert reply.status_code == 200 and len(scans) == 1
        assert reply.json()["plan"] == {"a": {"port": "/dev/A", "recognised": False, "teaching_mode": "leader", "leader_port": "/dev/L"}}


def test_the_leader_moves_to_the_other_arm_in_one_step_and_each_arm_selects_its_own_controls(tmp_path):
    from fastapi.testclient import TestClient
    from uuid import uuid4
    from orchid_demo.api import create_app
    import time as clock
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        auth = {"x-orchid-token": token, "x-orchid-operator": str(uuid4())}
        app_engines = client.app.state.engines
        a, b = app_engines["a"], app_engines["b"]
        client.post("/api/heartbeat", json={}, headers=auth)
        client.post("/api/connect-all", json={"prepared": True}, headers=auth)
        deadline = clock.monotonic() + 5
        # Both connections finished, not just begun: a command sent while one is still running is refused as busy.
        while clock.monotonic() < deadline and not (a.leader and b.arm and not a.pending and not b.pending):
            client.post("/api/heartbeat", json={}, headers=auth)
            clock.sleep(0.02)
        assert a.leader and b.arm and not b.leader
        assert b.public["selected"] in ROLES["b"]  # never C, the keys arm's control
        moved = client.post("/api/leader/move", json={"to": "b"}, headers=auth)
        assert moved.status_code == 200 and moved.json()["message"].endswith("Chord Arm."), moved.text
        assert a.leader is None and b.leader is not None and a.teaching_mode == "manual"
        assert b.leader_calibration == a.leader_calibration  # one shared leader calibration (none yet in this fresh setup)
        assert client.post("/api/leader/move", json={"to": "b"}, headers=auth).json()["message"] == "This arm already has the leader."


def test_the_leader_can_join_an_arm_that_is_holding_and_leave_one_without_releasing_it(rig):
    r, a, b = rig
    a_teaches_c_then_hands_over_the_leader(a, b)
    command(b, "teach_begin", control="chord.maj", follow=True)
    tick(a, b, seconds=3)
    command(b, "teach_set_home")
    command(b, "teach_hold")
    tick(a, b)
    command(b, "leader_detach")  # B keeps holding, powered, without the leader
    assert b.phase == "teach_hold" and b.arm.enabled and b.leader is None and b.teach.read_leader is None
    port = r.released_leader
    command(b, "connect_leader", prepared=True, leader_port=port)  # straight back while still holding
    assert b.phase == "teach_hold" and b.arm.enabled and b.leader is not None and b.teach.read_leader is not None
    command(b, "teach_follow", control="chord.maj")
    tick(a, b, seconds=3)
    assert b.teach.mode == "following"


def test_an_arm_at_its_rest_is_parked_too(rig):
    from test_teach_app import move_leader
    r, a, b = rig
    following(a)
    teach_points(a, "C")
    run(a, 10, until=lambda: a.phase == "teach_hold")
    command(a, "teach_follow")
    run(a, 3, until=lambda: a.teach.mode == "following")
    move_leader(a, "shoulder_pan", 96)  # rest: well away from home (two of the largest simulated nudges)
    move_leader(a, "shoulder_pan", 96)
    command(a, "teach_set_rest")
    command(a, "teach_go_rest")
    run(a, 15, until=lambda: a.phase == "teach_hold")
    a.publish()
    assert a.parked_now == (True, "at rest")
    r.claim("b", "teach_play", {})  # Chord Arm may move while Keys Arm rests
    assert r.holder == "b"


def test_an_arm_with_no_home_yet_does_not_stop_the_other_being_taught_with_the_leader(rig):
    """Just connected, calibrated or swapped, an arm has no home or rest to park at. Holding still, it lets the other
    arm follow the leader (the operator guides it past), but nothing moves the other arm by itself."""
    r, a, b = rig
    a.parked_now = (False, "position unknown: no home set or not calibrated")
    r.claim("b", "teach_begin", {"follow": True})  # set the Chord Arm's home and rest with the leader
    r.claim("b", "teach_follow", {})
    assert r.holder == "b"
    r.holder = None
    with pytest.raises(SafetyError, match="Keys Arm is not parked"):
        r.claim("b", "teach_play", {})  # an automatic move still waits for it
    with pytest.raises(SafetyError, match="Keys Arm is not parked"):
        r.claim("b", "teach_go_rest", {})
    a.parked_now = (False, "moving")  # someone is moving it: not even guided teaching
    with pytest.raises(SafetyError, match="Keys Arm is not parked"):
        r.claim("b", "teach_follow", {})
