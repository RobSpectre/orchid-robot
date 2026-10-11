#!/usr/bin/env python3
"""How the set sounds right now, for a DJ agent's heartbeat (DJ.md): read only, nothing moves.

From the last few minutes of what Orchid actually sent (the Keys Arm's key checks) and Orchid Studio's loops:
loudness (velocity), timing, repetition, whether the chords stay in a key, and whether the loop layers agree with each
other. Ends with the fixes that matter most, worst first.

    python3 scripts/dj_report.py              # the last 16 minutes
    python3 scripts/dj_report.py --minutes 30
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics as st
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "hardware" / "operator.sqlite3"
STUDIO = "http://127.0.0.1:8765/command"
NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
PC = {n: i for i, n in enumerate(NAMES)}
SHAPES = {"Maj": (0, 4, 7), "Min": (0, 3, 7), "Sus": (0, 5, 7), "Dim": (0, 3, 6), "note": (0,)}
MAJOR = (0, 2, 4, 5, 7, 9, 11)
QUIET, LOUD = 25, 35  # velocity: under QUIET is weak and buried; LOUD and up is healthy


def played(minutes):
    """Timed notes (scores, phrases, chords on Studio's beat) from the Keys Arm's key checks."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = db.execute("SELECT created, message FROM events WHERE kind='key_check' AND created >= ? ORDER BY id", (since,)).fetchall()
    notes = []
    for created, message in rows:
        if not re.search(r"on the beat|ms (late|early)|beats? late", message):
            continue  # untimed: console plays and key tests
        for part in message.split("; "):
            m = re.match(r"([A-G]#?)(?: \+ (\w+))?", part)
            if not m:
                continue
            velocity = re.search(r"velocity (\d+)", part)
            ms = re.search(r"(\d+) ms (late|early)", part)
            notes.append({"name": m.group(1) + (f"+{m.group(2)}" if m.group(2) else ""), "ok": "✓" in part,
                          "velocity": int(velocity.group(1)) if velocity else None,
                          "off_ms": 0 if "on the beat" in part else int(ms.group(1)) if ms else None, "text": part})
    return notes


def tones(name):
    key, _, colour = name.partition("+")
    return {(PC[key] + i) % 12 for i in SHAPES.get(colour or "note", (0,))} if key in PC else set()


def best_key(sets):
    """(share of the sets inside one major scale, that scale's key)."""
    sets = [s for s in sets if s]
    if not sets:
        return 1.0, None
    def inside(k):
        scale = {(k + i) % 12 for i in MAJOR}
        return sum(s <= scale for s in sets)
    k = max(range(12), key=inside)
    return inside(k) / len(sets), NAMES[k]


def chord_name(pcs):
    for root in range(12):
        for colour, shape in SHAPES.items():
            if colour != "note" and {(root + i) % 12 for i in shape} == pcs:
                return f"{NAMES[root]}+{colour}"
    return "+".join(NAMES[p] for p in sorted(pcs)) if pcs else "-"


def studio():
    request = urllib.request.Request(STUDIO, data=json.dumps({"command": "status"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=16)
    args = parser.parse_args()
    fixes = []  # (priority, text): lower is more urgent

    notes = played(args.minutes)
    print(f"LAST {args.minutes:g} MIN · {len(notes)} timed notes")
    if notes:
        names = [n["name"] for n in notes]
        velocities = [n["velocity"] for n in notes if n["velocity"] is not None]
        offs = [abs(n["off_ms"]) for n in notes if n["off_ms"] is not None]
        run, longest = 1, 1
        for a, b in zip(names, names[1:]):
            run = run + 1 if a == b else 1
            longest = max(longest, run)
        cycles = Counter(tuple(names[i:i + 4]) for i in range(len(names) - 3)).most_common(1)
        fit, key = best_key(map(tones, names))
        problems = [n["text"] for n in notes if not n["ok"]]
        vel = st.median(velocities) if velocities else None
        print(f"  loudness   median velocity {vel} (min {min(velocities, default='-')}, max {max(velocities, default='-')})")
        print(f"  timing     {sum(o <= 30 for o in offs)}/{len(offs)} within 30 ms, {sum(o <= 80 for o in offs)}/{len(offs)} within 80 ms")
        print(f"  variety    {len(set(names))} different of {len(names)}: " + ", ".join(f"{k}×{v}" for k, v in Counter(names).most_common(6)))
        print(f"  repetition longest run of one chord {longest}" + (f"; most played 4-chord cycle {' → '.join(cycles[0][0])} ×{cycles[0][1]}" if cycles else ""))
        print(f"  harmony    {fit:.0%} of what was played fits {key} major (or its relative minor)")
        if problems:
            print(f"  problems   {len(problems)}: " + "; ".join(problems[:4]))
            fixes.append((0, "Orchid heard wrong or missed notes: stop arm moves and tell the operator (orchid-keys rules)."))
        if vel is not None and vel < QUIET:
            fixes.append((1, f"Too quiet: median velocity {vel}. Press hardness is the operator's: tell them and suggest about "
                             "85% in the console. Loops keep the velocity they were played at: re-record quiet layers after."))
        if longest >= 3:
            fixes.append((2, f"The same chord {longest} times in a row: the loop already repeats it. Write one looped score "
                             "and leave it, or change the chord."))
        if cycles and cycles[0][1] >= 4:
            fixes.append((3, f"{' → '.join(cycles[0][0])} played {cycles[0][1]} times: move to the next section of SET.md."))
        if fit < 0.8:
            fixes.append((3, f"Only {fit:.0%} fits one key: pick a home key per section and keep every layer in it."))

    status = studio()
    if status is None:
        print("STUDIO  not reachable on 127.0.0.1:8765")
        fixes.append((0, "Orchid Studio is not reachable: tell the operator."))
    else:
        tempo, looper = status.get("tempo") or {}, status.get("looper") or {}
        monitor, drums, mixer = status.get("key_monitor") or {}, status.get("drums") or {}, status.get("mixer") or {}
        bars = (looper.get("settings") or {}).get("bars") or 8
        print(f"STUDIO  {tempo.get('bpm')} BPM · loops {'running' if looper.get('running') else 'stopped'} ({bars} bars) · "
              f"drums {drums.get('beat')} at {round((mixer.get('drums') or {}).get('volume') or 0, 2)} · key monitor {monitor.get('status')}")
        if monitor.get("status") != "ready":
            fixes.append((0, f"Studio's key monitor is {monitor.get('status')} ({monitor.get('error')}): nothing is being checked. Tell the operator."))
        if not looper.get("running"):
            fixes.append((1, "The loops are stopped: the groove has stopped. Start them (loop-start) or tell the operator."))
        layers = [entry for entry in looper.get("layers") or [] if entry.get("notes")]
        loop_beats = bars * 4
        for layer in layers:
            slot = layer["slot"]
            chords, beat = [], None
            for note in sorted(layer["notes"], key=lambda n: n["beat"]):
                if beat is None or note["beat"] - beat >= 0.5:
                    chords.append([note["beat"], set()])
                    beat = note["beat"]
                chords[-1][1].add(note["note"] % 12)
            names = [chord_name(c[1]) for c in chords]
            runs = []  # consecutive repeats of one chord (a pattern or arp restrikes it) as one entry
            for (b, _), n in zip(chords, names):
                if runs and runs[-1][1] == n:
                    runs[-1][2] += 1
                else:
                    runs.append([b, n, 1])
            vels = [n.get("velocity", 0) for n in layer["notes"]]
            volume = round((mixer.get(f"layer-{slot}") or {}).get("volume") or 0, 2)
            print(f"  layer {slot}{' (muted)' if layer.get('muted') else ''} · {layer.get('mode')} · volume {volume} · "
                  f"velocity {st.median(vels) if vels else '-'} · beat " + ", ".join(f"{b:g} {n}" + (f"×{c}" if c > 1 else "") for b, n, c in runs))
            if vels and st.median(vels) < QUIET and not layer.get("muted"):
                fixes.append((2, f"Layer {slot} was recorded at velocity {st.median(vels)}: replace it with a louder take."))
        # Rhythm: where in the bar notes start, across the playing layers (a Perform mode's restrikes count, as heard).
        live = [entry for entry in layers if not entry.get("muted")]
        if live:
            onsets = sorted({round(n["beat"] * 4) / 4 for entry in live for n in entry["notes"]})  # to the 16th
            in_bar = {round(t % 4, 2) for t in onsets}
            off_beat = [t for t in onsets if t % 1]
            on_one = [t for t in onsets if t % 4 == 0]
            per_bar = len(onsets) / bars
            print(f"  rhythm     {per_bar:.1f} note starts a bar across the playing layers · {len(in_bar)} different places in "
                  f"the bar ({', '.join(f'{b + 1:g}' for b in sorted(in_bar)[:8])}) · {len(off_beat) / max(1, len(onsets)):.0%} off the beat")
            if per_bar < 2 or len(in_bar) <= 2:
                fixes.append((2, f"Rhythm is square: {per_bar:.1f} note starts a bar, in {len(in_bar)} places "
                                 f"({len(on_one)} of {len(onsets)} on beat 1). Add a rhythm layer from SET.md's rhythm "
                                 "patterns (a syncopated bass or stabs on the off-beats), or a Perform `pattern` on the pad."))
            elif not off_beat:
                fixes.append((3, "Nothing falls off the beat: syncopate one layer (a note on an 'and', an anticipation on 4.5)."))
        # Do the unmuted layers agree? Every half beat, what all of them sound together, against one major scale.
        if len(live) > 1:
            clash, steps = [], 0
            for i in range(int(loop_beats * 2)):
                t = i / 2
                pcs = {n["note"] % 12 for entry in live for n in entry["notes"] if n["beat"] <= t < n["beat"] + n.get("duration", 1)}
                if not pcs:
                    continue
                steps += 1
                if best_key([pcs])[0] < 1:
                    clash.append(t)
            agree = 1 - len(clash) / steps if steps else 1.0
            print(f"  together   the {len(live)} playing layers agree on a key {agree:.0%} of the loop"
                  + (f" (clashing from beat {clash[0]:g})" if clash else ""))
            if agree < 0.9:
                fixes.append((1, f"The layers clash {1 - agree:.0%} of the loop: they play different keys at once. Mute or clear "
                                 "the layer that is out of the section's key, then rewrite it in key."))

    print("FIX FIRST" if fixes else "SOUNDS HEALTHY: keep the arc moving (DJ.md step 2).")
    for _, text in sorted(fixes, key=lambda f: f[0])[:4]:
        print(f"  - {text}")


if __name__ == "__main__":
    main()
