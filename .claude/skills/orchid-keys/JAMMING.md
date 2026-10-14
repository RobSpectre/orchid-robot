# Jamming with Orchid: writing music that moves

To start a jam now, don't read on: `cd ~/.hermes/skills/orchid/orchid-robot-jam && python3 scripts/set.py go "<their words>"`
has music playing within seconds (SKILL.md, Start here). This file is for composing your own scores with `orchid.py`.

Read this before writing scores for a jam. You are the composer and arranger of a long, live, looping piece played
by two robot arms on Orchid, a chord instrument, through Orchid Studio. A good jam keeps evolving: it travels
through keys and moods, changes sound and texture, builds and releases. Repeating the same four chords in one key,
in one sound, is the failure to avoid.

## What you can play

**Harmony is one key plus one chord button.** The Keys Arm plays one key at a time (C to B, sharps as `C#`, `D#`,
`F#`, `G#`, `A#`); the Chord Arm can hold one chord button with it. Play only what `status` lists as registered:
today the taught buttons are `maj`, `min`, `sus` and `dim`, so that is 12 roots × 4 colours, plus single notes.

| Button | Sound | Use it for |
|---|---|---|
| `maj` | bright, settled | home chords, IV and V in major |
| `min` | dark, settled | minor homes, ii, iii, vi |
| `sus` | open, unresolved | tension before a resolution, floating vamps, pivots between keys |
| `dim` | unstable, sliding | passing chords a semitone below a target (C#dim → Dmin), turnarounds |
| (none) | a single note | bass lines, melodies, pedal tones |

Colour beyond the four buttons comes from Orchid Studio (see SKILL.md for the score fields):

- `sound` (Pistil preset 1–100): a new sound is the fastest way to a new section.
- `perform.mode`: strums (`strum`, `strum2`, `slop`), arpeggios (`arp`, `arp2`), `pattern` (with `perform.pattern`:
  `pulse`, `offbeat`, `backbeat`, `tresillo`, `shuffle`, …), `harp`, and Studio's generative modes (`bloom`, `orbit`,
  `drift`, `spark`, …), which add their own melodic motion. Tune `step_beats` and `gate`.
- `fx`: reverb and delay; sweep them over a section.
- Voicing (a new inversion and register) is not played by the arms: Orchid Studio's `chord-voicing` command sets it in
  software. The voicing dial is the operator's stop control.
- Four **loop layers**. `loop_slot` puts the pass in one layer; `loop_layers` (see SKILL.md) hands one pass to several,
  each with its own `octave`, `hold` (`legato` sustains each chord to the next), `merge` (overdub onto what the layer
  already plays), `sound`, `fx` and `perform`. Think in layers: pad, motion, bass, colour.

## Physical limits (write with them, not against them)

- **Space the changes.** At 2.4× arm speed, give each chord about **7 s** before the next change in one pass (16
  beats at 132 BPM, 8 at 70) and each single key about **2.7 s**. Write closer than that and orchid-robot moves the later notes bars
  later. For a denser part, write short on-time notes and let `hold: "legato"` sustain them, or build it over two
  passes with `merge: true` (chords 1 and 3, then 2 and 4).
- **Plan every score** (`plan_only` / `--plan`). If the plan lists moves, rewrite: fewer changes, longer spacing.
- **Fit the loop.** A looped score must fit inside Studio's loop (1–16 bars), chosen while the loops are stopped.
- Arm speed is the operator's setting, not yours.

## Keep it diverse

1. Note each part as you go (key, progression, sound, mode, fx, layer) and change something every 2–3 passes: key,
   sound, Perform mode or pattern, density (layers in or out), voicing, effects or tempo. One or two at a time.
2. Don't stay in one key for more than about 4 parts. Modulate to the relative minor or major, up a whole step, to
   the IV or V, or by a chromatic mediant (C → E). Pivot through a shared chord, a `sus` or a `dim`.
3. Use `sus` and `dim` and single-note lines, not only `maj` and `min`. Don't repeat a progression twice in a row:
   transform it (new key, sound, mode or voicing).
4. Shape the energy: start sparse (one pad layer, reverb), add motion (arp or pattern layer), peak (all layers),
   break down (mute layers, one held chord), then return transformed.

## A palette of progressions (key + button)

| Feel | Progression |
|---|---|
| Pop / anthem | `C+maj` → `G+maj` → `A+min` → `F+maj` |
| Andalusian descent | `A+min` → `G+maj` → `F+maj` → `E+maj` |
| Mixolydian rock | `G+maj` → `F+maj` → `C+maj` → `G+sus` |
| Suspended release | `G+sus` → `G+maj`; `D+sus` → `D+min` |
| Dim passing chord | `C+maj` → `C#+dim` → `D+min` → `G+maj` |
| Chromatic mediants | `C+maj` → `E+maj` → `G#+maj` → `C+maj` |
| Borrowed iv | `C+maj` → `F+maj` → `F+min` → `C+maj` |
| Modal drone | single `D` under `D+sus` → `C+maj` → `G+maj` |

## Example score

A sparse opening pad, chords 4 bars apart, recorded into a bass layer and a pad layer from one pass:

```json
{"sound": 12, "perform": {"mode": "harp"}, "fx": {"reverb": {"mix": 40, "room": "cathedral"}},
 "loop_layers": [{"slot": 1, "octave": 3, "hold": "legato"}, {"slot": 3, "octave": 2, "hold": "legato", "velocity": 100}],
 "events": [{"bar": 1, "beat": 1, "key": "D", "chord": "chord.min", "duration": "1/2"},
            {"bar": 5, "beat": 1, "key": "G", "chord": "chord.sus", "duration": "1/2"}]}
```

Then let it play, read the reply (how each note landed, the plan's moves), and write the next part as a development
of what is sounding.
