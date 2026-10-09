#!/usr/bin/env python3
"""Play and configure taught Orchid controls through the running operator console's API.

    python3 scripts/orchid.py status                  # taught controls, speed, press length, readiness
    python3 scripts/orchid.py play C                  # play one key (waits until it is back home)
    python3 scripts/orchid.py play C --press 0.8 --speed 2
    python3 scripts/orchid.py play cw --turn 25       # dial directions: cw / ccw (turn angle in degrees)
    python3 scripts/orchid.py seq "C E G C"           # a sequence, through home between controls
    python3 scripts/orchid.py seq "C:0.5 E G:1.2"     # per-step press length in seconds
    python3 scripts/orchid.py set C --press 0.5       # a key's default press length
    python3 scripts/orchid.py set ccw --turn -25      # a dial direction's default turn
    python3 scripts/orchid.py speed 2                 # default speed for everything (0.1-3)
    python3 scripts/orchid.py press 0.4               # default press length for keys without their own
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
    seq.add_argument("steps", help='space-separated controls, optionally with a press length: "C:0.5 E G"')
    for p in (play, seq):
        p.add_argument("--speed", type=float)
        p.add_argument("--press", type=float, help="press length in seconds (keys)")
        p.add_argument("--no-wait", action="store_true", help="return as soon as playback starts")
    play.add_argument("--turn", type=float, help="dial turn in degrees (negative = the other way)")
    setter = sub.add_parser("set")
    setter.add_argument("control")
    setter.add_argument("--press", type=float)
    setter.add_argument("--turn", type=float)
    sub.add_parser("speed").add_argument("value", type=float)
    sub.add_parser("press").add_argument("value", type=float)
    sub.add_parser("hardness").add_argument("percent", type=float, help="10-100; lower presses more gently")
    sub.add_parser("home")
    sub.add_parser("stop")
    args = parser.parse_args(argv)
    console = Console(args.url)

    if args.command == "status":
        listing = console.call("GET", "/api/controls")
        print(f"phase {listing['phase']} · ready to play: {listing['ready_to_play']} · "
              f"speed {listing['settings']['speed']:g}× · default press {listing['settings']['press_s']:g} s"
              + (f" · press hardness {listing['settings']['press_hardness']:.0%}" if "press_hardness" in listing["settings"] else ""))
        for c in listing["controls"]:
            extra = (f" press {c['press_s']:g} s" if c.get("press_s") is not None else "") + \
                    (f" turn {c['turn_degrees']:g}°" if c.get("turn_degrees") is not None else "")
            print(f"  {c['id']:<12} {c['name']:<22} {c.get('status', 'empty')}{extra}")
        if not listing["ready_to_play"]:
            print("Not ready: in the console, select a taught control and press Play once so the follower is holding.")
        return
    if args.command == "play":
        body = {k: v for k, v in {"speed": args.speed, "press_s": args.press, "turn_degrees": args.turn}.items() if v is not None}
        report(console.call("POST", f"/api/controls/{control_id(args.control)}/play", {**body, "wait": not args.no_wait}))
    elif args.command == "seq":
        steps = []
        for token in args.steps.split():
            name, _, press = token.partition(":")
            step = {"control": control_id(name)}
            if press or args.press is not None:
                step["press_s"] = float(press) if press else args.press
            steps.append(step)
        body = {"steps": steps, "wait": not args.no_wait, **({"speed": args.speed} if args.speed is not None else {})}
        report(console.call("POST", "/api/sequence", body))
    elif args.command == "set":
        body = {k: v for k, v in {"press_s": args.press, "turn_degrees": args.turn}.items() if v is not None}
        if not body:
            raise SystemExit("Give --press (keys) or --turn (dial).")
        report(console.call("POST", f"/api/controls/{control_id(args.control)}", body))
    elif args.command == "speed":
        report(console.call("POST", "/api/settings", {"speed": args.value}))
    elif args.command == "press":
        report(console.call("POST", "/api/settings", {"press_s": args.value}))
    elif args.command == "hardness":
        report(console.call("POST", "/api/settings", {"press_hardness": args.percent / 100}))
    elif args.command == "home":
        report(console.call("POST", "/api/home", {}))
    elif args.command == "stop":
        report(console.call("POST", "/api/stop", {}))


if __name__ == "__main__":
    main(sys.argv[1:])
