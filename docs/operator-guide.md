# Operator guide

Run `python app.py` in the prepared Python environment and open http://127.0.0.1:8080. The **SIMULATION · NO HARDWARE** badge means every motion is virtual. Hardware requires launching with `--enable-hardware` and has a distinct badge.

## Preparation

Secure the arm base and synthesizer to reproducible marks. Fit the rubber tip gloves or padded contact surface, inspect the attachment, and select the matching **Contact tool** in the console. Check the gloves cannot slide or bunch up. Keep the opening fixed during teaching and tests. There is no fingertip force sensor: use the minimum effective contact, and watch for compression or key bottoming out.

Support the arm's full weight before any torque release. Six OFF readings mean motor drive is disabled, not that the gearbox is frictionless. Do not overcome significant resistance by forcing a joint. Keep an accessible physical power stop and a safe resting support available.

Safety confirmations are large touchscreen buttons. Tap **Arm this step** (or the named confirmation), check its highlighted **Confirmed** state, then press the action below it. Confirming alone sends no motor command. Tap again to cancel. Each attempt consumes its confirmations, including canceled countdowns and rejected captures; you must confirm again to retry. The optional saved-placement choice remains separate. Recovery has its own **Arm recovery** button.

Click **Refresh connections** to scan USB adapters using the same read-only detection as `find_ports.py`. Only ports with responding Feetech motors appear. Each arm shows its motor IDs and measured bus voltage. The voltage is a snapshot from the first responding motor, timestamped at refresh; leader/follower is inferred using the existing 8 V threshold. Choose the follower with motor IDs **1–6**. To use both arms, select **Use leader to teach** and choose the separate low-voltage arm in **Leader connection**. A leader cannot be selected as the follower; incomplete motor sets and arms with unreadable voltage cannot connect. `/dev/ttyACM` numbers can change after reconnecting. Refresh again after changing USB or power connections.

**Find and connect all arms** searches USB and connects everything it finds in one press (the search runs on a follower that is not connected). **Find leader** is also available from the calibration screen while the follower is connected, idle and torque off; it excludes the follower's occupied serial port. Neither scan changes torque or motor settings. Busy/unreadable adapters are skipped with a message; close any other serial controller before retrying. A scan failure clears the old choices. Connection independently reads all six motors' supply and operating mode without configuring the robot or enabling torque. Low supply voltage, missing motors, and the wrong operating mode are rejected. Simulation only displays practice arms and never scans physical devices.

Choose a placement name. “Keep saved placement” means the same physical setup, not just the same name. Leave it unconfirmed after moving anything, changing pad thickness or glove fit, adjusting the gripper, or repairing a joint. Selecting a different contact tool creates a new fixture even if “Keep saved placement” is confirmed. Previous records remain on disk but no longer count as registered for the new fixture.

To return to connection setup, select **01 · Connect the arm** in the sidebar. Opening this screen sends no motor command. If connected, pause leader following, support both arms, confirm **Arm this step**, then choose **Release & return to connections**. The optional five-second delay still applies. Saved calibrations and registered motions remain stored; an unfinished teaching attempt ends, and an unfinished calibration is canceled with its previous motor settings restored and verified. **Stay on current step** returns without disconnecting. Once disconnected, refresh ports and select the arms or teaching mode again.

If USB was unplugged or an arm was replaced, Release can fail because it still addresses the old connection. In the stopped session, open **01 · Connect the arm → USB unplugged or arm replaced?** Rest/support both arms and physically disconnect both motor power supplies; unplugging USB alone does not release torque. Confirm **Arms supported** and **Motor power disconnected**, then choose **Forget unavailable connection**. This closes the old connections without motor commands or a calibration rollback; it does not claim torque was verified off. Saved records remain stored. Reconnect with the arms secure, refresh ports, and select the detected arms. For a replacement arm or interrupted calibration, perform a full calibration and teach a new home; leave **Keep saved placement** unconfirmed and re-teach its motions. If closing the old port also fails, keep motor power disconnected and restart Python.

## Motor calibration

Select **Calibrate motors** in the sidebar or **Back to calibration** from the training screen. Choose **Follower** or **Leader**; each card shows its connection and calibration status. Selecting a card only changes the screen. If teaching or holding, pause leader following first, then confirm support and use **Release & return to calibration**. Saved calibrations and registered motions remain stored; the unfinished teaching attempt ends. Changing the arm during a calibration requires finishing or canceling that attempt first.

If the leader is not connected, choose **Leader → Find leader**, select its low-voltage port, confirm **Arm leader connection**, and press **Connect leader**. The follower stays connected and keeps its saved calibration. This enables leader teaching; both references must be verified before the workflow marks calibration complete. Support both arms, confirm **Arm this step**, and press **Calibrate leader**. After saving, use **Back to training** or **Train controls** in the sidebar. Reset/reload uses the selected arm's reference.

1. Rest/support the arm clear of Orchid. Confirm support and begin calibration. With the delay enabled, there are five seconds to get both hands onto the arm before release.
2. Center the joints and half-open the gripper. Hold steady during midpoint capture. The app updates and verifies encoder references; it does not move the arm to a midpoint.
3. The highlighted joint is recorded continuously. Gently move it in both directions through its usable range, staying away from strain and cable tension. Other joints may move as needed to support the arm; only the active joint's extremes are saved at this step.
4. Confirm each range in turn: base rotation, shoulder, elbow, wrist bend, gripper. Minimum and maximum tick values update on the page. There must be a meaningful span before continuing. Wrist rotation uses its encoder range without a sweep.
5. Review and save. Torque remains off. Set the final padded gripper opening before teaching notes.

The app backs up the previous motor calibration before homing changes. A normal abort or software fault attempts restoration and verifies it. If serial communication or power is lost, restoration may be unverified. Secure the arm, reconnect, and perform a full calibration before teaching; do not assume either the old or new references are usable.

Open **Reset or reload calibration** below the current step to see whether a saved six-motor reference exists and whether it is active. These controls are available during setup and at any calibration step, including the final review:

- **Reset calibration** discards the unfinished midpoint/ranges and starts again at the midpoint. It restores the pre-calibration motor settings before starting the new attempt. Your last saved calibration and registered motions remain saved until you finish and save a replacement calibration.
- **Reload saved calibration** discards the unfinished calibration, writes the last saved reference to all six motors, and verifies it before returning to training. Confirm that this is the same arm with no motors or joints replaced or reseated since that save. If the mechanism changed, reset and perform the full calibration instead. Reload is disabled until a calibration has been saved in this mode.

Both actions use their own large **Arm this step** support confirmation and the optional five-second delay. They keep torque off and never command an arm position. A failed write attempts to restore and verify the previous motor settings; an unverified restoration stops the session. During a note capture, powered hold, test or fault, first support the arm and use **Release or disconnect → Release torque** to return to setup. A reload preserves registrations only when their existing calibration and fixture checks still match; it does not make moved fixtures or changed contact tips valid again. Simulation and hardware references stay separate.

Midpoint capture checks the original steady pose, calculates new offsets from the current encoder reference, verifies the offset registers, and allows a short bounded interval for fresh position feedback. It accepts only three consecutive readings within 2047 ±3 ticks and a 2-tick spread per motor. A failed readback names each affected motor and its offset/spread; it does not assume you moved. Persistent mismatches still reject calibration and trigger restoration of the previous settings. No motor target or torque-enable command is used.

The eight-step tracker stays above the current instruction. During each sweep, the active joint is highlighted in the model and motor cards. Minimum, current, and maximum encoder readings update continuously. **Save range & continue** becomes available once travel exceeds the software's minimum sample spread and you confirm both directions; this is not an instruction to force extra travel. Existing calibrated limits are hidden while new references are being recorded. The preparation checklist, optional spoken cues, and capture delay keep the workflow in the browser.

## Reading motor status and the 3D view

Each of the six motor cards shows the measured encoder ticks, torque readback, angle from the captured midpoint, and position within the recorded range. A near-limit or outside-range label calls attention to the calibrated bounds. During a powered hold/test, the card also shows the commanded target and signed measured-minus-target difference. Unknown or stale torque is labeled **UNKNOWN**, not OFF. Old encoder values may remain visible as dimmed last readings.

Voltage and temperature are explicit snapshots. Use **Refresh volts & temperature** when the app is ready and all six motors report torque off. The button is unavailable during calibration, capture, holds, and tests, so health reads cannot slow those control loops. The timestamp tells you when the snapshot was taken; the supply shown in Arm status is the connection-time reading. Health measurements are diagnostics, not a force sensor or thermal protection system.

The 3D guide shows the manufacturer's SO101 CAD: its base, mounting brackets, six servo housings, wrist, and hinged jaw. During a sweep, match the **cyan servo, moving link, numbered marker, and curved direction arrows** to the physical arm. A large motor ID, name, physical landmark, and handling cue appear above the model. The camera turns to the current target when the step changes. The five sweeps correspond to motor IDs **1, 2, 3, 4, and 6**; motor 5 (wrist rotation) is not swept. Midpoint capture and review identify all six motors instead of singling out the base.

Drag to orbit, select a camera preset, or choose **Show target motor** to restore the recommended view. Focus the canvas and use arrow keys; `+` and `-` zoom. Camera controls do not send motor commands. Outside calibration, select a motor card to inspect that joint. During calibration, the step controls the target and motor selection is disabled. On wide screens the model stays beside the instructions as you scroll; on narrow screens it appears before them. If WebGL is unavailable, the app labels its fallback as a joint schematic, with the same motor numbers and guidance.

Before midpoint capture, the view is a **reference model**, not a measured pose. Afterward, encoder angles drive the model. A disconnected or stale view never claims a live pose. The view does not know the exact physical midpoint, mounting orientation, instrument location, pad thickness, glove fit, or slippage. The jaw is an estimated opening based on the recorded gripper range. Use the physical arm to assess clearance, and compare the model's direction and alignment joint by joint during commissioning. It is not an automatic motion planner.

References: [SO101 calibration guide](https://huggingface.co/docs/lerobot/en/so101) and [manufacturer's joint model](https://github.com/TheRobotStudio/SO-ARM100/blob/385e8d7c68e24945df6c60d9bd68837a4b7411ae/Simulation/SO101/so101_new_calib.urdf).

## Register each of the 12 notes

The steps below describe **Guide follower by hand**. For powered leader input, use the leader workflow below instead.

Select a key when the console is ready and choose **Teach** while supporting the arm. The active note is shown above the capture steps.

| Step | Operator action | Result |
| --- | --- | --- |
| Hover | Hold the pad just clear above the key and capture. | Begins recording the downward stroke. Torque stays off. |
| First contact | Lower until the pad barely touches the key without pressing it. Capture. | Marks the contact boundary. |
| Sounding press | Press only until the selected note sounds. Confirm support and choose **Capture press & hold**. | Saves hover → contact → press, seeds measured goals, enables torque and holds at the captured press. |
| Retreat | Clear your hands and choose **Retreat to hover**. | Moves directly from the pressed pose through contact to hover, reversing the saved path without repeating the press. |
| Test | After the hold is confirmed, gently remove hands and confirm the path is clear. | One slow press and release, followed by a hold at clearance. |
| Review | Listen and watch. Accept only a clean intended note and release. | Saves one successful trial; three are required for registration. |
| Continue | Support the arm and release torque. Move by hand to the next note. | Advances to the next unregistered key. |

Small pauses while deciding what to do next are fine with the page connected. During manual downward-stroke recording, keep movements gentle and local. If a pose is near a limit, a sample jumps, or the grip changes, correct the physical setup and re-teach. Do not increase software limits to get a bad path accepted.

**Reject & re-teach** clears the trial count for that attempt. A control's **REGISTERED** or checkmark state means three accepted trials with the current fixture/calibration. A simulation never counts as hardware registration. There is no autonomous travel between controls in this release.

For both-hand handling, leave the five-second delay enabled. Pressing a delayed control starts a visible countdown; support and position the arm before it reaches zero. Captures also require a brief steady reading. Optional spoken cues can help, but always check the visible status; browser speech can be muted.

## Teach with the leader arm

Leader teaching records the motion you make with the leader and replays it, using the installed LeRobot SO101 drivers and the same control loop as `teach_key.py` (`orchid_demo/teach.py`).

<kbd>Space</kbd> presses the highlighted button at every step, so one hand can stay on the leader.

1. **Connect arms.** Leader teaching is the default. The page scans for the arms by itself and selects the 12 V follower and the 5 V leader; connecting only reads the motors. Do not run another controller (such as `teach_key.py`) alongside this app; each takes an exclusive lock on the ports, so the second one refuses.
2. Calibrations that match the motors are reused. Otherwise calibrate the arm the page names under **Calibrate motors**.
3. **Teach C** (or select another key or chord button first). The follower's goal is set to its measured pose, LeRobot's `SOFollower.configure` applies its motor settings, and torque holds it there. The first time, that pose becomes the arm's **home**, used by every key. Then the follower ramps to the leader's pose at up to 30°/s and mirrors it 1:1. Keep hands off the follower and hold the leader roughly where the follower is. If the follower was already powered, the page first asks you to support it: torque blinks off for a moment while the settings are written.
4. **Hover, touch, press.** Guide the follower just above the key and press Space (hover); lower until the tip just touches without pressing, Space (touch); press only until it sounds, Space (press). A beep confirms each capture. Tapping ✓ Touch or ✓ Hover re-captures it before you go on. Tapping ✓ Home moves the arm's one home: every key, including keys already taught, then starts and ends there, and no key's hover/touch/press is cleared. Each point stores the commanded goal, so the press replays the depth you taught even where the key stopped the arm.
5. After the press is captured the follower returns on its own: press → touch → hover → home, then holds at home, and the key counts as taught. Put the leader back at rest before following again (following always ramps to wherever the leader is).
6. **Play.** The follower ramps to home (up to 30°/s, faster at higher speeds), waits until it has settled there, then goes home → hover → touch → press, holds the press for the key's **Press length** (default 0.3 s, 0–5 s), and returns the same way, with smooth joint moves (45°/s peak between home and hover, 20°/s for the strokes, at 1×). **Arm speed** is one global slider in the top bar (0.25–3×, where 3× is the maximum). Letting go of it saves it, and it applies from the next play to every key, chord button, dial turn and the API; try a new key at 1× first. **Press hardness** (top bar, 10–100%, default 50%) slows only the final touch → press stroke of every key and chord button, as a share of the other strokes: lower is gentler, 100% is as fast as the strokes. It applies from the next play; it does not change the press depth, and the release is not slowed. A press length typed next to Play is saved for that key when you play it. If the arm did not reach home (more than 8° off), it holds and offers **Play anyway**.
7. **Rest.** Click **☾ Rest** on the map, follow the leader to where the arm should wait, and press **Set rest here**. **Go to rest** moves the arm there (up to 30°/s) when you want it parked. Playback always ends at home, where the arm waits between key presses; a Play from rest first moves up to home. Home and rest are held for minutes. A joint held against its end of travel keeps its motor pushing, and the arm shivers. So the arm holds home and rest about 4° (45 encoder steps) inside each joint's travel, wrist rotation excepted. You can set them folded against a stop. The follower's wrist rotation reaches about ±168°. Its wrist follows the leader's as one continuous turn, even when the leader's reading wraps from +180° to −180°. When the leader is turned further than the follower's wrist can go, the follower's wrist waits at its end, and the training page says so. It follows again as soon as you turn the leader back within reach. A pose captured there is saved where the follower's wrist actually is. Capturing any pose (home, rest or a taught step) is refused when the follower's wrist is more than 4° short of the leader's. A pose saved like that earlier is played and held at the rotation the wrist reached.
8. Select another key on the map, press **Follow the leader**, and capture its hover, touch and press.
   **Re-teaching** a key, chord button or the dial goes through every step again, in order, as the first time. Following a
   taught control starts from hover (hover, open, lower and grip for the dial). Its saved motion is kept, and still
   plays, until the last step is captured. **✕ Cancel re-teach** drops the steps captured so far and keeps the saved
   motion exactly as it was, including the dial's shared steps; the arm keeps following or holding. Following again part
   way carries on from the next step. Release torque from **Release or disconnect** with the arm supported or resting.

Moves between points are straight lines in joint space, not obstacle-avoiding paths. If the move from home to hover passes too close to Orchid, capture hover higher or choose a home closer to the keys. **Voicing dial.** Select **↻ CW** or **↺ CCW** and teach, with the leader, **hover** (above the knob), **open** (jaws open, still above it), **lower** (lowered around the knob, not touching) and **grip** (jaws closed on it). These four steps are shared by both directions. Capturing the grip lets go, raises to the open pose, then goes to hover and home on its own. The turn is not taught with the leader: each direction has an angle (default +20° CW, −20° CCW; flip the sign if it turns the wrong way, up to ±90°), and playback rotates only the wrist by it. Playback: home → hover → open → lower → grip → turn → let go in place → raise to the open pose → hover → home; it never turns back while gripping. While following, the follower's wrist rotates with the leader's on either arm, and a taught point keeps the rotation it had when captured. Wrist rotation's reading wraps at ±180°. The follower's wrist follows the leader's as one continuous turn, waits at its own end (about ±168°) when the leader is turned further (the console says so), and follows again once the leader is turned back within reach. A dial direction's turn is still the computed wrist rotation from the grip.

Following and playback clip each goal to 15° from the measured pose (LeRobot's `max_relative_target` rule): a lagging or blocked joint is limited, never faulted. There are no tracking, settle, stall, home or route checks. **Stop motion** (Esc) holds the follower at its measured pose and stays in the teaching session. Losing the operator page for five seconds stops with a hold. A recording stays valid until the follower calibration changes. This is joint position control, without collision or force sensing; keep the physical power stop within reach.

**Playing without the leader.** Once a control is taught, only the follower is needed to play it. Connect with **Guide follower by hand** (or with the leader unplugged), select a taught control and press **Play**: the follower holds where it is, then plays through home. Teaching, re-teaching, following and setting home or rest still need the leader connected and calibrated.

Hardware conformance can be checked without opening a port: run `PYTHONPATH=.:tests /path/to/hardware/python -m unittest tests/test_teach_hardware.py` and `PYTHONPATH=. /path/to/hardware/python tests/check_native_teleop.py` using the app's LeRobot environment.

Simulation provides **Practice leader movement** with a joint selector and ± buttons while following.

## Checking notes with Orchid Studio

With hardware, every Play and sequence is checked against what Orchid actually sent, read from
[Orchid Studio](https://github.com/RobSpectre/orchid-studio)'s key monitor (start Studio with
`--sound-input Orchid`; the console looks for it on port 8765, `--studio-port` changes that and `0` turns
the check off). After a play the console shows **Orchid heard: C ✓ velocity 72, 0.11 s into the 0.80 s
press, held 0.31 s**, or **Check the arm: C: wrong key, B sounded**, a missed note or a repeated one. The
API and `scripts/orchid.py` report the same as `key_check`, and each result is saved in the session log.

Keys are compared by note name, because the voicing dial can move the octave. Chord buttons send no MIDI
on their own, so they are reported as unchecked unless a key sounds. Dial turns report the voicing-dial
clicks they produced. The check never changes or repeats a motion; if Studio is not running, it is marked
unavailable and the play is unaffected.

### Correct keys with Orchid

**04 Correct keys** in the sidebar plays each key the way it is played and listens through Orchid Studio. It records
which key actually sounded, then moves the taught motion until it presses the right key, cleanly, every time. If the
arm is not holding, the page offers **Go to home**. Confirm you are beside the arm, then press a key in the grid to
correct it, or **Correct all taught keys** to run them in turn. The grid shows each key's last result (✓ corrected,
✗ re-teach).

**Test all taught keys** checks them without changing anything. Each key is played 5 times at your playing speed and
press hardness, and the grid shows how many presses were clean (✓ 5/5 clean, ✗ 3/5 clean). Hover over a key for what
was heard, for example "E: 3/5 clean, F 2×". Nothing is moved or saved. Afterwards **Correct the keys that failed** corrects just those: it
lists the keys whose last test or correction did not pass. Run the test again whenever you like, for example after re-teaching a key or changing Arm
speed.

**Keys taught off the pattern.** The page fits all your taught presses to the keyboard's pattern: a white key every
slot, black keys half a slot along and further back. It lists any key whose press sits off that pattern, for example
"D# is 6 mm toward E". Each key is compared with the pattern of the other keys. The arm model's absolute position can
be 2 cm off, but every key is off the same way, so the comparison still works. When no key has failed, **Correct the keys off the
pattern** runs just those, leaving out keys that have since passed. **Correct all** starts with them.

For each key:

0. **Straighten.** No press. A stroke that comes down to the side of its press (touch or hover more than 1.5 mm along
   the row from the press) is rebuilt to come straight down onto it, keeping the finger's angle.
1. **Test.** The key is played 5 times in a row from home, at your **Arm speed** and **Press hardness**, as in
   playing. Each press is recorded: the right key, a neighbour, two keys at once, the key twice, or nothing.
2. **Move.** When a neighbour sounded, the hover, touch and press slide together, away from it:
   - **A black key that sounded a white neighbour** (D# hitting E) moves along the row toward the black key. A white
     key's back part runs right alongside the black keys, so the finger was beside the black key, not in front of it.
   - **A white key that sounded a black key** (C hitting C#) moves forward, onto the white key's wide front, clear of the
     black keys.
   - **Two white keys** move along the row, away from the neighbour.

   Each press the neighbour took moves the stroke a share of half the way between the two keys, and two keys at once
   count half. The first move is at least as far as the keyboard pattern says the key sits toward that neighbour. One
   round moves at most half a key's width, and the next test shows how much further to go. Up to 6 tests.
3. **Depth.** When it is the right key every time but some presses missed or sounded twice, gentle presses (0.5×
   speed, 25% press hardness) find where the key triggers. Touch then goes 1° before that point and press 0.75° past
   it. In later tests a miss presses 0.25° deeper (up to 1.5°), and a double trigger gives 0.5° more room above the
   trigger (up to 2.5°). A neighbour sounding on one of the deeper, gentle presses moves the stroke away from it
   and tests again. Otherwise it then tests again.

A key is corrected when a test gives 5 clean presses out of 5, within 6 tests. Correcting stops for a re-teach with
the leader if:
- a key more than one slot away sounds;
- the stroke would move more than a key's width from where it was taught;
- it would press more than 3° deeper than taught;
- the arm could only reach the new place by swinging. That is any part of it moving more than three times as far
  as the finger, or a joint turning over 30°.

The sideways moves are worked out with the arm model. That is accurate for moves of a few millimetres, because only
the model's zero points are estimated, not its geometry. The finger keeps its angle. Nothing is saved until a test
passes. The leader-taught hover, touch and press are kept, and correcting again always starts from them, so
corrections cannot creep. Gripper opening and wrist roll are never changed. **Stop motion**, **Stop correcting**,
release and disconnect end it, including the rest of the list. Correcting runs from the console only, not the API,
and only for keyboard keys, since chord buttons and the dial send no notes. Correct the keys again after moving the
arm or Orchid, or after changing Arm speed or Press hardness a lot.

The key check after every play also reports two keys struck together ("C and C# sounded together"): the finger landed
between them.

## Two followers on the registration plate

A second follower needs no setting. The connection page has one button, **Find and connect all arms**: it searches USB
and connects every arm it finds. A follower recognised by the calibration in its motors goes to its own arm, a new one to
the arm with no match, and the leader to the Keys Arm (or to the arm being connected). Connecting only reads the motors. When a scan finds two followers (or the Chord Arm has been used before), the sidebar
shows **Keys Arm** and **Chord Arm**; with one follower the console looks as it always has. Each follower
is recognised by the calibration stored in its motors' position limits (read-only), so the connect page preselects the
right port and refuses to connect the Keys Arm's hardware as the Chord Arm, which would overwrite its calibration. A scan never probes a
port the other arm has open. Each arm is only ever taught and plays its own controls: the Keys Arm the twelve keys, the Chord Arm the
chord buttons and voicing dial, whether or not the other arm is connected. The Chord Arm keeps its own data in `DATA_DIR/arm-b` (calibration,
home, taught controls); The Keys Arm's data is unchanged. The console shows the chosen arm's connection, calibration, training
and key correction, and greys out the other arm's controls. **Stop motion** and Esc stop both arms.

One leader teaches both, and on the training page it follows the arm you select in the sidebar: the arm that had it stops
following and holds where it is (torque stays on), and the selected arm connects it, even while that arm is holding. The
strip **Use the leader with this arm** does the same if switching did not (for example while the other arm was playing). Or, on the arm that has it, **Calibrate motors →
Leader → Hand the leader over**, then on the other arm **Find leader → Connect leader**. The leader's calibration is shared, so it does not need calibrating again.
**Re-centre wrist rotation** (Calibrate motors, torque released) works on one follower at a time with a shared leader. It moves that follower's wrist-roll zero by half a turn and leaves the leader alone: that follower then adds half a turn to the leader's wrist reading, so it follows the same physical turn. Use it when an arm's taught controls sit at the end of its wrist rotation (about ±168°) and the wrist cannot turn further while teaching. Its saved home, rest and taught controls are updated to match, so nothing needs re-teaching, and the other arm is unchanged.

The arms' reach overlaps above Orchid, so an **interlock** keeps them apart: an arm may leave its home only while the
other is parked (holding still within 5° of its own home or rest, or not connected), and only one arm may be away from home at a
time. A move back home needs only the other arm to hold still. Teach each arm a home and a rest clear of the other arm's reach.

**Chords.** A key played with a chord button is the one time both arms are away from home together, and orchid-robot runs
it from one instruction (`orchid.py chord C maj`, `POST /api/chords/play`, or a sequence step with a `chord`):

1. The Chord Arm presses the chord button and holds it down.
2. The Keys Arm plays the key while the Chord Arm holds.
3. As soon as the key is down (0.1 s after it reaches its press), the Chord Arm lets go and returns home while the Keys Arm finishes.

Both arms must be holding at home (or A at its rest) first. Before anything moves, both arms' paths are modelled on the plate. A chord whose
centre lines come within 40 mm is refused. That floor catches a pose taught into the other arm's way. It is not a precise
gap: the links are about 45 mm wide and the model can be 2 cm off, so teach chord-button motions clear of the Keys Arm by eye. The overlap is allowed only for that chord's own steps:
any other move on either arm is refused until both are parked again. **Stop motion** during a chord holds both arms
where they are, the chord button possibly still down. **Hold & keep playing**, then send the Chord Arm home. If the Keys Arm's
key is refused or does not start, the Chord Arm lets go and returns home. With Orchid Studio, the key check confirms the chord by
its tones above the key (so the voicing dial's inversions do not matter). It reports a press that sounded only the key
as "the chord button was not held".

The 3D view's **Plate** button shows the registration plate, Orchid in its pocket and both arms on their mounts with
their live poses. Each base was placed by fitting the SO101 base's four bolt holes to the plate's four M5 sockets (within
0.1 mm; see `orchid_demo/layout.py`); Orchid is drawn 43 mm tall. Poses are modelled from encoders and the CAD, so they
can be about 2 cm off: use it to see where the arms are, not to judge clearance by eye (the chord check allows for that error). Rebuild the plate visual with
`python scripts/build_plate_visual.py PLATE.stl` if the plate changes.

## Play from scripts or Claude (API)

While the console is open, connected and holding (Play, Teach or Follow any control once; playing needs only the follower), taught controls can be played and adjusted from `scripts/orchid.py` or the `orchid-keys` Claude skill. The API refuses to move the arm when the console is not in control, so its Stop button, <kbd>Esc</kbd> and lost-page stop always apply.

```bash
python3 scripts/orchid.py status                 # controls, speed, default note value, readiness
python3 scripts/orchid.py clock                  # Orchid Studio's tempo; whether notes land on its beat
python3 scripts/orchid.py play C --duration 1/4  # waits until the arm is back home
python3 scripts/orchid.py seq "C:1/4 E:1/4 r:1/4 G:1/2" --speed 1.5
python3 scripts/orchid.py chord C maj --duration 1/2   # both arms: Maj held by the Chord Arm, C played by the Keys Arm
python3 scripts/orchid.py seq "C+maj:1 A+min:1 F+maj:1 G+sus:1"
python3 scripts/orchid.py chord C maj --duration 4bars   # a long sweeping chord: the Keys Arm holds C for four bars
python3 scripts/orchid.py set ccw --turn -25     # a dial direction's turn angle
python3 scripts/orchid.py speed 2                # shared arm speed
python3 scripts/orchid.py duration 1/4           # the note value when none is given
python3 scripts/orchid.py home | stop
```

**Musical time.** The API takes note values, not seconds: a quarter note is one beat. You can write `1/4`, `1/8`,
`1/2`, `1`, dotted `1/8.`, triplet `1/8t`, or `q`/`e`/`h`/`w`/`s`. Long notes are bars of 4/4 (`4bars`), and `+` ties
values (`2bars+1/2`). A note can be held for any number of bars, for long sweeping chords; Stop motion ends it early. While a key or chord button is held, the arm does not keep pushing toward the taught press. About 0.15 s after it reaches the bottom, it holds where the key actually stopped it, plus a 0.5° preload that keeps the key down. It writes that goal once, and lifts from there.
Each note sounds for its value at Orchid Studio's
tempo, read from Studio's timeline (`clock` command), the same timeline its MIDI clock output follows. The note runs
from the strike, halfway down the press stroke, to the release, halfway back up; the hold at the bottom fills the rest.
While Studio's transport runs, the arm waits above each key and strikes on Studio's beat. The first note goes on the
next beat it can make, and each later note goes its written length after the one before. When it is stopped, a phrase
keeps its own time from its first note. The arm returns home between notes, so a note that comes sooner than the arm can
get there lands late by whole beats, and the rest of the phrase moves with it. The reply says how many beats late it was.
Simulation, with no Studio, keeps 120 BPM. The console's own Play still uses the press length in seconds.

HTTP endpoints (`X-Orchid-Token` from `GET /api/session` on POSTs): `GET /api/controls`, `GET /api/clock`, `POST /api/controls/{id}/play` (`duration?`, `speed?`, `turn_degrees?`), `POST /api/controls/{id}` (`turn_degrees`), `POST /api/sequence` (steps `{control or "rest", chord?, duration?}`), `POST /api/chords/play` (`key`, `chord`, `speed?`, `duration?`), `POST /api/settings` (`speed`, `duration`, `press_hardness` 0.1–1), `POST /api/home`, `POST /api/stop`. A sequence on one arm is one motion that passes through home between controls. A sequence with chords or with both arms' controls plays one step at a time, each arm back home before the next, keeping the rhythm across steps.

## Register the eight chord buttons

Select a button on the left of the instrument map. The upper row is **Dim, Min, Maj, Sus**; the lower row is **6, m7, M7, 9**. Lowercase m7 and uppercase M7 are separate buttons. Use the same light press → first contact → clearance → three tests sequence as the keyboard.

Set up a reference chord and keep Orchid's playstyle consistent. A chord button is not necessarily a standalone note trigger: extensions require a chord, and chord-type behavior depends on playstyle. Review the display and musical response as well as the clean physical release. The app records individual button motions; it does not coordinate simultaneous key/button combinations. **Continue at home** advances within the chord group during leader teaching; manual teaching uses **Release & continue**. After the group is complete, select another group on the map.

## Register the large voicing dial

Train **CW** and **CCW** separately. This first workflow uses a fixed rubber-covered or padded tip to nudge the rim. It does not squeeze, clamp, or press down on the dial. If the tips cannot reliably move the rim without slipping, reject the attempt and revise the contact setup before powered testing.

Before capture, describe the **reference chord and starting voicing** (the instrument's Geek Out view can help) and the **expected change** from this small gesture. This dial controls relative voicing changes; the number of clicks per inversion depends on the chord. Do not assume a gesture reaches an absolute setting or always changes one inversion.

| Step | Operator action |
| --- | --- |
| Start | Support the arm with torque off, set the reference voicing, and capture a position just clear of the rim. |
| Contact | Bring the fixed tip into light rim contact without turning or pressing down. Capture. |
| Turn | Make a small turn in the selected direction. Observe the expected change, confirm it, and capture. |
| Lift off | Lift completely clear without reversing the turn or dragging the rim. Confirm clearance and capture. |
| Return | Stay clear of the instrument and return near the initial clearance. Watch the live return error (target ≤ 6 encoder ticks). Confirm the whole return was clear, support the arm, and capture to establish a hold. |
| Test | With the tip held clear, manually restore the reference chord/voicing. Confirm that reset and hands clear, then test one gesture. |
| Review | Accept only the intended direction and expected effect, with no slipping, downward press, or reverse turn during return. Reset the reference and repeat until three trials pass. |

The powered trial follows the recorded loop forward, including the lift-off and separate clear return. It does not replay the turning stroke backward as it does for a key release. Clearance and musical effect remain operator observations: encoder checks cannot detect rim contact or count dial detents. Teaching the two directions adds two motions to the twelve keys and eight chord buttons, for **22 registered motions** total.

Instrument references: [Playstyles](https://support.telepathicinstruments.com/hc/en-us/articles/15280843614863-What-s-the-Difference-Between-Playstyles), [chord extensions](https://support.telepathicinstruments.com/hc/en-us/articles/16576229505167-Chord-Extensions-Explained), and [voicing and inversions](https://support.telepathicinstruments.com/hc/en-us/articles/15292199149839-Voicing-Engine-and-Inversions).

## Recovery and closing

| Situation | What to do |
| --- | --- |
| “Stopped” or a tracking error | Support the arm. Inspect the reported cause. Use the supported release/retry control; do not try another powered stroke first. |
| Browser disconnected | Motion stops when its control lease expires. Return to the page; inspect state before recovery. Use physical power stop if feedback is lost. |
| Read-only window | Another tab owns control. Close the old tab and wait five seconds. Taking over a lost powered session causes a stop, not automatic continuation. |
| Waiting for operator control | Reloading even a single tab creates a new page session. Leave it open for up to five seconds while the previous session expires. If another session keeps renewing control, the label changes to Read-only window. |
| Operator control unavailable | The control heartbeat failed for a reason other than another operator. Read the displayed error (for example, an invalid token or failed request). The page retries automatically with a fresh session token; controls stay disabled until a heartbeat succeeds. |
| Restarted Python | Starts disconnected. Connect and verify the unchanged fixture. A saved note remains a record; incomplete captures and active holds are not resumed. |
| Wrong port, permission denied, or busy port | Disconnect competing tools, check USB and local serial permissions, refresh ports, and select the follower. |
| Motor reference mismatch | Complete calibration before teaching. Never reuse old paths after reseating a joint. |
| Midpoint/range calibration interrupted | Normal cancellation attempts rollback. After loss of power/USB, recalibrate fully and re-teach. |

For normal shutdown, support the arm, open **Release or disconnect**, confirm support, and choose **Disconnect**. Rest the unpowered arm safely. Then close the browser and stop Python with Ctrl+C. Never rely on closing a browser to release torque.

Use **Copy error** beside an error to copy its full text, including joint measurements and targets. Copy is available while stopped, disconnected, or viewing without operator control, and on error entries in Session activity. If browser clipboard access is blocked, the app exposes selected text for manual copying. Copying sends no command to either arm.

Faults now show **STOPPED · UNVERIFIED** and the actual stop reason beside the leader controls. A joint-boundary notice can occur separately from the fault; read the stop reason before retrying. The Python terminal immediately prints arm faults, failed holds, rejected commands and worker/cleanup errors as JSON, including the failing measured/requested positions and traceback when available. The same records are saved in `data/hardware/arm-events.jsonl` (or `data/simulation/arm-events.jsonl`), with three rotated 5 MB backups. Feedback in an error record is cached and timestamped; logging does not add reads to a failed motor bus.

Explicit follower arming performs the position-control setup used by the pinned LeRobot replay driver: response delay, acceleration, angle-feedback mode, PID gains and gripper protection settings. It verifies the settings with torque off, then takes a stable position reading, seeds that position as the target, and enables the hold. Connection and leader input remain read-only. This setup does not change saved calibration. Leader positioning uses the native defaults described above; recorded automatic playback still uses the separate playback controller. A setup failure leaves torque off and reports the failed register.

Export the session for review. For a full backup, stop the app and copy `data/hardware/` (or `data/simulation/`). Do not copy just a live SQLite database while its WAL files may contain recent changes. Keep all files together.

## Commissioning the repaired arm

This new web workflow has not yet been validated on the repaired physical arm. Before a demo:

1. Inspect the reseated motor 3 and every joint fastening, the pad, cables, base, and keyboard mounting. Start with the instrument clear of the arm.
2. Confirm all six voltage/mode readings and supported torque release. Verify the OFF flags match freely supported manual handling; persistent gear drag alone is not evidence of powered torque.
3. Complete the full calibration and restart/reconnect once to verify saved calibration matches motor readback.
4. Teach C with minimal contact depth. Watch the handover hold, each of the three test presses, and release. Confirm the intended note, full key release, repeatability, and no excessive pressure.
5. Test software stop and browser-loss behavior while holding at a safe clearance. Verify supported recovery and the independently accessible physical stop. Do not test failures with the pad pressing the key.
6. Repeat registration for the other 11 notes and eight chord buttons. Commission one small dial direction before attempting the other; verify tip grip, actual voicing change, lift-off clearance, and repeatability from the same reference. The dial workflow also needs physical validation.
7. Save fixture marks, glove/pad details, date, and the exported session. Re-teach after contact-tool, glove-fit, opening, hardware, or placement changes.

Encoder position and tracking checks cannot measure force, detect every collision, or guarantee safety after mechanical slippage. Automatic performances, travel between keys, force sensing, and velocity-sensitive playing are future work.

## Reporting an arm error

Every new arm fault or rejected operation saves a local incident automatically. Open **Error reports**, optionally describe what you saw, then press **Report to Codex**. This queues that incident for the existing Codex chat; the chat checks the queue about once a minute while the Codex app is running. The report changes from **Queued** to **Codex is reviewing**, then **Reviewed** or **Operator action needed**. Queueing does not mean Codex has read it yet. Only reports you explicitly queue request attention; ordinary errors do not restart background arm monitoring.

**Download diagnostics** exports the same JSON bundle for manual sharing. Every error message also has **Copy error**. These actions work without owning the motor session and issue no motor commands. If delivery is not configured, reports remain local. If saving fails, the app says so instead of claiming a report exists.

A bundle contains up to 30 seconds of available telemetry (at most 900 samples), the failing sample before the stop handler, recent operator actions, both calibrations, the fixture/home reference, cached motor setup and health readings, controller limits, and Python/LeRobot versions and source hashes. A session shorter than 30 seconds has less history. Missing or stale voltage/temperature data stays identified as such; recording does not add serial reads. Targets are distinguished as proposed, sent (the write returned), and goal-register readback (when available). A sent target alone does not prove physical motion.

Bundles and report acknowledgments live under `data/hardware/incidents/` (practice uses `data/simulation/incidents/`) and are ignored by Git. They contain local paths, fixture labels, calibration and operator notes. Files remain available across restarts; download them before manually deleting old incidents. Saving uses a bounded memory buffer and a separate writer, so disk persistence does not run on the motor worker. Reporting never changes motion limits, releases torque or restarts the controller. Support/clearance confirmation is still needed before a physical trial or a restart that could affect a powered arm.
