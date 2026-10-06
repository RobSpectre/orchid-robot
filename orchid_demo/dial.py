"""A taught dial nudge: approach, turn, lift off, return clear. Never reverse in contact."""
from . import motion as m

MARKERS = ("contact_index", "turn_index", "release_index")


def validate(entry, complete=False):
    # Retain all existing pose, grip, sample-gap, and local-workspace bounds.
    m.validate_entry({**entry, "touch_index": None, "complete": False})
    m.require(entry.get("direction") in ("cw", "ccw"), "Choose a dial direction.")
    m.require(entry.get("contact_method") == "fixed_pad", "Dial training requires a fixed padded contact surface.")
    m.require(entry.get("padded_tip") is True, "Dial contact requires a padded surface.")
    path = entry["path"]
    indexes = []
    for marker in MARKERS:
        value = entry.get(marker)
        if value is not None:
            m.require(type(value) is int and 0 < value < len(path), f"Invalid dial {marker}.")
            indexes.append(value)
    m.require(indexes == sorted(set(indexes)), "Dial stages must be captured in order.")
    contact = entry.get("contact_index")
    if contact is not None:
        end = entry.get("release_index", len(path) - 1)
        for pose in path[contact:end + 1]:
            m.require(m.distance(path[contact], pose) <= m.MAX_CONTACT_EXCURSION,
                      "Dial contact motion is too large. Re-teach a smaller nudge and lift.")
    if complete:
        m.require(entry.get("complete") is True and len(indexes) == 3,
                  "Capture dial clearance, contact, turn, lift-off, and return before testing.")
        contact, turn, release = indexes
        m.require(release < len(path) - 1, "Record the clear return after lifting off the dial.")
        for a, b in ((0, contact), (contact, turn), (turn, release)):
            m.require(m.distance(path[a], path[b]) > 0, "Each dial stage must contain a measured movement.")
        m.require(m.distance(path[0], path[-1]) <= m.START_TOLERANCE,
                  "Return near the initial clearance before holding. Follow the live return error; do not force the arm.")
        for field in ("reference", "expected_effect"):
            m.require(isinstance(entry.get(field), str) and 1 <= len(entry[field].strip()) <= 160,
                      "Describe the reference chord/view and expected voicing change.")
        m.require(budget(path) <= m.MAX_RUN_TIME, "Dial path is too long; re-teach a smaller local gesture.")


def budget(path):
    alignment = dict(path[0])
    alignment["wrist_flex"] += m.START_TOLERANCE
    return (3 + len(list(m.segment(alignment, path[0]))) * m.PERIOD + 1
            + sum(len(list(m.segment(a, b))) * m.PERIOD + 1 for a, b in zip(path, path[1:])))


def plan(entry):
    validate(entry, complete=True)
    return [{**pose, "gripper": entry["path"][0]["gripper"]} for pose in entry["path"]]


def run(controller, entry):
    """Execute the recorded forward loop once, finishing clear at its start area."""
    validate(entry, complete=True)
    m.require(controller.enabled, "Establish a supported hold before testing the dial.")
    current = controller.tick(controller.previous, "dial_pre_trial")
    controller.check_start(current)
    controller.started = controller.clock()
    for _ in range(round(3 / m.PERIOD)):
        controller.tick(current, "dial_armed_hold")
    for target in m.segment(current, controller.path[0]):
        controller.tick(target, "dial_clear_alignment")
    controller.settle(controller.path[0], "dial_start")
    for i, (a, b) in enumerate(zip(controller.path, controller.path[1:]), 1):
        stage = ("dial_approach" if i <= entry["contact_index"] else
                 "dial_turn" if i <= entry["turn_index"] else
                 "dial_lift_off" if i <= entry["release_index"] else "dial_clear_return")
        for target in m.segment(a, b):
            controller.tick(target, stage)
        controller.settle(b, stage + "_settle")
    controller.record(stage="dial_complete", direction=entry["direction"], reference=entry["reference"])
    controller.started = None
