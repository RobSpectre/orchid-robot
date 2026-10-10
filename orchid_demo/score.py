"""A score: chords and keys at bars and beats, planned as one performance on Orchid Studio's beat (api /api/score).

Each event is a key, optionally with a chord button held (the Chord Arm), at a bar and beat of 4/4 (Studio's bars),
sounding for a note value. Every note is held for its written length: long, sustained chords are what Orchid does
best. The plan works out, from the arms' own motion times plus what earlier performances measured (learn), when each
change can be made. Between notes the Keys Arm waits over the last key's hover and goes straight to the next key's
(home only when something else comes between, such as turning the voicing dial); a chord button goes down before its
key, with the Keys Arm holding still. A note that cannot sound on its beat moves later by whole bars, keeping its beat of the bar, and the
rest of the score with it (music.Schedule slip). The performance reports how far each note landed from its beat.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import motion as m
from . import music

OVERHEAD_S = 0.4  # per move: a command reaching the arm, and its settle at the start
CHORD_LETS_GO_S = 0.1  # the chord button is released this long after the key is down (rig.RELEASE_AFTER_S)


def steps(events: list, default_duration: str) -> list:
    """Events -> steps in time order: {control, chord?, offset (beats from bar 1 beat 1), beats, bar, beat}."""
    placed = []
    for event in events:
        bar, beat = event["bar"], event["beat"]
        m.require(type(bar) is int and bar >= 1, f"Bars count from 1 (not {bar!r}).")
        m.require(isinstance(beat, (int, float)) and 1 <= beat < music.BEATS_PER_BAR + 1,
                  f"A beat is 1 to {music.BEATS_PER_BAR + 1} (not inclusive) in a {music.BEATS_PER_BAR}/4 bar, not {beat!r}.")
        offset = (bar - 1) * music.BEATS_PER_BAR + (beat - 1)
        placed.append({"control": event["key"], **({"chord": event["chord"]} if event.get("chord") else {}),
                       "offset": float(offset), "beats": music.beats(event.get("duration") or default_duration),
                       "bar": bar, "beat": beat})
    placed.sort(key=lambda s: s["offset"])
    for before, after in zip(placed, placed[1:]):
        m.require(after["offset"] > before["offset"], f"Two notes at bar {after['bar']} beat {after['beat']:g}: the Keys Arm "
                  "plays one key at a time. Put a chord on one key with its chord button instead.")
    return placed


def change_kind(before: dict, after: dict) -> str:
    """key>key, key>chord, chord>key or chord>chord: what a change between two notes asks of the arms."""
    return f"{'chord' if 'chord' in before else 'key'}>{'chord' if 'chord' in after else 'key'}"


def motion_s(step: dict, nxt: dict, timing, direct: bool) -> float:
    """Seconds the arms' motions take from the end of step's hold until nxt sounds. direct: the Keys Arm goes from
    step's hover straight to nxt's (it waits there between keys), else by way of home."""
    t = timing(step)
    t_next = timing(nxt, step["control"]) if direct else timing(nxt)
    back = (t.get("lift", t["rise"]) if direct else t["rise"]) + OVERHEAD_S
    if "chord" in step:  # the next move waits for the Chord Arm to be home too
        back = max(back, CHORD_LETS_GO_S + t["chord_release"] + OVERHEAD_S)
    return back + t_next["lead"] + (t_next["chord_press"] + OVERHEAD_S if "chord" in nxt else 0.0)


def plan(placed: list, beat_s: float, timing, learned=None, before=None) -> dict:
    """When each note can sound. Every note is held for its written length (long chords are what Orchid is for); a
    note that cannot sound on its beat after the one before is moved later by whole bars, so it stays on the beat of
    the bar it was written on, and so is everything after it. timing(step, after=None) -> {"lead": home (after: that
    key's hover) to the note, "rise": the note back home with no hold, "lift": the note back up to its hover,
    "chord_press": the Chord Arm home to held down (chords), "chord_release": letting go back home (chords)}, seconds
    at the playing speed. learned: {change kind: extra seconds} measured in performances (learn), on top of the motion
    times. before: {note index: seconds of other work before it} (turning the voicing dial, from home)."""
    learned, before = learned or {}, before or {}
    bar_s = music.BEATS_PER_BAR * beat_s
    moved_bars, moves = 0, []
    for i, step in enumerate(placed):
        step["hold_s"] = round(step["beats"] * beat_s, 3)
        step["moved_bars"] = moved_bars
        if i + 1 == len(placed):
            break
        nxt = placed[i + 1]
        gap = (nxt["offset"] - step["offset"]) * beat_s
        motion = motion_s(step, nxt, timing, direct=i + 1 not in before)
        needs = step["hold_s"] + motion + learned.get(change_kind(step, nxt), 0.0) + before.get(i + 1, 0.0)
        step["needs_s"] = round(needs, 2)
        if needs > gap + 1e-9:
            bars = math.ceil((needs - gap) / bar_s - 1e-9)
            moved_bars += bars
            name = f"{nxt['control']}+{nxt['chord'].split('.')[-1]}" if "chord" in nxt else nxt["control"]
            moves.append({"bar": nxt["bar"], "beat": nxt["beat"], "to_bar": nxt["bar"] + moved_bars, "bars": bars,
                          "needs_s": round(needs, 2), "has_s": round(gap, 2),
                          "text": f"{name} at bar {nxt['bar']} beat {nxt['beat']:g} moves {bars} bar{'s' if bars != 1 else ''} "
                                  f"later, to bar {nxt['bar'] + moved_bars}: after the note before is held "
                                  f"{step['hold_s']:.1f} s the change needs {needs - step['hold_s']:.1f} s more, and the score "
                                  f"gives {gap:.1f} s in all"})
    return {"beat_s": beat_s, "beats_per_bar": music.BEATS_PER_BAR, "moves": moves, "moved_bars": moved_bars,
            "length_bars": math.ceil((placed[-1]["offset"] + placed[-1]["beats"]) / music.BEATS_PER_BAR) + moved_bars}


def learn(learned: dict, placed: list, strikes: list, timing, before=None) -> dict:
    """After a performance: for each change, how much longer it really took than the motion times say, from when the
    next note could first have sounded (music.Schedule's earliest). The median of the last few per kind of change
    goes into the next plan."""
    history = {k: list(v) for k, v in (learned.get("history") or {}).items()}
    before = before or {}
    for i in range(len(placed) - 1):
        if i + 1 >= len(strikes) or "earliest" not in strikes[i + 1]:
            break
        step, nxt = placed[i], placed[i + 1]
        model = step["hold_s"] + motion_s(step, nxt, timing, direct=i + 1 not in before) + before.get(i + 1, 0.0)
        took = strikes[i + 1]["earliest"] - strikes[i]["strike_at"]
        kind = change_kind(step, nxt)
        history[kind] = (history.get(kind, []) + [round(took - model, 3)])[-LEARN_CHANGES:]
    extra = {kind: sorted(v)[len(v) // 2] for kind, v in history.items() if v}
    return {"history": history, "extra": {k: max(0.0, round(v, 3)) for k, v in extra.items()}}


LEARN_CHANGES = 9
# Measured on this rig (2026-10-10, 1.2x, 76 BPM): a chord-to-chord change took about 2.2 s longer than the motion
# times alone (the arms' settling, the interlock's waits, commands). Replaced by what each performance measures.
SEEDED = {"chord>chord": 2.2}


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    return {"history": data.get("history") or {}, "extra": {**SEEDED, **(data.get("extra") or {})}}


def save(path: Path, learned: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(learned, indent=1))
    temp.replace(path)


# --- Sound, perform and fx: Orchid Studio settings at the start and at chosen bars -------------------------------

STUDIO_VOICE = 5  # Studio's Live Perform voice: where Orchid's notes sound
CHANGE_LEAD_S = 0.03  # send a change this early so it is in place on the bar line


def setting_steps(settings: dict, perform_running: bool, voices=(STUDIO_VOICE,)) -> list:
    """The Studio commands for one set of settings: [(command, fields, text)]. voices: the Studio voices that sound
    this music: the live voice (5) for the arms' pass, and for a looped score its loop layer's voice (1-4), which
    plays every pass after."""
    steps = []
    if settings.get("sound") is not None:
        steps += [("layer-preset", {"slot": voice, "preset": settings["sound"]}, f"sound {settings['sound']} (voice {voice})")
                  for voice in voices]
    if settings.get("perform"):
        command = "perform-update" if perform_running else "perform-configure"
        steps.append((command, {"settings": settings["perform"]},
                      "perform " + ", ".join(f"{k} {v}" for k, v in settings["perform"].items())))
    if settings.get("fx"):
        steps += [("fx", {"slot": voice, **settings["fx"]}, "fx " + ", ".join(f"{k} {v}" for k, v in settings["fx"].items())
                   + f" (voice {voice})") for voice in voices]
    return steps


def changes(items: list, placed: list) -> list:
    """Settings changes at bars (and beats): each with its place in the score and the first note at or after it."""
    out = []
    for item in items:
        offset = (item["bar"] - 1) * music.BEATS_PER_BAR + (item.get("beat", 1) - 1)
        event = next((i for i, step in enumerate(placed) if step["offset"] >= offset - 1e-9), len(placed))
        out.append({**item, "offset": offset, "event": event})
    return sorted(out, key=lambda c: c["offset"])


class Conductor:
    """Fires each settings change on its bar line while the arms perform. A note that moves later moves its bar line
    too, so a change lands with the note it was written with. known() -> {note index: when it is due to sound} as the
    performance learns them (each note's time is known once its arm is on its way to it)."""

    def __init__(self, pending: list, placed: list, beat_s: float, apply, known, clock=None, sleep=None):
        import threading
        import time
        self.pending, self.placed, self.beat_s, self.apply, self.known = list(pending), placed, beat_s, apply, known
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.fired, self.errors, self.finished = [], [], threading.Event()
        self.thread = threading.Thread(target=self.run, name="orchid-score-conductor", daemon=True)

    def due(self, change, known):
        """When this change belongs on the bar line, or None while that is not yet known."""
        j = change["event"]
        if j in known:  # the note it comes with is on its way: count back from it
            return known[j] - (self.placed[j]["offset"] - change["offset"]) * self.beat_s
        before = [i for i in known if i < j]
        if before and max(before) == j - 1:  # the note before is sounding: count on from it, and past the bars the plan
            i = j - 1                        # already moved its note later
            moved = (self.placed[j].get("moved_bars", 0) - self.placed[i].get("moved_bars", 0)) if j < len(self.placed) else 0
            return known[i] + (change["offset"] - self.placed[i]["offset"] + moved * music.BEATS_PER_BAR) * self.beat_s
        return None

    def run(self):
        while self.pending:
            known = self.known()
            now = self.clock()
            for change in list(self.pending):
                due = self.due(change, known)
                if due is not None and now >= due + change.get("after_s", -CHANGE_LEAD_S):
                    self.pending.remove(change)
                    try:
                        self.fired.append({"bar": change["bar"], "beat": change.get("beat", 1), "late_ms": round((now - due) * 1000),
                                           "applied": self.apply(change)})
                    except Exception as exc:  # a refused change is reported; the music goes on
                        self.errors.append({"bar": change["bar"], "error": str(exc)})
            if self.finished.is_set() and all(self.due(c, known) is None for c in self.pending):
                break  # the performance is over and nothing left can be placed
            self.sleep(0.01)

    def start(self):
        self.thread.start()

    def finish(self, wait_s=60.0):
        self.finished.set()
        self.thread.join(wait_s)
        return {"fired": self.fired, "errors": self.errors,
                "missed": [{"bar": c["bar"], "beat": c.get("beat", 1)} for c in self.pending]}


# --- Looping: Studio repeats what the arms played, each chord on its written beat ----------------------------------

def fits(placed: list, loop_beats: float) -> None:
    """The score has to sit inside Studio's loop, its bar 1 on the loop's."""
    for step in placed:
        m.require(step["offset"] < loop_beats, f"Bar {step['bar']} beat {step['beat']:g} is past the end of Studio's "
                  f"{loop_beats / music.BEATS_PER_BAR:g}-bar loop: lengthen the loop (loop-configure) or shorten the score.")


def take(placed: list, heard: list) -> tuple:
    """This pass as Studio loop chords: what Orchid sent for each note (its notes, voicing and velocity), placed on the
    note's written beat for its written length, however early or late it was. heard: the key check's steps, one per
    note in order. A note nothing sounded for is left out. Returns (chords, the notes left out)."""
    chords, missed = [], []
    for step, note in zip(placed, list(heard) + [{}] * (len(placed) - len(heard))):
        if not note.get("midi"):
            missed.append({"bar": step["bar"], "beat": step["beat"], "key": step["control"], "chord": step.get("chord")})
            continue
        chords.append({"beat": step["offset"], "duration": max(0.125, step["beats"]), "notes": list(note["midi"]),
                       "velocity": note.get("velocity") or 80})
    return chords, missed
