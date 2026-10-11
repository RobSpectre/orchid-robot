"""Physical Orchid controls the arms play: the 12 keys and the chord buttons."""
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
CATALOG = {name: {"id": name, "label": name, "name": name, "kind": "key", "group": "keys"} for name in KEYS}
CATALOG.update({name: {"id": name, "label": label, "name": title, "kind": "button", "group": "chords", "row": row}
                for name, label, title, row in CHORDS})
EXTRA_CONTROLS = tuple(name for name in CATALOG if name not in KEYS)


def group_members(control):
    return tuple(name for name, value in CATALOG.items() if value["group"] == CATALOG[control]["group"])
