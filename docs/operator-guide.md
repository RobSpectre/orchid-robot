# Operator guide

Run `python app.py` in the prepared Python environment and open http://127.0.0.1:8080. The **SIMULATION · NO HARDWARE** badge means every motion is virtual. Hardware requires launching with `--enable-hardware` and has a distinct badge.

## Preparation

Secure the arm base and synthesizer to reproducible marks. Fit the rubber tip gloves or padded contact surface, inspect the attachment, and select the matching **Contact tool** in the console. Check the gloves cannot slide or bunch up. Keep the opening fixed during teaching and tests. There is no fingertip force sensor: use the minimum effective contact, and watch for compression or key bottoming out.

Support the arm's full weight before any torque release. Six OFF readings mean motor drive is disabled, not that the gearbox is frictionless. Do not overcome significant resistance by forcing a joint. Keep an accessible physical power stop and a safe resting support available.

Safety confirmations are large touchscreen buttons. Tap **Arm this step** (or the named confirmation), check its highlighted **Confirmed** state, then press the action below it. Confirming alone sends no motor command. Tap again to cancel. Each attempt consumes its confirmations, including canceled countdowns and rejected captures; you must confirm again to retry. The optional saved-placement choice remains separate. Recovery has its own **Arm recovery** button.

Click **Refresh connections** to scan USB adapters using the same read-only detection as `find_ports.py`. Only ports with responding Feetech motors appear. Each arm shows its motor IDs and measured bus voltage. The voltage is a snapshot from the first responding motor, timestamped at refresh; leader/follower is inferred using the existing 8 V threshold. Leaders, incomplete motor sets, and arms with unreadable voltage remain visible with an explanation but cannot be selected for connection. Choose the follower with motor IDs **1–6**; `/dev/ttyACM` numbers can change after reconnecting. Refresh again after changing USB or power connections.

Refresh runs only while disconnected and never changes torque or motor settings. Busy/unreadable adapters are skipped with a message; close any other serial controller before retrying. A scan failure clears the old choices. Connection independently reads all six motors' supply and operating mode without configuring the robot or enabling torque. Low supply voltage, missing motors, and the wrong operating mode are rejected. Simulation only displays the practice arm and never scans physical devices.

Choose a placement name. “Keep saved placement” means the same physical setup, not just the same name. Leave it unconfirmed after moving anything, changing pad thickness or glove fit, adjusting the gripper, or repairing a joint. Selecting a different contact tool creates a new fixture even if “Keep saved placement” is confirmed. Previous records remain on disk but no longer count as registered for the new fixture.

## Motor calibration

1. Rest/support the arm clear of Orchid. Confirm support and begin calibration. With the delay enabled, there are five seconds to get both hands onto the arm before release.
2. Center the joints and half-open the gripper. Hold steady during midpoint capture. The app updates and verifies encoder references; it does not move the arm to a midpoint.
3. The highlighted joint is recorded continuously. Gently move it in both directions through its usable range, staying away from strain and cable tension. Other joints may move as needed to support the arm; only the active joint's extremes are saved at this step.
4. Confirm each range in turn: base rotation, shoulder, elbow, wrist bend, gripper. Minimum and maximum tick values update on the page. There must be a meaningful span before continuing. Wrist rotation uses its encoder range without a sweep.
5. Review and save. Torque remains off. Set the final padded gripper opening before teaching notes.

The app backs up the previous motor calibration before homing changes. A normal abort or software fault attempts restoration and verifies it. If serial communication or power is lost, restoration may be unverified. Secure the arm, reconnect, and perform a full calibration before teaching; do not assume either the old or new references are usable.

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

Select a key when the console is ready and choose **Teach** while supporting the arm. The active note is shown above the capture steps.

| Step | Operator action | Result |
| --- | --- | --- |
| Sounding press | Press gently until the selected note sounds. Hold steady, then capture. | Begins recording the release path. Torque stays off. |
| First contact | Slowly lift until the key is fully released and the pad barely touches it. Capture. | Marks the contact boundary. |
| Clearance | Lift a small distance clear along the same path. Confirm support and capture. | Validates the stroke, seeds measured goals, enables torque, and verifies the hold. |
| Test | After the hold is confirmed, gently remove hands and confirm the path is clear. | One slow press and release, followed by a hold at clearance. |
| Review | Listen and watch. Accept only a clean intended note and release. | Saves one successful trial; three are required for registration. |
| Continue | Support the arm and release torque. Move by hand to the next note. | Advances to the next unregistered key. |

Small pauses while deciding what to do next are fine with the page connected. During manual release-path recording, keep movements gentle and local. If a pose is near a limit, a sample jumps, or the grip changes, correct the physical setup and re-teach. Do not increase software limits to get a bad path accepted.

**Reject & re-teach** clears the trial count for that attempt. A control's **REGISTERED** or checkmark state means three accepted trials with the current fixture/calibration. A simulation never counts as hardware registration. There is no autonomous travel between controls in this release.

For both-hand handling, leave the five-second delay enabled. Pressing a delayed control starts a visible countdown; support and position the arm before it reaches zero. Captures also require a brief steady reading. Optional spoken cues can help, but always check the visible status; browser speech can be muted.

## Register the eight chord buttons

Select a button on the left of the instrument map. The upper row is **Dim, Min, Maj, Sus**; the lower row is **6, m7, M7, 9**. Lowercase m7 and uppercase M7 are separate buttons. Use the same light press → first contact → clearance → three tests sequence as the keyboard.

Set up a reference chord and keep Orchid's playstyle consistent. A chord button is not necessarily a standalone note trigger: extensions require a chord, and chord-type behavior depends on playstyle. Review the display and musical response as well as the clean physical release. The app records individual button motions; it does not coordinate simultaneous key/button combinations. **Release & continue** advances within the chord group. After the group is complete, select another group on the map.

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
| Restarted Python | Starts disconnected. Connect and verify the unchanged fixture. A saved note remains a record; incomplete captures and active holds are not resumed. |
| Wrong port, permission denied, or busy port | Disconnect competing tools, check USB and local serial permissions, refresh ports, and select the follower. |
| Motor reference mismatch | Complete calibration before teaching. Never reuse old paths after reseating a joint. |
| Midpoint/range calibration interrupted | Normal cancellation attempts rollback. After loss of power/USB, recalibrate fully and re-teach. |

For normal shutdown, support the arm, open **Release or disconnect**, confirm support, and choose **Disconnect**. Rest the unpowered arm safely. Then close the browser and stop Python with Ctrl+C. Never rely on closing a browser to release torque.

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
