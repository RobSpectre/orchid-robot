---
name: orchid-keys
description: Play, sequence and adjust the taught Orchid synth controls (12 keys, chord buttons, voicing dial) on the SO101 arm through the running operator console's API. Use when the user asks to play a key/chord/dial direction or a melody, change a key's press length or a dial turn angle, change the arm's speed, send the arm home, or stop it.
---

# Orchid keys

The SO101 follower plays controls that were taught in the operator console (home → hover → touch → press for
keys and chord buttons; hover → open → lower → grip plus a turn angle for the dial). This skill drives them
through the console's local API with `scripts/orchid.py` (standard library only).

## Before anything moves

1. Run `python3 scripts/orchid.py status`. It lists every control, its status, press length / turn angle,
   the default speed, and whether the arm is **ready to play**.
2. Only `registered` controls can be played. `empty` means not taught; `needs_reteach` means it must be
   taught again in the console (do not try to work around it).
3. If it is not ready, tell the user what the refusal or `status` says. Usually: in the console at
   http://127.0.0.1:8081, select a taught control and press **Play** (or **⌂ Home → Go to home**) once so
   the follower is holding; the leader arm is not needed to play. If the phase is `fault` (stopped), they
   must support the follower and press **Hold & keep playing** in the console. The API refuses to move the
   arm unless that console is open and in control — that is deliberate (its Stop button, Esc key and
   lost-page stop stay in charge). Reloading or closing the console tab while the arm is holding stops it.
   Never start or restart the app, connect arms, or release torque from here.

## Commands

```bash
python3 scripts/orchid.py status
python3 scripts/orchid.py play C                      # waits until the arm is back home
python3 scripts/orchid.py play F# --press 0.8         # hold the press 0.8 s (also becomes F#'s default)
python3 scripts/orchid.py play C --speed 2            # this play only
python3 scripts/orchid.py play cw --turn 25           # dial directions: cw / ccw; turn in degrees
python3 scripts/orchid.py seq "C E G C"               # one motion through home between controls
python3 scripts/orchid.py seq "C:0.5 E G:1.2" --speed 1.5   # per-step press length in seconds
python3 scripts/orchid.py set C --press 0.5           # default press length for one key
python3 scripts/orchid.py set ccw --turn -25          # default turn for a dial direction
python3 scripts/orchid.py speed 2                     # default speed for everything, 0.1–3×
python3 scripts/orchid.py press 0.4                   # default press for keys without their own
python3 scripts/orchid.py hardness 40                 # press hardness 10–100 %: lower presses more gently
python3 scripts/orchid.py home                        # move to the saved home
python3 scripts/orchid.py stop                        # hold where it is now
```

Names: keys `C C# D D# E F F# G G# A A# B` (lowercase ok); chord buttons `dim min maj sus 6 m7 M7 9`
(`m7` and `M7` differ); dial `cw` / `ccw`. Limits: speed 0.1–3, press 0–5 s, press hardness 10–100 %, turn ±1–90°.

## Rules

- The arm moves when you run `play`, `seq` or `home`. Only do it because the user asked for it, and say
  what will move before running a long sequence.
- One command at a time; each `play`/`seq` waits until the arm is back home. Do not loop or retry a
  refused or failed play. Report the printed message instead.
- If a command prints an error, a fault, or "start pose" / "something may be in the way", stop and tell
  the user; they must look at the arm.
- With hardware and Orchid Studio running, `play`/`seq` also print what Orchid actually sent
  (`Orchid heard: C ✓ velocity 72, 0.11 s into the 0.80 s press, held 0.31 s`). `CHECK THE ARM` means a
  wrong key, a missed or repeated note: stop and tell the user; do not replay to "fix" it. Chord buttons
  send no MIDI on their own, so they are reported as unchecked. `Note check unavailable` only means
  Studio is not reachable; the play itself still happened. `python3 scripts/orchid.py stop` holds the arm in place.
- Speed above ~2× is fast. Suggest trying a new key at the default speed first.
- Press length is how long the key is held down (it shapes the note). Press hardness is how fast the final
  touch → press stroke moves, as a share of the other strokes (default 50 %); lower is gentler. It applies
  to every key and chord button; the release is not slowed. Neither changes how deep the press goes. A dial turn angle is pure wrist rotation from the grip pose; flip the sign if it turns the wrong way.
- Teaching and re-teaching happen in the console with the leader arm, not through this skill.
- A key that keeps double-triggering or not sounding can be fixed with **04 Calibrate keys** in the console
  (the user beside the arm). Suggest it; it is not available through the API. While it runs, plays are refused.

## HTTP API (what the script calls)

Base `http://127.0.0.1:8081`. POSTs need header `X-Orchid-Token` from `GET /api/session` → `token`.

| Method & path | Body | Result |
|---|---|---|
| `GET /api/controls` | — | `{phase, ready_to_play, settings:{speed,press_s,press_hardness}, controls:[{id,name,kind,group,status,press_s?,turn_degrees?}]}` |
| `POST /api/controls/{id}/play` | `{speed?, press_s?, turn_degrees?, wait=true}` | `{status, phase, message, error, key_check?}` after it finishes |
| `POST /api/sequence` | `{steps:[{control, press_s?, turn_degrees?}], speed?, wait=true}` | same |
| `POST /api/controls/{id}` | `{press_s}` or `{turn_degrees}` | saves the default |
| `POST /api/settings` | `{speed?, press_s?, press_hardness? (0.1–1)}` | saves defaults |
| `POST /api/home`, `POST /api/stop` | `{}` | |

Refusals come back as HTTP 409 (state/safety) or 422 (out-of-range value) with `detail`.
