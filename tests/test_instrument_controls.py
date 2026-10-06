"""Chord buttons, directed dial gestures, and non-destructive schema upgrades."""
from copy import deepcopy
from pathlib import Path
import json
import sqlite3

import pytest

from orchid_demo import dial, motion as m
from orchid_demo.controls import CATALOG, CHORDS, DIALS
from orchid_demo.engine import Engine
from orchid_demo.storage import Repository
from test_operator_engine import Clock, command, connect, calibrate, teach, accept_trial


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    connect(e)
    calibrate(e)
    yield e
    e.close()


def teach_dial(e, control="voicing.cw"):
    if e.phase != "dial_ready" or e.selected != control:
        command(e, "control_start", control=control, supported=True)
    command(e, "dial_capture_start", reference="C major, Geek Out view, C–E–G",
            expected_effect="Small repeatable voicing change", fixed_pad=True)
    command(e, "dial_capture_contact")
    command(e, "dial_capture_turn", direction_verified=True)
    command(e, "dial_capture_lift", rim_clear=True)
    command(e, "dial_capture_return", supported=True, rim_clear=True)
    assert e.phase == "holding"


def test_eight_chord_buttons_without_expanding_note_slots(engine):
    command(engine, "control_start", control=CHORDS[0][0], supported=True)
    for name, _, _, _ in CHORDS:
        assert engine.selected == name
        teach(engine)
        for _ in range(3):
            accept_trial(engine)
        command(engine, "next", supported=True)
    assert engine.phase == "ready"
    assert len(engine.notes) == 12
    assert all(value is None for value in engine.notes.values())
    assert all(engine.statuses(engine.controls)[name]["status"] == "registered" for name, *_ in CHORDS)
    assert engine.controls["chord.m7"] != engine.controls["chord.M7"]
    assert len(engine.export_snapshot()["controls"]) == 10


@pytest.mark.parametrize("control", [name for name, *_ in DIALS])
def test_dial_three_trials_preserves_forward_stage_order(engine, control):
    teach_dial(engine, control)
    entry = deepcopy(engine.draft)
    assert entry["direction"] == CATALOG[control]["direction"]
    assert entry["contact_index"] < entry["turn_index"] < entry["release_index"] < len(entry["path"]) - 1
    for _ in range(3):
        command(engine, "test", hands_clear=True, reference_reset=True)
        command(engine, "pass", effect_verified=True)
    assert engine.statuses(engine.controls)[control]["status"] == "registered"
    events = [json.loads(line) for line in Path(engine.log.name).read_text().splitlines()]
    stages = [event.get("stage") for event in events]
    assert stages.count("dial_complete") == 3
    assert not any(stage in ("up", "down", "up_settle") for stage in stages)
    first_approach = stages.index("dial_approach")
    first_turn = stages.index("dial_turn")
    first_lift = stages.index("dial_lift_off")
    first_return = stages.index("dial_clear_return")
    assert first_approach < first_turn < first_lift < first_return < stages.index("dial_complete")
    before = entry["path"][entry["contact_index"]]["wrist_roll"]
    end = entry["path"][entry["turn_index"]]["wrist_roll"]
    turns = [event["target"]["wrist_roll"] for event in events if event.get("stage") == "dial_turn"]
    assert all(min(before,end) <= value <= max(before,end) for value in turns)
    assert engine.arm.current == entry["path"][-1]
    command(engine, "next", supported=True)
    assert not engine.arm.enabled
    assert engine.selected == ("voicing.ccw" if control.endswith(".cw") else "voicing.cw")


def test_dial_needs_reference_reset_and_observed_effect(engine):
    teach_dial(engine)
    with pytest.raises(m.SafetyError, match="reference"):
        engine.dispatch("test", {"hands_clear":True})
    command(engine, "test", hands_clear=True, reference_reset=True)
    with pytest.raises(m.SafetyError, match="Verify"):
        engine.dispatch("pass", {})
    assert engine.trials == 0
    command(engine, "pass", effect_verified=True)
    command(engine, "test", hands_clear=True, reference_reset=True)
    command(engine, "fail")
    assert engine.controls["voicing.cw"]["verification"]["successful_trials"] == 0
    assert engine.arm.enabled
    command(engine, "retry", supported=True)
    assert engine.phase == "dial_ready"
    assert not engine.arm.enabled


def test_missing_lift_cannot_arm_dial(engine):
    command(engine, "control_start", control="voicing.cw", supported=True)
    command(engine, "dial_capture_start", reference="C", expected_effect="Small turn", fixed_pad=True)
    command(engine, "dial_capture_contact")
    command(engine, "dial_capture_turn", direction_verified=True)
    with pytest.raises(m.SafetyError, match="not available"):
        engine.dispatch("dial_capture_return", {"supported":True, "rim_clear":True})
    with pytest.raises(m.SafetyError, match="completely clear"):
        engine.dispatch("dial_capture_lift", {})
    assert not engine.arm.enabled


@pytest.mark.parametrize("issue", ["missing_release", "far_return", "grip", "oversized_turn", "wrong_direction", "bad_reference"])
def test_invalid_dial_paths_fail_before_powered_motion(engine, issue):
    teach_dial(engine)
    entry = deepcopy(engine.draft)
    if issue == "missing_release":
        del entry["release_index"]
    elif issue == "far_return":
        entry["path"][-1]["shoulder_pan"] += 10
    elif issue == "grip":
        entry["path"][entry["turn_index"]]["gripper"] += 5
    elif issue == "oversized_turn":
        # Smooth contact excursion still exceeds the allowed contact envelope.
        contact = entry["path"][entry["contact_index"]]
        entry["path"] = entry["path"][:entry["contact_index"]+1]
        entry["path"] += [{**contact, "wrist_roll":contact["wrist_roll"]+i} for i in (12,24,36,40)]
        entry["turn_index"] = len(entry["path"])-1
        entry.pop("release_index")
    elif issue == "wrong_direction":
        entry["direction"] = "auto"
    else:
        entry["reference"] = ""
    with pytest.raises(m.SafetyError):
        dial.validate(entry, complete=True)


def test_dial_tracking_and_lease_faults_preserve_torque(engine):
    teach_dial(engine)
    engine.arm.jammed = True
    engine.heartbeat("operator")
    engine.submit("operator", "jam", "test", engine.revision, {"hands_clear":True,"reference_reset":True})
    engine.step()
    assert engine.phase == "fault"
    assert engine.controls["voicing.cw"] is None
    assert engine.arm.enabled
    command(engine, "retry", supported=True)
    engine.arm.jammed = False
    teach_dial(engine)
    engine.lease_until = 0
    engine.step()
    assert engine.phase == "fault"
    assert engine.arm.enabled


def test_control_persistence_mode_and_fixture_invalidation(engine, tmp_path):
    teach_dial(engine)
    command(engine, "test", hands_clear=True, reference_reset=True)
    command(engine, "pass", effect_verified=True)
    entry = deepcopy(engine.controls["voicing.cw"])
    command(engine, "disconnect", supported=True)
    other = Engine(tmp_path)
    hardware = Engine(tmp_path, "hardware")
    try:
        assert other.controls["voicing.cw"] == entry
        assert other.phase == "disconnected" and other.arm is None
        assert all(value is None for value in hardware.controls.values())
        command(other, "connect", prepared=True, fixture="Test fixture", fixture_unchanged=True, tool="rubber_gloved_tips")
        assert other.statuses(other.controls)["voicing.cw"]["status"] == "needs_reteach"
        assert other.fixture["tool"] == "rubber_gloved_tips"
    finally:
        other.close()
        hardware.close()


def test_schema_one_upgrade_preserves_notes_and_documents(tmp_path):
    directory = tmp_path / "simulation"
    directory.mkdir()
    old = sqlite3.connect(directory / "operator.sqlite3")
    old.executescript('CREATE TABLE notes(note TEXT PRIMARY KEY,entry TEXT NOT NULL); CREATE TABLE documents(name TEXT PRIMARY KEY,value TEXT NOT NULL); PRAGMA user_version=1;')
    old.execute('INSERT INTO notes VALUES (?,?)', ("C", json.dumps({"legacy":True})))
    old.execute('INSERT INTO documents VALUES (?,?)', ("fixture", json.dumps({"id":"existing", "label":"old fixture"})))
    old.commit()
    old.close()
    repo = Repository(tmp_path, "simulation")
    try:
        assert repo.notes()["C"] == {"legacy":True}
        assert repo.get("fixture")["id"] == "existing"
        assert len(repo.notes()) == 12
        assert len(repo.controls()) == 10
        assert repo.db.execute('PRAGMA user_version').fetchone()[0] == 2
    finally:
        repo.close()


def test_unknown_control_does_not_release_motors(engine):
    with pytest.raises(m.SafetyError, match="control"):
        engine.dispatch("control_start", {"control":"volume.max", "supported":True})
    assert engine.phase == "ready"
