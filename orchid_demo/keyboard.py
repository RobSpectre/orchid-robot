"""Where Orchid's keys are, fitted from the Keys Arm's taught presses, for correcting keys (tune.py, engine.tune_*).

The keys lie in a row: a white key every slot (C D E F G A B), a black key half a slot along and further back. Fitting
the taught presses (modelled fingertips, plate mm) to that pattern gives the keyboard's direction along the row (`along`,
low to high), which way is back (`back`, toward the black keys), the slot pitch and how far back the black keys are.

The model's absolute positions can be 2 cm off, but every key is off the same way, so the pattern still fits. A key
whose press sits away from the pattern is probably taught off-centre. When a neighbour sounds instead, the direction
to move is from that neighbour's slot to this key's.
"""
from __future__ import annotations

import math

from . import kinematics

SLOTS = {"C": (0.0, 0), "C#": (0.5, 1), "D": (1.0, 0), "D#": (1.5, 1), "E": (2.0, 0), "F": (3.0, 0), "F#": (3.5, 1),
         "G": (4.0, 0), "G#": (4.5, 1), "A": (5.0, 0), "A#": (5.5, 1), "B": (6.0, 0)}  # (slot along the row, row: 1 black)
NOMINAL = {"pitch_mm": 17.0, "back_mm": 35.0}  # a guess until three keys are taught
OFF_ALONG_MM = 4.0  # a press this far along the row from the pattern is flagged (or a quarter of the slot pitch, if more)
OFF_BACK_MM = 8.0  # ... or a white key pressed this far back, toward the black keys (black keys work anywhere along)


class Keyboard:
    def __init__(self, tips: dict):
        """tips: {key: (x, y, z)} modelled press fingertips, plate mm."""
        self.tips = {k: tuple(v) for k, v in tips.items() if k in SLOTS}
        keys = list(self.tips)
        fit = [k for k in keys if SLOTS[k][1] == 0]
        if len({SLOTS[k][0] for k in fit}) < 2:
            fit = keys
        if len({SLOTS[k][0] for k in fit}) >= 2:
            mean_s = sum(SLOTS[k][0] for k in fit) / len(fit)
            mean_p = [sum(self.tips[k][i] for k in fit) / len(fit) for i in range(2)]
            var = sum((SLOTS[k][0] - mean_s) ** 2 for k in fit)
            v = [sum((SLOTS[k][0] - mean_s) * (self.tips[k][i] - mean_p[i]) for k in fit) / var for i in range(2)]
            self.pitch = math.hypot(*v) or NOMINAL["pitch_mm"]
            self.along = (v[0] / self.pitch, v[1] / self.pitch)
            self.origin = (mean_p[0] - v[0] * mean_s, mean_p[1] - v[1] * mean_s)
        else:  # one key or none: Orchid's keyboard runs along the plate's x axis (layout.ORCHID)
            self.pitch, self.along = NOMINAL["pitch_mm"], (1.0, 0.0)
            anchor = next(iter(self.tips.values()), (0.0, 0.0, 0.0))
            key = next(iter(self.tips), "C")
            self.origin = (anchor[0] - SLOTS[key][0] * self.pitch, anchor[1])
        self.back = (-self.along[1], self.along[0])  # +90 deg: away from the plate's front edge, toward the arms
        offsets = {row: [self._local(k)[1] for k in keys if SLOTS[k][1] == row] for row in (0, 1)}
        if offsets[0] and offsets[1]:
            self.black_back = sum(offsets[1]) / len(offsets[1]) - sum(offsets[0]) / len(offsets[0])
        else:
            self.black_back = NOMINAL["back_mm"]
        if self.black_back < 0:  # the black keys are further from the player: point `back` at them
            self.back, self.black_back = (-self.back[0], -self.back[1]), -self.black_back
        self.white_back = sum(offsets[0]) / len(offsets[0]) if offsets[0] else 0.0
        if self.back != (-self.along[1], self.along[0]):  # flipped: recompute the white row in the new frame
            self.white_back = -self.white_back

    def _local(self, key):
        return self.local(self.tips[key])

    def expected(self, key):
        """(along, back) where the pattern puts this key's press."""
        slot, row = SLOTS[key]
        return slot * self.pitch, self.white_back + row * self.black_back

    def offsets(self):
        """{key: (along_mm, back_mm)}: each taught press from where the other keys' pattern puts it (fitted without
        it, so a key taught off-centre cannot pull the pattern toward itself)."""
        result = {}
        for key in self.tips:
            others = Keyboard({k: v for k, v in self.tips.items() if k != key}) if len(self.tips) >= 4 else self
            got, want = others.local(self.tips[key]), others.expected(key)
            result[key] = (round(got[0] - want[0], 1), round(got[1] - want[1], 1))
        return result

    def local(self, tip):
        """(along, back) of a fingertip from the row's origin, mm."""
        d = (tip[0] - self.origin[0], tip[1] - self.origin[1])
        return d[0] * self.along[0] + d[1] * self.along[1], d[0] * self.back[0] + d[1] * self.back[1]

    def report(self):
        """Keys taught off the pattern, with which way they are off, once three keys define it."""
        if len(self.tips) < 3:
            return {"fitted": False, "pitch_mm": round(self.pitch, 1), "keys": {}}
        limit = max(OFF_ALONG_MM, self.pitch / 4)
        keys = {}
        for key, (along, back) in self.offsets().items():
            back = back if SLOTS[key][1] == 0 and back > 0 else 0.0
            if abs(along) > limit or back > OFF_BACK_MM:
                parts = []
                if abs(along) > limit:
                    lower, higher = self.neighbours(key)
                    toward = higher if along > 0 else lower
                    parts.append(f"{abs(along):.0f} mm toward {toward}" if toward else
                                 f"{abs(along):.0f} mm toward the {'high' if along > 0 else 'low'} end")
                if back > OFF_BACK_MM:
                    parts.append(f"{back:.0f} mm too far back, toward the black keys")
                keys[key] = {"along_mm": along, "back_mm": back, "text": f"{key} is " + " and ".join(parts)}
        return {"fitted": True, "pitch_mm": round(self.pitch, 1), "keys": keys}

    def neighbours(self, key):
        order = sorted(SLOTS, key=lambda k: SLOTS[k][0])
        i = order.index(key)
        return (order[i - 1] if i else None), (order[i + 1] if i + 1 < len(order) else None)

    def correction(self, key, sounded):
        """Which way to move this key's stroke when a neighbour sounded instead, and how far there is between them
        (mm): ((x, y, 0) unit vector on the plate, distance), or None if they are not neighbours (more than a slot
        apart: too far off to correct by moving).

        A black key that sounded its white neighbour: move along the row toward the black key. A white key's back part
        runs right alongside the black keys, so moving toward the white key's middle (mostly forward or back) would
        not leave it. A white key that sounded a black key: move forward, onto the white key's wide front, clear of the
        black keys' row. Two white keys: along the row, away from the neighbour."""
        if sounded not in SLOTS or key not in SLOTS or abs(SLOTS[key][0] - SLOTS[sounded][0]) > 1.0:
            return None
        (slot, row), (other_slot, other_row) = SLOTS[key], SLOTS[sounded]
        if row == 0 and other_row == 1:
            return (-self.back[0], -self.back[1], 0.0), self.black_back
        toward = 1.0 if slot > other_slot else -1.0
        return (toward * self.along[0], toward * self.along[1], 0.0), abs(slot - other_slot) * self.pitch

    def toward(self, key, sounded):
        """How far the pattern says this key's press already sits toward that neighbour, along the row (mm, >= 0)."""
        along = self.offsets().get(key, (0.0, 0.0))[0]
        return max(0.0, along if SLOTS[sounded][0] > SLOTS[key][0] else -along)

    def spacing(self, key, other):
        """How far apart two keys' middles are, mm (from the fitted pattern)."""
        (a1, b1), (a0, b0) = self.expected(key), self.expected(other)
        return math.hypot(a1 - a0, b1 - b0)

    def along3(self):
        return (self.along[0], self.along[1], 0.0)


def press_tips(points_by_key: dict, calibration: dict, arm: str) -> dict:
    """{key: press fingertip (plate mm)} from each key's points."""
    return {key: kinematics.fingertip(points["press"]["goal"], calibration, arm)[0] for key, points in points_by_key.items()}
