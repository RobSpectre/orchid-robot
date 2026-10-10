# Jamming with Orchid: writing music that moves

Read this before writing scores for a jam. You are the composer and arranger of a long, live, looping piece played
by two robot arms on Orchid, a chord instrument, through Orchid Studio. A good jam keeps evolving: it travels
through keys and moods, changes colour and texture, builds and releases, and never sits on the same four chords
for long. Repeating major and minor triads in one key is the failure to avoid.

## What you can play

**Harmony comes from one key plus one chord button.** The Keys Arm plays one key at a time (C to B, sharps as `C#`,
`D#`, `F#`, `G#`, `A#`). The Chord Arm can hold one chord button with it. That gives 12 roots × 8 colours, plus single
notes:

| Button | Sound | Use it for |
|---|---|---|
| `maj` | bright, settled | home chords, IV and V in major |
| `min` | dark, settled | minor homes, ii, iii, vi |
| `sus` | open, unresolved | tension before a resolution, floating vamps, modal ambiguity |
| `dim` | unstable, sliding | passing chords a semitone below a target (C#dim → Dm7), turnarounds |
| `6` | sweet, vintage | major colour without the leading tone; endings |
| `M7` | lush, dreamy | Lydian and Ionian colour, I and IV in jazz and soul |
| `m7` | soft minor, groove | Dorian vamps, ii in ii–V–I, neo-soul |
| `9` | rich dominant or added colour | V chords, funk, lift before a change |
| (none) | a single note | bass lines, melodies, pedal tones, call and response |

The **voicing dial** (`voicing`, 0–127) changes how Orchid stacks the chord: its inversion and register. Moving it is
an evolution in itself, since the same progression sounds new in a different voicing. Each move costs the Chord Arm a
few seconds, so move it at section changes, not every bar.

**Orchid Studio turns the harmony into sound and motion:**
- `sound`: Pistil preset 1–100 (Studio's `sounds-list` has names). A new sound is the fastest way to a new section.
- `perform.mode`: how a held chord is played.
  - Strums: `off`, `strum`, `strum2`, `slop`.
  - Arpeggios: `arp`, `arp2`.
  - Rhythmic: `pattern` (with `perform.pattern`: `pulse`, `offbeat`, `backbeat`, `tresillo`, `shuffle`, `rising`,
    `falling`, `pendulum`).
  - Sweeping: `harp`.
  - Studio's generative modes add their own melodic motion and colour: `bloom` (maj9/#11), `orbit` (Dorian, 3–3–2),
    `drift` (sparse voice leading), `spark` (chromatic ii–V–I), `tide`, `sunline`, `sidekick`, `electric`,
    `bluehour`, `anthem`.
  - Also tune `step_beats`, `gate`, `spread_beats`, `slop` and `harp_octaves`.
- `fx`: reverb (`room`: small … cathedral, `mix`) and delay (`beats`: 0.75 is a dotted eighth, `feedback`, `mix`).
  Sweep them over a section. In a looped score, `sound` and `fx` apply to that part's loop layer, so each layer can
  have its own sound and space.
- Four **loop layers** (`loop_slot` 1–4). Each holds one part and repeats it exactly once the arms have played it.
  Think in layers: pad, motion, bass or melody, colour.
- Through Studio's own API, not the score: `tempo` (with `transition_seconds` for gradual drifts), `loop-mute` and
  `loop-clear` for dropping parts out, `mixer-set` with fades for builds and breakdowns.

## Physical limits (write with them, not against them)

- **Play scores at 2.5–3× arm speed** (`speed`). Arm speed is how fast the arms move between notes, not the tempo,
  and at 1.2× every change takes about twice as long. Slow it down only when the user asks for gentler presses.
- **Chord changes take time.** The Keys Arm goes straight from one key's hover to the next, so a change between
  nearby keys is quickest and a jump across the keyboard takes longer; a chord adds its button press. The plan says
  what fits: if chords move bars later, give each chord two bars, or let a Perform mode supply the motion while the
  harmony moves slowly. A voicing change sends the Keys Arm home first, so it costs more again.
- **Long chords are the instrument's strength.** Every note is held for exactly its written length. Write chords of
  1–4 bars and let Studio's Perform modes, the layers and the effects make the rhythm.
- **Plan every score** (`plan_only` / `--plan`) and read its moves. If a chord would move bars later, rewrite: fewer
  changes, longer chords, or a single-note part instead.
- **Fit the loop.** A looped score must fit inside Studio's loop (1–16 bars). The loop length can only change while
  the loops are stopped, so choose it at the start (8 bars suits most jams).

## Keep it diverse: rules for every jam

1. **Keep a jam journal** in your notes as you go: for each part, record its key or mode, its progression with chord
   types, sound, Perform mode, fx, loop layer and the time. Check it before writing the next part.
2. **Change something every 2–3 passes.** Rotate through the dimensions: harmony, key or mode, sound, Perform mode or
   pattern, rhythm placement, density (layers in or out), voicing, effects, tempo. Change one or two at a time, so the
   music evolves rather than jumps.
3. **Never stay in one key for more than about 4 parts.** Modulate:
   - to the relative minor or major (C → Am);
   - up a whole step (C → D), the classic lift;
   - to the IV or V key;
   - by a chromatic mediant for drama (C → E, C → G#).

   Pivot through a shared chord, a `sus` or a `dim`.
4. **Use at least four chord types in every 8 parts.** Favour colour: `m7`, `M7`, `9`, `6` and `sus` more often than
   plain `maj` and `min`.
5. **Don't repeat a progression twice in a row.** When you return to an idea, transform it: a new mode, voicing,
   Perform mode, rhythm or key.
6. **Shape the energy.** A jam has an arc. It starts sparse (one pad layer, reverb), adds motion (an arp or pattern
   layer), peaks (all four layers, busier patterns, brighter sound), breaks down (mute layers, one held chord or a
   single-note line, long delay), and returns transformed. Then it moves on somewhere new.
7. **Vary the rhythm of the changes.** Not every chord on beat 1:
   - anticipate a change on beat 4.5 of the bar before;
   - land on beat 3;
   - mix 1-bar and 3-bar chords;
   - leave a bar of rest.
8. **Give each layer a role** and write them to fit together. A pad of long `M7`, `m7` or `sus` chords. Motion: the
   same harmony through `arp`, `pattern` or `harp`. A bass or melody of single notes, the roots or a simple tune,
   with space. Colour: sparse `9` or `6` stabs, or a `bloom`, `drift` or `orbit` line.

## A palette of progressions (key + button)

Transpose freely: all 12 roots are available.

| Feel | Progression |
|---|---|
| Jazz ii–V–I | `D+m7` → `G+9` → `C+M7` (then try it in F: `G+m7` → `C+9` → `F+M7`) |
| Dorian vamp | `A+m7` ↔ `D+9` (or `D+6`); `E+m7` ↔ `A+9` |
| Lydian float | `C+M7` ↔ `D+maj` (the bright II), or `F+M7` ↔ `G+maj` |
| Mixolydian rock | `G+maj` → `F+maj` → `C+maj` → `G+9` |
| Andalusian descent | `A+min` → `G+maj` → `F+M7` → `E+9` |
| Neo-soul | `D+m7` → `G+9` → `E+m7` → `A+m7` |
| Suspended release | `G+sus` → `G+9`; `D+sus` → `D+min` |
| Dim passing chord | `C+M7` → `C#+dim` → `D+m7` → `G+9` |
| Chromatic mediants | `C+M7` → `E+maj` → `G#+M7` → `C+M7` (cinematic) |
| Borrowed iv | `C+maj` → `F+maj` → `F+min` → `C+6` (bittersweet) |
| Modal drone | hold `D` (no button) for 4 bars under a pad of `D+sus` → `C+M7` → `G+maj` |
| Minor blues colour | `A+m7` → `D+9` → `E+9` → `A+m7` |

Key changes:

| Move | Progression |
|---|---|
| Up a step | end in C on `G+sus`, then start the next part on `D+maj` |
| Through the relative | `C+M7` → `A+m7`: now treat A as home (A Dorian) |
| By a mediant | `C+M7` → `E+M7` and continue in E |

## Before each score: a checklist

- **Journal:** what changes from the last part? Name at least one dimension.
- **Key:** is this part in a different key or mode from the last 3–4? If not, does it at least change chord colours?
- **Chord types:** more than `maj` and `min`?
- **Timing:** chord lengths and placements varied? Within the arms' limits? Did the plan come back without moves?
- **Layers:** which layer, and what is its role? Does it replace something that has played long enough?
- **Studio settings:** sound, mode or fx: should one change now? Is a voicing move due at this section change?

## Example scores

A sparse opening pad on layer 1, D Dorian, cathedral reverb:

```json
{"loop_slot": 1, "sound": 12, "perform": {"mode": "harp", "harp_octaves": 2},
 "fx": {"reverb": {"mix": 40, "room": "cathedral"}},
 "events": [{"bar": 1, "beat": 1, "key": "D", "chord": "chord.m7", "duration": "4bars"},
            {"bar": 5, "beat": 1, "key": "G", "chord": "chord.9", "duration": "4bars"}]}
```

A motion layer on 2 over the same harmony, then a lift a step up, with a dotted-eighth delay:

```json
{"loop_slot": 2, "perform": {"mode": "arp2", "step_beats": 0.5, "gate": 0.6},
 "fx": {"delay": {"mix": 25, "beats": 0.75, "feedback": 35}},
 "events": [{"bar": 1, "beat": 1, "key": "D", "chord": "chord.m7", "duration": "2bars"},
            {"bar": 3, "beat": 4.5, "key": "G", "chord": "chord.9", "duration": "2bars"},
            {"bar": 6, "beat": 1, "key": "E", "chord": "chord.m7", "duration": "3bars"}]}
```

A single-note bass line on layer 3, leaving space:

```json
{"loop_slot": 3, "perform": {"mode": "off"},
 "events": [{"bar": 1, "beat": 1, "key": "D", "duration": "1"},
            {"bar": 2, "beat": 3, "key": "A", "duration": "1/2"},
            {"bar": 5, "beat": 1, "key": "G", "duration": "1"},
            {"bar": 7, "beat": 1, "key": "E", "duration": "2bars"}]}
```

A new section: a new sound, a mediant key, a new voicing and a pattern:

```json
{"loop_slot": 1, "sound": 47, "voicing": 70, "perform": {"mode": "pattern", "pattern": "tresillo"},
 "events": [{"bar": 1, "beat": 1, "key": "F#", "chord": "chord.M7", "duration": "2bars"},
            {"bar": 3, "beat": 1, "key": "G#", "chord": "chord.maj", "duration": "2bars"},
            {"bar": 5, "beat": 1, "key": "D#", "chord": "chord.m7", "duration": "2bars"},
            {"bar": 7, "beat": 1, "key": "C#", "chord": "chord.sus", "duration": "2bars"}]}
```

Then let it play. Listen to the reply (how each note landed, the plan's moves). Write the next part as a development
of what is sounding, not a reset, and keep the journey going.
