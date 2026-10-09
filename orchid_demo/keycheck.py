"""Check what Orchid actually sent while the arm played, using Orchid Studio's key monitor.

Studio (github.com/RobSpectre/orchid-studio, `--sound-input Orchid`) records each key press with its
name, velocity and time.monotonic() arrival time; this app plays on the same clock. After a play the
check reads those events for the play's time window and compares them with what was played:

* keys        the right note name sounded once; velocity, when in the press stroke, how long it held
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
    def at(t):  # recording time -> clock time
        return started + t / speed

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
