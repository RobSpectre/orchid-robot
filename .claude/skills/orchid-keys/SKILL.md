---
name: orchid-keys
description: Play and compose music (jams that evolve: see JAMMING.md) on the taught Orchid synth controls (12 keys, chord buttons, voicing dial) through orchid-robot's performance API, including chords (a key played with a chord button held, using both arms) and whole scores on Orchid Studio's beat. Use when the user asks to play a key, a chord, a melody, a rhythm, a chord progression or a set, to send the arms home or to rest, or to stop them. How the arms move (holding, speed, hardness, the order of moves, safety) is orchid-robot's and the operator console's, never this skill's.
---

# Orchid keys

The SO101 follower plays controls that were taught in the operator console (home → hover → touch → press for
keys and chord buttons; hover → open → lower → grip plus a turn angle for the dial). This skill sends **music** to
orchid-robot's performance API with `scripts/orchid.py` (standard library only), and can ask for the arms to go home
or to rest. orchid-robot owns everything about how the arms move: holding, the order of moves, arm speed, press
hardness, dial angles, planning, the interlock and every safety check. The operator owns the console. This skill
never commands an arm directly. The API refuses console commands from programs, refuses motion overrides (`speed`,
`turn_degrees`), and plays **one performance at a time**:
while one runs (`status` shows `Performing now`), a second is refused, so wait for the first to return.

With two followers, the Keys Arm plays the 12 keys and the Chord Arm the chord buttons and dial. **A chord is one instruction**: name
the key and the chord button, and orchid-robot runs both arms itself. The Chord Arm presses and holds the button, the Keys
Arm plays the key, and once the key is down the Chord Arm lets go and goes home. Never sequence the arms yourself, for example by playing
`maj` and then `C`: a chord button pressed on its own only goes down and comes back up, so no chord sounds.

## Before anything moves

1. Run `python3 scripts/orchid.py status`. It lists every control, its status, turn angle (dial), the default
   speed and note value, and whether the arm is **ready to play**. `clock` shows Orchid Studio's tempo.
2. Only `registered` controls can be played. `empty` means not taught; `needs_reteach` means it must be
   taught again in the console (do not try to work around it).
3. Not holding is fine: a play takes the hold itself, where the arm is, whenever that needs no torque gap (the
   motors unpowered, or powered with their settings already in place). Otherwise the refusal says what the
   operator must do: usually, in the console at http://127.0.0.1:8081, select a taught control and press
   **Play** once with a hand on the arm. Tell the user what the refusal says. If the phase is `fault` (stopped), they
   must support the follower and press **Hold & keep playing** in the console. The API refuses to move the
   arm unless that console is open and in control — that is deliberate (its Stop button, Esc key and
   lost-page stop stay in charge). Reloading or closing the console tab while the arm is holding stops it.
   Never start or restart the app, connect arms, or release torque from here.

## Musical time

Durations are **note values**, never seconds: a quarter note is one beat. Write `1/4 1/8 1/2 1 1/16`, dotted `1/8.`
(×1.5), triplet `1/8t` (×2/3), or the letters `w h q e s` (`q`, `e.`). Long notes are bars of 4/4 (`2bars`, `1bar`,
`1.5bars`), and `+` ties values (`2bars+1/2`, `1/4+1/16`). The tempo is Orchid Studio's. While Studio's
transport runs, every note lands on its beat grid: the arm waits above the key and strikes on the beat. When it is
stopped, a phrase keeps its own time at Studio's tempo from its first note. orchid-robot does all the timing. Give it
the notes and their values, never sleeps or delays between commands.

In a phrase each step's note value is how long it sounds and when the next one comes. `r:1/4` is a rest. Between two
keys the Keys Arm lifts to the key's hover and goes straight to the next key's hover; it goes home only at the end (or
when something else comes between, such as a voicing turn). A change still takes a second or more. When a note comes
sooner than the arm can get there, it lands
late by whole beats and the rest of the phrase moves with it. The reply then says `N beats late, the arm could not move
faster`. That is not a fault: suggest longer note values, rests, or a slower tempo in Studio (arm speed is the operator's setting).

## Scores: whole pieces on the beat

Live sets are the Hermes skill's (`~/.hermes/skills/orchid/orchid-robot-jam`).

**Jamming? Read [JAMMING.md](JAMMING.md) first.** It covers composing diverse, evolving music on this rig: the chord
colours, key changes, layer roles, Studio's Perform modes and the rules that keep a jam from settling into one key.

For anything with timing, a progression, a riff or a song, send the whole piece as **one score**. orchid-robot plans
every move as one performance on Orchid Studio's beat: when each arm leaves home, when the chord button goes down,
and how long each note can be held so the next one still lands. Don't send a piece as many separate plays: each
separate call starts from scratch and costs time between notes.

```bash
python3 scripts/orchid.py score "1.1 C+maj:1  2.1 A+min:1  3.1 F+maj:1  4.1 G+sus:1" --plan   # plan only: nothing moves
python3 scripts/orchid.py score "1.1 C+maj:1  2.1 A+min:1  3.1 F+maj:1  4.1 G+sus:1"
python3 scripts/orchid.py score "1.1 C+maj:2bars  3.1 A+min:1  4.1 F+maj:1/2  4.3 G+maj:1/2"
python3 scripts/orchid.py score piece.json         # {"events": [{"bar": 1, "beat": 1, "key": "C", "chord": "maj", "duration": "1"}]}
```

Each note is `BAR.BEAT KEY[+CHORD][:NOTE]`. Bars are Studio's 4/4 bars counted from 1. Beats are 1–4, and `1.2.5`
is bar 1, the "and" of beat 2. A chord is one key with one chord button. The Keys Arm plays one key at a time, so
two notes at the same moment are refused. The score starts on its first note's beat of Studio's next bar while the
transport runs.

**Write long chords.** Orchid is a chord instrument, and it shines with sustained chords. Prefer chords of a bar or
more (`:1`, `:2bars`, `:4bars`) over many short notes, and let a progression breathe. Every note is held for exactly
its written length, however long. Holds are never cut to make room.

**Plan first** (`--plan`, or `"plan_only": true`). After a note's hold, the Keys Arm lifts to that key's hover and
goes straight to the next key's hover while the next chord button goes down. The plan works out each change from the
taught motions (from hover to hover), plus what earlier performances measured. Changes between nearby keys are
quickest. When a change cannot be made on its written bar, it **moves
later by whole bars**, keeping its beat of the bar, and everything after it moves too. The plan lists every move, for
example `A+min at bar 2 beat 1 moves 1 bar later, to bar 3`. If the plan has moves, tell the user and suggest
writing the chord a bar later or a slower tempo in Studio. Don't shorten the chords.

Scores play at the operator's arm speed (`status` shows it; 2.4–3× suits scores). At 1.2× every change takes about
twice as long, and more of the score moves later: if so, tell the operator. A note the arms can only reach up to 80 ms late is played that little late, not moved a bar.

**After playing**, each note is reported as on the beat, so many ms early or late, or moved later by whole bars.
orchid-robot learns from Orchid's notes where every key sounds, and the rig's fixed delay (about 0.1 s), and how long
changes really take, so timing improves as it plays. Don't compensate by shifting notes in the score yourself.

### Sound, perform and fx (Orchid Studio)

A score can set Orchid Studio's sound for Orchid's voice, at the start and at chosen bars:

- `sound`: a Pistil preset 1–100 (Studio's `sounds-list` names them).
- `perform`: Studio's Live Perform settings, which turn each held chord into a figure. `mode` is strum, arp, harp,
  bloom, orbit, drift, spark and more; there are also `pattern`, `step_beats` and `gate`. Studio's `perform-options`
  lists them all.
- `fx`: `{"reverb": {"mix": 0–100, "room": "hall"}, "delay": {"mix": 0–100, "beats": 0.5, "feedback": 0–95}}`. Rooms
  are small, medium, large, chamber, hall, large-hall, plate and cathedral. A mix of 0 turns an effect off.
- `voicing`: Orchid's own voicing dial position (0–127, the value Orchid reports). The Chord Arm turns the dial until
  Orchid reports that position: usually 1–3 turns, each a few seconds. It learns how far a turn moves the dial.
  The dial must be taught on the Chord Arm.
- `changes`: `[{"bar": 9, "perform": {"mode": "harp"}}, {"bar": 17, "sound": 40, "voicing": 52, "fx": {...}}]`. Studio
  settings land on their bar line, and move with it if the music before it moved later. A voicing change turns the
  dial between the notes, before that bar's note. The plan counts that time, so the note may move a bar or two later.
  Set the voicing at the start where you can, and change it sparingly.

Long chords through a Perform mode such as arp, harp or bloom, with reverb, are where this instrument shines.

`sound` and `fx` go to Studio's live voice (voice 5, the arms' pass). In a looped score (`loop_slot`) they also go to
that loop layer's voice, which plays every pass after. The layer also plays through the score's `perform` settings.
Each loop layer keeps its own sound and effects, so set them per part. A `Studio refused fx: unknown host command`
means Studio's audio host needs rebuilding (`orchid-studio build-host`, then restart Studio): tell the user.

```bash
python3 scripts/orchid.py score "1.1 C+maj:2bars 3.1 A+min:2bars 5.1 F+maj:2bars 7.1 G+sus:2bars" --sound 12 --perform mode=harp --fx '{"reverb": {"mix": 35, "room": "cathedral"}}'
```

### Looping: Studio repeats what the arms played, on the beat

With `loop_slot` (CLI `--loop 1`) and Studio's loops playing (`loop-start`), the score starts on the loop's next pass.
The arms play it once. Then Studio's loop layer gets **what Orchid sent on that pass**: each chord's notes, voicing and
velocity, placed exactly on its written beat for its written length. So it repeats the arms' real performance, without
their timing slips, from the next loop boundary. A chord nothing sounded for is left out, and the reply lists it. The
score must fit inside Studio's loop (1–16 bars), so set the loop length first, and keep the arms' plan inside it.

**Loop layers: one pass, several parts, written so the arms stay on time.** Instead of `loop_slot`, `loop_layers` lists
the layers that keep the pass, each shaped for its part:
`{"slot": 1-4, "octave"?: 0-8, "transpose"?: semitones, "hold"?: "written"|"legato"|beats, "merge"?: bool,
"velocity"?: 1-127, "sound"?, "fx"?, "perform"?}`.

- `octave` puts each chord's lowest note in that octave (C4 = 60) whatever octave Orchid is set to: 2 for a bass, 3 for
  pads, 4–5 above them. Orchid's own octave changes, so set it on every layer.
- `hold: "legato"` holds each chord in the layer until the next one (the last to the loop's end). Write the arms' notes
  **short and on time** (`1/2`) and let the layer sustain them, rather than writing long holds that push changes bars
  late. A number holds each chord that many beats, cut at the next.
- `merge: true` overdubs: the pass is added to what the layer plays (a chord at the same beat is replaced). At 2.4× arm
  speed the arms need about 7 s between chord changes and 2.7 s between keys in one pass (whatever the tempo; plan
  first), so build a denser part over passes: chords 1 and 3, then chords 2 and 4.
- A layer's own `sound`/`fx` go to its voice only; `perform` is its Perform setting (add `velocity_limit: 127`, or
  Studio caps velocities at 80). The score's `sound`/`fx` go to the live voice and the layers without their own.
- **Make the live pass sound like the layer.** While the arms play, the room hears Studio's live voice, at Orchid's own
  octave and press velocity (often 15–40) on whatever sound it had, so a pass recorded into a bass layer two octaves
  down at velocity 100 seems silent the first time round. Give the score the layer's `sound` and `fx`, and a `perform`
  with the layer's `octave` and `velocity` (Studio Perform settings, as the layer's own register and level), and set
  Studio's `live` mixer channel to the layer's level.
- Several layers from one pass need an Orchid Studio that keeps a waiting composition per layer
  (`status.looper.pending_composes`); an older one is refused with a 409.

## Long notes and sweeping chords

A note can be held for **any number of bars**: there is no upper limit. Use it for long, sweeping chords and pads.

```bash
python3 scripts/orchid.py chord C maj --duration 4bars                     # one chord held for four bars
python3 scripts/orchid.py play E --duration 2bars+1/2                      # a single key, tied past the bar
python3 scripts/orchid.py seq "C+maj:4bars A+min:4bars F+maj:2bars G+sus:2bars"   # a slow progression
python3 scripts/orchid.py seq "C+maj:2bars r:1bar F+maj:4bars"            # a bar of silence between them
```

How a long note plays:

- **The chord sounds from the held key.** The Chord Arm presses the button before the strike and lets go as soon as
  the key is down, then goes home. Only the Keys Arm stays on the key for the bars written. Orchid keeps the chord
  sounding while the key is held.
- **The arm does not keep pushing.** About 0.15 s after the key bottoms out, the arm holds where the key stopped it,
  plus a light 0.5° preload that keeps the key down. It writes that goal once and leaves it. So a long hold neither
  strains nor buzzes the motor, and it lifts straight up from there. Held chord buttons are held the same way.
- **The command waits for the whole note**, plus the trip home. Before starting a long note, tell the user how long it
  will take: 4 bars is 16 beats, so 16 × 60 / BPM seconds (about 11 s at 90 BPM). Use `clock` for the BPM.
- **`stop` ends a held note early** and holds the arm where it is. Use it if the user asks you to stop. The next play
  starts from home again.
- In a phrase, a long note pushes everything after it back by its length, as written. The next note does not overlap it.

With Orchid Studio running, the key check line ends with how long Orchid held the note (`held 10.62 s`). If that is
well short of the note's `duration.seconds`, the light preload did not keep that key down. Stop and tell the user. It
is a setting in orchid-robot (`HOLD_PUSH_DEG`), not something to work around by replaying.

## Commands

```bash
python3 scripts/orchid.py status
python3 scripts/orchid.py clock                       # Studio's BPM; whether notes land on its running beat
python3 scripts/orchid.py play C                      # the default note value (1/8 unless changed); waits until home
python3 scripts/orchid.py play F# --duration 1/2      # a half note
python3 scripts/orchid.py seq "C:1/4 E:1/4 G:1/2"     # a phrase in time
python3 scripts/orchid.py seq "C:1/4 r:1/4 G:1/2"     # r: a rest
python3 scripts/orchid.py chord C maj --duration 1/2  # C major: the Chord Arm holds Maj, the Keys Arm plays C
python3 scripts/orchid.py seq "C+maj:1 A+min:1 F+maj:1 G+sus:1"   # a progression; KEY+CHORD steps mix with plain keys
python3 scripts/orchid.py home                        # both arms home: orchid-robot moves them, one at a time
python3 scripts/orchid.py rest                        # both arms to rest, by way of home (--arm a|b for one)
python3 scripts/orchid.py stop                        # emergency only: hold both arms where they are now
```

Names: keys `C C# D D# E F F# G G# A A# B` (lowercase ok); chord buttons `dim min maj sus 6 m7 M7 9`
(`m7` and `M7` differ); dial `cw` / `ccw` (each turns by its taught angle; a score's `voicing` sets the dial).
Notes can be any length. A chord is `KEY+BUTTON[:NOTE]` in a sequence (`C+maj`, `F#+m7:1/2`) or `chord KEY BUTTON`.
Both the key (Keys Arm) and the chord button (Chord Arm) must be `registered`. Extensions (`6 m7 M7 9`) are buttons
like the others.

## Rules

- The arm moves when you run `play`, `seq`, `chord` or `score`. Only do it because the user asked for it, and say
  what will move before running a long sequence. `home` and `rest` park the arms: ask for them (between pieces, at
  the end of a set, when the user says to stop), and orchid-robot carries them out, both arms in a safe order. Never
  send hold, chord-button or settings commands: orchid-robot does those itself, and refuses them from programs.
- One command at a time; each `play`/`seq` waits until the arm is back home. Do not loop or retry a
  refused or failed play. Report the printed message instead.
- If a command prints an error, a fault, or "start pose" / "something may be in the way", stop and tell
  the user; they must look at the arm.
- With hardware and Orchid Studio running, `play`/`seq` also print what Orchid actually sent
  (`Orchid heard: C ✓ velocity 72, 0.11 s into the 0.80 s press, held 0.49 s`). `CHECK THE ARM` means a
  wrong key, a missed or repeated note: stop and tell the user; do not replay to "fix" it. Chord buttons
  send no MIDI on their own, so they are reported as unchecked. `Note check unavailable` only means
  Studio is not reachable; the play itself still happened. `python3 scripts/orchid.py stop` holds the arm in place.
- Arm speed and press hardness are the operator's settings in the console (`status` shows them). Speed is how fast
  the arms move between notes, not the tempo (Studio's). Press hardness sets how hard keys are struck, which is
  Orchid's velocity: if replies report velocities under ~20, tell the operator rather than replaying.
- Chords: a refusal saying the arms "would come within N mm" means that key and button are too close together for
  both arms at once (modelled on the plate). Do not try to get around it (e.g. by playing the button and then the
  key). Tell the user, and offer another key or voicing. Every refusal of a chord leaves both arms safe. A
  `CHECK THE ARM` result on a chord says what was heard: `the chord button was not held` (only the key sounded),
  `not a minor chord (heard 1 3 5)`, a wrong key, or a missed note. Stop and tell the user.
- A sequence with chords or with both arms' controls plays one step at a time and always waits (`--no-wait` does not
  apply). It takes longer than a single-arm sequence.
- Teaching and re-teaching happen in the console with the leader arm, not through this skill.
- **04 Correct keys** in the console fixes a key the arm presses wrongly. Suggest it when a key keeps sounding a
  neighbour (`wrong key, C# sounded`), two keys at once (`C and C# sounded together`), double-triggers or does not
  sound. It plays each key 5 times at the user's playing speed, records which key sounded each time, moves the
  hover, touch and press toward the right key (or sets the press depth), and repeats until 5 presses out of 5 are
  clean. **Test all taught keys** on the same page only checks every key (5 presses each, nothing changed) and
  offers to correct the ones that failed. The console also lists keys taught off the keyboard's pattern. The user must be beside the arm. It is not
  available through the API, and plays are refused while it runs. A key more than a slot off, or more than a key's
  width, still needs re-teaching with the leader.

## HTTP API (what the script calls)

Base `http://127.0.0.1:8081`. POSTs need header `X-Orchid-Token` from `GET /api/session` → `token`.

| Method & path | Body | Result |
|---|---|---|
| `GET /api/controls` | — | `{phase, ready_to_play, settings:{speed,duration,press_hardness}, performing:{what, since}\|null, controls:[{id,name,kind,group,arm,status,turn_degrees?}]}`; with two arms `ready_to_play` is `{a, b}` |
| `GET /api/clock` | — | `{bpm, beat_s, grid}`: `grid` is set while Studio's transport runs (notes land on its beat) |
| `POST /api/controls/{id}/play` | `{duration? ("1/4", "2bars", "1bar+1/2"), wait=true}` | `{status, phase, message, error, duration:{beats,seconds}, rhythm, key_check?}` after it finishes |
| `POST /api/sequence` | `{steps:[{control ("rest" for a rest), chord?, duration?}], wait=true}` | `{…, rhythm:{bpm, grid, steps:[{control, strike_at, slipped_beats}]}}` |
| `POST /api/score` | `{events:[{bar, beat, key, chord?, duration?}], sound?, perform?, fx?, voicing?, changes?:[{bar, beat?, sound?, perform?, fx?, voicing?}], loop_slot?, loop_velocity?, loop_layers?:[{slot, octave?, transpose?, hold?, merge?, velocity?, sound?, fx?, perform?}], plan_only?}` | `{status, message, plan:{bpm, on_studio_beat, moves:[{bar, beat, to_bar, bars, needs_s, has_s, text}], moved_bars, length_bars}, played:[{bar, beat, key, chord, slipped_beats, late_ms, heard}], key_check?}` |
| `POST /api/chords/play` | `{key, chord, duration?}` (`chord` is a button id, e.g. `chord.maj`) | `{…, key, chord, clearance_mm, duration, rhythm, key_check?}` once both arms are home |
| `POST /api/home`, `POST /api/rest` (`?arm=a\|b` for one) | `{}` | both arms home, or to rest by way of home: orchid-robot orders the moves and checks each; refused while a performance runs |
| `POST /api/stop` | `{}` | emergency only: holds both arms where they are |

`duration` is a note value string (`"1/4"`, `"1/8."`, `"1/8t"`, `"q"`, `"4bars"`, `"1bar+1/2"`); there is no `press_s`
in the API. Long durations are fine: the request blocks until the note has been held and both arms are home, so give
your HTTP client no timeout or one longer than the music (`duration.seconds` in the reply says how long the note was).
`POST /api/stop` from another request ends it early.
Refusals come back as HTTP 409 (state/safety, or another performance running) or 422 (a bad value, or a `speed` /
`turn_degrees`: those are orchid-robot's) with `detail`. Console commands (`/api/commands` (settings among them),
`/api/heartbeat`, connecting) answer 403 to programs: they belong to the console.
