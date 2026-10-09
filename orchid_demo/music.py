"""Musical time for played controls: note values, Orchid Studio's beat clock, and when each strike should land.

Durations are note values, a quarter note being one beat: "1/4", "1/8", "1/2", "1", "1/16"; a trailing "." dots one
(x1.5) and a trailing "t" makes it a triplet (x2/3): "1/8.", "1/4t". The letters w h q e s (whole, half, quarter,
eighth, sixteenth) work too: "q", "e.", "et".

Tempo and the beat grid come from Orchid Studio's timeline, the clock its MIDI clock output (24 PPQN) is made from,
read over its loopback API with the time.monotonic() stamp both programs share. While Studio's transport runs, a
play's first strike lands on the next beat of that grid; otherwise the phrase keeps its own time from its first note.
A note the arm cannot reach in time slips by whole beats, and every later note slips with it, so the rhythm holds.
"""
from __future__ import annotations

from fractions import Fraction
import json
import math
import re
import urllib.error
import urllib.request

from . import motion as m

LETTERS = {"w": Fraction(4), "h": Fraction(2), "q": Fraction(1), "e": Fraction(1, 2), "s": Fraction(1, 4)}
NOTATION = re.compile(r"^(?:(?P<letter>[whqes])|(?P<num>\d+)(?:/(?P<den>\d+))?)(?P<mod>[.t]?)$")
MAX_BEATS = 240  # sixty whole notes; the arm holds a note up to a minute (teach.MAX_PRESS_S)
DEFAULT_DURATION = "1/8"


def beats(value) -> float:
    """A note value in beats (quarter note = 1). Raises SafetyError with how to write it."""
    match = NOTATION.match(value.strip()) if isinstance(value, str) else None
    m.require(match, f"Write a duration as a note value: 1/4, 1/8, 1/2, 1, a dotted 1/8. or a triplet 1/8t (not {value!r}).")
    if match["letter"]:
        length = LETTERS[match["letter"]]
    else:
        m.require(match["den"] is None or int(match["den"]) in (1, 2, 4, 8, 16, 32, 64),
                  f"{value!r}: the note value's lower number must be 1, 2, 4, 8, 16, 32 or 64.")
        length = Fraction(int(match["num"])) * 4 / int(match["den"] or 1)
    length *= {"": 1, ".": Fraction(3, 2), "t": Fraction(2, 3)}[match["mod"]]
    m.require(0 < length <= MAX_BEATS, f"{value!r} is out of range: up to sixty whole notes.")
    return float(length)


class StudioClock:
    """Orchid Studio's beat timeline over its loopback HTTP API (command "clock")."""

    def __init__(self, port, timeout=0.5):
        self.url, self.timeout = f"http://127.0.0.1:{port}/command", timeout

    def __call__(self):
        request = urllib.request.Request(self.url, data=json.dumps({"command": "clock"}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise m.SafetyError(f"Orchid Studio is not reachable on {self.url} ({getattr(exc, 'reason', exc)}): musical "
                                "durations follow its tempo and beat. Start Orchid Studio, then play again.") from None
        clock = reply.get("clock") if reply.get("status") == "ok" else None
        m.require(clock, f"Orchid Studio did not report its clock ({reply.get('error') or 'update Orchid Studio'}).")
        return clock


def fixed_clock(bpm=120.0):
    """A stand-in for Studio's clock (simulation and tests): a tempo, and no running transport."""
    return lambda: {"bpm": bpm, "beat": 0.0, "running": False, "paused": False, "t": None}


def timing(clock: dict) -> dict:
    """What the motor loop needs from a clock reading: seconds per beat, and the beat grid if the transport runs."""
    bpm = float(clock["bpm"])
    m.require(20 <= bpm <= 300, f"Orchid Studio reports {bpm:g} BPM; set a tempo from 20 to 300.")
    running = clock.get("running") and not clock.get("paused") and clock.get("t") is not None
    return {"bpm": bpm, "beat_s": 60.0 / bpm,
            "grid": {"t": float(clock["t"]), "beat": float(clock["beat"])} if running else None}


class Schedule:
    """When each strike should land. offsets: each timed step's position in beats from the phrase's first note.
    The first strike goes on the next grid beat it can make (or as soon as it can, with no grid, or at `at`); every
    later one at its offset from there. A strike that cannot be made slips the rest of the phrase by whole beats."""

    def __init__(self, timing: dict, offsets: list, at=None):
        self.beat_s, self.grid, self.offsets, self.at = timing["beat_s"], timing.get("grid"), list(offsets), at
        self.origin, self.slip_beats, self.report = None, 0, []

    def strike(self, i, earliest):
        """The time step i's strike should land, given the earliest it can (both time.monotonic())."""
        if self.origin is None:
            if self.at is not None:
                self.origin = self.at
            elif self.grid:
                beat = self.grid["beat"] + (earliest - self.grid["t"]) / self.beat_s
                self.origin = self.grid["t"] + (math.ceil(beat - 1e-6) - self.grid["beat"]) * self.beat_s
            else:
                self.origin = earliest
        target = self.origin + (self.offsets[i] - self.offsets[0] + self.slip_beats) * self.beat_s
        late = 0
        if earliest > target + 1e-6:
            late = math.ceil((earliest - target) / self.beat_s - 1e-6)
            self.slip_beats += late
            target += late * self.beat_s
        self.report.append({"step": i, "strike_at": round(target, 4), "slipped_beats": late})
        return target
