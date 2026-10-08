"""Incident evidence survives stop handling; reporting has no hardware side effects."""
import json
import threading

from fastapi.testclient import TestClient
import pytest

from orchid_demo.api import create_app
from orchid_demo.engine import Engine
from orchid_demo.incidents import IncidentStore, MAX_SAMPLES, atomic_json
from test_operator_engine import Clock, command
from test_leader_teaching import prepare


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    store = IncidentStore(tmp_path, clock=clock)
    yield store, clock
    store.close()


def test_history_is_bounded_by_time_and_count_and_copied(store):
    s, clock = store
    sample = {'follower': {'elbow_flex': 2992}}
    for _ in range(MAX_SAMPLES + 100):
        s.record(sample)
    assert len(s.history) == MAX_SAMPLES
    sample['follower']['elbow_flex'] = 0
    assert s.history[-1]['follower']['elbow_flex'] == 2992
    clock.sleep(31)
    identity = s.capture('fault', 'test', {'instance_id': 'one', 'sample': sample})
    sample['follower']['elbow_flex'] = 12
    s.work.join()
    data = json.loads(s.path(identity).read_text())
    assert data['telemetry'] == []
    assert data['context']['sample']['follower']['elbow_flex'] == 0
    assert data['runtime']['source_sha256']['leader.py']


def test_saving_is_off_worker_and_failure_is_visible(store, monkeypatch):
    s, _ = store
    entered, finish = threading.Event(), threading.Event()
    def failed_write(*args):
        assert threading.current_thread().name == 'orchid-incident-writer'
        entered.set()
        assert finish.wait(2)
        raise OSError('disk full')
    monkeypatch.setattr('orchid_demo.incidents.atomic_json', failed_write)
    identity = s.capture('fault', 'test', {'instance_id': 'one'})
    try:
        assert entered.wait(1)
        assert s.listing()['incidents'][0]['state'] == 'saving'
    finally:
        finish.set()
    s.work.join()
    assert s.listing()['incidents'][0]['state'] == 'save_failed'
    assert not s.path(identity, '.meta.json').exists()


def test_same_fault_and_rejection_deduplicate_but_later_failure_is_new(store):
    s, clock = store
    context = {'instance_id': 'one'}
    identity = s.capture('fault', 'same failure', context)
    assert s.capture('rejected', 'same failure', context) == identity
    clock.sleep(3)
    assert s.capture('fault', 'same failure', context) != identity


def test_fault_keeps_failed_tick_before_stop_and_cached_history(tmp_path, monkeypatch):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    try:
        prepare(e, local=False)
        e.arm.jammed = True
        command(e, 'leader_resume', hands_clear=True)
        e.leader.current['elbow_flex'] += 12
        e.step()
        sent = []
        send = e.arm.teleop_send
        failed_once = False
        def fail_once(action, gripper):
            nonlocal failed_once
            sent.append(e.arm.joint_target(action))
            if not failed_once:
                failed_once = True
                raise ConnectionError('native Goal_Position send failed')
            return send(action, gripper)
        monkeypatch.setattr(e.arm, 'teleop_send', fail_once)
        e.leader.current['elbow_flex'] += 12
        e.step()
        assert e.phase == 'fault'
        e.incidents.work.join()
        fault = next(i for i in e.incidents.listing()['incidents'] if i['kind'] == 'fault')
        bundle = json.loads(e.incidents.path(fault['id']).read_text())
        failed = bundle['context']['sample']
        assert failed['actual']['elbow_flex'] == 2047
        assert failed['target']['elbow_flex'] > 2055
        assert failed['command_sent'] is False
        assert failed['torque_verified'] is True
        assert sent[-1]['elbow_flex'] == 2047  # measured hold replaced the hardware goal
        history = [s for s in bundle['telemetry'] if s.get('controller', {}).get('command_sent')]
        assert history and history[-1]['controller']['target']['elbow_flex'] > 2047
        assert bundle['context']['calibration']
        assert bundle['context']['leader_calibration']
        assert bundle['context']['policy']['leader_driver']['max_relative_target'] is None
        assert 'native Goal_Position send failed' in bundle['traceback']
        assert e.arm.enabled  # reporting has not released torque
    finally:
        e.close()


def test_http_reporting_works_without_motor_owner_and_is_idempotent(tmp_path, monkeypatch):
    e = Engine(tmp_path, 'hardware', hardware_factory=lambda *_: pytest.fail('opened hardware'))
    e.incidents.record({'follower': {'elbow_flex': 2992}})
    identity = e.incidents.capture('fault', 'elbow stopped', {'mode': 'hardware'})
    e.incidents.work.join()
    app = create_app(tmp_path, 'hardware', engine=e)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        monkeypatch.setattr(e, 'submit', lambda *a, **k: pytest.fail('sent motor command'))
        monkeypatch.setattr(e, 'heartbeat', lambda *a, **k: pytest.fail('acquired motor ownership'))
        path = f'/api/incidents/{identity}'
        assert client.get(path).json()['telemetry'][0]['follower']['elbow_flex'] == 2992
        assert 'attachment' in client.get(path).headers['content-disposition']
        assert client.post(path + '/report', json={}).status_code == 403
        token = client.get('/api/session').json()['token']
        headers = {'x-orchid-token': token}
        assert client.post(path + '/report', headers={**headers, 'origin': 'https://evil.example'}, json={}).status_code == 403
        response = client.post(path + '/report', headers=headers, json={'note': 'Barely moved'})
        assert response.json()['report']['state'] == 'queued'
        original = s_request = e.incidents.path(identity, '.request.json').read_text()
        assert json.loads(s_request)['operator_note'] == 'Barely moved'
        atomic_json(e.incidents.path(identity, '.status.json'), {'state': 'reviewed', 'summary': 'Test diagnosis'})
        response = client.post(path + '/report', headers=headers, json={'note': 'again'})
        assert response.json()['report']['state'] == 'reviewed'
        assert e.incidents.path(identity, '.request.json').read_text() == original
        assert client.get('/api/incidents').json()['incidents'][0]['report']['summary'] == 'Test diagnosis'
        assert client.get('/api/incidents/not-an-id').status_code == 404
        assert client.post('/api/incidents/not-an-id/report', headers=headers, json={}).status_code == 404
        assert client.post(path + '/report', headers=headers, json={'note': 'x' * 1001}).status_code == 422
        assert e.arm is None and e.owner is None and e.commands.empty()
