"""Read-only telemetry, safe diagnostic scheduling, and calibration reference gating."""
from copy import deepcopy
import math

import pytest

from orchid_demo import motion as m
from orchid_demo.engine import Engine
from orchid_demo.telemetry import motor_status, pose_angles
from test_operator_engine import Clock, command, connect, calibrate, teach


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()


def test_pose_is_unavailable_before_midpoint_and_after_disconnect(engine):
    assert engine.snapshot()['pose_angles'] is None
    connect(engine)
    assert engine.snapshot()['pose_angles'] is None
    command(engine, 'calibrate', supported=True)
    assert engine.snapshot()['pose_reference_ready'] is False
    command(engine, 'calibration_center', supported=True)
    snapshot = engine.snapshot()
    assert snapshot['pose_reference_ready'] is True
    assert snapshot['pose_angles']['shoulder_pan'] == 0
    assert snapshot['pose_angles']['gripper'] == pytest.approx(math.pi / 4)
    command(engine, 'disconnect', supported=True)
    assert engine.snapshot()['pose_angles'] is None


def test_recalibration_clears_old_ranges_and_reference(engine):
    connect(engine)
    calibrate(engine)
    previous = deepcopy(engine.calibration)
    command(engine, 'calibrate', supported=True)
    assert engine.offsets is None and engine.ranges == {}
    assert engine.snapshot()['pose_angles'] is None
    command(engine, 'calibration_center', supported=True)
    status = engine.snapshot()['motor_status']
    assert status['shoulder_pan']['range_min'] == 2047
    assert status['elbow_flex']['range_min'] is None  # never show old limits against new references
    assert engine.calibration == previous


def test_diagnostics_are_explicit_idle_only_reads(engine):
    connect(engine)
    assert engine.snapshot()['diagnostics_at'] is None
    command(engine, 'refresh_diagnostics')
    snapshot = engine.snapshot()
    assert snapshot['diagnostics_at'] is not None
    assert all(v['voltage_v'] == 12 and v['temperature_c'] == 25 for v in snapshot['motor_status'].values())
    assert engine.arm.enabled is False
    command(engine, 'calibrate', supported=True)
    with pytest.raises(m.SafetyError, match='not available'):
        engine.dispatch('refresh_diagnostics', {})
    command(engine, 'disconnect', supported=True)
    assert engine.snapshot()['diagnostics_at'] is None


def test_diagnostics_failure_clears_snapshot_without_changing_motor_state(engine, monkeypatch):
    connect(engine)
    command(engine, 'refresh_diagnostics')
    def fail():
        raise OSError('Read timed out')
    monkeypatch.setattr(engine.arm, 'read_diagnostics', fail)
    with pytest.raises(m.SafetyError, match='unavailable'):
        engine.dispatch('refresh_diagnostics', {})
    engine.publish()
    assert engine.snapshot()['diagnostics_at'] is None
    assert engine.snapshot()['diagnostics_error'] == 'Read timed out'
    assert engine.phase == 'connected' and not engine.arm.enabled


def test_powered_targets_and_fault_telemetry(engine):
    connect(engine)
    calibrate(engine)
    command(engine, 'note_start', key='C', supported=True)
    teach(engine)
    status = engine.snapshot()['motor_status']
    assert all(v['torque_enabled'] is True and v['target_ticks'] is not None for v in status.values())
    assert all(v['tracking_error_ticks'] == 0 for v in status.values())
    with pytest.raises(m.SafetyError, match='not available'):
        engine.dispatch('refresh_diagnostics', {})
    engine.fault(m.SafetyError('test feedback lost'))
    status = engine.snapshot()['motor_status']
    assert all(v['torque_enabled'] is None and v['target_ticks'] is None for v in status.values())
    assert engine.snapshot()['feedback_at'] is None


@pytest.mark.parametrize('raw,expected', [(999,'outside'),(1001,'near_limit'),(2047,'within'),(3099,'near_limit'),(3101,'outside')])
def test_motor_range_status(raw, expected):
    calibration = {name:{'range_min':1000,'range_max':3100} for name in m.MOTORS}
    status = motor_status(dict.fromkeys(m.MOTORS,raw),dict.fromkeys(m.MOTORS,0),calibration,{},reference_ready=True)
    assert all(v['limit_status'] == expected for v in status.values())


def test_encoder_to_radian_mapping_does_not_normalize_arm_joint_travel():
    raw = dict.fromkeys(m.MOTORS,2047)
    raw['elbow_flex'] += 1024
    angles = pose_angles(raw,{},True)
    assert angles['elbow_flex'] == pytest.approx(math.pi/2)
    assert angles['wrist_roll'] == 0
    assert pose_angles(raw,{},False) is None
    assert pose_angles({'elbow_flex':2047},{},True) is None


def test_gripper_model_uses_calibrated_travel_without_extrapolating():
    raw = dict.fromkeys(m.MOTORS,2047)
    calibration = {'gripper':{'range_min':1000,'range_max':3000}}
    raw['gripper'] = 1000
    assert pose_angles(raw,calibration,True)['gripper'] == pytest.approx(-.174533)
    raw['gripper'] = 3500
    assert pose_angles(raw,calibration,True)['gripper'] == pytest.approx(1.74533)
