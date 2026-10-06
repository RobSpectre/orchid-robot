# Operator guide

Run `python app.py` in the prepared Python environment and open http://127.0.0.1:8080. A green **SIMULATION · NO HARDWARE** badge means every motion is virtual. Hardware requires launching with `--enable-hardware` and has a distinct badge.

## Preparation

Secure the arm base and synthesizer to reproducible marks. Fit the padded contact surface and inspect the gripper attachment. Keep the opening fixed during a note's teaching and tests. There is no fingertip force sensor: use the minimum press that sounds the note, and watch for pad compression or key bottoming out.

Support the arm's full weight before any torque release. Six OFF readings mean motor drive is disabled, not that the gearbox is frictionless. Do not overcome significant resistance by forcing a joint. Keep an accessible physical power stop and a safe resting support available.

Choose the follower port by device description; `/dev/ttyACM` numbers can change after reconnecting. Connection reads motor state without configuring the robot or enabling torque. Low supply voltage, missing motors, and the wrong operating mode are rejected.

Choose a placement name. “Unchanged” means the same physical setup, not just the same name. Leave it unchecked after moving anything, changing pad thickness, adjusting the gripper, or repairing a joint. Previous records remain on disk but no longer count as registered for the new fixture.

## Motor calibration

1. Rest/support the arm clear of Orchid. Confirm support and begin calibration. With the delay enabled, there are five seconds to get both hands onto the arm before release.
2. Center the joints and half-open the gripper. Hold steady during midpoint capture. The app updates and verifies encoder references; it does not move the arm to a midpoint.
3. The highlighted joint is recorded continuously. Gently move it in both directions through its usable range, staying away from strain and cable tension. Other joints may move as needed to support the arm; only the active joint's extremes are saved at this step.
4. Confirm each range in turn: base rotation, shoulder, elbow, wrist bend, gripper. Minimum and maximum tick values update on the page. There must be a meaningful span before continuing. Wrist rotation uses its encoder range without a sweep.
5. Review and save. Torque remains off. Set the final padded gripper opening before teaching notes.

The app backs up the previous motor calibration before homing changes. A normal abort or software fault attempts restoration and verifies it. If serial communication or power is lost, restoration may be unverified. Secure the arm, reconnect, and perform a full calibration before teaching; do not assume either the old or new references are usable.

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

**Reject & re-teach** clears the trial count for that attempt. The keyboard's green registered state means three accepted trials with the current fixture/calibration. A simulation never counts as hardware registration. There is no autonomous travel from one key to another in this release.

For both-hand handling, leave the five-second delay enabled. Pressing a delayed control starts a visible countdown; support and position the arm before it reaches zero. Captures also require a brief steady reading. Optional spoken cues can help, but always check the visible status; browser speech can be muted.

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
6. Repeat registration for the other 11 notes. Save fixture marks, pad details, date, and the exported session. Re-test after any hardware or placement change.

Encoder position and tracking checks cannot measure force, detect every collision, or guarantee safety after mechanical slippage. Automatic performances, travel between keys, force sensing, and velocity-sensitive playing are future work.
