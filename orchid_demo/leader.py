"""LeRobot SO101 position following, with explicit operator engage/pause."""
import math

from . import motion as m
from .home import HomeArm, TRAVEL_PHASES

FOLLOW_LEASE = 1.2  # Browser permission lease, not a motor tuning parameter.
TEACH_PHASES = ("home_positioning", "home_arrival", "note_ready", "note_hover", "note_pressed", "note_touch", "dial_ready", "dial_approach",
               "dial_contact", "dial_turned", "dial_lifted", *TRAVEL_PHASES)


class LeaderController(m.Controller):
    def __init__(self, arm, leader, current, log, *, guard, permission, observe, leader_feedback,
                 feedback, clock, sleep, follow_gripper=False):
        super().__init__(arm, [current], log, clock=clock, sleep=sleep)
        self.device = arm.device if isinstance(arm, HomeArm) else arm
        self.leader = leader
        self.origin = dict(current)
        self.previous = dict(current)
        self.desired = dict(current)
        self.gripper = current["gripper"]
        self.follow_gripper = follow_gripper
        self.guard, self.permission = guard, permission
        self.observe, self.leader_feedback = observe, leader_feedback
        self.feedback = feedback
        self.engaged = False
        self.limited = False
        self.boundary_joints = []
        self.travel = "approach" if isinstance(arm, HomeArm) else None
        # Resolve the native driver before enabling torque, not on a live tick.
        self.settings = self.device.teleop_settings
        self.leader_settings = self.leader.teleop_settings

    @property
    def controlled_motors(self):
        return m.MOTORS if self.follow_gripper else m.MOTORS[:-1]

    def set_travel(self, kind, current):
        m.require(not self.engaged, "Pause following before changing the taught route.")
        # Home was captured in a paused hold. Keep that gripper goal throughout
        # approach, contact and return; only home positioning accepts jaw input.
        self.follow_gripper = False
        self.travel = kind
        self.origin = dict(current)

    def set_local(self, current):
        self.arm.check_clearance(current)
        self.travel = None
        self.origin = dict(current)

    def read_leader(self):
        raw, action = self.leader.teleop_read()
        self.leader_feedback(raw)
        return raw, action

    def exact_action(self, target):
        action = self.device.joint_action(target, self.controlled_motors)
        decoded = self.device.joint_target(action)
        # SDK conversion truncates integers. Put any floating-point round-trip
        # miss at the center of its encoder bin, preserving the exact raw target.
        for name in self.controlled_motors:
            if decoded[name] != target[name]:
                cal = self.device.calibration[name]
                units_per_tick = 100 / (cal["range_max"] - cal["range_min"]) if name == "gripper" else 360 / 4095
                action[name] += (target[name] - decoded[name] - 0.5) * units_per_tick
        m.require(self.device.joint_target(action) == {n: target[n] for n in self.controlled_motors},
                  "LeRobot position conversion did not preserve the requested encoder target.")
        return action

    def arm_here(self):
        self.guard()
        self.leader.require_torque(False)
        self.read_leader()
        if prepare := getattr(self.device, "prepare_motion", None):
            prepare(guard=self.guard)
        current = m.stable_pose(self.arm, self.sleep)
        self.guard()
        self.enabled = True  # Partial enable must still enter stop handling.
        self.device.arm_at_calibrated(current)
        self.previous = dict(current)
        self.tick(stage="leader_hold")

    def resume(self):
        self.guard()
        m.require(self.permission(), "Keep the operator page visible before engaging the leader.")
        # The UI countdown lets the operator match poses and clear the path.
        # Once engaged, use native absolute positions, without relative offsets.
        self.tick(stage="leader_hold")
        self.engaged = True

    def pause(self):
        self.tick(stage="leader_pause", pause=True)

    def tick(self, _target=None, stage="leader_tick", *, pause=False, home_target=None):
        self.guard()
        start = self.clock()
        self.last_sample = {"time": start, "stage": stage, "following": self.engaged,
                            "previous_target": dict(self.previous), "command_sent": False,
                            "driver_settings": self.settings, "gripper_follows_leader": self.follow_gripper}
        source, action = self.read_leader()
        self.last_sample.update(leader=dict(source), leader_action=dict(action))
        current, _ = self.device.teleop_read()
        self.last_sample["actual"] = dict(current)
        self.device.require_torque(True)
        self.leader.require_torque(False)
        self.last_sample["torque_verified"] = True
        self.observe(current)
        pausing = self.engaged and (pause or not self.permission())
        if pausing:
            self.engaged = False
        self.limited = False
        self.boundary_joints = []
        if home_target is not None:
            m.require(not self.engaged and self.follow_gripper, "Home positioning requires a paused six-motor hold.")
            self.device.check_calibrated_pose(home_target)
            target = dict(home_target)
            sent_action = self.exact_action(target)
        elif self.engaged:
            goal = {n: action[n] for n in self.controlled_motors}
            # The native gripper range is 0..100.
            if self.follow_gripper and not 0 <= goal["gripper"] <= 100:
                self.boundary_joints.append("gripper")
            target = {"gripper": self.gripper, **self.device.joint_target(goal)}
            self.last_sample["desired_target"] = dict(target)
            # Retain the follower's recorded physical endpoints without adding
            # an inset or relative anchor to the native calibrated pose.
            for name in self.controlled_motors:
                cal = self.device.calibration[name]
                bounded = max(cal["range_min"], min(cal["range_max"], target[name]))
                if bounded != target[name]:
                    self.boundary_joints.append(name)
                    target[name] = bounded
            sent_action = self.exact_action(target)
        else:
            # Latch once on pause; don't turn sag into a constantly moving goal.
            target = dict(current if pausing else self.previous)
            if not self.follow_gripper:
                target["gripper"] = self.gripper
            sent_action = self.exact_action(target)
        self.desired = dict(target)
        self.last_sample.update(target=dict(target), follower_action=sent_action,
                                boundary_joints=list(self.boundary_joints))
        self.guard()
        actual_action = self.device.teleop_send(sent_action, self.gripper)
        self.last_sample.update(command_sent=True, sent_action=actual_action, send_returned_at=self.clock())
        self.previous = {"gripper": self.gripper, **self.device.joint_target(actual_action)}
        self.gripper = self.previous["gripper"]
        self.last_tick = start
        self.record(stage=stage, actual=current, target=self.previous, leader=source, following=self.engaged,
                    driver_settings=self.settings, gripper_follows_leader=self.follow_gripper)
        self.feedback(current, "leader_following" if self.engaged else "leader_paused")
        self.sleep(max(0, m.PERIOD - (self.clock() - start)))
        return current

    def move_home(self, target, duration, progress):
        """Supervised joint-space homing through the native LeRobot send path.

        The operator confirms clearance of the whole swept path, including the
        jaw. Zero duration sends the native position target directly; a positive
        duration is an operator-selected ramp, not a tracking/force limit.
        Never call the keypress playback routine while getting to home.
        """
        m.require(not self.engaged and self.enabled and self.follow_gripper, "Establish the paused home hold first.")
        m.require(type(duration) in (int, float) and math.isfinite(duration) and (duration == 0 or 1 <= duration <= 60),
                  "Choose Direct (0) or a home move time from 1 to 60 seconds.")
        self.device.check_calibrated_pose(target)
        current = self.tick(stage="home_start_hold")
        self.record(stage="home_move_begin", actual=current, target=target, duration=duration)
        count = max(1, math.ceil(duration / m.PERIOD))
        for index in range(1, count + 1):
            u = index / count
            pose = m.blend(current, target, 10*u**3 - 15*u**4 + 6*u**5)
            self.tick(stage="home_move", home_target=pose)
            progress(u)
        self.record(stage="home_target_sent", target=self.previous)

    def stop(self):
        self.engaged = False
        if not self.enabled:
            return
        try:
            current, _ = self.device.teleop_read()
            action = self.exact_action(current)
            self.device.teleop_send(action, self.gripper)
            self.previous = {"gripper": self.gripper, **self.device.joint_target(action)}
            self.gripper = self.previous["gripper"]
        except Exception as exc:
            if self.error_reporter:
                self.error_reporter("hold_failed", exc)
            else:
                print(f"Could not establish a hold: {exc}. Support the arm before releasing torque.", flush=True)
