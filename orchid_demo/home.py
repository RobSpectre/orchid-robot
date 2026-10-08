"""A taught home, measured approach/return routes, and bounded supervised playback."""
from copy import deepcopy

from . import motion as m, dial

MAX_TRAVEL_SAMPLES = 1024
MAX_ROUTE_TIME = 600.0
TRAVEL_PHASES = ("home_approach", "home_return")


class HomeArm:
    """Explicit home envelope; ordinary key/CLI adapters retain their margin checks.

    Only the taught home and its start tolerance extend the working margins.
    Absolute recorded travel is never extended. This envelope applies to capture
    and automatic playback; live leader positioning uses the native driver.
    """
    def __init__(self, device, pose):
        self.device = device
        device.check_calibrated_pose(pose)
        self.home = dict(pose)
        self.bounds = {}
        for name, cal in device.calibration.items():
            tolerance = m.GRIPPER_TOLERANCE if name == "gripper" else m.START_TOLERANCE
            self.bounds[name] = (
                max(cal["range_min"], min(cal["range_min"] + m.JOINT_MARGIN, pose[name] - tolerance)),
                min(cal["range_max"], max(cal["range_max"] - m.JOINT_MARGIN, pose[name] + tolerance)))

    def __getattr__(self, name):
        return getattr(self.device, name)

    def check_pose(self, pose):
        self.device.check_calibrated_pose(pose)
        for name, value in pose.items():
            low, high = self.bounds[name]
            m.require(low <= value <= high, f"{name} left the taught home/working envelope {low}..{high}.")
        m.require(abs(pose["gripper"] - self.home["gripper"]) <= m.GRIPPER_TOLERANCE,
                  "Keep the gripper at the taught home opening.")

    def check_clearance(self, pose):
        self.check_pose(pose)
        for name in m.MOTORS[:-1]:
            cal = self.calibration[name]
            m.require(cal["range_min"] + m.JOINT_MARGIN <= pose[name] <= cal["range_max"] - m.JOINT_MARGIN,
                      f"{name} is still near a recorded endpoint. Guide into the working margin before key capture.")

    def read(self):
        pose = self.device.read_raw()
        self.check_pose(pose)
        return pose

    def send(self, pose):
        self.check_pose(pose)
        self.device.send_calibrated(pose)

    def arm_at_current(self, pose):
        self.check_pose(pose)
        self.device.arm_at_calibrated(pose)


def at_home(pose, reference):
    return (m.distance(pose, reference) <= m.START_TOLERANCE
            and abs(pose["gripper"] - reference["gripper"]) <= m.GRIPPER_TOLERANCE)


def append(path, current, previous, *, force=False):
    """Decimate actual observations; never bridge a missing read with invented data."""
    m.pose_valid(current)
    m.pose_valid(previous)
    m.require(m.distance(current, previous) <= m.MAX_POINT_GAP, "Home route feedback jumped; teach the route again.")
    m.require(abs(current["gripper"] - path[0]["gripper"]) <= m.GRIPPER_TOLERANCE,
              "Gripper changed during the home route.")
    if current != path[-1] and (force or m.distance(current, path[-1]) >= m.AUTO_SAMPLE_GAP):
        extra = [dict(previous)] if m.distance(current, path[-1]) > m.MAX_POINT_GAP else []
        m.require(len(path) + len(extra) + 1 <= MAX_TRAVEL_SAMPLES, "Home route has too many samples; teach a simpler route.")
        path.extend(extra + [dict(current)])


def _validate_path(path, arm):
    m.require(isinstance(path, list) and 1 <= len(path) <= MAX_TRAVEL_SAMPLES, "Invalid home route samples.")
    for pose in path:
        arm.check_pose(pose)
    for a, b in zip(path, path[1:]):
        m.require(m.distance(a, b) <= m.MAX_POINT_GAP, "A home route contains a gap between recorded samples.")


def plan(entry):
    travel = entry["home_motion"]
    local = dial.plan(entry) if entry.get("direction") else m.plan(entry)
    stroke = local if entry.get("direction") else local + list(reversed(local[:-1]))
    # Joins are short, explicitly verified clearance poses; never a transit shortcut.
    result = travel["approach"] + stroke + travel["return"]
    grip = travel["approach"][0]["gripper"]
    return [{**p, "gripper": grip} for p in result]


def budget(entry):
    poses = plan(entry)
    alignment = {**poses[0], "wrist_flex": poses[0]["wrist_flex"] + m.START_TOLERANCE}
    return (5.0 + len(list(m.segment(alignment, poses[0]))) * m.PERIOD
            + sum(len(list(m.segment(a, b))) * m.PERIOD + 0.15
                  for a, b in zip(poses, poses[1:])) + 1.0)


def validate(entry, reference, arm):
    travel = entry.get("home_motion")
    m.require(isinstance(travel, dict) and travel.get("home_id") == reference["id"],
              "This route belongs to a different home. Teach it again from the current home.")
    if entry.get("direction"):
        dial.validate(entry, complete=True)
    else:
        m.validate_entry(entry, complete=True)
    for path in (travel.get("approach"), travel.get("return")):
        _validate_path(path, arm)
    for p in entry["path"]:
        arm.check_clearance(p)
    m.require(at_home(travel["approach"][0], reference["pose"]) and at_home(travel["return"][-1], reference["pose"]),
              "Every route must start and end at the taught home.")
    m.require(m.distance(travel["approach"][-1], entry["path"][0]) <= m.START_TOLERANCE,
              "Key clearance does not match the recorded approach. Return to that clearance before capturing.")
    end = entry["path"][-1] if entry.get("direction") else entry["path"][0]
    m.require(m.distance(end, travel["return"][0]) <= m.START_TOLERANCE, "Return route must begin at key clearance.")
    for p in plan(entry):
        arm.check_pose(p)
    grip = travel["approach"][0]["gripper"]
    for p in travel["approach"] + entry["path"] + travel["return"]:
        m.require(abs(p["gripper"] - grip) <= m.GRIPPER_TOLERANCE, "Home route must keep one gripper opening.")
    m.require(budget(entry) <= MAX_ROUTE_TIME, "Home route exceeds the ten-minute playback budget. Teach a shorter route.")


def run(controller, entry):
    """One complete taught route, with an explicit deadline and the normal guards."""
    poses = plan(entry)
    m.require(controller.enabled, "Establish a home hold before testing.")
    current = controller.tick(controller.previous, "home_pre_trial")
    controller.check_start(current)
    controller.started = controller.clock()
    for _ in range(round(3 / m.PERIOD)):
        controller.tick(controller.previous, "home_armed_hold")
    pressed_index = (len(entry["home_motion"]["approach"]) + len(m.plan(entry)) - 1
                     if not entry.get("direction") else None)
    previous = current
    for index, pose in enumerate(poses):
        for target in m.segment(previous, pose):
            controller.tick(target, "home_route")
        # No one-second settle at every travel sample; tracking remains checked on every tick.
        if not entry.get("direction") and index == pressed_index:
            controller.settle(pose, "home_key_pressed")
            for _ in range(round(.2 / m.PERIOD)):
                controller.tick(pose, "home_key_hold")
        previous = pose
    controller.settle(poses[-1], "home_returned")
    controller.record(stage="home_route_complete", home_id=entry["home_motion"]["home_id"])
    controller.started = None


def attach(entry, reference, approach, returning):
    return {**entry, "home_motion": {"home_id": reference["id"], "approach": deepcopy(approach),
                                    "return": deepcopy(returning)}}
