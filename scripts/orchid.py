#!/usr/bin/env python3
"""Play and configure taught Orchid controls through the running operator console's API, in musical time.

Durations are note values (a quarter note is one beat): 1/4 1/8 1/2 1 1/16, dotted 1/8. and triplet 1/8t (or q e h w s).
They follow Orchid Studio's tempo, and while its transport runs every note lands on its beat.

    python3 scripts/orchid.py status                  # taught controls, speed, default note value, readiness
    python3 scripts/orchid.py clock                   # Orchid Studio's tempo, and whether notes land on its beat
    python3 scripts/orchid.py play C                  # one key for the default note value (waits until home)
    python3 scripts/orchid.py play C --duration 1/4 --speed 2
    python3 scripts/orchid.py play cw --turn 25       # dial directions: cw / ccw (turn angle in degrees)
    python3 scripts/orchid.py seq "C:1/4 E:1/4 G:1/2" # a phrase: each step lands its note value after the last
    python3 scripts/orchid.py seq "C:1/4 r:1/4 E:1/2" # r: a rest
    python3 scripts/orchid.py chord C maj --duration 1/2   # C with the Maj button held (Chord Arm)
    python3 scripts/orchid.py seq "C+maj:1 A+min:1 F+maj:1 G+sus:1"   # chords in a phrase; mix with plain keys
    python3 scripts/orchid.py set ccw --turn -25      # a dial direction's default turn
    python3 scripts/orchid.py speed 2                 # arm speed for everything (0.1-3)
    python3 scripts/orchid.py duration 1/4            # the note value when none is given
    python3 scripts/orchid.py hardness 40             # press hardness: touch -> press speed, 10-100 % of the strokes
    python3 scripts/orchid.py home | stop

The console (http://127.0.0.1:8081) must be open, connected and holding (Play any taught control once);
the API never moves the arm on its own. Standard library only; exits non-zero with the reason on refusal.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

ALIASES = {"cw": "voicing.cw", "ccw": "voicing.ccw", "dim": "chord.dim", "min": "chord.min", "maj": "chord.maj",
           "sus": "chord.sus", "6": "chord.6", "m7": "chord.m7", "M7": "chord.M7", "9": "chord.9"}


def control_id(name: str) -> str:
    name = name.strip()
    if name in ALIASES:
        return ALIASES[name]
    if len(name) <= 2 and name[:1].isalpha():
        return name[0].upper() + name[1:]  # c -> C, c# -> C#
    return name


class Console:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.token = None

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 330):
        if method == "POST" and self.token is None:
            self.token = self.call("GET", "/api/session")["token"]
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **({"X-Orchid-Token": self.token} if self.token else {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise SystemExit(f"Refused ({exc.code}): {detail}") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"Cannot reach the console at {self.url} ({exc.reason}). Is app.py running?") from None


def report(result: dict) -> None:
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
    chord = sub.add_parser("chord", help="play a key with a chord button held (needs both arms)")
    chord.add_argument("key")
    chord.add_argument("chord", help="dim, min, maj, sus, 6, m7, M7 or 9")
    for p in (play, seq, chord):
        p.add_argument("--speed", type=float)
        p.add_argument("--duration", help="note value: 1/4, 1/8, 1/2, 1/8., 1/8t ... (keys and chords)")
        if p is not chord:
            p.add_argument("--no-wait", action="store_true", help="return as soon as playback starts")
    play.add_argument("--turn", type=float, help="dial turn in degrees (negative = the other way)")
    setter = sub.add_parser("set")
    setter.add_argument("control")
    setter.add_argument("--turn", type=float, required=True)
    sub.add_parser("speed").add_argument("value", type=float)
    sub.add_parser("duration").add_argument("value", help="the default note value, e.g. 1/8")
    sub.add_parser("clock")
    sub.add_parser("hardness").add_argument("percent", type=float, help="10-100; lower presses more gently")
    sub.add_parser("home")
    sub.add_parser("stop")
    args = parser.parse_args(argv)
    console = Console(args.url)

    if args.command == "status":
        listing = console.call("GET", "/api/controls")
        print(f"phase {listing['phase']} · ready to play: {listing['ready_to_play']} · "
              f"speed {listing['settings']['speed']:g}× · default note {listing['settings'].get('duration', '1/8')}"
              + (f" · press hardness {listing['settings']['press_hardness']:.0%}" if "press_hardness" in listing["settings"] else ""))
        for c in listing["controls"]:
            extra = f" turn {c['turn_degrees']:g}°" if c.get("turn_degrees") is not None else ""
            print(f"  {c['id']:<12} {c['name']:<22} {c.get('status', 'empty')}{extra}")
        if not listing["ready_to_play"]:
            print("Not ready: in the console, select a taught control and press Play once so the follower is holding.")
        return
    if args.command == "play":
        body = {k: v for k, v in {"speed": args.speed, "duration": args.duration, "turn_degrees": args.turn}.items() if v is not None}
        report(console.call("POST", f"/api/controls/{control_id(args.control)}/play", {**body, "wait": not args.no_wait}))
    elif args.command == "seq":
        steps = []
        for token in args.steps.split():
            name, _, note = token.partition(":")
            name, _, held = name.partition("+")
            step = {"control": "rest" if name.lower() in ("r", "rest") else control_id(name), **({"chord": control_id(held)} if held else {})}
            if note or args.duration:
                step["duration"] = note or args.duration
            steps.append(step)
        body = {"steps": steps, "wait": not args.no_wait, **({"speed": args.speed} if args.speed is not None else {})}
        report(console.call("POST", "/api/sequence", body))
    elif args.command == "chord":
        body = {"key": control_id(args.key), "chord": control_id(args.chord),
                **{k: v for k, v in {"speed": args.speed, "duration": args.duration}.items() if v is not None}}
        report(console.call("POST", "/api/chords/play", body))
    elif args.command == "set":
        report(console.call("POST", f"/api/controls/{control_id(args.control)}", {"turn_degrees": args.turn}))
    elif args.command == "speed":
        report(console.call("POST", "/api/settings", {"speed": args.value}))
    elif args.command == "duration":
        report(console.call("POST", "/api/settings", {"duration": args.value}))
    elif args.command == "clock":
        clock = console.call("GET", "/api/clock")
        print(f"{clock['bpm']:g} BPM · " + ("Orchid Studio's transport is running: notes land on its beat" if clock["grid"] else
                                            "Orchid Studio's transport is stopped: phrases keep their own time at this tempo"))
    elif args.command == "hardness":
        report(console.call("POST", "/api/settings", {"press_hardness": args.percent / 100}))
    elif args.command == "home":
        report(console.call("POST", "/api/home", {}))
    elif args.command == "stop":
        report(console.call("POST", "/api/stop", {}))


if __name__ == "__main__":
    main(sys.argv[1:])
