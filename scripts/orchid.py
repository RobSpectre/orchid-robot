#!/usr/bin/env python3
"""Play taught Orchid controls through orchid-robot's performance API, in musical time.

Durations are note values (a quarter note is one beat): 1/4 1/8 1/2 1 1/16, dotted 1/8. and triplet 1/8t (or q e h w s),
or bars of 4/4: 2bars, 1bar; tie them with +: 1bar+1/2. A note can be held for any number of bars.
They follow Orchid Studio's tempo, and while its transport runs every note lands on its beat.

    python3 scripts/orchid.py status                  # taught controls, speed, default note value, readiness
    python3 scripts/orchid.py clock                   # Orchid Studio's tempo, and whether notes land on its beat
    python3 scripts/orchid.py play C                  # one key for the default note value (waits until home)
    python3 scripts/orchid.py play C --duration 1/4
    python3 scripts/orchid.py seq "C:1/4 E:1/4 G:1/2" # a phrase: each step lands its note value after the last
    python3 scripts/orchid.py seq "C:1/4 r:1/4 E:1/2" # r: a rest
    python3 scripts/orchid.py chord C maj --duration 1/2   # C with the Maj button held (Chord Arm)
    python3 scripts/orchid.py seq "C+maj:1 A+min:1 F+maj:1 G+sus:1"   # chords in a phrase; mix with plain keys
    python3 scripts/orchid.py chord C maj --duration 4bars              # a long sweeping chord
    python3 scripts/orchid.py score "1.1 C+maj:1  2.1 A+min:1  3.1 F+maj:1  4.1 G+sus:1"   # a whole piece, bar.beat
    python3 scripts/orchid.py score piece.json --plan  # {"events": [{"bar":1,"beat":1,"key":"C","chord":"maj",...}]}
    python3 scripts/orchid.py score "1.1 C+maj:2bars 3.1 A+min:2bars" --sound 12 --perform mode=harp --loop 1
    python3 scripts/orchid.py home                    # both arms home (orchid-robot moves them, one at a time)
    python3 scripts/orchid.py rest                    # both arms to rest, by way of home
    python3 scripts/orchid.py stop                    # emergency only: hold both arms where they are

This performs music and can park the arms (home, rest). Everything else about the arms is orchid-robot's and the
operator console's: holding, arm speed, press hardness, the order of moves, safety. The console (http://127.0.0.1:8081) must be open,
connected and holding. orchid-robot plays one performance at a time and refuses a second while one runs.
Standard library only; exits non-zero with the reason on refusal.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from urllib.parse import quote
import urllib.request

ALIASES = {"dim": "chord.dim", "min": "chord.min", "maj": "chord.maj",
           "sus": "chord.sus", "6": "chord.6", "m7": "chord.m7", "M7": "chord.M7", "9": "chord.9"}


def control_id(name: str) -> str:
    name = name.strip()
    if name in ALIASES:
        return ALIASES[name]
    if len(name) <= 2 and name[:1].isalpha():
        return name[0].upper() + name[1:]  # c -> C, c# -> C#
    return name


def score_events(text: str) -> list:
    """Events from "1.1 C+maj:1/2 2.3 E" (BAR.BEAT KEY[+CHORD][:NOTE]; beat 2.5 is "1.2.5"), a .json file or stdin."""
    if text == "-" or text.endswith(".json"):
        data = json.load(sys.stdin if text == "-" else open(text))
        return data  # a list of events, or the whole score: events, sound, perform, fx, changes, loop_slot
    tokens, events = text.replace(",", " ").replace("|", " ").split(), []
    for when, what in zip(tokens[::2], tokens[1::2]):
        bar, _, beat = when.partition(".")
        name, _, note = what.partition(":")
        key, _, held = name.partition("+")
        events.append({"bar": int(bar), "beat": float(beat or 1), "key": control_id(key),
                       **({"chord": control_id(held)} if held else {}), **({"duration": note} if note else {})})
    if len(tokens) % 2:
        raise SystemExit(f"Each note is BAR.BEAT then KEY: {tokens[-1]!r} has no partner.")
    return events


def perform_settings(text: str) -> dict:
    """mode=arp,pattern=pulse,step_beats=0.5 -> {"mode": "arp", "pattern": "pulse", "step_beats": 0.5}"""
    settings = {}
    for part in text.split(","):
        name, _, value = part.partition("=")
        try:
            settings[name.strip()] = json.loads(value)
        except ValueError:
            settings[name.strip()] = value.strip()
    return settings


class Console:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.token = None

    def call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        """No timeout by default: a play waits for its notes, however many bars they are held."""
        if method == "POST" and self.token is None:
            self.token = self.call("GET", "/api/session")["token"]
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **({"X-Orchid-Token": self.token} if self.token else {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout if timeout or method != "GET" else 10) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise SystemExit(f"Refused ({exc.code}): {detail}") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"orchid-robot is not reachable at {self.url} ({exc.reason}): it is not running, or this is "
                             "the wrong --url. If it is not running, the operator starts it (Start Orchid); programs never "
                             "start or restart it.") from None


def report(result: dict, say: bool = True) -> None:
    if say:
        print(result.get("message") or result.get("status"))
    check = result.get("key_check")
    if check:  # what Orchid actually sent, from Orchid Studio's key monitor
        label = {"ok": "Orchid heard", "problem": "CHECK THE ARM", "unavailable": "Note check unavailable"}.get(check["status"], "Notes")
        print(f"{label}: {check['summary']}")
    if result.get("error"):
        raise SystemExit(f"Error: {result['error']}")
    if result.get("phase") == "fault":
        raise SystemExit("The arm stopped (fault). Check the console before trying again.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    play = sub.add_parser("play")
    play.add_argument("control")
    seq = sub.add_parser("seq")
    seq.add_argument("steps", help='space-separated steps, each CONTROL[+CHORD][:NOTE] or r:NOTE for a rest: "C+maj:1/2 r:1/4 E:1/4"')
    piece = sub.add_parser("score", help="play a score: keys and chords at bars and beats, on Orchid Studio's beat")
    piece.add_argument("events", help='"BAR.BEAT KEY[+CHORD][:NOTE] ..." (1.1 C+maj:1/2 2.3 E), a .json file, or - for JSON on stdin')
    piece.add_argument("--plan", action="store_true", help="only plan it: nothing moves")
    piece.add_argument("--sound", type=int, help="Studio's Pistil preset 1-100 for Orchid's voice")
    piece.add_argument("--perform", help="Studio Live Perform settings: mode=arp,pattern=pulse,step_beats=0.5")
    piece.add_argument("--fx", help='Studio effects as JSON: {"reverb": {"mix": 30, "room": "hall"}}')
    piece.add_argument("--loop", type=int, metavar="LAYER", help="hand the composition to Studio's loop layer 1-4 to repeat")
    piece.add_argument("--voicing", type=int, metavar="1-60", help="Orchid's chord voicing (moves chords up and down the "
                       "keyboard), set by Orchid Studio in software, never the dial")
    chord = sub.add_parser("chord", help="play a key with a chord button held (needs both arms)")
    chord.add_argument("key")
    chord.add_argument("chord", help="dim, min, maj, sus, 6, m7, M7 or 9")
    for p in (play, seq, chord):
        p.add_argument("--duration", help="note value or bars: 1/4, 1/8., 1/8t, 2bars, 1bar+1/2 ... (keys and chords)")
        if p is not chord:
            p.add_argument("--no-wait", action="store_true", help="return as soon as playback starts")
    sub.add_parser("clock")
    for where in ("home", "rest"):
        sub.add_parser(where, help=f"both arms to {where} (orchid-robot moves them)").add_argument("--arm", choices=("a", "b"))
    sub.add_parser("stop", help="emergency only: hold both arms where they are")
    args = parser.parse_args(argv)
    console = Console(args.url)

    if args.command == "status":
        listing = console.call("GET", "/api/controls")
        print(f"phase {listing['phase']} · ready to play: {listing['ready_to_play']} · "
              f"speed {listing['settings']['speed']:g}× · default note {listing['settings'].get('duration', '1/8')}"
              + (f" · press hardness {listing['settings']['press_hardness']:.0%}" if "press_hardness" in listing["settings"] else ""))
        for c in listing["controls"]:
            print(f"  {c['id']:<12} {c['name']:<22} {c.get('status', 'empty')}")
        if listing.get("performing"):
            print(f"Performing now: {listing['performing']['what']} (since {listing['performing']['since']}); wait for it.")
        # What keeps a program from playing, one line each, naming the operator's step (they act; programs wait).
        stopped = (listing.get("dial_stop") or {}).get("stopped")
        if stopped:
            print(f"WAITING FOR THE OPERATOR: stopped by Orchid's voicing dial at {stopped.get('at', '?')}; the operator plays a "
                  "key or sends an arm home in the console before programs can move the arms again.")
        arms = listing.get("arms") or {}
        for arm in arms.values():
            print(f"{arm['name']}: {arm['state']}")
            if arm.get("problem"):
                print(f"WAITING FOR THE OPERATOR: {arm['problem']}")
        ready = listing["ready_to_play"]
        if not arms and (ready is False or isinstance(ready, dict) and not all(ready.values())):
            print("Not ready: ask the operator to hold the arms in the console (select a taught control and press Play once).")
        return
    if args.command == "play":
        body = {"duration": args.duration} if args.duration else {}
        report(console.call("POST", f"/api/controls/{quote(control_id(args.control), safe='')}/play", {**body, "wait": not args.no_wait}))
    elif args.command == "seq":
        steps = []
        for token in args.steps.split():
            name, _, note = token.partition(":")
            name, _, held = name.partition("+")
            step = {"control": "rest" if name.lower() in ("r", "rest") else control_id(name), **({"chord": control_id(held)} if held else {})}
            if note or args.duration:
                step["duration"] = note or args.duration
            steps.append(step)
        body = {"steps": steps, "wait": not args.no_wait}
        report(console.call("POST", "/api/sequence", body))
    elif args.command == "chord":
        body = {"key": control_id(args.key), "chord": control_id(args.chord),
                **({"duration": args.duration} if args.duration else {})}
        report(console.call("POST", "/api/chords/play", body))
    elif args.command == "score":
        given = score_events(args.events)
        body = {**(given if isinstance(given, dict) else {"events": given}), "plan_only": args.plan,
                **({"sound": args.sound} if args.sound is not None else {}),
                **({"perform": perform_settings(args.perform)} if args.perform else {}),
                **({"fx": json.loads(args.fx)} if args.fx else {}),
                **({"voicing": args.voicing} if args.voicing is not None else {}),
                **({"loop_slot": args.loop} if args.loop is not None else {})}
        result = console.call("POST", "/api/score", body)
        print(result.get("message"))
        loop = (result.get("studio") or {}).get("loop")
        if loop and loop.get("kept"):
            print(f"Studio's loop layer {loop['slot']} repeats this pass, each chord on its written beat, from Studio beat "
                  f"{loop.get('applies_at_beat')}" + ("; left out (nothing sounded): " + ", ".join(
                      f"bar {m_['bar']} beat {m_['beat']:g} {m_['key']}" for m_ in loop["missed"]) if loop.get("missed") else ""))
        elif loop:
            print(f"Loop layer {loop['slot']} unchanged: {loop.get('reason')}")
        for note in result.get("played") or []:
            name = note["key"] + (f"+{note['chord'].split('.')[-1]}" if note.get("chord") else "")
            late = note.get("late_ms")
            where = ("not heard" if late is None else "on the beat" if abs(late) < 10 else
                     f"{abs(late)} ms {'late' if late > 0 else 'early'}")
            slipped = f", {note['slipped_beats']} beat{'s' if note['slipped_beats'] != 1 else ''} late" if note.get("slipped_beats") else ""
            print(f"  bar {note['bar']} beat {note['beat']:g}  {name:<10} {where}{slipped}")
        report(result, say=False)
    elif args.command == "clock":
        clock = console.call("GET", "/api/clock")
        print(f"{clock['bpm']:g} BPM · " + ("Orchid Studio's transport is running: notes land on its beat" if clock["grid"] else
                                            "Orchid Studio's transport is stopped: phrases keep their own time at this tempo"))
    elif args.command in ("home", "rest"):
        report(console.call("POST", f"/api/{args.command}" + (f"?arm={args.arm}" if args.arm else ""), {}))
    elif args.command == "stop":
        report(console.call("POST", "/api/stop", {}))


if __name__ == "__main__":
    main(sys.argv[1:])
