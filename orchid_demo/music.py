"""Musical time for played controls: note values, Orchid Studio's beat clock, and when each strike should land.

Durations are note values, a quarter note being one beat: "1/4", "1/8", "1/2", "1", "1/16"; a trailing "." dots one
(x1.5) and a trailing "t" makes it a triplet (x2/3): "1/8.", "1/4t". The letters w h q e s (whole, half, quarter,
eighth, sixteenth) work too: "q", "e.", "et". Bars are Studio's 4/4 bars: "2bars", "1bar", "1.5bars"; there is no
upper limit, so a chord can sweep on for as many bars as wanted (Stop motion ends it any time). "+" ties values
together: "2bars+1/2", "1/4+1/16".

Tempo and the beat grid come from Orchid Studio's timeline, the clock its MIDI clock output (24 PPQN) is made from,
read over its loopback API with the time.monotonic() stamp both programs share. While Studio's transport runs, a
play's first strike lands on the next beat of that grid; otherwise the phrase keeps its own time from its first note.
A note the arm cannot reach in time slips by whole beats, and every later note slips with it, so the rhythm holds.
A note it misses by no more than LATE_OK_S is played that little late instead: waiting a beat or bar would cost more.
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
BARS = re.compile(r"^(?P<bars>\d+(?:\.\d+)?)\s*bars?$")
BEATS_PER_BAR = 4  # Studio's bars (its loops and drum patterns are 4/4)
DEFAULT_DURATION = "1/8"
# A note the arm can only reach this late is played late rather than moved to the next beat (or bar, in a score):
# under about 1/30 of a beat at 76 BPM, too little to hear, where waiting would cost a whole beat or bar.
LATE_OK_S = 0.08
HOW = "Write a duration as a note value (1/4, 1/8, 1/2, 1, dotted 1/8., triplet 1/8t), bars (2bars), or tied with + (1bar+1/4)"


def beats(value) -> float:
    """A duration in beats (quarter note = 1): note values and bars, tied with "+". Raises SafetyError with how to write it."""
    m.require(isinstance(value, str) and value.strip(), f"{HOW}, not {value!r}.")
    total = sum(_part(part.strip(), value) for part in value.split("+"))
    m.require(total > 0, f"{value!r} has no length. {HOW}.")
    return float(total)


def _part(text, value):
    bars = BARS.match(text)
    if bars:
        return Fraction(bars["bars"]) * BEATS_PER_BAR
    match = NOTATION.match(text)
    m.require(match, f"{HOW}, not {value!r}.")
    if match["letter"]:
        length = LETTERS[match["letter"]]
    else:
        m.require(match["den"] is None or int(match["den"]) in (1, 2, 4, 8, 16, 32, 64),
                  f"{value!r}: the note value's lower number must be 1, 2, 4, 8, 16, 32 or 64.")
        length = Fraction(int(match["num"])) * 4 / int(match["den"] or 1)
    return length * {"": 1, ".": Fraction(3, 2), "t": Fraction(2, 3)}[match["mod"]]


class Studio:
    """Orchid Studio's loopback HTTP API (its /command endpoint)."""

    def __init__(self, port, timeout=2.0):
        self.url, self.timeout = f"http://127.0.0.1:{port}/command", timeout

    def __call__(self, command, **fields):
        request = urllib.request.Request(self.url, data=json.dumps({"command": command, **fields}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read())
        except urllib.error.HTTPError as exc:  # Studio answers a refused command with an error body
            try:
                reply = json.loads(exc.read())
            except ValueError:
                reply = {"status": "error", "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise m.SafetyError(f"Orchid Studio is not reachable on {self.url} ({getattr(exc, 'reason', exc)}). "
                                "Start Orchid Studio, then try again.") from None
        m.require(reply.get("status") != "error", f"Orchid Studio refused {command}: {reply.get('error') or 'no reason given'}.")
        return reply


class StudioClock:
    """Orchid Studio's beat timeline (command "clock")."""

    def __init__(self, port, timeout=0.5):
        self.studio = Studio(port, timeout)

    def __call__(self):
        try:
            reply = self.studio("clock")
        except m.SafetyError as exc:
            raise m.SafetyError(f"{exc} Musical durations follow its tempo and beat.") from None
        clock = reply.get("clock")
        m.require(clock, "Orchid Studio did not report its clock (update Orchid Studio).")
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

    def __init__(self, timing: dict, offsets: list, at=None, align=1, phase=0.0, slip=1):
        """align, phase: start where Studio's beat count is phase beats past a multiple of align (a score: its first
        note on its own beat of the bar, align=BEATS_PER_BAR). slip: a late note moves this many beats at a time (a
        score: whole bars, so every note stays on the beat of the bar it was written on)."""
        self.beat_s, self.grid, self.offsets, self.at = timing["beat_s"], timing.get("grid"), list(offsets), at
        self.align, self.phase, self.slip = align, phase, slip
        self.origin, self.slip_beats, self.report = None, 0, []

    def strike(self, i, earliest):
        """The time step i's strike should land, given the earliest it can (both time.monotonic())."""
        if self.origin is None:
            if self.at is not None:
                self.origin = self.at
            elif self.grid:
                beat = self.grid["beat"] + (earliest - self.grid["t"]) / self.beat_s
                start = self.phase + self.align * math.ceil((beat - self.phase) / self.align - 1e-6)
                self.origin = self.grid["t"] + (start - self.grid["beat"]) * self.beat_s
            else:
                self.origin = earliest
        target = self.origin + (self.offsets[i] - self.offsets[0] + self.slip_beats) * self.beat_s
        late = 0
        if target + 1e-6 < earliest <= target + LATE_OK_S:  # a hair late: play it now, a few ms off, not a bar later
            self.report.append({"step": i, "strike_at": round(earliest, 4), "slipped_beats": 0, "earliest": round(earliest, 4),
                                "late_ms": round((earliest - target) * 1000)})
            return earliest
        if earliest > target + 1e-6:
            late = self.slip * math.ceil((earliest - target) / (self.beat_s * self.slip) - 1e-6)
            self.slip_beats += late
            target += late * self.beat_s
        # earliest: when it could have sounded, for learning how long changes really take (score.learn)
        self.report.append({"step": i, "strike_at": round(target, 4), "slipped_beats": late, "earliest": round(earliest, 4)})
        return target
