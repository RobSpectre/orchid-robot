# Orchid robot instrument

A Python app with a local web interface for calibrating an SO101 follower arm and registering Orchid's **12 notes, eight chord buttons, and two voicing-dial directions**. Run the source with `python app.py`; there is no application installer, frontend build, or cloud service.

The console mirrors the instrument's charcoal surface, gold case, chord bank, voicing dial, and single-octave keyboard. It walks an operator through connection, motor calibration, teaching a local motion, and three observed trials. Simulation works without LeRobot, a serial connection, or the instrument.

**Status:** the complete workflow is covered by simulated/fake motor tests and browser checks. The new web hardware adapter still needs supervised commissioning on the repaired arm. This is position control without contact-force sensing or collision detection; registration does not establish that arbitrary motion between notes is safe.

![Console during a simulated registration](docs/console-preview.jpg)

## Run locally

Python **3.12** on Linux is the reference environment. From the repository:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints-web.txt
python app.py
```

Open **http://127.0.0.1:8080** on the same computer. Installing requirements installs Python dependencies, not this project as an application. Subsequent runs only need environment activation and `python app.py`.

The default is **simulation**. All controls—including calibration sweeps, capture, test, reject, and recovery—are usable with the virtual arm. Simulated trials are labeled and stored separately from physical trials.

Options:

```bash
python app.py --port 8081
python app.py --data-dir /path/to/local/session-data
```

The server binds only to `127.0.0.1`. Keep one operator tab open. A second tab can view state but cannot take control while the first tab's lease is active. Do not expose this service with a reverse proxy or remote tunnel.

## Operator workflow

1. **Connect.** Check mounting, padding, and access to the power stop. Name the fixture and select rubber-covered tips or a padded gripper. Only mark the placement unchanged if the base, instrument, contact surface, and gripper opening really are unchanged.
2. **Calibrate.** Support the arm while torque releases. Capture the midpoint, then record the usable ranges of base, shoulder, elbow, wrist bend, and gripper. Wrist rotation does not require a cable-twisting sweep. Review and save; hardware writes are read back for verification.
3. **Teach a note.** With torque off, manually find the lightest sounding press. Capture it, gently lift to first contact, then lift to a small visible clearance. The app records this short release path and reverses it for the press. Keep the gripper opening fixed.
4. **Establish a hold.** The clearance capture explicitly enables torque at the current supported position. Wait for **Holding position**, then clear your hands.
5. **Test and review.** Run one press, listen, and accept or reject it. Repeat the test three times from the held clearance; no repeated teaching is needed between trials.
6. **Next note.** Support the arm and choose **Release & continue**. Move to the next note by hand. Continue through C, C#, D, D#, E, F, F#, G, G#, A, A#, B.

Select the **chord bank** to teach Dim, Min, Maj, Sus, 6, m7, M7, and 9 with the same press/contact/clearance workflow. Review the intended button response with a consistent reference chord and playstyle; extensions do not sound alone.

Select **CW or CCW** beside the large voicing dial to teach a separate forward loop: start clear → light rim contact → small turn → lift off → return through clear space. Rubber-covered tips keep a fixed opening and nudge the rim. The app does not close the gripper around the dial. Describe the starting reference and expected change, restore that reference before each test, and accept three observed trials per direction. These are relative gestures, not absolute dial positions. Together the instrument has **22 independently registered motions**.

Enable **5-second delay** to free both hands before capture or torque release. It defaults on in hardware mode. Spoken cues are optional and depend on browser speech support. There is no microphone requirement. Cancel the countdown before it finishes to take no action; Escape also requests a stop when connected.

Detailed instructions: [Operator guide](docs/operator-guide.md).

## When the physical arm returns

Use the repaired SO101 follower with securely fitted rubber-covered tips or a padded gripper, Linux serial permissions, Python 3.12, and the pinned LeRobot interface:

```bash
python -m pip install -r requirements-hardware.txt
python app.py --enable-hardware
```

Opening the page does not connect or enable motors. Choose the follower's current port in the browser; USB names can change. Connection checks all six motor IDs, follower supply voltage, position mode, and saved calibration agreement. The leader is not needed for this workflow.

Finish the [hardware commissioning checklist](docs/operator-guide.md#commissioning-the-repaired-arm) before a public demo. Previous terminal-taught files are preserved locally but are **not automatically imported or trusted** by the web app. A joint repair requires fresh calibration and note teaching.

## Stops, persistence, and restart

- **Stop motion** requests a hold at a fresh measured position. It never blindly retracts or switches torque off. If feedback is unavailable, the last motor target can remain active. Support the arm and use the physical power stop if needed.
- A lost operator heartbeat stops powered motion after at most five seconds plus current I/O; feedback/trajectory checks also reject stale control intervals. This is not a safety-rated emergency stop.
- Support the arm, use **Release or disconnect → Disconnect**, and rest it before stopping Python. Closing the browser or Python does not intentionally drop torque.
- State lives in `data/simulation/` or `data/hardware/`: SQLite database, calibration backup, control revisions, and trial logs. Those directories are ignored by Git. New processes always start disconnected; they never resume a movement.
- Use **Export session** for a readable JSON snapshot of calibration, 12 note records, ten additional control records, and recent activity. To make a complete backup, disconnect, stop the app, and copy the entire mode directory. JSON export is a record, not an automatic restore/import feature.
- Existing version-1 databases upgrade transactionally to version 2, preserving notes and history while adding chord/dial records. Keep a full backup before upgrading if you need to run the old app again; it cannot open version 2.
- Changing the fixture, contact tool, or calibration invalidates prior registrations. A changed glove fit or gripper opening also requires a new fixture and re-teaching. Interrupted teaching restarts from a supported torque-off pose; the app does not drive back to an old starting point.

## Development

```bash
python -m pip install -r requirements-dev.txt -c constraints-web.txt
python -m pytest -q
python -m ruff check .
```

For the tested web dependency snapshot, add `-c constraints-web.txt` when installing the web or development requirements. Hardware dependencies have their own requirement file; do not apply the web-only snapshot to LeRobot's separate dependency stack.

See [architecture](docs/architecture.md) and [contributing](CONTRIBUTING.md). CI runs lint and the hardware-free suite on Python 3.12.

```text
app.py                    Python entry point
orchid_demo/
  api.py                  Local HTTP interface and ownership
  engine.py               Calibration/registration state machine
  devices.py              Simulated and physical motor adapters
  motion.py               Bounded stroke controller and validation
  controls.py             Keyboard, chord, and dial motion catalog
  dial.py                 Bounded forward dial loop and validation
  storage.py              Durable local state
  static/                 HTML, CSS, browser JavaScript (no build)
tests/                    Motion, workflow, API, and adapter tests
docs/                     Operator and developer documentation
```

The original replay and terminal utilities remain for development/history: [legacy replay guide](docs/legacy-replay.md), [first-key notes](ORCHID_FIRST_KEY.md), `replay_lw.py`, `find_ports.py`, `orchid_key.py`, `orchid_session.py`, and `orchid_calibrate.py`. Do not run a legacy controller concurrently with the web app. `repair_leader_voltage.py` is a one-off diagnostic/repair utility, not part of normal operation.
