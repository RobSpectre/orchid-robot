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


def test_rejected_action_logs_context_without_enabling_motors(engine, capsys):
    reject(engine, 'home_start', supported=True)
    record = records(capsys.readouterr().out)[-1]
    assert record['kind'] == 'rejected' and record['action'] == 'home_start'
    assert record['phase'] == 'disconnected' and record['command_id']
    assert engine.arm is None


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
