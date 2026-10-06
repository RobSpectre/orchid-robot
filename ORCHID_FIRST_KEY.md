> Legacy terminal workflow, retained for reference. The [local Python web console](README.md) is now the operator entry point. Do not run both controllers at once.

# Orchid key teaching

## Chat-guided session: all 12 keys

This is the preferred workflow with the padded gripper. One program stays open;
send short voice messages to the agent while your hands guide the arm. The agent
relays those messages to the local session and reads actual acknowledgements.
The program does not listen to the microphone itself.

Stop other arm scripts first, with the arm securely supported/resting. Run:

```bash
/home/rspectre/.virtualenvs/replay-lw/bin/python \
  /home/rspectre/workspace/replay-lw/orchid_session.py serve \
  --port /dev/ttyACM1 \
  --calibration /home/rspectre/.cache/huggingface/lerobot/calibration/robots/so_follower/so101_follower.json \
  --contact-ready --fixture-note "Padded fixed gripper; Orchid voice session"
```

Type `ready` once with the arm resting securely clear of the instrument. There
are 10 seconds to support it with both hands before torque releases. After
`SESSION READY`, tell the agent **session ready**. Keep the terminal visible for
immediate motor-state messages; bells are optional and may be muted.

Start with C and proceed C#, D, D#, E, F, F#, G, G#, A, A#, B. Hold each endpoint
steady and wait for the capture acknowledgement before moving to the next stage.
There is no per-stage countdown or timeout requiring you to rush.

| Say to the agent | What you physically do | What the session does |
| --- | --- | --- |
| **Pressed** | Support the arm's weight; gently press the selected key only far enough to sound. Hold still. | Capture the pressed endpoint with all six motors OFF; start recording the actual lift. |
| **Touching** | Slowly lift until the key is fully released but the pad still barely touches it. Hold still. | Mark light contact within the continuous release path. |
| **Clear** | Lift slowly to a small visible clearance above this same key; hold still and keep supporting. | Finish the path, validate it, seed current-position goals, enable a hold. No stroke starts. |
| **Test** | After **HOLDING, torque ON**, gently remove your hands and watch the key. | Perform one bounded press along the recorded path in reverse, then return along it to hover. |
| **Pass** / **Fail** | Report whether the intended key sounded once and released. | Pass saves that key with the observed trial count; fail preserves the existing map and keeps the draft. |
| **Next, supported** | Support the held arm with both hands before speaking. | Release torque, verify OFF, and select the next key. Move between keys by hand, clear of the instrument. |

Say **test** again while held to check repeatability, followed by pass/fail for
that trial. One successful trial is only an initial check, not proof of long-run
reliability. Say **retry, supported** to redo the current key without restarting
the program; **release, supported** to release without advancing; or
**quit, supported** to release and end. These support confirmations must come
from the human, never inferred by an agent.

The key difference: the current clear pose becomes the new hover, so you never
manually hunt for an old set of six encoder values. Only actual measured points
are retained, and the existing motion, joint-limit, timing, gripper-opening, and
tracking checks remain. If a capture fails, the process stays open for a retry.
The old map remains until a successful trial is explicitly accepted. Each save
backs up the previous map; attempts and raw observations stay in `orchid_session/`.

This captures local strokes for the 12 keys. Powered travel between keys still
requires a separately taught and checked clearance route. Never play a cross-key
straight-line move based only on these hover positions. Changing the pad, jaw,
instrument placement, or arm mounting invalidates the physical teaching even if
the encoder calibration file has not changed.

### Agent controls

The human's terminal owns the serial port. These commands only access local
files, so they also work when the agent's sandbox cannot see `/dev/ttyACM1`:

```bash
python3 /home/rspectre/workspace/replay-lw/orchid_session.py status
python3 /home/rspectre/workspace/replay-lw/orchid_session.py send pressed
python3 /home/rspectre/workspace/replay-lw/orchid_session.py send touch
python3 /home/rspectre/workspace/replay-lw/orchid_session.py send clear
python3 /home/rspectre/workspace/replay-lw/orchid_session.py send test
python3 /home/rspectre/workspace/replay-lw/orchid_session.py send pass
python3 /home/rspectre/workspace/replay-lw/orchid_session.py send next --supported
```

Read fresh status before acting, confirm the selected key/stage, and send each
physical command only in response to the human's matching cue. `select --key D`
chooses a different key when ready. A command is bound to a session ID and stage
revision and expires after 10 seconds. Duplicate or outdated commands cannot
replay a stroke. `queued` means queued, not executed: inspect `status` and the
returned response file before reporting success or submitting another command.
Do not automatically retry a failed movement. Hardware state can change while
the chat is idle; use the fresh feedback timestamp as well as the heartbeat.

`send stop` is checked locally during a stroke or hold and requests a measured
hold without retracting or dropping torque. **Chat is not an emergency stop.**
Use the accessible motor power stop for an urgent problem while supporting the
arm against falling. Ctrl+C ends the process with a best-effort hold; it never
automatically drops torque. A serial/power failure can prevent any software stop
from taking effect. This is position control, with no contact-force sensor.

Software tests use fake motors, including the complete 12-key sequence. Actual
contact geometry and musical success must still be verified at the instrument.

## Original single-key workflow

This prototype stores **12 keys: C, C#, D, D#, E, F, F#, G, G#, A, A#, B**.
Each taught key contains a short downstroke, with a hover position, a first-contact
sample, and an endpoint where the key just triggers. Release reverses the same
path. Only one press is attempted per invocation; there are no automatic retries
or transitions between keys.

`orchid_keys.json` starts with 12 empty slots. No robot positions have been guessed.
The default `play` command prints a plan without opening any hardware.

## Physical setup

The current contact tool is the existing gripper. **Capture hover only while it is
bare. Before contact teaching, fit a small soft pad to one jaw**, secure it so it
cannot slip, and use that jaw as the finger. Keep the opening unchanged throughout
teaching and playback. The pad must fit within the key with positioning clearance.
Mount the arm and secure Orchid against movement.

This is position control without a force sensor, calibrated Cartesian geometry,
or collision checking. Small joint movements can still exert damaging force. A
soft pad cushions contact but does not impose a reliable force limit. Begin with
a compliant dummy surface, then attended trials on the instrument. Support the
arm whenever torque is off, and provide a physical catch/support and an accessible
motor power stop so a power loss cannot drop the arm onto Orchid.

Teach the fingertip approximately straight down over one key, with closely spaced
samples. Joint interpolation does not guarantee a straight fingertip path between
samples. Watch the wrist, other jaw, and arm links as well as the contacting jaw.
Never use the key to support the weight of the unpowered arm. Press only far enough
to trigger; do not deliberately bottom out the key.

## Environment and device selection

The implementation was checked against the existing `replay-lw` environment with
LeRobot 0.6.1. The older replay setup elsewhere in the README pins 0.4.5; do not
reinstall or change versions as part of this trial. The key file records the
installed LeRobot version and a fingerprint of the arm calibration. Playback
rejects a mismatch and requires re-teaching after checking the setup.

```bash
source /home/rspectre/.virtualenvs/replay-lw/bin/activate
cd /home/rspectre/workspace/replay-lw
python find_ports.py
```

Use the confirmed follower port from that output; the value below is an example.
The calibration path below is the existing follower file on this machine.

```bash
export FOLLOWER_PORT=/dev/ttyACM1
export ORCHID_CALIBRATION=/home/rspectre/.cache/huggingface/lerobot/calibration/robots/so_follower/so101_follower.json
```

All live commands require an explicit port, explicit calibration file, and an
attended terminal (the read-only `status` command also works without a terminal).
The bus connection checks the existing motor setup; it does
not run calibration, change gains, or call LeRobot's robot configuration routine.
Positions are native STS3215 ticks, not normalized replay-dataset values.

For a separate new map, use `python orchid_key.py init --file another_map.json`.
`init` refuses to overwrite a file. Pass the same `--file` to subsequent commands.

## 1. Capture hover with the current gripper

Start with one accessible white key; these examples use C. The label identifies
the physical key and does not assume any particular MIDI octave.

```bash
python orchid_key.py teach --key C \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION" \
  --fixture-note "Initial fixed placement; bare gripper; hover only"
```

Support the arm, then type `supported` when the script asks. It disables torque
and verifies that all motors are off. Manually position one jaw above the key,
with clear separation from the surface. Hold still and press Enter. This saves
hover only and does not permit contact playback. Torque stays off; keep supporting
the arm until it is resting safely.

Agents must leave these physical actions and terminal responses to the human.
Never pipe answers into the script or fabricate captures.

Preview the saved hover without hardware:

```bash
python orchid_key.py play --key C --hover-only
```

## 2. Teach the downstroke after fitting the pad

Changing the tool changes its contact geometry, so capture the hover again with
the pad fitted. Record a placement description that will help reproduce the setup.

### Hands-free capture

If an earlier teaching process is still running, support/rest the arm safely and
exit that process with Ctrl+C first. Use only one process on the follower port.

```bash
python orchid_key.py teach --key C --contact-ready --hands-free \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION" \
  --fixture-note "Padded fixed jaw; first C teaching"
```

Type `ready` once, with the arm resting securely clear of Orchid. You then have
**10 seconds to support it with both hands before torque is released**. The script
prints each motor's actual `Torque_Enable` value. All six must report `0 (OFF)`.

If a resting joint is near or outside the calibrated working margin, the script
now lets you reach this supported torque-release step. It then pauses capture,
prints the affected joint's current encoder value and permitted range, and waits
up to 60 seconds for you to reposition the supported arm by hand, clear of the
keys. The hover countdown starts only when every joint is back within range.
It never sends a recovery movement or changes the calibration. Recorded poses
and all powered playback still enforce the same joint limits.

Follow the three displayed stages, with **8 seconds per stage**:

1. **HOVER:** hold the pad clear above the key until `CAPTURED` appears.
2. **FIRST CONTACT:** lower slowly until the pad just touches the key, without
   pressing it. Hold that position until `CAPTURED`.
3. **JUST TRIGGERED:** press gently until the key just sounds, then hold still until
   `CAPTURED`. Don't bottom out the key.

Intermediate positions are recorded automatically while you move. No typing is
needed during these stages. Keep the jaw opening fixed. Watch the countdowns;
terminal bells are also emitted but may be muted by your terminal. Increase
`--stage-seconds` (up to 30) if 8 seconds is too short. `--setup-seconds` adjusts
the initial support countdown (5–60 seconds).

Position sampling targets a 50 ms period. If skipping a small measured movement
would make the next saved gap too large, the actual skipped measurement is kept.
No missing positions are invented. A jump larger than 24 ticks between actual
encoder readings still stops capture; its error now names the joint, both
readings, and the gap. Restart with hover close above the key and move more
slowly; `--stage-seconds 15` gives more time without changing movement limits.

Capture requires at least 0.6 seconds of stable readings. If you are still moving
when a stage ends, it waits up to five extra seconds, then aborts instead of
advancing with an unstable pose. Move slowly: missing path spans, gripper drift,
torque turning on, incomplete motor replies, or stale feedback abort capture.
Teaching sends no position goals and never enables torque. The torque flags are
checked before and after every position read throughout timed capture.

After the final cue, lift the arm clear and rest it safely. Only then type `save`
to accept the recording, or anything else to discard it. **The countdown does not
detect contact or a sounding note.** Save only if the captured endpoints matched
the actual hover, first contact, and just-triggered positions. A mistimed or failed
attempt leaves the old key definition unchanged. Omitting `--contact-ready` uses
the same hands-free flow for hover only.

### Manual capture (optional)

The original Enter/`touch`/`pressed` workflow is still available by omitting
`--hands-free`:

```bash
python orchid_key.py teach --key C --contact-ready \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION" \
  --fixture-note "Fixed cradle and arm; soft pad on the fixed lower jaw"
```

1. Support the arm and confirm torque release.
2. Capture hover just above the selected key, with enough clearance to release it.
3. Lower a small amount, hold still, and press Enter to capture an intermediate
   sample. Repeat as needed. A sample over 24 ticks (about 2.1 motor degrees) from
   the previous sample is rejected; return closer and capture a smaller step.
4. At the first gentle contact, type `touch` to mark that sample.
5. Continue with small, supported samples. When the key just sounds, type
   `pressed`. This saves the complete local stroke. Lift off manually afterward.

The gripper must remain at its initial opening. Capture requires three stable
readings. `q` discards the current teaching session. A successful re-teach replaces
only that key, with the previous entire key file saved as a uniquely named `.bak`.

### Torque and resistance

Torque release now writes only the six RAM `Torque_Enable` switches, then prints
and verifies all six readbacks. It leaves EEPROM lock and protection settings
unchanged. A missing motor reply is an error, never evidence that torque is off.

After exiting any process using the port, inspect the current flags without
changing anything:

```bash
python orchid_key.py status \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION"
```

`0 (OFF)` means the servo reports its active torque switch disabled. It does not
disconnect the gears or guarantee frictionless movement. Steady mechanical drag
can remain. A joint holding position, springing back, buzzing, or binding should
not be forced. Stop teaching and inspect the reported values first. Another
teleoperation/replay process can change motor state, which is why only one process
should use the follower port.

`status` also prints current encoder positions and any joint outside the capture
margin. A "calibrated working margin" error is a position check, not a permission
denial. Re-running `teach` allows supported, torque-off repositioning as described
above. If normal playing positions remain outside the reported range, check the
physical setup and arm calibration rather than widening limits to force a replay.

To distinguish electrical holding from passive resistance, first rest/support
the arm away from Orchid, stop control programs, and disconnect the follower's
12 V motor supply. Gently compare the feel over a small, unobstructed range. If
resistance persists without motor power, investigate the gearing, joint assembly,
and cable routing rather than increasing force. Reconnect power only with the arm
supported, then verify the torque flags again. Do not attempt encoder recording
with the motors' supply disconnected.

## 3. Preview and check the starting pose

```bash
python orchid_key.py play --key C --fraction 0.25
```

The fraction selects part of the taught path **after first contact**. It is a
fraction of cumulative joint travel, not millimetres or a force setting. `1.0`
reaches the taught pressed endpoint; values above 1 are refused. A quarter stroke
may not sound a note, and the script will not deepen or retry it automatically.

With torque off and the arm supported, use read-only alignment feedback:

```bash
python orchid_key.py align --key C \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION"
```

It displays target-minus-current tick differences and exits after a second within
the starting tolerance. Continue supporting the arm. Alignment sends no goals and
does not change torque. Ctrl+C ends it.

First test holding at hover:

```bash
python orchid_key.py play --key C --hover-only --hands-free-start --execute \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION"
```

With `--hands-free-start`, type `play` once while the arm is resting securely.
There are then ten seconds to support the arm with both hands. Follow the live
target-minus-current tick display to guide it back to the saved hover pose, with
the pad clear above the key. Motors remain off throughout alignment. Once aligned,
the script automatically rechecks stability, seeds and verifies current-position
goals, then enables torque. Keep supporting the arm until the holding-position
message, then clear hands during the three-second countdown. A small correction
to saved hover is interpolated; no move from a distant start is attempted.

The read-only alignment stage times out after 60 seconds without enabling motors
if the arm cannot be aligned. `--setup-seconds` changes the initial ten-second
countdown. Omitting `--hands-free-start` retains the original flow, which requires
the arm to be supported and already aligned when you type `play`.

**Playback leaves torque ON at hover.** Support the arm, then explicitly release:

```bash
python orchid_key.py release \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION"
```

Type `supported` when prompted. It verifies torque is off. Do this before manual
repositioning, re-running playback, or leaving the setup.

## 4. Attempt one supervised press

After the hover test and inspection of the path, align the supported, torque-off
arm again and run:

```bash
python orchid_key.py play --key C --fraction 0.25 --hands-free-start --execute \
  --port "$FOLLOWER_PORT" --calibration "$ORCHID_CALIBRATION"
```

It approaches, performs the selected part of the contact stroke, holds for 0.2
seconds, and returns along the same path. Each taught waypoint must settle before
continuing. Inspect the result before a separate trial at a larger fraction such
as `0.5`, then `1.0`. Always release torque with support and realign between trials.
If the full taught stroke does not work, inspect/re-teach instead of increasing
limits or pressing beyond it.

Each live attempt writes a JSONL log under `orchid_runs/`, including the taught
entry, fraction, timestamps, commanded/measured positions, outcome, and optional
MIDI events. Software checks bound the taught joint envelope, fixed gripper,
target-to-measured error, loop delays, and total routine time. Smooth command
profiles are quantized to motor ticks; they do not measure actual force or speed.

On Ctrl+C or a control fault, the script attempts a hold at a fresh measured
position. It **does not blindly retract or release torque**. If communication is
lost, the previous servo target can remain active; a software timeout cannot stop
a disconnected servo. Support the arm and use the hardware stop when necessary.
Do not leave it powered against the key after a fault. If the release command
cannot communicate or rejects the hardware state, use the supported power-off
procedure rather than repeatedly retrying the stroke.

## Optional MIDI verification

Without MIDI, the outcome says that motion completed and musical success has not
been automatically verified. You must listen/watch. MIDI input is optional and
is not yet installed in the existing environment; to add it later:

```bash
python -m pip install mido python-rtmidi
python orchid_key.py midi-ports
```

Use the exact input name, a single selected MIDI channel, and the note number
observed from a manual press under the same Orchid settings. Turn off chord/key
mode, arpeggiation, bass, loops, and other transformations for this first test.
The physical key label C does not guarantee MIDI note 60.

Add `--midi-port "EXACT INPUT NAME" --midi-note 60 --midi-channel 1` to a live play
command only after confirming those values. The listener sends no MIDI output.
It expects exactly one matching note-on followed by note-off, and rejects missing,
wrong, duplicate, or reversed events on that channel. Verification happens after
releasing to hover. A failed result never triggers another press.

## Recalibration and remaining work

Re-run `teach --key C` (or another key) to replace only that definition. Keep
keyboard placement, arm mounting, jaw opening, and pad unchanged between teaching
and playback. Re-teach affected keys after any of them changes. Changes to the arm
calibration or LeRobot version automatically invalidate saved entries.

This first milestone uses taught joint paths. It does **not yet** implement the
three-reference-point keyboard coordinate system, automatic collision checking,
force-limited contact, or safe travel between keys. Those require measured tool
geometry and physical validation. The 12-slot map can later hold keyboard-relative
targets without storing every possible key-to-key transition.

Run the hardware-free controller and fault tests with:

```bash
python -m unittest discover -s tests -v
```
