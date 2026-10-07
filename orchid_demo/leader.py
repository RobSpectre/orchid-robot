"""Bounded relative leader input. All calls run on the single motor worker."""
from . import motion as m

GAIN = 0.25
MAX_INPUT_GAP = 128
FOLLOW_LEASE = 1.2
TEACH_PHASES = ("note_ready", "note_pressed", "note_touch", "dial_ready", "dial_approach",
                "dial_contact", "dial_turned", "dial_lifted")


class LeaderController(m.Controller):
    def __init__(self, arm, leader, current, log, *, guard, permission, observe, leader_feedback,
                 validate_target, feedback, clock, sleep):
        envelope = [{n: current[n] if n == "gripper" else current[n] + sign * m.MAX_EXCURSION
                     for n in m.MOTORS} for sign in (-1, 1)]
        super().__init__(arm, envelope, log, clock=clock, sleep=sleep)
        self.previous = dict(current)
        self.origin = dict(current)
        self.leader = leader
        self.guard, self.permission = guard, permission
        self.observe, self.leader_feedback = observe, leader_feedback
        self.validate_target, self.feedback = validate_target, feedback
        self.engaged = False
        self.last_input = None
        self.floating = dict(current)
        self.limited = False

    def read_leader(self):
        start = self.clock()
        self.leader.require_torque(False)
        current = self.leader.read_raw()
        m.require(self.clock() - start <= m.MAX_IO_TIME, "Leader feedback is stale.")
        self.leader_feedback(current)
        return current

    def check_leader_limits(self, current):
        # The input gripper is ignored, but its raw reading and OFF flag are checked.
        for name in m.MOTORS[:-1]:
            cal = self.leader.calibration[name]
            m.require(cal["range_min"] + m.JOINT_MARGIN <= current[name] <= cal["range_max"] - m.JOINT_MARGIN,
                      f"Leader {name} is near/outside its calibrated range. Pause and reposition it.")

    def arm_here(self):
        self.guard()
        self.read_leader()
        current = m.stable_pose(self.arm, self.sleep)
        m.require(m.distance(current, self.origin) <= 2 and current["gripper"] == self.origin["gripper"],
                  "Follower moved before the supported hold. Position it again and retry.")
        self.guard()
        self.enabled = True  # A partial enable must enter measured-hold fault handling.
        self.arm.arm_at_current(current)
        self.previous, self.floating = dict(current), dict(current)
        self.tick(current, "leader_hold")

    def resume(self):
        self.guard()
        m.require(self.permission(), "Keep the operator page visible before engaging the leader.")
        current = self.read_leader()
        self.check_leader_limits(current)
        # Fresh reference pair: never jump to the leader's absolute pose.
        self.tick(self.previous, "leader_hold")
        self.last_input = self.read_leader()
        self.check_leader_limits(self.last_input)
        self.floating = dict(self.previous)
        self.engaged = True

    def pause(self):
        self.engaged = False
        self.last_input = None
        self.tick(self.previous, "leader_pause")

    def tick(self, _target=None, stage="leader_tick"):
        self.guard()
        start = self.clock()
        if self.last_tick is not None:
            m.require(start - self.last_tick <= m.MAX_IO_TIME, "Leader control loop stalled; no catch-up motion issued.")
        source = self.read_leader()
        current = self.arm.read()
        self.arm.require_torque(True)
        m.require(self.clock() - start <= m.MAX_IO_TIME, "Arm feedback is stale; no leader target sent.")
        self.inside(current)
        m.require(m.distance(current, self.previous) <= m.TRACKING_TOLERANCE, "Follower is not tracking the preceding leader target.")
        m.require(abs(current["gripper"] - self.origin["gripper"]) <= m.GRIPPER_TOLERANCE, "Follower gripper opening changed.")
        self.observe(current)
        if not self.permission():
            self.engaged = False
        self.limited = False
        if self.engaged:
            self.check_leader_limits(source)
            m.require(m.distance(source, self.last_input) <= MAX_INPUT_GAP,
                      "Leader input jumped or crossed an encoder wrap; following stopped.")
            # Incremental mapping discards excess input, so a fast hand movement
            # never leaves a queued destination for the follower to catch up to.
            for name in m.MOTORS[:-1]:
                delta = GAIN * (source[name] - self.last_input[name])
                limit = m.SPEED * m.PERIOD
                self.limited |= abs(delta) > limit
                self.floating[name] += max(-limit, min(limit, delta))
            target = {n: round(v) for n, v in self.floating.items()}
            self.last_input = dict(source)
        else:
            # Pause/visibility timeout holds the measured pose, discarding lag.
            target = dict(current)
            self.floating = dict(current)
            self.last_input = None
        target["gripper"] = self.origin["gripper"]
        self.inside(target)
        m.require(m.distance(target, self.origin) <= m.MAX_EXCURSION, "Leader motion left the local teaching area. Reposition with torque off.")
        self.arm.check_pose(target)
        m.require(m.distance(target, current) <= m.TRACKING_TOLERANCE, "Next leader target is too far from measured position.")
        self.validate_target(target)
        self.guard()
        m.require(self.clock() - start <= m.MAX_IO_TIME, "Leader command preparation is stale.")
        self.arm.send(target)
        m.require(self.clock() - start <= m.MAX_IO_TIME, "Leader motor command took too long.")
        self.previous, self.last_tick = dict(target), start
        self.record(stage=stage, actual=current, target=target, leader=source, following=self.engaged, limited=self.limited)
        self.feedback(current, "leader_following" if self.engaged else "leader_paused")
        self.sleep(max(0, m.PERIOD - (self.clock() - start)))
        return current

    def stop(self):
        self.engaged = False
        super().stop()
