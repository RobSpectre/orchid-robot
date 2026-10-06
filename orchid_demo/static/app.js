"use strict";
const $ = (id) => document.getElementById(id);
const notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const motors = {shoulder_pan: "Base rotation", shoulder_lift: "Shoulder", elbow_flex: "Elbow", wrist_flex: "Wrist bend", wrist_roll: "Wrist rotation", gripper: "Gripper"};
const owner = crypto.randomUUID();
let state, token, online = false, owns = false, renderKey = "", sending = false, timer = null;
let lastReceipt = "", instance = "", firstLoad = true, keyboardKey = "";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const isSim = () => state?.mode === "simulation";
const activeControl = () => state.catalog[selectedNote || state.selected];
const allStatuses = () => ({...state.keys, ...state.controls});
const statusText = value => value.status === "registered" ? "✓ REGISTERED" : value.status === "needs_reteach" ? "RE-TEACH" : value.trials ? `${value.trials}/3 TRIALS` : "—";
const confirm = (name, text) => `<label class="confirmation"><input type="checkbox" data-confirm="${name}"> ${text}</label>`;
const button = (action, text, secondary = false, attrs = "") => `<button data-action="${action}" class="${secondary ? "secondary" : "primary"}" ${attrs}>${text}</button>`;
const support = () => confirm("supported", "I am supporting the arm’s weight; it is safe to release or hold here.");
const hands = () => confirm("hands_clear", "My hands are clear of the arm and key path.");
const intro = (title, description) => `<h3>${title}</h3><p class="description">${description}</p>`;
const actions = (html) => `<div class="actions">${html}</div>`;
function say(text) {
  if ($("speech").checked && "speechSynthesis" in window) {
    speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }
}
function error(message) { $("error").textContent = message; $("error").hidden = !message; }
async function request(path, body) {
  const options = {cache: "no-store", signal: AbortSignal.timeout(3500)};
  if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Orchid-Token": token, "X-Orchid-Operator": owner}, body: JSON.stringify(body)});
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = await response.text();
    try { message = JSON.parse(message).detail || message; } catch { /* Plain middleware error. */ }
    throw new Error(typeof message === "string" ? message : "The command could not be accepted.");
  }
  return response.json();
}
function renderPorts() {
  const select = $("port"), discovery = state.discovery;
  if (!select || !discovery) return;
  const {ports, scanning, scanned_at: scannedAt, warnings, error: scanError} = discovery;
  const signature = JSON.stringify(discovery);
  if (select.dataset.snapshot === signature) return;
  select.dataset.snapshot = signature;
  const previous = select.value;
  const role = p => p.role === "simulator" ? "Practice arm" : p.role === "follower" ? "Follower" : p.role === "leader" ? "Leader" : "Unidentified arm";
  const volts = p => p.voltage === null ? "Voltage unavailable" : `${p.voltage.toFixed(1)} V`;
  const eligible = ports.filter(p => p.connectable);
  select.innerHTML = (!eligible.length ? `<option value="">${scanning ? "Scanning robot arms…" : scannedAt ? "No ready follower found" : "Refresh to find robot arms"}</option>` : "") +
    ports.map(p => `<option value="${esc(p.path)}" ${p.connectable ? "" : "disabled"}>${esc(role(p))} · ${esc(p.path)} · ${volts(p)} · Motors ${esc(p.motor_ids.join(", "))}</option>`).join("");
  select.value = eligible.some(p => p.path === previous) ? previous : eligible[0]?.path || "";
  $("arm-discovery").innerHTML = ports.map(p => `<article class="discovered-arm ${p.connectable ? "ready-arm" : ""}"><div class="arm-identity"><strong>${esc(role(p))}</strong><span>${esc(p.path)}</span></div><strong class="arm-voltage">${volts(p)}</strong><div class="discovered-motors"><span>Motor IDs</span>${p.motor_ids.map(id => `<b>${esc(id)}</b>`).join("")}</div><p>${isSim() ? "Simulated readings · no USB connection" : esc(p.problem || "Six motors detected · ready to connect")}</p></article>`).join("");
  const status = scanning ? "Scanning USB adapters for Feetech motors…" : scanError ? `Scan failed: ${scanError}` : isSim() ? "Practice arm available. No physical devices are scanned." : scannedAt ?
    `${ports.length} robot arm${ports.length === 1 ? "" : "s"} found · scanned ${new Date(scannedAt * 1000).toLocaleTimeString()}. Voltage is a scan-time reading from each arm’s first responding motor. ${ports.length ? "Leader / follower is inferred from voltage." : "Check USB and arm power, then refresh."}` :
    "Refresh connections to find powered robot arms. Only adapters with responding Feetech motors appear here.";
  $("discovery-status").textContent = status;
  $("discovery-warnings").textContent = warnings.join("\n");
  $("discovery-warnings").hidden = !warnings.length;
}
function noteSteps() {
  const phase = state.phase;
  const n = ["note_ready", "note_pressed", "note_touch"].indexOf(phase);
  const stage = n >= 0 ? n : ["arming", "holding", "testing"].includes(phase) ? 3 : 4;
  return `<div class="stages">${["Press", "Contact", "Clear", "Test", "Review"].map((text,i) => `<span class="${i < stage ? "done" : i === stage ? "current" : ""}">${text}</span>`).join("")}</div>`;
}
function trials() {
  return `<div class="trials">${[1,2,3].map(i => `<span class="${state.trials >= i ? "done" : ""}">${state.trials >= i ? "✓" : "○"} Trial ${i}</span>`).join("")}</div>`;
}
function dialWorkflow(s) {
  const p = s.phase, direction = s.selected_control.label.toLowerCase();
  const messages = {
    dial_ready: ["Begin clear of the rim.", "Support the arm with torque off. Place the fixed pad just clear of the large voicing dial. Describe a repeatable reference chord and the small change this gesture should produce."],
    dial_approach: ["Touch the rim gently.", "Bring the fixed pad into light contact with the rim without rotating or pressing down on the dial. Hold steady for capture."],
    dial_contact: [`Nudge ${direction}.`, "Make one small, deliberate turn. Watch Orchid’s voicing display or listen to the reference chord. Keep the gripper opening fixed; stop if the pad slips."],
    dial_turned: ["Lift completely off the dial.", "Lift clear without dragging the rim or turning it back. Capture the lift-off before moving back toward your starting position."],
    dial_lifted: ["Return through clear space.", "Stay clear of the dial and return close to the starting clearance. The app records this return separately. Keep supporting: the next capture enables a hold."],
    arming: ["Keep supporting the arm.", "Checking a hold at the completed clear return. Wait for confirmation before removing your hands."],
    holding: ["Test the complete dial gesture.", "One approach, a small turn in the taught direction, lift-off, and a return through clear space. Restore the instrument reference while the pad is clear, then take your hands away."],
    testing: ["Turn, lift, return clear.", "Keep hands clear. The arm follows the recorded loop once, including the captured lift-off and clear return. Watch for slipping or unintended contact."],
    result: ["Did the voicing change as expected?", isSim() ? "The simulated gesture finished. Confirm the requested direction and practice reviewing its effect; no physical dial has been verified." : "Accept only the intended direction and described change, with no slipping, downward press, or reverse turn during lift-off and return."],
    saved: [s.trials >= 3 ? "Direction registered." : "Trial accepted. Restore the reference.", s.trials >= 3 ? "Three accepted trials for this direction. Support and release the arm before teaching the other direction or selecting another control." : "The dial’s state has changed. Restore the same reference chord and voicing while the pad is clear before repeating this gesture."]
  };
  const text = messages[p] || ["Waiting for the arm…", s.message];
  const phases = ["dial_ready", "dial_approach", "dial_contact", "dial_turned", "dial_lifted"];
  const stage = phases.includes(p) ? phases.indexOf(p) : 5;
  let html = `<div class="note-title"><span class="note-symbol dial-symbol">${s.selected_control.direction === "cw" ? "↻" : "↺"}</span><div><div class="eyebrow">VOICING / ${esc(direction.toUpperCase())}</div><h3>${text[0]}</h3></div></div>` +
    `<div class="stages">${["Start", "Contact", "Turn", "Lift off", "Return", "Test"].map((label,i) => `<span class="${i < stage ? "done" : i === stage ? "current" : ""}">${label}</span>`).join("")}</div><p class="description">${text[1]}</p>`;
  if (p === "dial_ready") html +=
    '<label class="form-field">Reference chord & starting voicing<input id="dial-reference" maxlength="160" placeholder="C major · Geek Out view · C–E–G"></label>' +
    '<label class="form-field">Expected change for this nudge<input id="dial-effect" maxlength="160" placeholder="Describe the small change you can repeat and observe"></label>' +
    confirm("fixed_pad", "The rubber-covered or padded tips stay fixed and nudge the rim; they do not grip, squeeze, or press the dial.") + actions(button("dial_capture_start", "Capture start clearance →"));
  if (p === "dial_approach") html += actions(button("dial_capture_contact", "Capture rim contact →"));
  if (p === "dial_contact") html += confirm("direction_verified", `The dial turned ${esc(direction)} and produced the intended change.`) + actions(button("dial_capture_turn", "Capture turned position →"));
  if (p === "dial_turned") html += confirm("rim_clear", "The pad is completely clear of the dial rim.") + actions(button("dial_capture_lift", "Capture lift-off →"));
  if (p === "dial_lifted") html += '<div class="return-meter">Return error <strong id="dial-return-error">—</strong><span>target ≤ 6 ticks · same clear start area</span></div>' + confirm("rim_clear", "The entire return path stayed clear of the dial.") + support() + actions(button("dial_capture_return", "Capture return & hold →"));
  const reference = () => `<div class="reference-card"><span>REFERENCE</span><p>${esc(s.dial_reference)}</p><span>EXPECTED EFFECT</span><p>${esc(s.dial_expected_effect)}</p></div>`;
  const reset = () => confirm("reference_reset", "I restored the reference chord and starting voicing while the pad was clear.");
  if (["holding", "saved", "result"].includes(p)) html += reference();
  if (p === "holding") html += reset() + hands() + actions(button("test", "Test dial gesture →"));
  if (p === "result") html += trials() + confirm("effect_verified", "Requested direction and expected effect, with no slip or reverse turn on the clear return.") + actions(button("pass", isSim() ? "Accept simulated trial ✓" : "Accept dial trial ✓") + button("fail", "Reject & re-teach", true));
  if (p === "saved") html += trials() + (s.trials >= 3 ? support() + actions(button("next", "Release & continue →")) : reset() + hands() + actions(button("test", "Test again →")));
  html += '<p class="hint">This is a relative gesture, not an absolute dial setting. The musical change depends on the chord. Clockwise and counterclockwise are taught separately.</p>';
  return html;
}
function workflow() {
  const s = state, sim = isSim(), p = s.phase;
  const chosen = activeControl();
  const chord = s.selected_control.kind === "button";
  const complete = Object.values(allStatuses()).every(k => k.status === "registered");
  let html = "";
  if (p === "disconnected") {
    html = intro(sim ? "A rehearsal, without the robot." : "Set up the follower arm.", sim ? "Walk through calibration, keys, chord buttons, and voicing gestures with a simulated arm. Practice data stays separate from the real instrument." : "Secure the arm and Orchid to their marked positions. Fit the soft pad, fix the gripper opening, and rest the arm safely clear of the keyboard.") +
      `<div class="field-row connection-picker"><label class="form-field">Follower connection<select id="port" aria-label="Follower connection" aria-describedby="discovery-status"></select></label><button class="secondary" id="refresh-ports" data-action="refresh_ports">↻ Refresh connections</button></div>` +
      '<div id="arm-discovery" class="arm-discovery" aria-label="Detected robot arms"></div><p id="discovery-status" class="hint" role="status"></p><p id="discovery-warnings" class="discovery-warnings" hidden></p>' +
      `<label class="form-field">Fixture / placement name<input id="fixture" maxlength="120" value="${esc(s.fixture.label)}" placeholder="Orchid demo · table A"></label>` +
      `<label class="form-field">Contact tool<select id="contact-tool"><option value="rubber_gloved_tips" ${s.fixture.tool !== "padded_gripper" ? "selected" : ""}>Rubber-covered gripper tips</option><option value="padded_gripper" ${s.fixture.tool === "padded_gripper" ? "selected" : ""}>Padded gripper</option></select></label>` +
      (s.fixture.id ? confirm("fixture_unchanged", "Arm, keyboard, contact tips, and gripper opening have not changed since this saved fixture.") : "") +
      confirm("prepared", sim ? "I understand this is a simulation; no physical notes are verified." : "Mounting and pad are secure, the workspace is clear, and I can reach the power stop.") +
      actions(button("connect", sim ? "Connect practice arm →" : "Connect follower →")) +
      '<p class="hint">Changed placement or pad? Leave “unchanged” unchecked. Saved notes will require teaching again.</p>';
  } else if (["connected", "ready"].includes(p)) {
    html = intro(complete ? "The instrument is registered." : s.calibrated ? `Ready to teach ${esc(chosen.name)}.` : "Give the arm its reference points.", complete ? "All 22 motions have three accepted trials. Rest the arm safely and export your session. Changing the fixture or pad requires re-teaching." : s.calibrated ? (chosen.kind === "dial" ? "Teach a small turn of the large voicing dial, then lift off and return clear. Each direction has its own path and verification trials." : chosen.kind === "button" ? "Teach this chord button’s lightest reliable press and release. Its musical effect depends on Orchid’s playstyle and reference chord; the button may not sound alone." : "Choose any key or control above. Teach its local motion by hand, then test it three times.") : "With torque off, capture a supported midpoint, then measure each joint’s usable range. Keep the arm clear of Orchid for the whole calibration.") +
      support() + actions((s.calibrated ? button("control_start", `Teach ${esc(chosen.name)} →`, false, `data-control="${esc(chosen.id)}"`) : "") + button("calibrate", s.calibrated ? "Recalibrate motors" : "Begin motor calibration →", s.calibrated));
    if (complete) html += '<p class="hint"><a href="/api/export" download>Download the session record →</a></p>';
    html += '<p class="hint">Support the full weight before releasing torque. Gear resistance can remain with all six motors OFF.</p>';
  } else if (p === "calibration_midpoint") {
    html = intro("Center the six joints.", "Keep the arm supported and clear of the instrument. Place each joint near the middle of its mechanical travel; half-open the gripper. Do not force a joint against a stop.") + support() + actions(button("calibration_center", "Capture midpoint →")) + '<p class="hint">The app checks a steady pose and reads back the new motor references. It does not drive to the midpoint.</p>';
  } else if (p === "calibration_range") {
    const instruction = window.OrchidPanels.guidance[s.range_motor];
    html = intro(`${s.range_index + 1} of 5 · ${esc(motors[s.range_motor])}`, instruction[1]) +
      `<div class="joint-instruction"><span>MOVE BY HAND · TORQUE OFF</span><strong>${instruction[0]}</strong><p>The highlighted joint in the 3D view is this step’s joint. Other joints may move as needed to support the arm.</p></div>` +
      '<div class="range-panel"><div class="range-values"><span>Minimum <b id="range-min">—</b></span><span>Current <b id="range-current">—</b></span><span>Maximum <b id="range-max">—</b></span></div><progress id="range-progress" max="4095" value="0" aria-label="Recorded joint travel"></progress><p id="range-span" class="range-span">Waiting for movement</p><p id="range-readiness" class="hint">Move gently in both directions.</p></div>' +
      (sim ? actions(button("simulate_sweep", "Simulate joint sweep", true)) : "") + confirm("range_complete", "Both ends of the usable travel are recorded.") + actions(button("calibration_next", s.range_index === 4 ? "Review calibration →" : "Save range & continue →"));
  } else if (p === "calibration_review") {
    html = intro("Review the measured travel.", "Confirm these ranges reflect deliberate sweeps. Wrist rotation uses the full encoder range; there is no cable-twisting sweep.") +
      `<table><thead><tr><th>JOINT</th><th>MIN</th><th>MAX</th></tr></thead><tbody>${Object.entries(motors).map(([name,label]) => `<tr><td>${label}</td><td>${name === "wrist_roll" ? 0 : s.ranges[name]?.min}</td><td>${name === "wrist_roll" ? 4095 : s.ranges[name]?.max}</td></tr>`).join("")}</tbody></table>` + confirm("range_complete", "These measured ranges cover the intended usable travel.") + actions(button("calibration_save", "Save & verify calibration →")) + '<p class="hint">Existing note paths are tied to their original calibration. Changed calibration makes them require re-teaching.</p>';
  } else if (p === "fault" || p === "failed") {
    html = intro(p === "failed" ? "Let’s teach that stroke again." : "The session has stopped.", p === "failed" ? "This attempt is not registered. Support the arm before releasing torque, then capture a gentler, shorter stroke." : "No new motion is being issued. Support the arm and inspect the reported cause before continuing. Motor state is unverified until feedback resumes.") + support() + actions(button(s.calibrated ? "retry" : "release", s.calibrated ? "Release & retry this control" : "Release torque & return to setup")) + '<p class="hint">If the motors are not responding, use the physical power stop while supporting the arm.</p>';
  } else if (s.selected_control.kind === "dial") {
    html = dialWorkflow(s);
  } else {
    const text = {
      note_ready: [chord ? "Find the lightest button press." : "Find the lightest sounding press.", chord ? `Gently actuate ${esc(s.selected_control.label)} with the fixed pad. Prepare a reference chord/playstyle and observe Orchid’s response. Extensions need a chord; they do not sound alone.` : `Support the arm’s weight. Gently press ${esc(s.selected)} with the pad, only until the note sounds. Keep the gripper opening fixed and hold still.`],
      note_pressed: ["Lift to first contact.", `Slowly release the ${chord ? "button" : "key"} until it is fully up, with the pad barely touching it. The app records your path continuously. Hold still for capture.`],
      note_touch: ["Lift to a small clearance.", `Lift the pad just clear of the ${chord ? "button" : "key"} along the same short path. Keep supporting the arm: this capture enables torque and holds this position.`],
      arming: ["Keep supporting the arm.", "Establishing and checking a hold at the captured clearance. Wait for confirmation before removing your hands."],
      holding: ["Now test one press.", "The arm is holding the captured clearance. Gently take your hands away. This test makes one slow press and release, then holds at clearance."],
      testing: ["One press. One release.", "Keep hands clear. The arm will return along the captured path and hold at clearance. Watch the selected control and verify a clean release."],
      result: [chord ? "Did the chord control respond?" : "Did the intended note sound?", chord ? "Accept only if the intended chord button activated and released cleanly, with the expected display or musical response. Keep the reference chord and playstyle consistent." : sim ? "The simulated press and release finished. Practice accepting or rejecting a trial. This does not verify a physical key." : "Accept only if the intended key sounded once, released cleanly, and the pad and joints stayed steady. Reject excess pressure, a miss, or contact with another key."],
      saved: [s.trials >= 3 ? (chord ? "Button registered." : "Note registered.") : "Trial accepted. Test it again.", s.trials >= 3 ? "Three trials accepted from this position. Support the arm before releasing torque and moving by hand to the next control." : "The arm is still holding at clearance. Repeat the test from here; there is no need to capture the same path again."]
    }[p] || ["Waiting for the arm…", s.message];
    html = `<div class="note-title"><span class="note-symbol">${esc(s.selected_control.label)}</span><h3>${text[0]}</h3></div>${noteSteps()}<p class="description">${text[1]}</p>`;
    if (p === "note_ready") html += actions(button("capture_pressed", chord ? "Capture button press →" : "Capture sounding press →"));
    if (p === "note_pressed") html += actions(button("capture_touch", "Capture first contact →"));
    if (p === "note_touch") html += support() + actions(button("capture_clear", "Capture clearance & hold →"));
    if (p === "holding") html += hands() + actions(button("test", "Test one press →"));
    if (p === "result") html += trials() + actions(button("pass", sim ? "Accept simulated trial ✓" : chord ? "Activated & released cleanly ✓" : "Sounded & released cleanly ✓") + button("fail", "Reject & re-teach", true));
    if (p === "saved") html += trials() + (s.trials >= 3 ? support() + actions(button("next", "Release & continue →")) : hands() + actions(button("test", "Test again →")));
    if (p.startsWith("note_")) html += `<p class="hint">${sim ? "Simulation places the virtual arm at each capture." : "Move gently. A large jump, changed grip, or stale reading stops capture."}</p>`;
  }
  if (p.startsWith("calibration_") || (p === "connected" && !s.calibrated)) html = window.OrchidPanels.calibration(s) + html;
  if (p === "connected" && !s.calibrated) html += '<div class="setup-checklist"><strong>Before starting</strong><ul><li>Secure the base and reseated joints; keep the instrument outside the arm’s reach.</li><li>Support the full arm weight before torque releases.</li><li>Use the delay and optional spoken cues to keep both hands available.</li></ul><p>Calibration stays torque off. OFF flags do not remove gearbox drag.</p></div>';
  $("workflow").innerHTML = html;
}
function render() {
  if (!state) return;
  const s = state, p = s.phase;
  const key = [s.instance_id, p, s.range_index, s.trials, s.selected, selectedNote, s.calibrated].join(":");
  if (key !== renderKey) {
    cancelCountdown(); renderKey = key; workflow();
    say(["dial_ready", "dial_approach", "dial_contact", "dial_turned", "dial_lifted", "note_ready", "note_pressed", "note_touch", "holding", "result", "saved", "fault"].includes(p) ? s.message : "");
  }
  renderPorts();
  const count = Object.values(allStatuses()).filter(k => k.status === "registered").length;
  $("completed").innerHTML = `${count}<span>/22</span>`;
  $("mode").textContent = isSim() ? "SIMULATION · NO HARDWARE" : "HARDWARE MODE";
  $("mode").className = `badge ${isSim() ? "" : "hardware"}`;
  $("connection").textContent = online ? (owns ? (s.connected ? "Follower connected" : "Local app ready") : "Read-only window") : "Local app unavailable";
  $("connection-warning").hidden = online && owns;
  $("connection-warning").textContent = !online ? "Connection to the local Python app was lost. Controls are disabled. If a physical arm is active, support it and use the power stop if needed." : "Another browser owns operator control. This window shows status only.";
  const section = p === "disconnected" ? "connect" : p.startsWith("calibration") || (!s.calibrated && ["connected", "fault"].includes(p)) ? "calibration" : "notes";
  document.body.classList.toggle("calibrating", section === "calibration");
  document.querySelector(".page-heading h1").textContent = section === "calibration" ? "Give the arm its bearings." : "Teach the instrument.";
  document.querySelector(".page-heading p").textContent = section === "calibration" ? "Guided calibration. Six motors. One joint at a time." : "Twelve keys. Eight chord buttons. Two voicing gestures.";
  for (const name of ["connect", "calibration", "notes"]) {
    $("nav-" + name).classList.toggle("active", name === section);
    $("nav-" + name).querySelector("b").textContent = name === "connect" && s.connected || name === "calibration" && s.calibrated || name === "notes" && count === 22 ? "✓" : "";
  }
  $("step-label").textContent = section === "connect" ? "01 / CONNECTION" : section === "calibration" ? "02 / MOTOR CALIBRATION" : "03 / CONTROL TRAINING";
  $("phase-badge").textContent = p === "fault" ? "STOPPED" : s.pending ? "IN PROGRESS" : p.replaceAll("_", " ").toUpperCase();
  const nextKeyboardKey = JSON.stringify([s.keys, s.controls, s.selected, selectedNote, p, s.calibrated]);
  if (nextKeyboardKey !== keyboardKey) {
    keyboardKey = nextKeyboardKey;
  $("keyboard").innerHTML = notes.map(n => `<button class="key ${n.includes("#") ? "sharp" : ""} ${s.keys[n].status} ${s.selected === n ? "selected" : ""}" data-note="${esc(n)}" aria-label="${esc(n)}: ${s.keys[n].status.replaceAll("_"," ")}, ${s.keys[n].trials} of 3 trials" ${!["ready","connected"].includes(p) || !s.calibrated ? "disabled" : ""}><strong>${esc(n)}</strong><small>${s.keys[n].status === "registered" ? "✓ REGISTERED" : s.keys[n].status === "needs_reteach" ? "RE-TEACH" : s.keys[n].trials ? `${s.keys[n].trials}/3 TRIALS` : "—"}</small></button>`).join("");
    const extraButton = c => {
      const value = s.controls[c.id];
      return `<button data-note="${esc(c.id)}" class="${c.kind === "dial" ? "direction-button" : "chord-button"} ${value.status} ${(selectedNote || s.selected) === c.id ? "selected" : ""}" aria-label="${esc(c.name)}: ${value.status.replaceAll("_", " ")}, ${value.trials} of 3 trials"><strong>${c.kind === "dial" ? (c.direction === "cw" ? "↻ CW" : "↺ CCW") : esc(c.label)}</strong><small>${statusText(value)}</small></button>`;
    };
    $("chord-buttons").innerHTML = Object.values(s.catalog).filter(c => c.group === "chords").map(extraButton).join("");
    $("dial-buttons").innerHTML = Object.values(s.catalog).filter(c => c.group === "voicing").map(extraButton).join("");
    for (const [group, id, total] of [["keys", "key-count", 12], ["chords", "chord-count", 8], ["voicing", "dial-count", 2]]) {
      const registered = Object.values(s.catalog).filter(c => c.group === group && allStatuses()[c.id].status === "registered").length;
      $(id).textContent = `${registered} / ${total}`;
    }
  }
  // Clicking a key selects a label only; torque release always needs the supported action.
  $("keyboard-hint").textContent = ["ready", "connected"].includes(p) && s.calibrated ? "Select a key, chord button, or dial direction. Support the arm, then choose Teach below." : "12 keys · 8 chord buttons · 2 voicing directions. Each motion is taught and verified separately.";
  window.OrchidPanels.update(s, online);
  const fresh = online && s.connected && s.feedback_at && Date.now()/1000 - s.feedback_at < 1.5 && p !== "fault";
  const flags = fresh ? Object.values(s.torque || {}) : [];
  const off = flags.length === 6 && flags.every(v => v === 0), on = flags.length === 6 && flags.every(v => v === 1);
  $("torque-summary").textContent = off ? "6 / 6 TORQUE OFF" : on ? (p === "testing" ? "TORQUE ON · MOVING" : "TORQUE ON · HOLD") : "UNVERIFIED";
  $("torque-summary").className = `badge ${on ? "powered" : "neutral"}`;
  $("feedback-age").textContent = s.feedback_at ? `Last read ${Math.max(0, (Date.now()/1000 - s.feedback_at)).toFixed(1)}s ago · raw ticks` : "No current motor feedback";
  $("voltage").textContent = s.voltage ? `${s.voltage.toFixed(1)} V` : "—";
  $("feedback").textContent = fresh ? (isSim() ? "Simulated" : "Live") : "Unavailable";
  $("recovery").hidden = !s.connected;
  if ($("range-min")) {
    const range = s.ranges[s.range_motor];
    $("range-min").textContent = range?.min ?? "—"; $("range-max").textContent = range?.max ?? "—";
    $("range-progress").value = range ? range.max - range.min : 0;
  }
  if ($("dial-return-error")) $("dial-return-error").textContent = `${s.dial_return_error ?? "—"} ticks`;
  $("events").innerHTML = s.events.length ? s.events.slice(0,12).map(e => `<li><time datetime="${esc(e.created)}">${esc(new Date(e.created).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}))}</time><span>${esc(e.message)}</span></li>`).join("") : '<li><span>No session activity yet. Connect the practice arm to begin.</span></li>';
  $("operation").hidden = !s.pending; $("operation").textContent = s.discovery?.scanning ? "Reading connected robot arms…" : s.message;
  if (s.last_receipt?.id !== lastReceipt || s.error) {
    lastReceipt = s.last_receipt?.id;
    error(s.error || (s.last_receipt?.status === "rejected" ? s.last_receipt.message : ""));
  }
  updateButtons();
}
let selectedNote = null;
function updateButtons() {
  const blocked = !online || !owns || state?.worker_alive === false || sending || state?.pending || !!timer;
  document.querySelectorAll("[data-action]").forEach(b => {
    const scope = b.dataset.recovery ? $("recovery") : $("workflow");
    let valid = [...scope.querySelectorAll("[data-confirm]")].filter(c => c.dataset.confirm !== "fixture_unchanged").every(c => c.checked);
    if (b.dataset.recovery) valid = $("recovery-supported").checked;
    if (["simulate_sweep", "fail", "refresh_ports"].includes(b.dataset.action)) valid = true;
    if (b.dataset.action === "connect") valid = valid && state.discovery?.ports.some(p => p.path === $("port")?.value && p.connectable);
    if (b.dataset.diagnostics) valid = state.connected && ["connected", "ready"].includes(state.phase) && Object.values(state.torque || {}).length === 6 && Object.values(state.torque).every(v => v === 0);
    if (b.dataset.action === "calibration_next") { const range = state.ranges[state.range_motor]; valid = valid && range && range.max - range.min > 32; }
    b.disabled = blocked || !valid;
  });
  if ($("port")) $("port").disabled = blocked || !state.discovery?.ports.some(p => p.connectable);
  if ($("refresh-ports")) $("refresh-ports").textContent = state.discovery?.scanning ? "Scanning…" : "↻ Refresh connections";
  document.querySelectorAll("[data-note]").forEach(b => b.disabled = blocked || !state.calibrated || !["ready", "connected"].includes(state.phase));
  $("stop").disabled = !online || !owns || !state?.connected;
  document.querySelectorAll("[data-note]").forEach(k => k.classList.toggle("selected", k.dataset.note === (selectedNote || state.selected)));
}
function cancelCountdown() { if (timer) clearInterval(timer); timer = null; $("countdown").hidden = true; }
async function submit(action, args = {}, revision = state.revision) {
  sending = true; error(""); updateButtons();
  try { await request("/api/commands", {id:crypto.randomUUID(), action, args, revision}); selectedNote = null; }
  catch (err) { error(err.message); }
  finally { sending = false; updateButtons(); }
}
function dispatchButton(b) {
  const action = b.dataset.action;
  const args = {};
  const scope = b.dataset.recovery ? $("recovery") : $("workflow");
  if (!b.dataset.diagnostics) scope.querySelectorAll("[data-confirm]").forEach(c => args[c.dataset.confirm] = c.checked);
  if (b.dataset.recovery) args.supported = $("recovery-supported").checked;
  if (action === "connect") { args.fixture = $("fixture").value; args.port = $("port").value; args.tool = $("contact-tool").value; }
  if (action === "control_start") args.control = b.dataset.control;
  if (action === "dial_capture_start") { args.reference = $("dial-reference").value; args.expected_effect = $("dial-effect").value; }
  const revision = state.revision;
  const delayed = ["calibrate","calibration_center","control_start","dial_capture_start","dial_capture_contact","dial_capture_turn","dial_capture_lift","dial_capture_return","capture_pressed","capture_touch","capture_clear","next","release","disconnect","retry"].includes(action);
  if ($("delay").checked && delayed) {
    let left = 5;
    const title = b.textContent;
    $("countdown").hidden = false;
    const draw = () => { $("countdown").innerHTML = `<div><strong>${left}</strong> seconds<br><small>${esc(title)} · hold steady</small></div><button id="cancel-countdown" class="secondary">Cancel</button>`; };
    draw(); say(`${title} in five seconds. Support the arm.`);
    timer = setInterval(() => {
      if (!online || !owns || state.revision !== revision) { cancelCountdown(); updateButtons(); return; }
      left -= 1;
      if (left === 0) { cancelCountdown(); submit(action,args,revision); }
      else draw();
    },1000);
    updateButtons();
  } else submit(action,args,revision);
}
document.addEventListener("click", event => {
  const b = event.target.closest("button"); if (!b || b.disabled) return;
  if (b.id === "cancel-countdown") { cancelCountdown(); updateButtons(); }
  else if (b.id === "stop") { cancelCountdown(); say("Stop requested"); submit("stop"); }
  else if (b.dataset.action) dispatchButton(b);
  else if (b.dataset.note) { selectedNote = b.dataset.note; render(); }
});
document.addEventListener("change", updateButtons);
document.addEventListener("keydown", event => { if (event.key === "Escape") { cancelCountdown(); if (!$("stop").disabled) $("stop").click(); updateButtons(); } });
async function poll() {
  try {
    const session = await request("/api/session");
    if (instance && instance !== session.state.instance_id) { cancelCountdown(); selectedNote = null; }
    instance = session.state.instance_id; token = session.token; state = session.state; online = state.worker_alive !== false;
    try { await request("/api/heartbeat", {}); owns = true; }
    catch { owns = false; cancelCountdown(); }
    if (firstLoad) { $("delay").checked = !isSim(); firstLoad = false; }
    render();
  } catch {
    online = false; owns = false; cancelCountdown();
    if (state) render(); else { $("connection-warning").hidden = false; $("connection-warning").textContent = "Cannot reach the local Python app. Start python app.py, then keep this page open to reconnect."; }
  } finally { setTimeout(poll, 500); }
}
poll();
