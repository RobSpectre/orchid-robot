"""Check what Orchid actually sent while the arm played, using Orchid Studio's key monitor.

Studio (github.com/RobSpectre/orchid-studio, `--sound-input Orchid`) records each key press with its
name, velocity and time.monotonic() arrival time; this app plays on the same clock. After a play the
check reads those events for the play's time window and compares them with what was played:

* keys        the right note name sounded once; velocity, when in the press stroke, how long it held
* key + chord (the other arm holding a chord button) the press sounded several notes, the key among them, and the
              chord's tones: pitch classes above the key, so the voicing dial's inversions and octaves do not matter
* chord       buttons send no MIDI on their own, so none is expected (a note would be a stray press)
* dial        voicing-dial clicks (CC115 positions) moved during the turn

Read-only: never changes motion, and a missing or stopped Studio only marks the check unavailable.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request

from .controls import CATALOG

STUDIO_PORT = 8765
NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
# Semitones above the key that each chord button must add (as pitch classes). Sus may be sus2 or sus4.
CHORD_TONES = {"chord.dim": ({3}, {6}), "chord.min": ({3}, {7}), "chord.maj": ({4}, {7}), "chord.sus": ({2, 5}, {7}),
               "chord.6": ({9},), "chord.m7": ({10},), "chord.M7": ({11},), "chord.9": ({2},)}
INTERVAL_NAMES = ("1", "b2", "2", "b3", "3", "4", "b5", "5", "#5", "6", "b7", "7")
SETTLE_S = 0.25  # let the last note-off reach Studio before reading
MARGIN_S = 0.15  # around each step's time window: MIDI and servo timing are not exact


class StudioKeys:
    """Studio's key-events command over its loopback HTTP API."""

    def __init__(self, port=STUDIO_PORT, timeout=0.5):
        self.url, self.timeout = f"http://127.0.0.1:{port}/command", timeout

    def __call__(self):
        request = urllib.request.Request(self.url, data=json.dumps({"command": "key-events", "after": 0}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise LookupError(f"Orchid Studio is not reachable on {self.url} ({reason}). "
                              "Start it with --sound-input Orchid to check notes.") from None
        if reply.get("status") != "ok":
            raise LookupError(f"Orchid Studio: {reply.get('error') or 'key events unavailable'}. "
                              "Restart it with --sound-input Orchid (key monitor).")
        return reply["events"]


def _seconds(value):
    return None if value is None else round(value, 3)


def check_step(step, started, speed, events):
    """One played control against the events inside its window on the shared clock."""
    control = step["key"]
    item = CATALOG[control]
    clock = step.get("clock") or {"t": started, "at": 0.0}  # a timed play waited at gates: time from the last one

    def at(t):  # recording time -> clock time
        return clock["t"] + (t - clock["at"]) / speed

    low, high = at(step["start"]) - MARGIN_S, at(step["end"]) + MARGIN_S
    inside = [e for e in events if low <= e["t"] <= high]
    presses = [e for e in inside if e["type"] == "press"]
    result = {"control": control, "name": item["name"], "kind": item["kind"], "notes": [e["name"] for e in presses]}
    if item["kind"] == "dial":
        clicks = [e for e in inside if e["type"] == "voicing"]
        moved = sum(e["delta"] or 0 for e in clicks)
        status = "ok" if clicks else "missed"
        text = f"{item['label']} turned the dial {moved:+d} clicks" if clicks else f"{item['label']}: the dial did not click"
        return {**result, "status": status, "clicks": moved, "text": text}
    if item["kind"] == "button":
        if presses:
            return {**result, "status": "stray", "text": f"{item['name']}: a key sounded ({', '.join(result['notes'])})"}
        return {**result, "status": "unchecked", "text": f"{item['name']}: no MIDI expected from a chord button"}
    expected = item["label"]
    if step.get("chord"):
        return check_chord(step, result, presses, at)
    if not presses:
        return {**result, "status": "missed", "text": f"{expected}: missed, no note"}
    if any(e["name"] != expected for e in presses):
        heard = ", ".join(result["notes"])
        return {**result, "status": "wrong", "text": f"{expected}: wrong key, {heard} sounded"}
    if len(presses) > 1:
        return {**result, "status": "repeated", "t": presses[0]["t"], "text": f"{expected}: sounded {len(presses)} times"}
    note = presses[0]
    marks = step.get("marks") or {}
    # Studio rounds press times; match the release by nearness, not equality.
    release = next((e for e in inside if e["type"] == "release" and abs(e.get("press_t", -1e9) - note["t"]) < 1e-3), None)
    timing = {}
    if "touch" in marks and "press" in marks:
        timing = {"into_stroke_s": _seconds(note["t"] - at(marks["touch"])),
                  "stroke_s": _seconds((marks["press"] - marks["touch"]) / speed),
                  "vs_bottom_s": _seconds(note["t"] - at(marks["press"]))}
    held = release["held_s"] if release else None
    text = f"{expected} ✓ velocity {note['velocity']}"
    if timing:
        text += f", {timing['into_stroke_s']:.2f} s into the {timing['stroke_s']:.2f} s press"
    text += f", held {held:.2f} s" if held is not None else ", release not seen"
    return {**result, "status": "ok", "t": note["t"], "velocity": note["velocity"], "octave": note.get("octave"),
            "held_s": held, **timing, "text": text}


def check_chord(step, result, presses, at):
    """A key played with a chord button held: Orchid sends the whole chord as one press (Studio groups it)."""
    chord = CATALOG[step["chord"]]
    label = f"{CATALOG[step['key']]['label']} + {chord['label']}"
    key = NAMES.index(CATALOG[step["key"]]["label"])
    mine = [e for e in presses if key in {n % 12 for n in e.get("notes") or ()}]
    result = {**result, "chord": step["chord"]}
    if not presses:
        return {**result, "status": "missed", "text": f"{label}: missed, no note"}
    if not mine:
        return {**result, "status": "wrong", "text": f"{label}: wrong key, {', '.join(result['notes'])} sounded"}
    if len(presses) > 1:
        return {**result, "status": "repeated", "t": mine[0]["t"], "text": f"{label}: sounded {len(presses)} times"}
    note = mine[0]
    tones = sorted({(n - key) % 12 for n in note["notes"]})
    heard = " ".join(INTERVAL_NAMES[t] for t in tones)
    result = {**result, "t": note["t"], "velocity": note["velocity"], "tones": tones,
              "chord_notes": [NAMES[n % 12] for n in note["notes"]]}
    if len(tones) < 2:
        return {**result, "status": "no_chord", "text": f"{label}: only {NAMES[key]} sounded; the chord button was not held"}
    missing = [group for group in CHORD_TONES.get(step["chord"], ()) if not group & set(tones)]
    if missing:
        return {**result, "status": "wrong_chord", "text": f"{label}: not a {chord['name'].lower()} chord (heard {heard})"}
    return {**result, "status": "ok", "text": f"{label} ✓ {' '.join(result['chord_notes'])} ({heard}), velocity {note['velocity']}"}


def check(context, events):
    steps = [check_step(step, context["started"], context["speed"], events) for step in context["steps"]]
    good = all(s["status"] in ("ok", "unchecked") for s in steps)
    return {"play": context["play"], "status": "ok" if good else "problem", "steps": steps,
            "summary": "; ".join(s["text"] for s in steps)}


class KeyChecker:
    """Runs checks off the motor loop; the engine collects finished results with poll()."""

    def __init__(self, fetch, *, settle_s=SETTLE_S, background=True, sleep=time.sleep):
        self.fetch, self.settle_s, self.background, self.sleep = fetch, settle_s, background, sleep
        self.results = queue.Queue()

    def request(self, context):
        if self.background:
            threading.Thread(target=self._run, args=(context,), name="orchid-key-check", daemon=True).start()
        else:
            self._run(context)

    def _run(self, context):
        if self.background:
            self.sleep(self.settle_s)
        try:
            result = check(context, self.fetch())
        except LookupError as exc:
            result = {"play": context["play"], "status": "unavailable", "steps": [], "summary": str(exc)}
        except Exception as exc:  # a broken reply must never reach the motor loop
            result = {"play": context["play"], "status": "unavailable", "steps": [], "summary": f"Key check failed: {exc}"}
        self.results.put(result)

    def poll(self):
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None
