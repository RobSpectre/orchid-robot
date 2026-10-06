"use strict";
const $ = (id) => document.getElementById(id);
const notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const motors = {shoulder_pan: "Base rotation", shoulder_lift: "Shoulder", elbow_flex: "Elbow", wrist_flex: "Wrist bend", wrist_roll: "Wrist rotation", gripper: "Gripper"};
const owner = crypto.randomUUID();
let state, token, online = false, owns = false, renderKey = "", sending = false, timer = null, ports = [];
let portError = "", lastReceipt = "", instance = "", firstLoad = true, keyboardKey = "";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const isSim = () => state?.mode === "simulation";
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
async function getPorts() {
  try { ports = (await request("/api/ports")).ports; portError = ""; }
  catch (err) { ports = []; portError = err.message; }
  const select = $("port");
  if (select) {
    const previous = select.value;
    select.innerHTML = ports.length ? ports.map(p => `<option value="${esc(p.path)}">${esc(p.path)} · ${esc(p.description)}</option>`).join("") : '<option value="">No follower ports found</option>';
    if (ports.some(p => p.path === previous)) select.value = previous;
  }
  if (portError) error(portError);
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
function workflow() {
  const s = state, sim = isSim(), p = s.phase;
  const complete = Object.values(s.keys).every(k => k.status === "registered");
  let html = "";
  if (p === "disconnected") {
    html = intro(sim ? "A rehearsal, without the robot." : "Set up the follower arm.", sim ? "Walk through motor calibration and all twelve notes with a simulated arm. Practice data stays separate from the real instrument." : "Secure the arm and Orchid to their marked positions. Fit the soft pad, fix the gripper opening, and rest the arm safely clear of the keyboard.") +
      `<div class="field-row"><label class="form-field">Follower connection<select id="port" aria-label="Follower connection"></select></label><button class="secondary" id="refresh-ports" aria-label="Refresh ports">↻</button></div>` +
      `<label class="form-field">Fixture / placement name<input id="fixture" maxlength="120" value="${esc(s.fixture.label)}" placeholder="Orchid demo · table A"></label>` +
      (s.fixture.id ? confirm("fixture_unchanged", "Arm, keyboard, pad, and gripper opening have not changed since this saved fixture.") : "") +
      confirm("prepared", sim ? "I understand this is a simulation; no physical notes are verified." : "Mounting and pad are secure, the workspace is clear, and I can reach the power stop.") +
      actions(button("connect", sim ? "Connect practice arm →" : "Connect follower →")) +
      '<p class="hint">Changed placement or pad? Leave “unchanged” unchecked. Saved notes will require teaching again.</p>';
  } else if (["connected", "ready"].includes(p)) {
    html = intro(complete ? "Your octave is registered." : s.calibrated ? "Ready to teach the first stroke." : "Give the arm its reference points.", complete ? "All twelve notes have three accepted trials. Rest the arm safely and export your session. Moving the fixture or changing the pad requires re-teaching." : s.calibrated ? "Choose a note on the keyboard above. You’ll teach a short release path by hand, then test the reverse path three times." : "With torque off, capture a supported midpoint, then measure each joint’s usable range. Keep the arm clear of Orchid for the whole calibration.") +
      support() + actions((s.calibrated ? button("note_start", `Teach ${esc(s.selected)} →`, false, `data-key="${esc(s.selected)}"`) : "") + button("calibrate", s.calibrated ? "Recalibrate motors" : "Begin motor calibration →", s.calibrated));
    if (complete) html += '<p class="hint"><a href="/api/export" download>Download the session record →</a></p>';
    html += '<p class="hint">Support the full weight before releasing torque. Gear resistance can remain with all six motors OFF.</p>';
  } else if (p === "calibration_midpoint") {
    html = intro("Center the six joints.", "Keep the arm supported and clear of the instrument. Place each joint near the middle of its mechanical travel; half-open the gripper. Do not force a joint against a stop.") + support() + actions(button("calibration_center", "Capture midpoint →")) + '<p class="hint">The app checks a steady pose and reads back the new motor references. It does not drive to the midpoint.</p>';
  } else if (p === "calibration_range") {
    html = intro(`${s.range_index + 1} of 5 · ${esc(motors[s.range_motor])}`, "Support the arm. Slowly move only this joint through both ends of its usable travel, then return to a comfortable supported position. Stop before mechanical strain or cable tension.") +
      '<div class="range-panel"><div class="range-values"><span>Minimum <b id="range-min">—</b></span><span>Maximum <b id="range-max">—</b></span></div><progress id="range-progress" max="4095" value="0" aria-label="Recorded joint travel"></progress><p class="hint">Raw encoder ticks · recorded continuously</p></div>' +
      (sim ? actions(button("simulate_sweep", "Simulate joint sweep", true)) : "") + confirm("range_complete", "Both ends of the usable travel are recorded.") + actions(button("calibration_next", s.range_index === 4 ? "Review calibration →" : "Save range & continue →"));
  } else if (p === "calibration_review") {
    html = intro("Review the measured travel.", "Confirm these ranges reflect deliberate sweeps. Wrist rotation uses the full encoder range; there is no cable-twisting sweep.") +
      `<table><thead><tr><th>JOINT</th><th>MIN</th><th>MAX</th></tr></thead><tbody>${Object.entries(motors).map(([name,label]) => `<tr><td>${label}</td><td>${name === "wrist_roll" ? 0 : s.ranges[name]?.min}</td><td>${name === "wrist_roll" ? 4095 : s.ranges[name]?.max}</td></tr>`).join("")}</tbody></table>` + confirm("range_complete", "These measured ranges cover the intended usable travel.") + actions(button("calibration_save", "Save & verify calibration →")) + '<p class="hint">Existing note paths are tied to their original calibration. Changed calibration makes them require re-teaching.</p>';
  } else if (p === "fault" || p === "failed") {
    html = intro(p === "failed" ? "Let’s teach that stroke again." : "The session has stopped.", p === "failed" ? "This attempt is not registered. Support the arm before releasing torque, then capture a gentler, shorter stroke." : "No new motion is being issued. Support the arm and inspect the reported cause before continuing. Motor state is unverified until feedback resumes.") + support() + actions(button(s.calibrated ? "retry" : "release", s.calibrated ? "Release & retry this note" : "Release torque & return to setup")) + '<p class="hint">If the motors are not responding, use the physical power stop while supporting the arm.</p>';
  } else {
    const text = {
      note_ready: ["Find the lightest sounding press.", `Support the arm’s weight. Gently press ${esc(s.selected)} with the pad, only until the note sounds. Keep the gripper opening fixed and hold still.`],
      note_pressed: ["Lift to first contact.", "Slowly release the key until it is fully up, with the pad barely touching it. The app records your path continuously. Hold still for capture."],
      note_touch: ["Lift to a small clearance.", "Lift the pad just clear of the key along the same short path. Keep supporting the arm: this capture enables torque and holds this position."],
      arming: ["Keep supporting the arm.", "Establishing and checking a hold at the captured clearance. Wait for confirmation before removing your hands."],
      holding: ["Now test one press.", "The arm is holding the captured clearance. Gently take your hands away. This test makes one slow press and release, then holds at clearance."],
      testing: ["One press. One release.", "Keep hands clear. The arm will return along the captured path and hold at clearance. Watch the selected key and listen for a clean release."],
      result: ["Did the intended note sound?", sim ? "The simulated press and release finished. Practice accepting or rejecting a trial. This does not verify a physical key." : "Accept only if the intended key sounded once, released cleanly, and the pad and joints stayed steady. Reject excess pressure, a miss, or contact with another key."],
      saved: [s.trials >= 3 ? "Note registered." : "Trial accepted. Test it again.", s.trials >= 3 ? "Three trials accepted from this position. Support the arm before releasing torque and moving by hand to the next note." : "The arm is still holding at clearance. Repeat the test from here; there is no need to capture the same path again."]
    }[p] || ["Waiting for the arm…", s.message];
    html = `<div class="note-title"><span class="note-symbol">${esc(s.selected)}</span><h3>${text[0]}</h3></div>${noteSteps()}<p class="description">${text[1]}</p>`;
    if (p === "note_ready") html += actions(button("capture_pressed", "Capture sounding press →"));
    if (p === "note_pressed") html += actions(button("capture_touch", "Capture first contact →"));
    if (p === "note_touch") html += support() + actions(button("capture_clear", "Capture clearance & hold →"));
    if (p === "holding") html += hands() + actions(button("test", "Test one press →"));
    if (p === "result") html += trials() + actions(button("pass", sim ? "Accept simulated trial ✓" : "Sounded & released cleanly ✓") + button("fail", "Reject & re-teach", true));
    if (p === "saved") html += trials() + (s.trials >= 3 ? support() + actions(button("next", "Release & continue →")) : hands() + actions(button("test", "Test again →")));
    if (p.startsWith("note_")) html += `<p class="hint">${sim ? "Simulation places the virtual arm at each capture." : "Move gently. A large jump, changed grip, or stale reading stops capture."}</p>`;
  }
  $("workflow").innerHTML = html;
  if (p === "disconnected") getPorts();
}
function render() {
  if (!state) return;
  const s = state, p = s.phase;
  const key = [s.instance_id, p, s.range_index, s.trials, s.selected, s.calibrated].join(":");
  if (key !== renderKey) {
    cancelCountdown(); renderKey = key; workflow();
    say(["note_ready", "note_pressed", "note_touch", "holding", "result", "saved", "fault"].includes(p) ? s.message : "");
  }
  const count = Object.values(s.keys).filter(k => k.status === "registered").length;
  $("completed").innerHTML = `${count}<span>/12</span>`;
  $("mode").textContent = isSim() ? "SIMULATION · NO HARDWARE" : "HARDWARE MODE";
  $("mode").className = `badge ${isSim() ? "" : "hardware"}`;
  $("connection").textContent = online ? (owns ? (s.connected ? "Follower connected" : "Local app ready") : "Read-only window") : "Local app unavailable";
  $("connection-warning").hidden = online && owns;
  $("connection-warning").textContent = !online ? "Connection to the local Python app was lost. Controls are disabled. If a physical arm is active, support it and use the power stop if needed." : "Another browser owns operator control. This window shows status only.";
  const section = p === "disconnected" ? "connect" : p.startsWith("calibration") || (!s.calibrated && ["connected", "fault"].includes(p)) ? "calibration" : "notes";
  for (const name of ["connect", "calibration", "notes"]) {
    $("nav-" + name).classList.toggle("active", name === section);
    $("nav-" + name).querySelector("b").textContent = name === "connect" && s.connected || name === "calibration" && s.calibrated || name === "notes" && count === 12 ? "✓" : "";
  }
  $("step-label").textContent = section === "connect" ? "01 / CONNECTION" : section === "calibration" ? "02 / MOTOR CALIBRATION" : "03 / NOTE REGISTRATION";
  $("phase-badge").textContent = p === "fault" ? "STOPPED" : s.pending ? "IN PROGRESS" : p.replaceAll("_", " ").toUpperCase();
  const nextKeyboardKey = JSON.stringify([s.keys, s.selected, p, s.calibrated]);
  if (nextKeyboardKey !== keyboardKey) {
    keyboardKey = nextKeyboardKey;
  $("keyboard").innerHTML = notes.map(n => `<button class="key ${n.includes("#") ? "sharp" : ""} ${s.keys[n].status} ${s.selected === n ? "selected" : ""}" data-note="${esc(n)}" aria-label="${esc(n)}: ${s.keys[n].status.replaceAll("_"," ")}, ${s.keys[n].trials} of 3 trials" ${!["ready","connected"].includes(p) || !s.calibrated ? "disabled" : ""}><strong>${esc(n)}</strong><small>${s.keys[n].status === "registered" ? "✓ REGISTERED" : s.keys[n].status === "needs_reteach" ? "RE-TEACH" : s.keys[n].trials ? `${s.keys[n].trials}/3 TRIALS` : "—"}</small></button>`).join("");
  }
  // Clicking a key selects a label only; torque release always needs the supported action.
  $("keyboard-hint").textContent = ["ready", "connected"].includes(p) && s.calibrated ? "Select a note, support the arm, then choose Teach below." : "Each note stores its own short press and release path.";
  $("motors").innerHTML = Object.entries(motors).map(([name,label],i) => `<div class="motor ${s.range_motor === name ? "active" : ""}"><span class="motor-id">${String(i+1).padStart(2,"0")}</span><span>${label}</span><span class="position">${s.position?.[name] ?? "—"}</span><span class="torque">${s.torque?.[name] === 0 ? "OFF" : s.torque?.[name] === 1 ? "ON" : "?"}</span></div>`).join("");
  const flags = Object.values(s.torque || {});
  const off = flags.length === 6 && flags.every(v => v === 0), on = flags.length === 6 && flags.every(v => v === 1);
  $("torque-summary").textContent = off ? "6 / 6 TORQUE OFF" : on ? "TORQUE ON · HOLD" : "UNVERIFIED";
  $("torque-summary").className = `badge ${on ? "powered" : "neutral"}`;
  $("feedback-age").textContent = s.feedback_at ? `Last read ${Math.max(0, (Date.now()/1000 - s.feedback_at)).toFixed(1)}s ago · raw ticks` : "No current motor feedback";
  $("voltage").textContent = s.voltage ? `${s.voltage.toFixed(1)} V` : "—";
  $("feedback").textContent = s.feedback_at && online ? (isSim() ? "Simulated" : "Live") : "Unknown";
  $("recovery").hidden = !s.connected;
  if ($("range-min")) {
    const range = s.ranges[s.range_motor];
    $("range-min").textContent = range?.min ?? "—"; $("range-max").textContent = range?.max ?? "—";
    $("range-progress").value = range ? range.max - range.min : 0;
  }
  $("events").innerHTML = s.events.length ? s.events.slice(0,12).map(e => `<li><time datetime="${esc(e.created)}">${esc(new Date(e.created).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}))}</time><span>${esc(e.message)}</span></li>`).join("") : '<li><span>No session activity yet. Connect the practice arm to begin.</span></li>';
  $("operation").hidden = !s.pending; $("operation").textContent = s.message;
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
    if (b.dataset.action === "simulate_sweep") valid = true;
    b.disabled = blocked || !valid;
  });
  document.querySelectorAll("[data-note]").forEach(b => b.disabled = blocked || !state.calibrated || !["ready", "connected"].includes(state.phase));
  $("stop").disabled = !online || !owns || !state?.connected;
  if (selectedNote && $("workflow").querySelector('[data-action="note_start"]')) {
    const b = $("workflow").querySelector('[data-action="note_start"]');
    b.dataset.key = selectedNote; b.textContent = `Teach ${selectedNote} →`;
    document.querySelectorAll("[data-note]").forEach(k => k.classList.toggle("selected", k.dataset.note === selectedNote));
  }
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
  scope.querySelectorAll("[data-confirm]").forEach(c => args[c.dataset.confirm] = c.checked);
  if (b.dataset.recovery) args.supported = $("recovery-supported").checked;
  if (action === "connect") { args.fixture = $("fixture").value; args.port = $("port").value; }
  if (action === "note_start") args.key = b.dataset.key;
  const revision = state.revision;
  const delayed = ["calibrate","calibration_center","note_start","capture_pressed","capture_touch","capture_clear","next","release","disconnect","retry"].includes(action);
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
  else if (b.id === "refresh-ports") getPorts();
  else if (b.id === "stop") { cancelCountdown(); say("Stop requested"); submit("stop"); }
  else if (b.dataset.action) dispatchButton(b);
  else if (b.dataset.note) { selectedNote = b.dataset.note; updateButtons(); }
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
