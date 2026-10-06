"""Physical Orchid controls and independently trained relative dial gestures."""
from .motion import KEYS

CHORDS = (
    ("chord.dim", "Dim", "Diminished", "type"),
    ("chord.min", "Min", "Minor", "type"),
    ("chord.maj", "Maj", "Major", "type"),
    ("chord.sus", "Sus", "Suspended", "type"),
    ("chord.6", "6", "Sixth", "extension"),
    ("chord.m7", "m7", "Minor seventh", "extension"),
    ("chord.M7", "M7", "Major seventh", "extension"),
    ("chord.9", "9", "Ninth", "extension"),
)
DIALS = (("voicing.cw", "Clockwise", "cw"), ("voicing.ccw", "Counterclockwise", "ccw"))
CATALOG = {name: {"id": name, "label": name, "name": name, "kind": "key", "group": "keys"} for name in KEYS}
CATALOG.update({name: {"id": name, "label": label, "name": title, "kind": "button", "group": "chords", "row": row}
                for name, label, title, row in CHORDS})
CATALOG.update({name: {"id": name, "label": label, "name": f"Voicing · {label.lower()}", "kind": "dial",
                       "group": "voicing", "direction": direction} for name, label, direction in DIALS})
EXTRA_CONTROLS = tuple(name for name in CATALOG if name not in KEYS)


def group_members(control):
    return tuple(name for name, value in CATALOG.items() if value["group"] == CATALOG[control]["group"])
