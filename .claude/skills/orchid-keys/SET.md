# The set: a plan that works on this rig

Follow this plan (DJ.md step 2) unless the operator's feedback or your notebook says otherwise. It is built only from
what is taught: the 12 keys and the chord buttons `maj`, `min`, `sus` and `dim`. Every progression stays in one key.
Every tempo and beat is one the arms can follow. Your job is to perform it well: when to move, smooth fades, dynamics,
and small variations inside a section. Don't reinvent it each heartbeat.

## Before the first note

- Check press hardness and arm speed in `status`: about 85% and 2.4× suit a set. They are the operator's settings,
  so ask the operator to change them in the console if needed. Below about 60% hardness, Orchid's velocity falls
  under 20 and the loops come out weak (see the notebook); if the operator keeps it lower on purpose, raise the mixer.
- Studio: loops of 8 bars (`loop-configure` while stopped). Mixer: layers 0.8, `live` 0.85, drums per section.
- Choose one Pistil sound per cycle from `sounds-list` (a warm pad or keys sound for cycles 1–2, something brighter for
  the peak). Note in the notebook which sounds worked.

## Layers: one job each

| Slot | Role | Perform mode | How to write it |
|---|---|---|---|
| 1 | Pad: the harmony | `harp` or `strum`, long reverb | 2–3 chords, each 2–4 bars, back to back (legato) |
| 2 | Motion: the same chords, moving | `arp2` or `pattern` (`pulse`, `offbeat`, `tresillo`) | exactly layer 1's chords on the same bars |
| 3 | Bass: single keys, roots and fifths | `off` | one key every 1–2 bars, with space |
| 4 | Colour: the peak only | `bloom`, `drift` or `orbit`, delay | a sparse chord or two from the same key |

**Write looped layers legato.** Each chord lasts until the next one, for example `1.1 A+min:4bars 5.1 F+maj:4bars`. The
loop layer gets every chord on its written beat for its written length, however the arms' pass went. The plan will
say a change "moves later": that only means the live pass takes longer. The loop is still exact. A chord change takes
the arms about 5 s, so a pass of a 4-chord, 8-bar loop runs about two loop lengths: record those with `live` turned down
(below). Keep at least 2 bars per chord.

**Avoid a live clash.** While the arms play a pass, Orchid's live voice sounds over the loops. If the new part is in a
different key from what is looping, either record it into a free or muted layer, or turn `live` down to about 0.3 for
that pass. Bring it up when the arms play in the loops' key.

## Rhythm: where it comes from

Long chords alone sound square. A set needs rhythm in at least two layers, placed in different spots in the bar. The
arms are the slowest source, so use them for what only they can add, and get the groove from four places:

1. **Drums.** Pick beats with a syncopated feel: `broken-compass` (3–3–2), `subway-steps` (two-step), `blue-platform`
   (loping two-step), `chrome-messenger` (kick syncopation). A four-on-the-floor beat needs off-beats elsewhere.
2. **Perform modes** turn a held chord into rhythm at no cost to the arms. Use `pattern` with `tresillo`, `offbeat`,
   `shuffle` or `backbeat`; `arp2` with `step_beats` 0.5 or 0.25 and a short `gate` (0.4) for staccato. Give the pad
   and motion layers different rhythms.
3. **Rhythmic loop layers.** A looped layer plays every note on its written beat, however long the arms took on the
   pass. So write real rhythm into it: syncopation, anticipations, 8th and 16th placements. The arms record it once
   (turn `live` down to about 0.3 for that pass); the loop plays it tight from then on. Each note costs the pass about 2
   s, so keep a layer to 8–12 notes per 8 bars. That is enough for a riff.
4. **The live arm part**, heard as played. The arms manage about **one short note per bar** at 116–124 BPM (less at 132).
   **Where it falls is free**: put it on the "and" of 2 (`x.2.5`), on beat 4, or anticipate the next bar (`x.4.5`).
   Use 1/8 or 1/16 note values. A long hold delays the next note.

Writing rhythm (a beat like `2.5` is the "and" of 2; `4.5` anticipates the next bar):
- **Anticipate chord changes.** Write the change on `4.5` of the bar before (`2.4.5 F+maj`) instead of on `3.1`. It
  costs nothing and pushes the groove forward.
- **Interlock the layers.** Bass on 1 and the "and" of 2; stabs on the off-beats (`x.2.5`, `x.4.5`); the pad holds. Two
  layers should not both hit only beat 1.
- **Call and response.** A riff in bars 1–2 and 5–6 on one layer, an answer in bars 3–4 and 7–8 on another.
- **Leave space.** A bar of rest in a riff makes the next note land.
- **Density follows the arc.** Intro: notes on 1. Build: add off-beats. Peak: the busiest riffs and patterns. Breakdown:
  back to long notes. Drop: everything on bar 1.

Loop riffs for cycle 1 (A minor), 8 bars each; transpose for the other cycles:
- Bass, tresillo feel: `1.1 A:1/8 1.2.5 A:1/8 1.4 E:1/8 3.1 F:1/8 3.2.5 F:1/8 3.4 C:1/8 5.1 A:1/8 5.2.5 A:1/8 5.4 E:1/8 7.1 G:1/8 7.2.5 G:1/8 7.4 D:1/8`
- Off-beat stabs (colour layer, `perform mode=off`, short): `1.2.5 E:1/16 2.4.5 C:1/16 3.2.5 A:1/16 4.4.5 E:1/16 5.2.5 E:1/16 6.4.5 C:1/16 7.2.5 B:1/16 8.4.5 E:1/16`
- Answer phrase (bars 3–4, 7–8): `3.1.5 C:1/8 3.3 B:1/8 4.2.5 A:1/8 7.1.5 C:1/8 7.3 D:1/8 8.2.5 E:1/8`

Live part over the loops, on time: notes 5½ beats apart, so each lands somewhere new in the bar (a displaced, dotted
feel). Keep live notes at least 5 beats apart at 120–132 BPM:
`1.1 A:1/8 2.2.5 C:1/8 3.4 E:1/8 5.1.5 D:1/8 6.3 C:1/8 7.4.5 B:1/8`

## Cycle 1: A minor, warm (about 12 minutes)

Beat `velvet-floor` (warm brushed micro-house, 116), then `peach-district` (122) at the build.

| Section | Phrases | Moves |
|---|---|---|
| Intro | 2 | Tempo 116. Drums 0.35. Layer 1: `1.1 A+min:4bars 5.1 F+maj:4bars`, harp, reverb hall 45. |
| Build | 3 | Layer 2: the same chords in `pattern` `tresillo`. Drums fade to 0.6 over 16 bars. Then layer 3: the tresillo bass riff (Rhythm, above). Tempo to 120 over 8 s; beat `peach-district`. |
| Peak | 4 | Layer 1: `1.1 A+min:2bars 2.4.5 F+maj:2bars 4.4.5 C+maj:2bars 6.4.5 G+sus:2bars` (anticipated). Layer 2 follows in `arp2`, `step_beats` 0.5, `gate` 0.4. Layer 4: the off-beat stabs, delay 0.75 beats. Drums 0.75. Between passes, play the live part over the top. |
| Breakdown | 2 | Drums fade to 0 over 8 bars. Mute 2, 3 and 4. Layer 1: `1.1 A+sus:8bars`, reverb 60, its layer volume down a little (softer). |
| Drop | 1 | On bar 1, all at once: unmute 2 and 3, drums 0.75, layer 1 back to the peak progression and full volume. |

## Transition to cycle 2 (2 phrases)

Keep drums and layer 3 going. Clear layer 4. Layer 3 to a bass shared by both keys: `1.1 E:4bars 5.1 C:4bars`. Then
replace layer 1 with cycle 2's pad and layer 2 with its motion. A minor → E minor is up a fifth: a gentle lift.

## Cycle 2: E minor, brighter (about 10 minutes)

Beat `glass-house` (bright clipped house, 124). Tempo 120 → 124 over 8 s at the start.

- Pad: `1.1 E+min:4bars 5.1 C+maj:2bars 7.1 D+maj:2bars`
- Peak pad: `1.1 E+min:2bars 3.1 C+maj:2bars 5.1 G+maj:2bars 7.1 D+sus:2bars`
- Bass: `1.1 E:2bars 3.1 C:2bars 5.1 G:2bars 7.1 D:2bars`
- Colour, peak only: `1.1 G+maj:4bars 5.1 A+min:4bars`
- Tension before the breakdown: end the pad on `7.1 B+maj:2bars` (the dominant), then breakdown on `1.1 E+sus:8bars`.

## Cycle 3: D minor, darker and slower (about 10 minutes)

The tempo drops in the breakdown, with the drums out: tempo 124 → 112 over 10 s. Beat `broken-compass` (interlocking
3–3–2, 112).

- Pad: `1.1 D+min:4bars 5.1 A#+maj:2bars 7.1 C+maj:2bars`
- Peak pad: `1.1 D+min:2bars 3.1 A#+maj:2bars 5.1 F+maj:2bars 7.1 A+maj:2bars`
- Bass: `1.1 D:2bars 3.1 A#:2bars 5.1 F:2bars 7.1 A:2bars`
- Passing colour: `C#+dim` for 1 bar before `D+min` (once per cycle, layer 4).

## Cycle 4: C major, the lift and finale (about 8 minutes)

Beat `aurora-drive` (airy synth-pop drive, 120). Tempo 112 → 120 over 10 s, drums back on bar 1.

- Pad: `1.1 C+maj:4bars 5.1 G+maj:2bars 7.1 F+maj:2bars`
- Peak pad: `1.1 C+maj:2bars 3.1 E+min:2bars 5.1 F+maj:2bars 7.1 G+sus:2bars`
- Bass: `1.1 C:2bars 3.1 E:2bars 5.1 F:2bars 7.1 G:2bars`
- Then back to cycle 1 (C major and A minor share every chord), with a different sound and Perform modes so it is not
  a repeat.

## Variations inside a section (pick one per heartbeat, at most)

- Swap layer 2's mode (`arp2` ↔ `pattern` with a different rhythm).
- Bass: move one note to the "and" (`2.3 E:1/2`), or rest a bar.
- Sweep reverb or delay on one layer over 8–16 bars.
- Drum volume ±0.1, or `drums-length` to 4 bars for a tighter loop at the peak.
- Mute the bass for 1 phrase, then bring it back on bar 1.
- A layer's volume ±0.1 between passes, for dynamics (the mixer, not the arms).
