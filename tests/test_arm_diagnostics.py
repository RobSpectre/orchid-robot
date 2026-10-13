"""Arm failures stay visible on stdout even when the motor loop or storage fails."""
import json
import time

import pytest

from orchid_demo.engine import Engine
from test_operator_engine import Clock, command, reject
from test_leader_teaching import prepare


@pytest.fixture
def engine(tmp_path, capsys):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()


def records(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith('{')]


def test_failed_native_send_logs_sample_before_stop_changes_target(engine, capsys, monkeypatch):
    prepare(engine, local=False)
    engine.arm.jammed = True
    command(engine, "leader_resume", hands_clear=True)
    engine.leader.current["elbow_flex"] += 12
    engine.step()
    send = engine.arm.teleop_send
    failed = False
    def fail_once(action, gripper):
        nonlocal failed
        if not failed:
            failed = True
            raise ConnectionError("native Goal_Position send failed")
        return send(action, gripper)
    monkeypatch.setattr(engine.arm, "teleop_send", fail_once)
    engine.leader.current["elbow_flex"] += 12
    engine.step()
    fault = [r for r in records(capsys.readouterr().out) if r['kind'] == 'fault'][-1]
    assert fault['phase'] == 'home_approach'
    assert 'Goal_Position send failed' in fault['message']
    assert fault['sample']['actual']['elbow_flex'] == 2047
    assert fault['sample']['target']['elbow_flex'] > 2055
    assert fault['sample']['following']
    assert fault['sample']['leader']['elbow_flex'] > 2047
    assert fault['exception'] == 'ConnectionError' and 'leader.py' in fault['traceback']
    assert engine.arm.enabled  # Logging does not release torque.
    assert fault == records(engine.event_log.path.read_text())[-1]
    stored = json.loads(engine.repo.db.execute("SELECT detail FROM events WHERE kind='fault' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert stored['sample'] == fault['sample']


def test_a_refused_action_is_logged_with_context_but_is_not_an_error_report(engine, capsys):
    refused = reject(engine, 'home_start', supported=True)
    record = records(capsys.readouterr().out)[-1]
    assert record['kind'] == 'refused' and record['action'] == 'home_start'
    assert record['phase'] == 'disconnected' and record['command_id']
    assert refused['message'] == 'Cannot home start while the arm is not connected. Connect the arm first (Find and connect all arms).'
    assert engine.arm is None
    engine.incidents.close()
    assert engine.incidents.listing()['incidents'] == []  # the app saying no is Session activity, not an error report


def test_a_hardware_failure_during_a_command_is_one_fault_row_and_one_report(engine, capsys, monkeypatch):
    prepare(engine, local=False)
    def broken(*args, **kwargs):
        raise ConnectionError("test follower torque write failed")
    monkeypatch.setattr(engine.arm, "release", broken)
    reject(engine, "release", supported=True)
    rows = [e for e in engine.events if "test follower torque write failed" in e["message"]]
    assert [e["kind"] for e in rows] == ["fault"]  # not also a "rejected" row with the same text
    engine.incidents.close()
    assert [i["kind"] for i in engine.incidents.listing()["incidents"]] == ["fault"]


def below_range(engine):
    """The follower's shoulder resting past the bottom of its calibrated range (as when the range sweep stopped short)."""
    engine.current = {**engine.current, "shoulder_lift": engine.calibration["shoulder_lift"]["range_min"] - 30}


FLAGGED = "Failed to write 'Torque_Enable' on id_=2 with '0' after 1 tries. "


def test_a_release_failure_names_the_device_and_always_the_way_out(engine, monkeypatch):
    prepare(engine, local=False)
    below_range(engine)
    def leader_fails():
        raise RuntimeError(FLAGGED)
    monkeypatch.setattr(engine.leader, "release", leader_fails)
    message = reject(engine, "release", supported=True)["message"]
    assert "Torque release could not be verified on the leader" in message and "Recalibrate" not in message
    assert "Forget unavailable connection" in message


def test_a_follower_release_failure_past_its_range_says_recalibrate_and_keeps_the_way_out(engine, monkeypatch):
    prepare(engine, local=False)
    def follower_fails():
        below_range(engine)  # it sank onto its stop as the torque went
        raise RuntimeError(FLAGGED)
    monkeypatch.setattr(engine.arm, "release", follower_fails)
    message = reject(engine, "release", supported=True)["message"]
    assert "30 ticks below its calibrated range" in message and "Recalibrate this follower" in message
    assert "support BOTH arms, disconnect their motor power, then use 01 · Connect the arm → Forget unavailable connection" in message


def test_a_motor_error_while_calibrating_does_not_say_recalibrate(engine, monkeypatch):
    prepare(engine, local=False)
    command(engine, "release", supported=True)
    command(engine, "calibrate", supported=True)
    assert engine.calibrating
    below_range(engine)
    def fails(action, args):
        raise RuntimeError("Failed to write 'Lock' on id_=2 with '0' after 1 tries. ")
    monkeypatch.setattr(engine, "dispatch_action", fails)
    receipt = reject(engine, "calibration_center", supported=True)
    assert "Recalibrate" not in receipt["message"] and "Recalibrate" not in (engine.error or "")
    assert receipt["message"] == engine.error  # the fault's own words, suffixes included


def test_actions_that_need_a_disconnected_arm_say_disconnect_even_after_a_fault(engine):
    engine.phase = "fault"
    with pytest.raises(Exception, match="Cannot refresh ports while the arm is stopped after a fault. Disconnect it first"):
        engine.dispatch("refresh_ports", {})
    engine.phase = "home_prepare"
    with pytest.raises(Exception, match="Finish or cancel setting its home first"):
        engine.require_phase("teach_hold")


def test_worker_storage_failure_and_failed_hold_are_both_logged(engine, capsys, monkeypatch):
    prepare(engine, local=False)
    def failed_read():
        raise ConnectionError('test follower USB read failed')
    def failed_storage(*args):
        raise OSError('test database full')
    monkeypatch.setattr(engine.arm, 'read_raw', failed_read)
    monkeypatch.setattr(engine.repo, 'event', failed_storage)
    engine.run()
    output = records(capsys.readouterr().out)
    assert any(r['kind'] == 'fault' and r['exception'] == 'ConnectionError' for r in output)
    assert any(r['kind'] == 'hold_failed' and 'USB read failed' in r['message'] for r in output)
    assert any(r['kind'] == 'worker_failed' and 'database full' in r['message'] for r in output)


def test_expired_lease_fault_is_logged_without_new_serial_reads(engine, capsys, monkeypatch):
    prepare(engine, local=False)
    engine.lease_until = time.monotonic() - 1
    monkeypatch.setattr(engine.controller, 'stop', lambda: None)
    monkeypatch.setattr(engine.arm, 'read_raw', lambda: pytest.fail('Logging opened the arm'))
    engine.step()
    fault = [r for r in records(capsys.readouterr().out) if r['kind'] == 'fault'][-1]
    assert 'connection lost' in fault['message']
    assert fault['last_cached_follower']
