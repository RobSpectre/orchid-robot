"""Chord buttons, and non-destructive schema upgrades."""
from copy import deepcopy
import json
import sqlite3

import pytest

from orchid_demo import motion as m
from orchid_demo.controls import CATALOG, CHORDS
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
    assert len(engine.export_snapshot()["controls"]) == 8


def test_control_persistence_mode_and_fixture_invalidation(engine, tmp_path):
    command(engine, "control_start", control="chord.maj", supported=True)
    teach(engine)
    accept_trial(engine)
    entry = deepcopy(engine.controls["chord.maj"])
    command(engine, "disconnect", supported=True)
    other = Engine(tmp_path)
    hardware = Engine(tmp_path, "hardware")
    try:
        assert other.controls["chord.maj"] == entry
        assert other.phase == "disconnected" and other.arm is None
        assert all(value is None for value in hardware.controls.values())
        command(other, "connect", prepared=True, fixture="Test fixture", fixture_unchanged=True, tool="rubber_gloved_tips")
        assert other.statuses(other.controls)["chord.maj"]["status"] == "needs_reteach"
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
        assert len(repo.controls()) == 8
        assert repo.db.execute('PRAGMA user_version').fetchone()[0] == 2
    finally:
        repo.close()


def test_unknown_control_does_not_release_motors(engine):
    with pytest.raises(m.SafetyError, match="control"):
        engine.dispatch("control_start", {"control":"volume.max", "supported":True})
    assert engine.phase == "ready"


def test_a_database_with_the_retired_voicing_dial_still_loads(tmp_path, caplog):
    """The voicing dial (voicing.cw / voicing.ccw) is no longer a robot control: saved entries are left alone, unused."""
    repo = Repository(tmp_path, "simulation")
    with repo.db:
        for name in ("voicing.cw", "voicing.ccw", "chord.maj"):
            repo.db.execute("INSERT INTO controls VALUES (?,?)", (name, json.dumps({"control_id": name, "turn_degrees": 20.0})))
    repo.put("teach_dial", {"points": {}, "calibration_sha256": "old", "mode": "simulation"})
    repo.close()
    with caplog.at_level("WARNING", logger="orchid_demo.storage"):
        clock = Clock()
        e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    try:
        assert "voicing.ccw, voicing.cw" in caplog.text and "Ignoring saved controls" in caplog.text
        assert set(e.controls) == {name for name, *_ in CHORDS}
        assert e.controls["chord.maj"]["control_id"] == "chord.maj"
        assert "voicing.cw" not in e.snapshot()["controls"] and "voicing.cw" not in CATALOG
        assert "voicing.cw" not in e.export_snapshot()["controls"]
        caplog.clear()
        e.repo.controls()
        assert "Ignoring" not in caplog.text  # said once per database, not on every read
        connect(e)
        calibrate(e)
        with pytest.raises(m.SafetyError, match="control"):
            e.dispatch("control_start", {"control": "voicing.cw", "supported": True})
        assert e.phase == "ready"
    finally:
        e.close()
    rows = sqlite3.connect(tmp_path / "simulation" / "operator.sqlite3").execute("SELECT control FROM controls").fetchall()
    assert {"voicing.cw", "voicing.ccw"} <= {row[0] for row in rows}  # left in place, never deleted
