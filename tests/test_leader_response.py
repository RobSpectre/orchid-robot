"""Native position-following behavior, including lag and explicit pause."""
import pytest
from orchid_demo import motion as m
from orchid_demo.engine import Engine
from test_operator_engine import Clock, command
from test_leader_teaching import prepare


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()


@pytest.mark.parametrize('start,end', [(839,831),(831,839)])
def test_reported_leader_endpoint_reading_is_not_a_motion_fault(engine,start,end):
    prepare(engine, local=False)
    engine.leader.calibration['shoulder_lift'].update(range_min=839, range_max=3183)
    engine.leader.current['shoulder_lift'] = start
    command(engine,'leader_resume',hands_clear=True)
    engine.leader.current['shoulder_lift'] = end
    engine.step()
    assert engine.phase == 'home_approach', engine.error
    goal = engine.arm.joint_target(engine.leader.joint_action(engine.leader.current, m.MOTORS[:-1]))
    cal = engine.arm.calibration['shoulder_lift']
    assert engine.arm.current['shoulder_lift'] == max(cal['range_min'], min(cal['range_max'], goal['shoulder_lift']))


def test_leader_pose_is_sent_directly_and_is_not_discarded_or_stall_timed(engine):
    prepare(engine,local=False)
    engine.arm.jammed = True
    command(engine,'leader_resume',hands_clear=True)
    engine.leader.current['elbow_flex'] += 200
    for _ in range(40):
        engine.step()
        assert engine.phase == 'home_approach', engine.error
    assert engine.controller.previous['elbow_flex'] == 2247
    assert engine.arm.current['elbow_flex'] == 2047
    assert engine.controller.settings['max_relative_target'] is None
    command(engine,'leader_pause')
    assert engine.controller.previous['elbow_flex'] == 2047
    command(engine,'leader_resume',hands_clear=True)
    engine.step()
    assert engine.controller.previous['elbow_flex'] == 2247


def test_paused_hold_uses_same_driver_without_invented_tracking_tolerance(engine):
    prepare(engine,local=False)
    goal = dict(engine.controller.previous)
    engine.arm.jammed = True
    engine.arm.current['elbow_flex'] += 21
    for _ in range(40):
        engine.step()
        assert engine.phase == 'home_approach', engine.error
        assert engine.controller.previous == goal


def test_communication_failure_stops_and_logs_before_new_action(engine):
    prepare(engine,local=False)
    command(engine,'leader_resume',hands_clear=True)
    before = dict(engine.arm.current)
    engine.leader.connected = False
    engine.step()
    assert engine.phase == 'fault'
    assert engine.arm.current == before and not engine.controller.engaged
    assert engine.arm.enabled


def test_fixed_gripper_never_follows_leader_gripper(engine):
    prepare(engine,local=False)
    initial = dict(engine.arm.current)
    command(engine,'leader_resume',hands_clear=True)
    engine.leader.current['gripper'] = 4095
    engine.leader.current['wrist_flex'] += 100
    engine.step()
    assert engine.arm.current['wrist_flex'] == initial['wrist_flex'] + 100
    assert engine.arm.current['gripper'] == initial['gripper']
    assert all(n in engine.controller.previous for n in m.MOTORS)
