"use strict";
const $ = (id) => document.getElementById(id);
const notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const motors = {shoulder_pan: "Base rotation", shoulder_lift: "Shoulder", elbow_flex: "Elbow", wrist_flex: "Wrist bend", wrist_roll: "Wrist rotation", gripper: "Gripper"};
const owner = crypto.randomUUID();
let state, token, online = false, owns = false, operatorError = null, renderKey = "", sending = false, timer = null;
let operatorConflictSince = null;
let connectionView = false, calibrationView = false, calibrationChoice = null, tuneView = false;
let lastReceipt = "", instance = "", firstLoad = true, keyboardKey = "";
// Two followers (rig.py): the console shows one at a time; Stop and the operator lease cover both.
let arm = (() => { try { return localStorage.getItem("orchid.arm") === "b" ? "b" : "a"; } catch { return "a"; } })();
let arms = null, connectPlan = null;
const instances = {};
const selectedArm = () => arm;  // for functions with their own local "arm"
const ARM_NAMES = {a: "Keys Arm · 12 keys", b: "Chord Arm · chords & dial"};
const ARM_SHORT = {a: "Keys Arm", b: "Chord Arm"};
const armStatus = a => !a.connected ? "not connected" : a.tuning ? "calibrating keys" : a.parked ? `parked ${a.parked_reason}` : a.parked_reason;
function renderArms() {
  const box = $("arm-switch");
  if (!box) return;
  const shown = arms ? Object.entries(arms).filter(([, a]) => a.available) : [];
  box.hidden = shown.length < 2;  // one follower: the console looks as it always has
  if (box.hidden) return;
  box.innerHTML = shown.map(([id, a]) => `<button type="button" data-arm="${id}" aria-pressed="${id === arm}" class="${a.parked || !a.connected ? "" : "away"}">` +
    `<strong>${esc(ARM_NAMES[id])}</strong><small>${esc(armStatus(a))}</small></button>`).join("");
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const isSim = () => state?.mode === "simulation";
const teachingWorkflowVersion = "leader-record-replay-v1";
const needsControllerUpdate = () => !!state?.mode && state.teaching_workflow_version !== teachingWorkflowVersion;
const recoveryAction = action => ["stop", "leader_pause", "release", "disconnect", "forget_connection"].includes(action);
const updateRequiredMessage = "Update not loaded: this page and the running Python controller use different teaching flows. Support both arms and disconnect motor power before restarting the Python app. Stop and supported release/disconnect remain available.";
const POSES = {home: {id: "home", label: "⌂", name: "Home", kind: "home"}, rest: {id: "rest", label: "☾", name: "Rest", kind: "rest"}};
const activeControl = () => POSES[selectedNote] || state.catalog[selectedNote || state.selected];
const targetControl = () => selectedNote && !POSES[selectedNote] ? selectedNote : state.selected;
const allStatuses = () => ({...state.keys, ...state.controls});
const statusText = value => value.status === "registered" ? "✓ REGISTERED" : value.status === "needs_reteach" ? "RE-TEACH" : value.recorded ? "RECORDED" : value.trials ? `${value.trials}/3 TRIALS` : "—";
const confirmationTitles = {supported: "Arm this step", hands_clear: "Arm test", prepared: "Arm connection",
  fixture_unchanged: "Keep saved placement", range_complete: "Confirm joint travel", fixed_pad: "Confirm fixed tips",
  direction_verified: "Confirm dial direction", rim_clear: "Confirm rim is clear", reference_reset: "Confirm reference reset",
  effect_verified: "Confirm expected effect", calibration_unchanged: "Confirm same arm & joints", path_clear: "Confirm clear route"};
const confirmed = b => b?.getAttribute("aria-pressed") === "true";
const confirm = (name, text, title = confirmationTitles[name] || "Confirm this step") => `<button type="button" class="confirmation" data-confirm="${name}" aria-pressed="false"><span class="confirmation-icon" aria-hidden="true">○</span><span class="confirmation-copy"><strong>${title}</strong><span>${text}</span><small class="confirmation-state">Tap to confirm · no motor command</small></span></button>`;
function setConfirmation(b, enabled) {
  b.setAttribute("aria-pressed", String(enabled));
  b.querySelector(".confirmation-icon").textContent = enabled ? "✓" : "○";
  b.querySelector(".confirmation-state").textContent = enabled ? "Confirmed · tap again to cancel" : "Tap to confirm · no motor command";
}
function clearConfirmations(scope = document) {
  scope.querySelectorAll("[data-confirm]").forEach(b => {
    if (b.dataset.confirm !== "fixture_unchanged") setConfirmation(b, false);
  });
}
const button = (action, text, secondary = false, attrs = "") => `<button data-action="${action}" class="${secondary ? "secondary" : "primary"}" ${attrs}>${text}</button>`;
const leaderMode = () => state?.teaching_mode === "leader";
// Leader teaching is the default; a deliberate choice of hand-guiding is remembered on this computer.
const preferredMode = () => { try { return localStorage.getItem("orchid.teachingMode") === "manual" ? "manual" : "leader"; } catch { return "leader"; } };
const rememberMode = value => { try { localStorage.setItem("orchid.teachingMode", value); } catch { /* storage unavailable */ } };
const referencesReady = (s = state) => !!s?.calibrated && (s.teaching_mode !== "leader" || !!s.leader?.calibrated);
const setupIdle = (s = state) => ["connected", "ready"].includes(s?.phase);
const calibrationTarget = () => state?.phase.startsWith("calibration_") ? state.calibration_target :
  calibrationChoice || (leaderMode() && state.calibrated && !state.leader?.calibrated ? "leader" : "follower");
const workflowSection = () => state.phase === "disconnected" || connectionView ? "connect" :
  tuneView && !state.phase.startsWith("calibration_") && referencesReady() ? "tune" : calibrationView || state.phase.startsWith("calibration_") || (!referencesReady() && ["connected","ready","fault"].includes(state.phase)) ? "calibration" : "notes";
const teachSessionPhases = ["teach_hold", "teach_follow", "teach_record", "teach_play"];
const powered = (s = state) => Object.values(s?.torque || {}).some(v => v === 1);
const teachingPhases = ["home_positioning","home_arrival","home_approach","home_return","note_ready","note_hover","note_pressed","note_touch","dial_ready","dial_approach","dial_contact","dial_turned","dial_lifted"];
const support = () => confirm("supported", leaderMode() ? "Both arms are supported or resting securely; it is safe to release torque or establish the follower hold." : "I am supporting the arm’s weight; it is safe to release or hold here.");
const calibrationPhases = ["connected", "ready", "calibration_midpoint", "calibration_range", "calibration_review"];
const actionScope = b => b.dataset.unavailable ? $("unavailable-tools") : b.dataset.connection ? $("connection-release") : b.dataset.recovery ? $("recovery") : b.dataset.home ? $("home-tools") : b.dataset.calibration ? $("calibration-tools") : b.dataset.leader ? $("leader-teaching") : $("workflow");
const homeSupport = () => confirm("supported", "I am supporting the follower, clear of Orchid. Establish a powered hold here; after I clear my hands, the leader will control all six motors, including the gripper.", "Arm home positioning");
const teachSupport = () => leaderMode() ? confirm("supported", "The follower is supported, clear of Orchid. Establish or keep its powered hold here, then return to saved home if needed.", "Arm teaching hold") : support();
const homeReadiness = () => '<div class="reference-card"><span>SHARED HOME</span><p id="home-readiness" role="status"></p><p>Each route starts and ends here. Moving to home requires clearance along the entire path, including the gripper.</p></div>';
function homeTools() {
  $("home-tools").hidden = true;  // Leader teaching records and replays; no shared home or routes.
  $("home-tools-content").innerHTML = '<p class="hint">Use the leader to position a new home. Start with a supported hold at the follower’s current position, clear your hands, then engage following. The old home stays saved until you capture its replacement; saving a new home requires re-teaching its routes.</p>' + homeSupport() + actions(button("home_start", "Position new home with leader →", false, 'data-home="true"'));
}
function calibrationTools() {
  const target = calibrationTarget(), s = target === "leader" ? {...state,...state.leader} : state;
  const available = calibrationPhases.includes(state.phase) && s.connected;
  const status = s.calibrating ? "New calibration in progress" : s.calibrated ? "Saved calibration is active" : "Saved calibration is not active";
  $("calibration-tools-content").innerHTML = `<div class="calibration-saved"><span class="eyebrow">${target.toUpperCase()} · SAVED REFERENCE</span><strong>${s.calibration ? esc(status) : "No saved calibration yet"}</strong><p>${s.calibration ? `Six motors · reference <code>${esc(s.calibration_id || "saved")}</code>` : "Complete and save the guided calibration to enable reload."}</p></div>` +
    `<p class="hint">Reset restarts the midpoint and all joint sweeps. Your last saved calibration and registered motions stay saved.</p>` +
    (!available ? `<p class="hint">${s.connected ? "Finish this control or use Release or disconnect → Release torque to return to setup." : `Connect the ${target} to reset or reload its calibration.`}</p>` : support() +
      actions(button("calibration_reset", "Reset calibration", true, 'data-calibration="true"')) +
      `<div class="calibration-reload"><p class="hint">Reload discards the unfinished calibration and verifies the saved settings on all six motors. Use reset after replacing or reseating a motor or joint.</p>` +
      (s.calibration ? confirm("calibration_unchanged", "This is the same arm; no motors or joints have been replaced or reseated since this calibration was saved.") : "") +
      actions(button("calibration_reload", "Reload saved calibration", true, 'data-calibration="true"')) + '</div>' +
      (leaderMode() && state.leader?.connected && state.calibrated && state.leader?.calibrated ?
        '<div class="calibration-reload"><p class="hint"><b>Re-centre wrist rotation</b> moves both arms’ wrist-roll zero by half a turn, so the wrist works mid-range instead of at the sensor’s −180°/+180° wrap. Saved home, rest, dial and keys are updated to match: nothing moves and nothing needs re-teaching. Do it once.</p>' +
        actions(button("recenter_wrist_roll", "Re-centre wrist rotation", true, 'data-calibration="true"')) + '</div>' : "")) +
    '<p class="hint">Both actions keep torque off. Reloading does not restore an earlier arm position. Motions still need the same calibration, fixture and contact tips.</p>';
}
const hands = () => confirm("hands_clear", "My hands are clear of the arm and key path.");
const intro = (title, description, copyError = false) => `<h3>${title}</h3><p class="description"${copyError ? ' data-error-copy' : ''}>${description}</p>`;
const actions = (html) => `<div class="actions">${html}</div>`;
function leaderPanel() {
  const s = state, l = s.leader;
  const simJoint = $("leader-sim-joint")?.value, simOpen = document.querySelector(".leader-simulation")?.open;
  $("leader-teaching").hidden = !leaderMode() || !l?.connected;
  if (!leaderMode() || !l?.connected) return;
  let html = `<div class="section-line"><h3>Leader → follower</h3><span id="leader-status" class="badge neutral"></span></div><p class="hint">Leader <b>${l.voltage?.toFixed(1) ?? "—"} V</b> · <span id="leader-torque"></span> · ${l.calibrated ? "calibrated" : "calibration needed"}<br>Follower ${s.calibrated ? "calibrated" : "calibration needed"} · ${["home_prepare","home_moving"].includes(s.phase) ? "gripper returns to its saved opening" : s.leader_gripper_enabled ? "gripper follows during home positioning" : "gripper holds its opening"}</p>`;
  if (s.teach && teachSessionPhases.includes(s.phase)) {
    const m = s.teach.mode;
    html += `<p class="hint">LeRobot SO101 driver · ${m === "aligning" ? "ramping to the leader’s pose at up to 30°/s" : m === "following" ? "follower mirrors the leader 1:1, gripper included" : ["ramping","settling"].includes(m) ? "moving to the recording’s start pose at up to 30°/s" : m === "playing" ? "playing the recorded motion" : "holding; moving the leader does not move the follower"}.</p>`;
    if (isSim() && ["teach_follow","teach_record"].includes(s.phase) && !connectionView) html += `<details class="leader-simulation"><summary>Practice leader movement</summary><p class="hint">Each tap moves the simulated leader by 12 ticks over time; the follower mirrors it once following.</p><label class="form-field">Joint<select id="leader-sim-joint">${Object.entries(motors).map(([id,label])=>`<option value="${id}" ${id === "wrist_flex" ? "selected" : ""}>${label}</option>`).join("")}</select></label>${actions(button("simulate_leader", "− Leader", true, 'data-leader="true" data-delta="-12"') + button("simulate_leader", "+ Leader", true, 'data-leader="true" data-delta="12"'))}</details>`;
  } else if (s.leader_teaching && teachingPhases.includes(s.phase)) {
    html += `<p class="hint">LeRobot SO101 · 1:1 joint motion · ${s.phase === "home_positioning" ? "position all six motors, including the gripper, for a new home" : s.phase === "home_arrival" ? "positioning available · confirm home below before recording a route" : "taught approach, local control motion, and return home"}. Pause to capture or reposition the leader. Uses the installed driver’s default position control. Pause holds the follower where it is. Every engagement has a five-second countdown, then the follower aligns to the leader’s current calibrated pose. Bring the leader close before the countdown ends.</p>`;
    html += s.leader_following ? actions(button("leader_pause", "Pause following · hold here", false, 'data-leader="true"')) : connectionView ? "" :
      confirm("hands_clear", "My hands and the entire path from the follower to the leader’s pose are clear. I will bring the leader close during the countdown.", "Arm following") + actions(button("leader_resume", "Engage leader · 5-second countdown →", false, 'data-leader="true"'));
    if (isSim() && !connectionView) html += `<details class="leader-simulation"><summary>Practice leader movement</summary><p class="hint">Each tap moves the simulated leader and follower by 12 ticks over time while engaged. Try a wrist bend press, pause and capture, then reverse to release.</p><label class="form-field">Joint<select id="leader-sim-joint">${Object.entries(motors).map(([id,label])=>`<option value="${id}" ${id === "wrist_flex" ? "selected" : ""}>${label}</option>`).join("")}</select></label>${actions(button("simulate_leader", "− Leader", true, 'data-leader="true" data-delta="-12"') + button("simulate_leader", "+ Leader", true, 'data-leader="true" data-delta="12"'))}</details>`;
  }
  html += '<p id="leader-feedback-status" class="hint" role="status"></p><p id="leader-recording-error" class="hint" role="alert" data-error-copy hidden></p>';
  $("leader-teaching").innerHTML = html;
  if (simJoint && $("leader-sim-joint")) $("leader-sim-joint").value = simJoint;
  if (simOpen && document.querySelector(".leader-simulation")) document.querySelector(".leader-simulation").open = true;
}
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
    throw Object.assign(new Error(typeof message === "string" ? message : "The command could not be accepted."), {status: response.status});
  }
  return response.json();
}
function renderPorts() {
  const select = $("port"), leaderSelect = $("leader-port"), discovery = state.discovery;
  const picker = select || leaderSelect;
  if (!picker || !discovery) return;
  const {ports, scanning, scanned_at: scannedAt, warnings, error: scanError} = discovery;
  const signature = JSON.stringify(discovery);
  if (picker.dataset.snapshot === signature) return;
  picker.dataset.snapshot = signature;
  const role = p => (p.role === "simulator" ? "Practice arm" : p.role === "follower" ? "Follower" : p.role === "leader" ? "Leader" : "Unidentified arm") +
    (p.arm ? ` · ${ARM_SHORT[p.arm]}` : "");  // recognised by the calibration in its motors
  const volts = p => p.voltage === null ? "Voltage unavailable" : `${p.voltage.toFixed(1)} V`;
  const eligible = ports.filter(p => p.connectable);
  if (select) {
  const previous = select.value;
  select.innerHTML = (!eligible.length ? `<option value="">${scanning ? "Scanning robot arms…" : scannedAt ? "No ready follower found" : "Refresh to find robot arms"}</option>` : "") +
    ports.map(p => `<option value="${esc(p.path)}" ${p.connectable ? "" : "disabled"}>${esc(role(p))} · ${esc(p.path)} · ${volts(p)} · Motors ${esc(p.motor_ids.join(", "))}</option>`).join("");
  // Prefer the follower whose motors carry this arm's calibration, then an unrecognised one.
  const mine = eligible.find(p => p.arm === arm) || eligible.find(p => !p.arm);
  select.value = eligible.some(p => p.path === previous) ? previous : (mine || eligible[0])?.path || "";
  }
  const previousLeader = leaderSelect.value;
  const leaders = ports.filter(p => p.leader_connectable);
  leaderSelect.innerHTML = leaders.length ? leaders.map(p => `<option value="${esc(p.path)}">Leader · ${esc(p.path)} · ${volts(p)} · Motors ${esc(p.motor_ids.join(", "))}</option>`).join("") : '<option value="">No ready leader found · refresh connections</option>';
  leaderSelect.value = leaders.some(p => p.path === previousLeader) ? previousLeader : leaders[0]?.path || "";
  $("arm-discovery").innerHTML = ports.map(p => `<article class="discovered-arm ${p.connectable || p.leader_connectable ? "ready-arm" : ""}"><div class="arm-identity"><strong>${esc(role(p))}</strong><span>${esc(p.path)}</span></div><strong class="arm-voltage">${volts(p)}</strong><div class="discovered-motors"><span>Motor IDs</span>${p.motor_ids.map(id => `<b>${esc(id)}</b>`).join("")}</div><p>${isSim() ? "Simulated readings · no USB connection" : p.leader_connectable ? "Ready for leader input · select Use leader to teach" : esc(p.problem || "Six motors detected · ready to connect")}</p></article>`).join("");
  const status = scanning ? "Scanning USB adapters for Feetech motors…" : scanError ? `Scan failed: ${scanError}` : isSim() ? "Practice arm available. No physical devices are scanned." : scannedAt ?
    `${ports.length} robot arm${ports.length === 1 ? "" : "s"} found · scanned ${new Date(scannedAt * 1000).toLocaleTimeString()}. Voltage is a scan-time reading from each arm’s first responding motor. ${ports.length ? "Leader / follower is inferred from voltage." : "Check USB and arm power, then refresh."}` :
    "Refresh connections to find powered robot arms. Only adapters with responding Feetech motors appear here.";
  $("discovery-status").textContent = status;
  $("discovery-warnings").textContent = warnings.join("\n");
  $("discovery-warnings").hidden = !warnings.length;
}
function calibrationSetup() {
  const target = calibrationTarget(), arm = target === "leader" ? state.leader : state;
  let html = intro("Choose an arm to calibrate.", "Each arm keeps its own saved calibration. Selecting an arm does not change motor settings.") +
    `<div class="calibration-arm-picker" role="group" aria-label="Arm to calibrate">${["follower","leader"].map(role => {
      const value = role === "leader" ? state.leader : state;
      return `<button type="button" class="secondary" data-calibration-target="${role}" aria-pressed="${role === target}"><strong>${role === "leader" ? "Leader" : "Follower"}</strong><span>${value?.connected ? value.calibrated ? "Calibration verified" : "Needs calibration" : "Not connected"}</span></button>`;
    }).join("")}</div>`;
  const otherArm = arms && Object.keys(arms).find(id => id !== selectedArm()), otherHasLeader = otherArm && arms[otherArm].leader;
  if (target === "leader" && !arm?.connected && otherHasLeader) {
    html += `<p class="notice" role="status">The leader is connected to the ${esc(ARM_SHORT[otherArm])}. Switch to it at the top and press <b>Hand the leader over</b> on its leader page, then connect the leader here.</p>`;
  }
  if (!arm?.connected) {
    html += '<p class="description">Connect the leader here to enable leader teaching. The follower stays connected and its saved calibration is retained.</p>' +
      '<div class="field-row connection-picker"><label class="form-field">Leader connection<select id="leader-port" aria-label="Leader connection" aria-describedby="discovery-status"></select></label>' +
      button("refresh_leader_ports", "↻ Find leader", true) + '</div><div id="arm-discovery" class="arm-discovery" aria-label="Detected robot arms"></div><p id="discovery-status" class="hint" role="status"></p><p id="discovery-warnings" class="discovery-warnings" hidden></p>' +
      confirm("prepared", "The leader is secure, powered, connected by USB and clear of the instrument.", "Arm leader connection") +
      actions(button("connect_leader", "Connect leader →"));
  } else {
    html += `<p class="description">${target === "leader" ? "Calibrate the leader while the follower rests securely. The follower’s saved calibration is retained." : "Capture the follower midpoint, then measure its usable joint ranges."}</p>` +
      (target === "follower" && state.calibration_foreign ?
        `<p class="notice" role="alert">This follower’s motors do not carry ${esc(arms ? "the " + ARM_SHORT[selectedArm()] : "this arm")}’s saved calibration, so it may be a different arm. ` +
        `If it is, disconnect and connect it as the other arm. Calibrating here replaces the saved calibration; ${state.calibration_dependents} taught controls recorded with it would need re-teaching.</p>` +
        confirm("replace_calibration", `Replace this arm’s calibration. ${state.calibration_dependents} taught controls will need re-teaching.`, "Replace calibration") : "") +
      support() + actions(button("calibrate", `${arm.calibrated ? "Recalibrate" : "Calibrate"} ${target} →`, false, `data-target="${target}"`));
    if (target === "leader" && otherArm) html += '<p class="hint">One leader teaches both followers. To teach the other arm, hand the leader over: it disconnects here and its calibration stays shared.</p>' +
      actions(button("leader_detach", `Hand the leader over to the ${esc(ARM_SHORT[otherArm])} →`, true));
  }
  if (referencesReady()) html += actions('<button type="button" class="secondary" data-view="notes">Back to training →</button>');
  return html;
}
// One leader, two followers: offer it here when the other arm has it (POST /api/leader/move).
function leaderStrip() {
  const holder = arms && Object.entries(arms).find(([id, a]) => id !== arm && a.leader)?.[0];
  if (!holder || state.leader?.connected) return "";
  return `<div class="leader-strip" role="status"><span>The leader is teaching the <b>${esc(ARM_SHORT[holder])}</b>.</span>` +
    button("move_leader", `Use the leader with the ${esc(ARM_SHORT[arm])} →`, true) + "</div>";  // secondary: Space stays on Play/Teach
}
async function moveLeader() {
  sending = true; error(""); updateButtons();
  try { await request("/api/leader/move", {to: arm}); }
  catch (err) { error(err.message); }
  finally { sending = false; updateButtons(); }
}
function connectSummary() {
  const d = state.discovery || {}, short = path => esc(String(path).replace("/dev/tty.", ""));
  const shown = Object.entries(arms || {a: {connected: state.connected, available: true}}).filter(([, a]) => a.available);
  const rows = shown.map(([id, a]) => {
    const step = (connectPlan || {})[id], name = esc(arms ? ARM_NAMES[id] : "Follower");
    if (a.connected) return `<li><b>${name}</b> <small>connected</small></li>`;
    if (!step) return `<li><b>${name}</b> <small>${isSim() || d.scanned_at ? "no follower found" : "not searched yet"}</small></li>`;
    return `<li><b>${name}</b> ← ${short(step.port)} <small>${step.recognised ? "recognised" : "new: calibrate it next"}${step.leader_port ? ` · with the leader (${short(step.leader_port)})` : ""}</small></li>`;
  });
  const problems = [d.error, ...(d.warnings || [])].filter(Boolean);
  return `<div class="connect-all"><strong>${d.scanning ? "Searching USB…" : "Arms"}</strong><ul>${rows.join("")}</ul>` +
    (problems.length ? `<p class="hint">${problems.map(esc).join("<br>")}</p>` : "") + "</div>";
}
async function findConnectAll() {
  sending = true; error(""); updateButtons();
  try { await request("/api/connect-all", {prepared: true, teaching_mode: "leader", scan: true}); }
  catch (err) { error(err.message); }
  finally { sending = false; updateButtons(); }
}
function noteSteps() {
  const phase = state.phase;
  const n = ["note_ready", "note_hover", "note_touch", "note_pressed"].indexOf(phase);
  const stage = n >= 0 ? n : phase === "retreating" ? 3 : ["arming", "holding", "testing"].includes(phase) ? 4 : 5;
  return `<div class="stages">${["Hover", "Contact", "Press", "Return", "Test", "Review"].map((text,i) => `<span class="${i < stage ? "done" : i === stage ? "current" : ""}">${text}</span>`).join("")}</div>`;
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
  if (p === "dial_lifted") html += '<div class="return-meter">Return error <strong id="dial-return-error">—</strong><span>target ≤ 6 ticks · same clear start area</span></div>' + confirm("rim_clear", "The entire return path stayed clear of the dial.") + (leaderMode() ? hands() : support()) + actions(button("dial_capture_return", leaderMode() ? "Capture dial clearance →" : "Capture return & hold →"));
  const reference = () => `<div class="reference-card"><span>REFERENCE</span><p>${esc(s.dial_reference)}</p><span>EXPECTED EFFECT</span><p>${esc(s.dial_expected_effect)}</p></div>`;
  const reset = () => confirm("reference_reset", "I restored the reference chord and starting voicing while the pad was clear.");
  if (["holding", "saved", "result"].includes(p)) html += reference();
  if (p === "holding") html += reset() + hands() + actions(button("test", "Test dial gesture →"));
  if (p === "result") html += trials() + confirm("effect_verified", "Requested direction and expected effect, with no slip or reverse turn on the clear return.") + actions(button("pass", isSim() ? "Accept simulated trial ✓" : "Accept dial trial ✓") + button("fail", "Reject & re-teach", true));
  if (p === "saved") html += trials() + (s.trials >= 3 ? teachSupport() + actions(button("next", leaderMode() ? "Continue at home →" : "Release & continue →")) : reset() + hands() + actions(button("test", "Test again →")));
  html += '<p class="hint">This is a relative gesture, not an absolute dial setting. The musical change depends on the chord. Clockwise and counterclockwise are taught separately.</p>';
  return html;
}
const teachPoints = ["home", "hover", "touch", "press"];
const dialPoints = ["home", "hover", "open", "lower", "grip"];  // shared by CW and CCW; the turn is an exact wrist angle
const stepsFor = id => state?.catalog?.[id]?.kind === "dial" ? dialPoints : teachPoints;
const pointLabels = {home: "Home", hover: "Hover", touch: "Touch", press: "Press", open: "Open", lower: "Lower", grip: "Grip"};
const pointsFor = (s, id) => s.teach?.points_for === id ? s.teach.points : allStatuses()[id]?.recorded ? stepsFor(id) : s.teach?.home_saved ? ["home"] : [];
const canCapture = (s = state) => (s.phase === "teach_hold" && s.teach?.mode === "holding") || (s.phase === "teach_follow" && s.teach?.mode === "following");
function poseWorkflow(s, pose) {
  const p = s.phase, t = s.teach || {}, following = p === "teach_follow" && t.mode === "following";
  const name = pose === "home" ? "home" : "rest", saved = pose === "home" ? t.home_saved : t.rest_saved;
  const savedAt = pose === "home" ? t.home_saved_at : t.rest_saved_at;
  const title = p === "teach_play" ? `Moving to ${name}…` : p === "teach_follow" && !following ? "Matching the leader…" :
    following ? `Guide the arm to ${name}, then Space.` : pose === "home" ? "Home" : "Rest";
  const about = pose === "home" ? "Every key starts and ends at this one home." : "Go to rest parks the arm here; playback waits at home between keys.";
  let html = `<div class="note-title"><span class="note-symbol">${POSES[pose].label}</span><h3>${flashing(pose) ? `${pose === "home" ? "Home" : "Rest"} captured ✓` : title}</h3></div>` +
    `<p class="description">${esc(s.message)}</p><p class="hint">${savedAt ? `${pose === "home" ? "Home" : "Rest"} last set ${esc(new Date(savedAt).toLocaleTimeString())}.` : `No ${name} set yet.`} ${about}</p>`;
  const go = saved ? button(`teach_go_${name}`, `Go to ${name}`, true) : "";
  if (following) html += actions(button(`teach_set_${name}`, `Set ${name} here`) + go + button("teach_hold", "Hold here", true));
  else if (p === "teach_follow") html += actions(button("teach_hold", "Hold here", true));
  else if (p === "teach_hold") html += actions((t.leader !== false ? button("teach_follow", "Follow the leader →") : "") + go);
  else if (p === "teach_play") html += actions(button("teach_hold", "■ Stop & hold here", true));
  return html;
}
// What Orchid sent during the last play, from Orchid Studio's key monitor (hardware only).
function keyCheck(s) {
  const k = s.key_check;
  if (!k || !["teach_hold", "teach_follow"].includes(s.phase)) return "";
  const label = {ok: "Orchid heard", problem: "Check the arm", pending: "Orchid", unavailable: "Note check unavailable"}[k.status] || "Orchid";
  return `<p class="${k.status === "problem" ? "notice" : "hint"} key-check" role="status"><b>${label}:</b> ${esc(k.summary)}</p>`;
}
// 04 Tune keys: MIDI-guided tune-up (tune.py). Gentle trials checked by Orchid, small capped corrections.
const tuning = (s = state) => s?.tune?.status === "running" || !!s?.tune_queue?.length;
function tuneSection() {
  const s = state, t = s.tune, limits = s.tune_limits || {}, results = s.tune_results || {}, queue = s.tune_queue || [];
  const busy = tuning(s), taught = notes.filter(k => allStatuses()[k]?.recorded);
  let html = intro("Calibrate the keys with Orchid.", `<b>Find</b>: gentle presses (${limits.speed}× speed, ${Math.round(limits.hardness * 100)}% hardness) while Orchid Studio listens, until two agree on where the key triggers. ` +
    `<b>Set</b>: touch ${limits.margin}° before that point and press ${limits.depth}° past it, so every key gets the same approach and pressure. ` +
    `<b>Verify</b>: ${limits.passes} clean presses in a row at your Arm speed and press hardness save it. Pressing deeper is capped at ${limits.limit_deg}° past what you taught; a wrong key stops for a re-teach with the leader.`);
  if (s.owns && !s.owns.some(k => notes.includes(k)))
    return html + `<p class="notice" role="status">Key calibration is for the keys arm. Choose the <b>${esc(ARM_SHORT.a)}</b> in the sidebar.</p>`;
  if (!s.key_check_available) return html + '<p class="notice" role="status">Tuning listens to Orchid through Orchid Studio, so it needs real hardware and Studio running with <b>--sound-input Orchid</b>.</p>';
  if (busy) {
    const key = t?.status === "running" ? t : null;
    const stage = key && (key.phase === "verify" ? `verify at ${key.speed}× · ${key.passes}/${limits.passes} clean` : "finding the trigger");
    html += `<div class="tune-progress" role="status"><strong>${key ? `Calibrating ${esc(key.name)} · try ${key.trial} · ${stage}` : "Next key…"}</strong>` +
      `<span>${queue.length ? `${queue.length} more: ${queue.map(esc).join(" ")}` : "Last key"}</span></div>` +
      (key ? `<ol class="tune-log">${key.log.map(e => `<li class="${esc(e.outcome)}"><b>${e.phase === "verify" ? "Verify" : "Find"} ${e.trial}</b> ${esc(e.text)}</li>`).join("")}</ol>` : "") +
      actions(button("tune_stop", "■ Stop calibration", false));
  } else {
    if (t) html += `<p class="${t.status === "done" ? "hint" : "notice"}" role="status"><b>${esc(t.name)}:</b> ${esc(t.message)}</p>`;
    html += confirm("beside_arm", "I am beside the arm with Stop motion in reach. Each try presses one key gently.", "Ready to tune");
    if (s.phase === "teach_follow") html += '<p class="hint">Hold the arm first; it follows the leader now.</p>' + actions(button("teach_hold", "Hold here", false));
    else if (s.phase !== "teach_hold") html += '<p class="hint">The arm holds at home before tuning.</p>' +
      (powered() ? confirm("supported", "The follower is already powered. I am supporting it: torque blinks off for a moment while LeRobot’s motor settings are applied.", "Support the follower") : "") +
      actions(button("teach_begin", "Go to home ▶", false, 'data-pose="home" data-play="1"'));
    else if (taught.length) html += actions(button("tune_start", taught.length === 1 ? `Calibrate ${esc(taught[0])}` : `Calibrate all ${taught.length} taught keys`, false, `data-controls="${esc(taught.join(","))}"`));
  }
  html += `<div class="tune-grid" role="group" aria-label="Keys">${notes.map(k => {
    const r = results[k], now = busy && t?.status === "running" && t.control === k, waiting = queue.includes(k);
    const label = !allStatuses()[k]?.recorded ? "not taught" : now ? "tuning…" : waiting ? "queued" : !r ? "—" :
      r.status === "done" ? "✓ calibrated" : r.status === "failed" ? "✗ re-teach" : "stopped";
    return `<button data-action="tune_start" data-controls="${esc(k)}" class="secondary tune-key ${now ? "current" : r ? esc(r.status) : ""}" title="${esc(r?.message || "")}"><strong>${esc(k)}</strong><small>${label}</small></button>`;
  }).join("")}</div>`;
  return html;
}
function teachWorkflow(s) {
  const p = s.phase, t = s.teach || {}, chosen = activeControl(), name = esc(chosen.name);
  if (POSES[chosen.id]) return poseWorkflow(s, chosen.id);
  const recorded = !!allStatuses()[chosen.id]?.recorded;
  const lead = t.leader !== false;  // the session follows a leader unless the controller says it has none
  const heard = p === "teach_hold" && t.played === chosen.id;
  const play = (label, secondary = true) => button("teach_play", label, secondary);  // speed comes from the shared setting
  const description = `<p class="description">${esc(s.message)}</p>` + keyCheck(s) + (t.roll_guard ?
    '<p class="notice" role="status">Wrist rotation paused: the leader’s wrist crossed its ±180° edge. Turn it back toward the follower’s wrist angle to resume.</p>' : "");
  const playback = p === "teach_play" ? '<progress id="teach-progress" max="1" value="0" aria-label="Playback progress"></progress>' + actions(button("teach_hold", "■ Stop & hold here", true)) : "";
  const got = pointsFor(s, chosen.id);
  // A step chosen for retraining (tap its box) comes first; otherwise the first step not yet taught.
  const retrain = targetPoint?.control === chosen.id && got.includes(targetPoint.point) ? targetPoint.point : null;
  const dial = chosen.kind === "dial", steps = stepsFor(chosen.id);
  const next = retrain || steps.find(n => !got.includes(n));
  const title = p === "teach_play" ? (t.returning ? "Returning to home…" : t.going_rest ? "Returning to rest…" : `Playing ${name}…`) :
    p === "teach_follow" && t.mode !== "following" ? "Matching the leader…" :
    !lead ? (heard ? `Played ${name}.` : recorded ? `Ready to play ${name}.` : `${name} is not taught yet.`) :
    retrain ? `Retrain ${pointLabels[retrain].toLowerCase()}: ${p === "teach_hold" ? "follow the leader, then" : ""} guide to it, then Space.` :
    next ? (p === "teach_hold" && next !== "home" ? `Holding · ready to teach ${name}.` : `Guide to ${pointLabels[next].toLowerCase()}, then Space.`) :
    heard ? `Played ${name}.` : `${name} is taught.`;
  let html = `<div class="note-title"><span class="note-symbol${dial ? " dial-symbol" : ""}">${dial ? (chosen.direction === "cw" ? "↻" : "↺") : esc(chosen.label)}</span><h3>${title}</h3></div>` +
    `<div class="teach-points" role="group" aria-label="Taught points">${`<span class="teach-point ${got.includes("home") ? "done" : ""}"><strong>${got.includes("home") ? "✓" : "○"} Home</strong><small>${got.includes("home") ? "the arm’s home" : "set with ⌂ Home"}</small></span>`}${steps.filter(n => n !== "home").map(n => {
      const done = got.includes(n), current = n === next, just = flashing(n, chosen.id);
      const classes = ["teach-point", current ? "current" : done ? "done" : "", just ? "flash" : ""].filter(Boolean).join(" ");
      const shared = dial ? " · both directions" : "";
      const note = (just ? "captured ✓" : current && done ? "retraining · Space" : current ? "next" : done ? (lead ? "tap to retrain" : "taught") : "") + (just ? "" : shared);
      return `<button type="button" data-target-point="${n}" class="${classes}" aria-pressed="${current}" ${(done || current) && lead ? "" : 'data-locked="true"'}><strong>${done && !current ? "✓" : current ? "●" : "○"} ${pointLabels[n]}</strong><small>${note}</small></button>`;
    }).join("")}<span class="teach-point-return">${dial ? "Play: … grip → turn the wrist → let go → raise → hover → home" : "↩ back the same way"}</span></div>` + description;
  const capture = next === "home" ? button("teach_set_home", "Set home here") :
    next ? button("teach_capture", next === "press" ? `${retrain ? "Recapture" : "Capture"} press & return home ↩` :
      next === "grip" && dial ? `${retrain ? "Recapture" : "Capture"} grip & let go ↩` : `${retrain ? "Recapture" : "Capture"} ${pointLabels[next].toLowerCase()}`, false, `data-point="${next}"`) : "";
  // The dial's turn is not taught with the leader: it is the grip pose with only the wrist rotated by this angle.
  const degrees = allStatuses()[chosen.id]?.turn_degrees ?? (chosen.direction === "cw" ? 20 : -20);
  const turnField = dial && !next ? `<label class="form-field turn-field">Turn ${chosen.direction === "cw" ? "↻" : "↺"} <input id="turn-degrees" type="number" min="-90" max="90" step="1" value="${esc(turnInput[chosen.id] ?? degrees)}"> degrees of wrist rotation <small>If it turns the wrong way, flip the sign.</small></label>` : "";
  const settings = s.teach_settings || {speed: 1, press_s: 0.3};
  const press = allStatuses()[chosen.id]?.press_s ?? settings.press_s;
  const pressField = !dial && !next ? `<label class="form-field turn-field">Press length <input id="press-seconds" type="number" min="0" max="5" step="0.1" value="${esc(pressInput[chosen.id] ?? press)}"> seconds held down <small>Saved for ${name} when you Play.</small></label>` : "";
  html += turnField + pressField;
  if (p === "teach_hold" && !lead) {
    // Holding with only the follower: play taught controls; teaching needs the leader.
    if (t.warning) html += actions(button("teach_play", `Play ${name} anyway ▶`, false, 'data-force="true"'));
    else if (recorded) html += actions(play(heard ? "Play again ▶" : `Play ${name} ▶`, false));
    if (recorded) html += '<p class="hint"><kbd>Space</kbd> plays the selected control. Pick another taught control above to play it.</p>';
    html += '<p class="notice" role="status">Teach buttons appear when the leader is connected; this session is guiding the follower by hand, so it can only play. To teach: support the follower, <b>Release torque</b> (Arm status → Release or disconnect), then <b>Calibrate motors → Leader → Find leader → Connect leader</b>.</p>';
  } else if (p === "teach_hold") {
    if (t.warning) html += actions(button("teach_play", `Play ${name} anyway ▶`, false, 'data-force="true"'));
    else if (!next) html += actions(play(heard ? "Play again ▶" : `Play ${name} ▶`, false) + button("teach_follow", "Re-teach with the leader", true));
    else if (next === "home") html += actions(capture + button("teach_follow", "Follow the leader →", true));
    else html += actions(button("teach_follow", "Follow the leader →") + (recorded ? play(`Play ${name} ▶`) : ""));
  }
  if (p === "teach_follow") html += t.mode === "following" ? actions(capture + (!next ? play(`Play ${name} ▶`, false) : "") + button("teach_hold", "Hold here", true)) : actions(button("teach_hold", "Hold here", true));
  html += playback;
  if (["teach_hold", "teach_follow"].includes(p) && lead) html += '<p class="hint"><kbd>Space</kbd> presses the highlighted button. To retrain one step, tap its box, guide the arm there, and press Space; the other steps are kept. ' +
    (dial ? "Hover above the knob, open the jaws, lower around it, then grip. Capturing the grip lets go and returns home on its own. Play turns only the wrist by the angle above, lets go, and raises back out; it never turns back while gripping. The steps are shared by CW and CCW; each has its own angle."
          : "Capturing the press returns to home on its own. Play goes home → hover → touch → press and back.") +
    ' Home is set with ⌂ Home on the map.</p>';
  return html;
}
function workflow() {
  const s = state, sim = isSim(), p = s.phase;
  const dialFields = p === "dial_ready" ? [$("dial-reference")?.value, $("dial-effect")?.value] : [];
  const chosen = activeControl();
  const chord = s.selected_control.kind === "button";
  const complete = Object.values(allStatuses()).every(k => k.status === "registered");
  const canTeach = referencesReady();
  let html = "";
  if (workflowSection() === "tune") { $("workflow").innerHTML = tuneSection(); return; }
  if (p === "disconnected") {
    // One press: find every arm on USB, connect each follower to its own arm and the leader (Rig.plan).
    html = intro(sim ? "Connect the practice arms." : "Connect the arms.", sim ?
        "Practice followers and a practice leader, with no robot attached. Practice data stays separate from the real instrument." :
        "Power the followers and the leader and rest them clear of Orchid. One press finds every arm on USB and connects each to its place. Connecting only reads the motors; nothing moves.") +
      connectSummary() +
      actions(button("find_connect_all", sim ? "Connect practice arms →" : "Find and connect all arms →")) +
      `<p class="hint">Each follower is recognised by the calibration in its motors, so it always goes back to its own arm; a new follower takes the empty slot. The leader joins the Keys Arm${arms?.b?.available ? ", or the arm being connected; hand it over from Calibrate motors → Leader" : ""}.</p>`;
  } else if (connectionView) {
    html = intro("Return to connections.", "Support both connected arms before releasing torque and disconnecting. You can then refresh ports or change teaching mode. Saved calibrations and registered motions stay saved; an unfinished teaching attempt ends.") +
      (p.startsWith("calibration_") ? '<p class="hint">The unfinished calibration will be canceled and its previous motor settings restored and verified before disconnecting.</p>' : "") +
      (s.leader_following ? '<p class="notice">Pause leader following above before supporting the follower and disconnecting.</p>' : "") +
      '<div id="connection-release">' + support() + actions(button("disconnect", "Release & return to connections →", false, 'data-connection="true"') +
        '<button type="button" class="secondary" data-view="current">Stay on current step</button>') + '</div>';
    if (p === "fault") html += '<details id="unavailable-tools" open><summary>USB unplugged or arm replaced?</summary>' +
      '<p class="notice" id="connection-fault" role="status"></p>' +
      '<p>Release needs a working connection to the original arm. To clear an unavailable connection, first support both arms and physically disconnect both motor power supplies. Unplugging USB alone does not release torque.</p>' +
      confirm("supported", "Both arms are resting securely or fully supported.", "Confirm arms supported") +
      confirm("motor_power_disconnected", "The motor power supplies for BOTH arms are physically disconnected.", "Confirm motor power disconnected") +
      actions(button("forget_connection", "Forget unavailable connection →", true, 'data-unavailable="true"')) +
      '<p class="hint">Closes the old USB connections without sending motor commands. Saved records stay saved. A replacement arm needs a full calibration and a new home.</p></details>';
  } else if (calibrationView && !setupIdle() && !p.startsWith("calibration_")) {
    html = intro("Return to calibration.", "Support both connected arms before releasing torque. This ends the current teaching attempt; saved calibrations and registered motions are retained.") +
      support() + actions(button("release", "Release & return to calibration →"));
  } else if (setupIdle() && (calibrationView || !canTeach)) {
    html = calibrationSetup();
  } else if (["connected", "ready"].includes(p) && leaderMode() && POSES[chosen.id]) {
    html = intro(chosen.id === "home" ? "The arm’s home." : "The arm’s rest pose.",
      chosen.id === "home" ? "Home is where every key starts and ends. Follow the leader, guide the arm to a clear pose, then Set home here." :
      "Rest is a parking pose away from the keys. Go to rest moves the arm there; between key presses it waits at home. Follow the leader, guide the arm to it, then Set rest here.") +
      (powered() ? confirm("supported", "The follower is already powered. I am supporting it: torque blinks off for a moment while LeRobot’s motor settings are applied.", "Support the follower") : "") +
      actions(button("teach_begin", "Follow the leader →", false, 'data-follow="true"'));
  } else if (["connected", "ready"].includes(p) && leaderMode()) {
    html = intro(complete ? "The instrument is registered." : `Ready to teach ${esc(chosen.name)}.`,
      "Choose a key above and press Teach (or Space). The follower holds where it is, that pose becomes home, and it starts following the leader at up to 30°/s. Then Space captures hover, touch and press.") +
      (powered() ? confirm("supported", "The follower is already powered. I am supporting it: torque blinks off for a moment while LeRobot’s motor settings are applied.", "Support the follower") : "") +
      actions((allStatuses()[chosen.id]?.recorded ?
        button("teach_begin", `Play ${esc(chosen.name)} ▶`, false, `data-control="${esc(chosen.id)}" data-play="1"`) +
        button("teach_begin", `Re-teach ${esc(chosen.name)}`, true, `data-control="${esc(chosen.id)}" data-follow="true"`) :
        button("teach_begin", `Teach ${esc(chosen.name)} →`, false, `data-control="${esc(chosen.id)}" data-follow="true"`)) +
        '<button type="button" class="secondary" data-view="calibration">Back to calibration</button>');
    if (complete) html += '<p class="hint"><a href="/api/export" data-export download>Download the session record →</a></p>';
  } else if (["connected", "ready"].includes(p) && canTeach && POSES[chosen.id]) {
    // Without the leader, a saved home or rest can still be visited; setting one needs the leader.
    const pose = chosen.id, saved = s.poses_saved?.[pose];
    html = intro(pose === "home" ? "The arm’s home." : "The arm’s rest pose.", saved ?
      `Go to ${pose} holds the follower where it is, then moves it to the saved ${pose} at up to 30°/s. The leader is not needed.` :
      `No ${pose} is set yet. Connect the leader to set it: <b>Calibrate motors → Leader → Find leader → Connect leader</b>.`) +
      (saved && powered() ? confirm("supported", "The follower is already powered. I am supporting it: torque blinks off for a moment while LeRobot’s motor settings are applied.", "Support the follower") : "") +
      actions((saved ? button("teach_begin", `Go to ${pose} ▶`, false, `data-pose="${pose}" data-play="1"`) : "") +
        '<button type="button" class="secondary" data-view="calibration">Back to calibration</button>');
  } else if (["connected", "ready"].includes(p) && allStatuses()[chosen.id]?.recorded) {
    // A taught control plays with only the follower; teaching it again needs the leader.
    html = intro(`${esc(chosen.name)} is taught.`,
      "Play holds the follower where it is, then plays the taught motion through home at the arm speed above. The leader is not needed to play.") +
      (powered() ? confirm("supported", "The follower is already powered. I am supporting it: torque blinks off for a moment while LeRobot’s motor settings are applied.", "Support the follower") : "") +
      actions(button("teach_begin", `Play ${esc(chosen.name)} ▶`, false, `data-control="${esc(chosen.id)}" data-play="1"`) +
        '<button type="button" class="secondary" data-view="calibration">Back to calibration</button>') +
      '<p class="hint">To re-teach it, connect the leader: <b>Calibrate motors → Leader → Find leader → Connect leader</b>.</p>';
  } else if (["connected", "ready"].includes(p)) {
    html = intro(complete ? "The instrument is registered." : canTeach ? `Ready to teach ${esc(chosen.name)}.` : (leaderMode() ? "Calibrate both arms." : "Give the arm its reference points."), complete ? "All 22 motions have three accepted trials. Rest the arm safely and export your session. Changing the fixture or pad requires re-teaching." : canTeach ? (chosen.kind === "dial" ? "Teach a small turn of the large voicing dial, then lift off and return clear. Each direction has its own path and verification trials." : chosen.kind === "button" ? "Teach this chord button’s lightest reliable press and release. Its musical effect depends on Orchid’s playstyle and reference chord; the button may not sound alone." : leaderMode() ? "Choose any key or control above. Use the leader to teach its route from home and back, then test it three times." : "Choose any key or control above. Teach its local motion by hand, then test it three times.") : "With torque off, capture a supported midpoint, then measure each joint’s usable range. Keep the arm clear of Orchid for the whole calibration.") +
      teachSupport() + actions(button("control_start", leaderMode() ? `Teach ${esc(chosen.name)} & hold →` : `Teach ${esc(chosen.name)} →`, false, `data-control="${esc(chosen.id)}"`) +
        '<button type="button" class="secondary" data-view="calibration">Back to calibration</button>');
    if (complete) html += '<p class="hint"><a href="/api/export" data-export download>Download the session record →</a></p>';
    if (leaderMode()) html += homeReadiness() + '<p class="hint">Teach establishes a hold here without releasing torque. If needed, the next step moves to saved home after a clearance countdown. Then bring the leader close and engage following to record the approach.</p>';
    else html += '<p class="notice" role="status">Hand-guide mode: the leader arm is not connected, so moving it does nothing. To teach with the leader, open <b>Calibrate motors → Leader → Find leader → Connect leader</b> (the follower stays connected), or disconnect and reconnect with <b>Use leader to teach</b>.</p>' +
      '<p class="hint">Support the full weight before releasing torque. Gear resistance can remain with all six motors OFF.</p>';
  } else if (p === "calibration_midpoint") {
    html = intro("Center the six joints.", "Keep the arm supported and clear of the instrument. Place each joint near the middle of its mechanical travel; half-open the gripper. Do not force a joint against a stop.") + support() + actions(button("calibration_center", "Capture midpoint →")) + '<p class="hint">The app checks a steady pose and reads back the new motor references. It does not drive to the midpoint.</p>';
  } else if (p === "calibration_range") {
    const instruction = window.OrchidPanels.guidance[s.range_motor];
    html = intro(`${s.range_index + 1} of 5 · ${esc(motors[s.range_motor])}`, instruction[1]) +
      `<div class="joint-instruction"><span>MOTOR ${s.motor_status[s.range_motor].id} · MATCH THE CYAN HIGHLIGHT IN 3D</span><strong>${instruction[0]}</strong><p>Move this joint by hand with torque off. The 3D guide marks its servo and moving link; other joints may move as needed for support.</p></div>` +
      '<div class="range-panel"><div class="range-values"><span>Minimum <b id="range-min">—</b></span><span>Current <b id="range-current">—</b></span><span>Maximum <b id="range-max">—</b></span></div><progress id="range-progress" max="4095" value="0" aria-label="Recorded joint travel"></progress><p id="range-span" class="range-span">Waiting for movement</p><p id="range-readiness" class="hint">Move gently in both directions.</p></div>' +
      (sim ? actions(button("simulate_sweep", "Simulate joint sweep", true)) : "") + confirm("range_complete", "Both ends of the usable travel are recorded.") + actions(button("calibration_next", s.range_index === 4 ? "Review calibration →" : "Save range & continue →"));
  } else if (p === "calibration_review") {
    html = intro("Review the measured travel.", "Confirm these ranges reflect deliberate sweeps. Wrist rotation uses the full encoder range; there is no cable-twisting sweep.") +
      `<table><thead><tr><th>JOINT</th><th>MIN</th><th>MAX</th></tr></thead><tbody>${Object.entries(motors).map(([name,label]) => `<tr><td>${label}</td><td>${name === "wrist_roll" ? 0 : s.ranges[name]?.min}</td><td>${name === "wrist_roll" ? 4095 : s.ranges[name]?.max}</td></tr>`).join("")}</tbody></table>` + confirm("range_complete", "These measured ranges cover the intended usable travel.") + actions(button("calibration_save", "Save & verify calibration →")) + '<p class="hint">Existing note paths are tied to their original calibration. Changed calibration makes them require re-teaching.</p>';
  } else if (p === "fault" && leaderMode() && canTeach) {
    html = intro("Stopped.", `Stop reason: ${esc(s.error || "Motor feedback is unavailable.")} The follower was asked to hold where it is.`, true) +
      confirm("supported", "I am supporting the follower (torque blinks off for a moment when teaching resumes).", "Support the follower") +
      actions(button("teach_begin", "Hold & keep teaching →", false, `data-control="${esc(chosen.id)}"`) + button("release", "Release torque", true)) +
      '<p class="hint">If the motors are not responding, use the physical power stop while supporting the arm.</p>';
  } else if (p === "fault" && canTeach && (allStatuses()[chosen.id]?.recorded || s.poses_saved?.[chosen.id])) {
    html = intro("Stopped.", `Stop reason: ${esc(s.error || "Motor feedback is unavailable.")} The follower was asked to hold where it is.`, true) +
      confirm("supported", "I am supporting the follower (torque blinks off for a moment when the hold resumes).", "Support the follower") +
      actions(button("teach_begin", "Hold & keep playing →", false, POSES[chosen.id] ? `data-pose="${chosen.id}"` : `data-control="${esc(chosen.id)}"`) + button("release", "Release torque", true)) +
      '<p class="hint">If the motors are not responding, use the physical power stop while supporting the arm.</p>';
  } else if (teachSessionPhases.includes(p)) {
    html = teachWorkflow(s);
  } else if (p === "fault" || p === "failed") {
    html = intro(p === "failed" ? "Let’s teach that stroke again." : "The session has stopped.", p === "failed" ? "This attempt is not registered. Support the arm before releasing torque, then capture a gentler, shorter stroke." : `Stop reason: ${esc(s.error || "Motor feedback is unavailable.")} Support the arm before releasing torque. Motor state is unverified until feedback resumes.`, p === "fault") + support() + actions(button(canTeach ? "retry" : "release", canTeach ? "Release & retry this control" : "Release torque & return to setup")) + '<p class="hint">If the motors are not responding, use the physical power stop while supporting the arm.</p>';
  } else if (p === "home_positioning") {
    html = intro("Position home with the leader.", s.leader_following ? "Move the leader to guide the follower and adjust its gripper opening at a home clear of Orchid. Pause following, then save the pose and opening." : "The follower is holding its current position. Clear your hands from the follower, then choose Arm following → Engage leader following above. Use the leader to position home; pause to capture.") +
      confirm("path_clear", "The follower is at my chosen home, clear of Orchid and obstacles. Save this position as the start and finish of every route.") +
      actions(button("capture_leader_home", `Save home & teach ${esc(s.selected_control.name)} →`)) +
      '<p class="hint">Saving keeps the follower powered and holds the captured gripper opening throughout key teaching. To cancel, support it and use Release or disconnect → Release torque. The previously saved home is kept until this capture succeeds.</p>';
  } else if (p === "home_prepare") {
    html = intro(`Return home before teaching ${esc(s.selected_control.name)}.`, "The follower is holding here. All six joints will move to saved home, including the gripper opening. Clear the entire swept path; the app cannot detect obstacles. If the tips are touching the instrument, use a supported release and lift clear first.") + homeReadiness() +
      '<label class="form-field">Home movement<select id="home-duration"><option value="0">Direct · LeRobot position control</option><option value="1">Timed · 1 second</option><option value="2">Timed · 2 seconds</option><option value="4">Timed · 4 seconds</option><option value="8">Timed · 8 seconds</option></select></label><p class="hint">Direct sends the saved target immediately after the countdown. Actual travel time depends on the motors; the app adds no speed ramp.</p>' +
      confirm("path_clear", "My hands and the entire move to home, including the gripper, are clear.", "Arm move to home") +
      actions(button("move_home", "Move home & teach · 5-second countdown →"));
  } else if (p === "home_moving") {
    html = intro("Moving to home.", "Keep hands clear. Leader positioning becomes available when the home command finishes.") +
      '<progress id="home-move-progress" max="1" value="0" aria-label="Home command progress"></progress>' + homeReadiness();
  } else if (p === "home_arrival") {
    html = intro("Leader positioning is available.", "The home command has finished. Engage the leader above to adjust the follower. The measured pose differs from saved home; if this is your intended clear home, pause and accept it below to start recording the approach.") + homeReadiness() +
      confirm("path_clear", "This is my intended home, clear of Orchid and obstacles. Use this measured pose as the start and finish of my routes.", "Confirm home position") +
      actions(button("capture_leader_home", `Use this pose as home & teach ${esc(s.selected_control.name)} →`)) +
      '<p class="hint">Accepting a new home replaces the saved reference; existing routes will need re-teaching. If the arm reaches the existing reference while paused, teaching continues automatically without changing home.</p>';
  } else if (p === "home_approach" && !s.leader_teaching) {
    html = intro("Establish the hold at home.", "Leave the follower at its taught resting pose and support its weight. The hold starts here, without an alignment move. Then clear your hands and engage the leader to guide the approach.") + homeReadiness() + support() + actions(button("leader_hold", "Establish follower hold →"));
  } else if (p === "home_approach") {
    html = intro(`Guide the approach to ${esc(s.selected_control.name)}.`, "Use the leader to lift from home and follow a clear path to just above the control. The measured approach is recorded. Pause following at clearance before touching the instrument.") +
      confirm("path_clear", "The entire approach stayed clear of Orchid and other obstacles; the tips are now clear above the selected control.") + actions(button("capture_key_clearance", s.selected_control.kind === "dial" ? "Capture dial clearance →" : "Capture hover →")) +
      '<p class="hint">At a resting endpoint, motion farther into that endpoint is blocked. Move away from it. Before re-engaging, bring the leader close to the held follower pose and clear the alignment path.</p>';
  } else if (p === "home_return") {
    html = intro("Teach the return home.", "Use the leader to guide a clear route back to the shared home. Keep clear of Orchid. Pause following at home, then finish the recording. Tests will start and end here.") + homeReadiness() +
      confirm("path_clear", "The entire return route stayed clear of Orchid and other obstacles, and the follower is at home.") + actions(button("capture_home_return", "Capture home & finish route →"));
  } else if (s.selected_control.kind === "dial") {
    html = dialWorkflow(s);
  } else {
    const text = {
      note_ready: ["Hover above the control.", `Position the pad just clear above the selected ${chord ? "button" : "key"}. Keep the gripper opening fixed and capture this hover.`],
      note_hover: ["Make first contact.", `Lower the pad along a short path until it barely touches the ${chord ? "button" : "key"}, without pressing it. Pause and capture first contact.`],
      note_touch: [chord ? "Press the button." : "Press until the note sounds.", "Continue only until the control activates. Pause and capture the press. Playback returns through first contact to hover along the same path in reverse."],
      note_pressed: ["Retreat from the pressed position.", "The arm is holding the captured press. Clear your hands, then choose Retreat to hover. It follows press → contact → hover along the saved path in reverse. It will not press again."],
      retreating: ["Retreating to hover.", "Keep hands clear. The arm is reversing the captured stroke through contact to hover. No downstroke is being repeated."],
      arming: leaderMode() ? ["Maintaining the follower hold.", "Keep hands clear while the powered hold is checked."] : ["Keep supporting the arm.", "Establishing and checking a hold at the captured position. Wait for confirmation before removing your hands."],
      holding: ["Now test one press.", "The arm is holding the captured clearance. Gently take your hands away. This test makes one slow press and release, then holds at clearance."],
      testing: ["One press. One release.", "Keep hands clear. The arm will return along the captured path and hold at clearance. Watch the selected control and verify a clean release."],
      result: [chord ? "Did the chord control respond?" : "Did the intended note sound?", chord ? "Accept only if the intended chord button activated and released cleanly, with the expected display or musical response. Keep the reference chord and playstyle consistent." : sim ? "The simulated press and release finished. Practice accepting or rejecting a trial. This does not verify a physical key." : "Accept only if the intended key sounded once, released cleanly, and the pad and joints stayed steady. Reject excess pressure, a miss, or contact with another key."],
      saved: [s.trials >= 3 ? (chord ? "Button registered." : "Note registered.") : "Trial accepted. Test it again.", s.trials >= 3 ? (leaderMode() ? "Three trials accepted. Continue at home to teach the next control; the follower keeps holding." : "Three trials accepted from this position. Support the arm before releasing torque and moving by hand to the next control.") : "The arm is still holding at clearance. Repeat the test from here; there is no need to capture the same path again."]
    }[p] || ["Waiting for the arm…", s.message];
    html = `<div class="note-title"><span class="note-symbol">${esc(s.selected_control.label)}</span><h3>${text[0]}</h3></div>${noteSteps()}<p class="description">${text[1]}</p>`;
    if (p === "note_ready") html += actions(button("capture_hover", "Capture hover →"));
    if (p === "note_hover") html += actions(button("capture_touch", "Capture first contact →"));
    if (p === "note_touch") html += (leaderMode() ? "" : support()) + actions(button("capture_pressed", leaderMode() ? "Capture press →" : "Capture press & hold →"));
    if (p === "note_pressed") html += confirm("hands_clear", "My hands are clear of the arm and key path.", "Arm retreat") + actions(button("retreat_from_press", "Retreat to hover →"));
    if (p === "holding") html += hands() + actions(button("test", "Test one press →"));
    if (p === "result") html += trials() + actions(button("pass", sim ? "Accept simulated trial ✓" : chord ? "Activated & released cleanly ✓" : "Sounded & released cleanly ✓") + button("fail", "Reject & re-teach", true));
    if (p === "saved") html += trials() + (s.trials >= 3 ? teachSupport() + actions(button("next", leaderMode() ? "Continue at home →" : "Release & continue →")) : hands() + actions(button("test", "Test again →")));
    if (p.startsWith("note_") && !leaderMode()) html += '<p class="notice" role="status">Hand-guide mode: move the follower itself. The leader arm is not connected, so moving it does nothing. To use the leader, support the arm, Release torque, and connect the leader under Calibrate motors → Leader.</p>';
    if (p.startsWith("note_")) html += `<p class="hint">${leaderMode() ? (sim ? "Use Practice leader movement to move the input arm; captures store measured follower positions." : "Move the leader; pause before each capture. The gripper keeps its taught opening.") : sim ? "Simulation places the virtual arm at each capture." : "Move gently. A large jump, changed grip, or stale reading stops capture."}</p>`;
  }
  if (!connectionView && !calibrationView && leaderMode() && s.leader_teaching && teachingPhases.includes(p)) {
    const cue = {note_ready:"Use the leader to position the pad just above the control. Pause, then capture hover.", note_hover:"Hover is captured. Lower the pad to first contact without pressing the control. Pause, then capture contact.", note_touch:"Press only until the control activates. Pause, then capture the press. The return will use this same path in reverse.", note_pressed:"Keep following paused and clear hands from the follower. Choose Retreat to hover to reverse from this pressed position through contact to hover. The press will not repeat.", dial_ready:"The follower is holding clear of the dial. Keep following paused, enter the reference and capture the start."}[p];
    if (cue) html = html.replace(/<p class="description">.*?<\/p>/, `<p class="description">${cue}</p>`);
    html += '<p class="leader-capture-hint">Follower torque is ON. Move the leader only; pause following before every capture.</p>';
  }
  if (!connectionView && !calibrationView && s.home_motion && ["holding","testing","result","saved"].includes(p)) {
    html = html.replace(/<p class="description">.*?<\/p>/, '<p class="description">This full route starts at home, follows the taught approach, performs the control motion, and returns home. Accept only a clear, repeatable route and the intended instrument response.</p>');
    html = html.replaceAll('Test one press →', 'Test full home route →').replaceAll('Test dial gesture →', 'Test full home route →');
  }
  if (!connectionView && p.startsWith("calibration_")) html = `<p class="calibration-arm-label">CALIBRATING ${s.calibration_target === "leader" ? "LEADER · follower stays torque off" : "FOLLOWER"}</p>` + window.OrchidPanels.calibration(s) + html;
  if (!connectionView && p === "connected" && !s.calibrated) html += '<div class="setup-checklist"><strong>Before starting</strong><ul><li>Secure the base and reseated joints; keep the instrument outside the arm’s reach.</li><li>Support the full arm weight before torque releases.</li><li>Use the delay and optional spoken cues to keep both hands available.</li></ul><p>Calibration stays torque off. OFF flags do not remove gearbox drag.</p></div>';
  if (workflowSection() === "notes") html = leaderStrip() + html;
  $("workflow").innerHTML = html;
  if (dialFields[0] !== undefined && $("dial-reference")) $("dial-reference").value = dialFields[0];
  if (dialFields[1] !== undefined && $("dial-effect")) $("dial-effect").value = dialFields[1];
}
function renderConnection() {
  const conflict = operatorError?.status === 409;
  const waiting = conflict && operatorConflictSince !== null && Date.now() - operatorConflictSince < 5000;
  $("connection").textContent = !online ? "Local app unavailable" : needsControllerUpdate() ? "Update not loaded" : owns ?
    (state.connected ? (state.leader?.connected ? "Leader + follower connected" : "Follower connected") : "Local app ready") :
    waiting ? "Waiting for operator control" : conflict ? "Read-only window" : "Operator control unavailable";
  $("connection-warning").hidden = online && owns && !needsControllerUpdate();
  $("connection-warning").textContent = !online ?
    "Connection to the local Python app was lost. Controls are disabled. If a physical arm is active, support it and use the power stop if needed." :
    needsControllerUpdate() ? updateRequiredMessage : owns ? "" : waiting ?
    "Waiting for the previous operator session to expire. Reloading this tab can cause a wait of up to five seconds. Keep this page open; it retries automatically." : conflict ?
    "Another operator session holds control. After that session closes, this page retries automatically within five seconds." :
    `Cannot acquire operator control: ${operatorError?.message || "The control heartbeat did not complete."} Retrying automatically; controls remain disabled.`;
}
function render() {
  if (!state) return;
  const s = state, p = s.phase;
  if (p === "disconnected") { connectionView = false; calibrationView = false; calibrationChoice = null; tuneView = false; }
  const key = [JSON.stringify(connectPlan), JSON.stringify(s.discovery), JSON.stringify(Object.values(arms || {}).map(a => [a.connected, a.available, a.leader])), s.instance_id, s.revision, p, s.range_index, s.trials, s.selected, selectedNote, s.calibrated, s.leader?.calibrated, s.leader_teaching, s.leader_following, s.home?.id, s.home_ready, connectionView, calibrationView, calibrationTarget(), s.teach?.mode, s.teach?.warning, s.teach?.played, s.teach?.returning, s.teach?.going_home, s.teach?.home_saved_at, s.teach?.going_rest, s.teach?.rest_saved_at, JSON.stringify(s.teach_settings), JSON.stringify(s.key_check), JSON.stringify(s.tune), JSON.stringify(s.tune_queue), JSON.stringify(s.tune_results), tuneView, JSON.stringify(s.teach?.points), s.teach?.points_for, powered(s), JSON.stringify(allStatuses()[selectedNote || s.selected])].join(":");
  if (key !== renderKey) {
    cancelCountdown(); clearConfirmations(); renderKey = key; workflow(); calibrationTools(); homeTools(); leaderPanel();
    say(["teach_record", "teach_play", "teach_follow", "dial_ready", "dial_approach", "dial_contact", "dial_turned", "dial_lifted", "note_ready", "note_hover", "note_pressed", "note_touch", "retreating", "holding", "result", "saved", "fault"].includes(p) ? s.message : "");
  }
  renderPorts();
  const receipt = s.last_receipt;
  if (receipt && receipt.id !== captureReceipt) {
    const first = captureReceipt === undefined;
    captureReceipt = receipt.id;  // handled exactly once, before any re-render
    // Every completed capture (including a redo) beeps and flashes its box, so it never looks like nothing happened.
    if (!first && receipt.status === "complete" && sentCaptures[receipt.id]) {
      beep();
      flash = {...sentCaptures[receipt.id], until: Date.now() + 2500};
      targetPoint = null;  // that step is retrained; go back to the normal next step
      delete sentCaptures[receipt.id];
      renderKey = "";
      setTimeout(() => { flash = null; renderKey = ""; if (state) render(); }, 2600);
      return render();
    }
  }
  syncSpeed(s);
  const homeAt = s.teach?.home_saved_at || null, restAt = s.teach?.rest_saved_at || null;
  $("home-control").classList.toggle("flash", flashing("home"));
  $("rest-control").classList.toggle("flash", flashing("rest"));
  $("rest-control-status").textContent = flashing("rest") ? "captured ✓" : restAt ? `set ${new Date(restAt).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}` : s.teach ? "not set" : "rest pose";
  $("home-control-status").textContent = flashing("home") ? "captured ✓" : homeAt ? `set ${new Date(homeAt).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}` : s.teach ? "not set" : "home pose";
  // Find the arms as soon as this page has control; no Refresh click needed.
  if (!isSim() && p === "disconnected" && owns && !autoScanned && !s.discovery?.scanned_at && !s.discovery?.scanning && !s.pending) {
    autoScanned = true; submit("refresh_ports");
  }
  if ($("connection-fault")) $("connection-fault").textContent = s.error || "Motor state is unverified.";
  const count = Object.values(allStatuses()).filter(k => k.status === "registered").length;
  $("completed").innerHTML = `${count}<span>/22</span>`;
  $("mode").textContent = isSim() ? "SIMULATION · NO HARDWARE" : "HARDWARE MODE";
  $("mode").className = `badge ${isSim() ? "" : "hardware"}`;
  renderConnection();
  const section = workflowSection();
  globalThis.OrchidArmView?.setSection(section);
  // The keyboard map is for training; connecting and calibrating do not use it.
  document.querySelector(".instrument-shell").style.display = ["connect", "calibration"].includes(section) ? "none" : "";
  $("calibration-tools").hidden = ["connect", "tune"].includes(section);
  document.body.classList.toggle("calibrating", section === "calibration");
  document.querySelector(".page-heading h1").textContent = section === "calibration" ? "Give the arm its bearings." : section === "tune" ? "Calibrate the keys." : "Teach the instrument.";
  document.querySelector(".page-heading p").textContent = section === "calibration" ? "Guided calibration. Six motors. One joint at a time." :
    section === "tune" ? "Find each trigger point. Set the same approach and pressure. Verify at playing speed." : "Twelve keys. Eight chord buttons. Two voicing gestures.";
  for (const name of ["connect", "calibration", "notes", "tune"]) {
    $("nav-" + name).classList.toggle("active", name === section);
    $("nav-" + name).querySelector("b").textContent = name === "connect" && s.connected || name === "calibration" && referencesReady() || name === "notes" && count === 22 ? "✓" : "";
  }
  $("step-label").textContent = section === "connect" ? "01 / CONNECTION" : section === "calibration" ? "02 / MOTOR CALIBRATION" : section === "tune" ? "04 / KEY CALIBRATION" : "03 / CONTROL TRAINING";
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
  $("keyboard-hint").textContent = leaderMode() && referencesReady() && (setupIdle() || ["teach_hold","teach_follow"].includes(p)) ? "Select a key, chord button, or dial direction, then Teach, Record or Play below." : setupIdle() && referencesReady() ? "Select a key, chord button, or dial direction. Support the arm, then choose Teach below." : "12 keys · 8 chord buttons · 2 voicing directions. Each motion is taught and verified separately.";
  const leaderView = s.leader?.connected && calibrationTarget() === "leader" && section === "calibration";
  $("arm-guide-title").textContent = leaderView ? "SO101 · leader motor guide" : "SO101 · follower guide";
  // Separate display identities prevent a stale leader pose being shown as the follower after a fault.
  window.OrchidPanels.update({...s,...(leaderView ? s.leader : {}),instance_id:`${s.instance_id}:${leaderView ? "leader" : "follower"}`}, online);
  if ($("leader-status")) {
    const l = s.leader, freshLeader = online && l.feedback_at && Date.now()/1000-l.feedback_at < 1.5 && p !== "fault";
    $("leader-recording-error").textContent = s.recording_error || "";
    $("leader-recording-error").hidden = !s.recording_error;
    $("leader-status").textContent = p === "fault" ? "STOPPED · UNVERIFIED" : p === "home_moving" ? "MOVING TO HOME" : s.teach?.mode === "aligning" ? "MATCHING LEADER" : p === "teach_play" ? "PLAYING" : s.leader_following ? "FOLLOWING" : s.leader_teaching ? "PAUSED · HOLDING" : "INPUT IDLE";
    $("leader-torque").textContent = freshLeader && Object.values(l.torque || {}).length === 6 && Object.values(l.torque).every(v=>v===0) ? "6 / 6 TORQUE OFF" : "TORQUE UNVERIFIED";
    $("leader-feedback-status").textContent = p === "fault" ? `Stopped: ${s.error || s.message}` : p === "home_moving" ? "Moving to saved home. Leader input is paused; keep hands clear." : s.leader_boundary_joints?.length ? `Joint boundary: ${s.leader_boundary_joints.map(n=>motors[n]).join(", ")}. Movement farther into the endpoint is blocked; guide away from it.` : s.leader_limited ? "The driver has limited the requested target." : !freshLeader ? "Leader feedback unavailable or paused during verification." : s.teach && s.leader_following ? "Live input. Stop motion (Esc) holds the follower where it is." : s.leader_following ? "Live input · leaving this page pauses following within 1.2 seconds." : "Following is disengaged. Repositioning the leader does not move the follower.";
  }
  if ($("home-move-progress")) $("home-move-progress").value = s.home_move_progress ?? 0;
  if ($("teach-seconds")) $("teach-seconds").textContent = `${(s.teach?.recorded_seconds ?? 0).toFixed(1)} s`;
  if ($("teach-progress")) $("teach-progress").value = s.teach?.progress ?? 0;
  if ($("home-readiness")) $("home-readiness").textContent = s.at_home ? "At home · ready" : `Home offset: ${s.home_error ?? "—"} ticks. ${p === "home_return" ? "Guide back with the leader, then pause." : p === "home_prepare" ? "Confirm clearance below to move to saved home." : p === "home_arrival" ? "Leader positioning is available. Confirm the intended home below before recording." : p === "home_moving" ? "Sending the home movement." : "Use Teach & hold to prepare the move to home."}`;
  const fresh = online && s.connected && s.feedback_at && Date.now()/1000 - s.feedback_at < 1.5 && p !== "fault";
  const flags = fresh ? Object.values(s.torque || {}) : [];
  const off = flags.length === 6 && flags.every(v => v === 0), on = flags.length === 6 && flags.every(v => v === 1);
  $("torque-summary").textContent = off ? "6 / 6 TORQUE OFF" : on ? (s.leader_following ? "TORQUE ON · FOLLOWING" : ["testing","retreating","home_moving","teach_play"].includes(p) ? "TORQUE ON · MOVING" : "TORQUE ON · HOLD") : "UNVERIFIED";
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
  const activityKey = JSON.stringify(s.events.slice(0,12));
  if ($("events").dataset.snapshot !== activityKey) {
    $("events").dataset.snapshot = activityKey;
    $("events").innerHTML = s.events.length ? s.events.slice(0,12).map(e => `<li><time datetime="${esc(e.created)}">${esc(new Date(e.created).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}))}</time><div><span ${["fault","rejected","connection_failed","hold_failed","worker_failed","cleanup_failed"].includes(e.kind) ? "data-error-copy" : ""}>${esc(e.message)}</span></div></li>`).join("") : '<li><span>No session activity yet. Connect the practice arm to begin.</span></li>';
  }
  $("operation").hidden = !s.pending; $("operation").textContent = s.discovery?.scanning ? "Reading connected robot arms…" : s.message;
  if (s.last_receipt?.id !== lastReceipt || s.error) {
    lastReceipt = s.last_receipt?.id;
    error(s.error || (s.last_receipt?.status === "rejected" ? s.last_receipt.message : ""));
  }
  updateButtons();
}
let selectedNote = null, autoScanned = false, captureReceipt, flash = null, targetPoint = null;
const turnInput = {}, pressInput = {};  // values typed but not yet played, kept across page refreshes
const sentCaptures = {};  // command id -> which point it captured
const flashing = (point, control) => flash && flash.until > Date.now() && flash.point === point && (!control || flash.control === control);
function beep() {
  try {
    const audio = beep.audio || (beep.audio = new AudioContext()), tone = audio.createOscillator(), level = audio.createGain();
    tone.frequency.value = 880; level.gain.setValueAtTime(0.15, audio.currentTime); level.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.15);
    tone.connect(level).connect(audio.destination); tone.start(); tone.stop(audio.currentTime + 0.15);
  } catch { /* no audio available */ }
}
function updateButtons() {
  const blocked = !online || !owns || state?.worker_alive === false || sending || state?.pending || !!timer;
  document.querySelectorAll("[data-confirm]").forEach(b => { b.disabled = blocked; });
  for (const id of Object.keys(topSliders)) if ($(id)) $(id).disabled = !online || !owns || state?.worker_alive === false;
  document.querySelectorAll("[data-action]").forEach(b => {
    const scope = actionScope(b);
    let valid = [...scope.querySelectorAll("[data-confirm]")].filter(c => c.dataset.confirm !== "fixture_unchanged" && c.closest?.("[hidden]")?.hidden !== true).every(confirmed);
    if (b.dataset.recovery) valid = confirmed($("recovery-supported"));
    if (b.dataset.connection) valid = valid && state.connected && !state.leader_following;
    if (b.dataset.unavailable) valid = valid && state.connected && state.phase === "fault";
    if (["capture_home","home_start"].includes(b.dataset.action)) valid = valid && setupIdle() && referencesReady();
    if (b.dataset.action === "capture_leader_home") valid = valid && ["home_positioning","home_arrival"].includes(state.phase);
    if (leaderMode() && b.dataset.action === "control_start") valid = valid && state.home_ready;
    if (b.dataset.action === "leader_hold") valid = valid && state.home_ready && state.at_home;
    if (b.dataset.action === "move_home") valid = valid && state.phase === "home_prepare" && state.leader_teaching;
    if (b.dataset.action === "capture_home_return") valid = valid && state.at_home;
    if (b.dataset.calibration) {
      valid = calibrationPhases.includes(state.phase) && confirmed(scope.querySelector('[data-confirm="supported"]'));
      if (b.dataset.action === "calibration_reload") valid = valid && !!(calibrationTarget() === "leader" ? state.leader?.calibration : state.calibration) && confirmed(scope.querySelector('[data-confirm="calibration_unchanged"]'));
    }
    if (["simulate_sweep", "fail", "refresh_ports", "refresh_leader_ports"].includes(b.dataset.action)) valid = true;
    if (b.dataset.action === "connect_leader") valid = valid && setupIdle() && !state.leader?.connected && state.discovery?.ports.some(p => p.path === $("leader-port")?.value && p.leader_connectable);
    if (b.dataset.action === "connect") valid = valid && state.discovery?.ports.some(p => p.path === $("port")?.value && p.connectable);
    if (b.dataset.action === "connect" && $("teaching-mode")?.value === "leader") valid = valid && state.discovery?.ports.some(p => p.path === $("leader-port")?.value && p.leader_connectable);
    if (b.dataset.action === "leader_pause") valid = !!state.leader_following;
    if (b.dataset.action === "leader_resume") valid = valid && state.leader_teaching && !state.leader_following && teachingPhases.includes(state.phase);
    if (b.dataset.action === "simulate_leader") valid = isSim() && state.leader_teaching && !state.simulated_leader_input;
    if (b.dataset.action === "teach_record") valid = valid && state.teach?.mode === "following";
    if (b.dataset.action === "move_leader") valid = true;  // moves no arm: the other arm only stops following and holds
    if (b.dataset.action === "tune_start") valid = valid && state.phase === "teach_hold" && b.dataset.controls.split(",").every(k => allStatuses()[k]?.recorded);
    if (tuning() && !b.dataset.recovery && !b.dataset.connection) valid = b.dataset.action === "tune_stop";
    if (b.dataset.action === "teach_capture") valid = valid && canCapture() && !b.dataset.locked;
    if (["teach_set_home", "teach_set_rest"].includes(b.dataset.action)) valid = valid && canCapture();
    if (b.dataset.action === "teach_go_home") valid = valid && !!state.teach?.home_saved && ["teach_hold", "teach_follow"].includes(state.phase);
    if (b.dataset.action === "teach_go_rest") valid = valid && !!state.teach?.rest_saved && ["teach_hold", "teach_follow"].includes(state.phase);
    if (leaderMode() && b.dataset.action !== "capture_home" && (b.dataset.action.startsWith("capture_") || b.dataset.action.startsWith("dial_capture_"))) valid = valid && state.leader_teaching && !state.leader_following;
    if (b.dataset.diagnostics) valid = state.connected && ["connected", "ready"].includes(state.phase) && Object.values(state.torque || {}).length === 6 && Object.values(state.torque).every(v => v === 0);
    if (b.dataset.action === "calibration_next") { const range = state.ranges[state.range_motor]; valid = valid && range && range.max - range.min > 32; }
    b.disabled = blocked || !valid || (needsControllerUpdate() && !recoveryAction(b.dataset.action));
  });
  if ($("port")) $("port").disabled = blocked || !state.discovery?.ports.some(p => p.connectable);
  if ($("leader-port")) $("leader-port").disabled = blocked || !state.discovery?.ports.some(p=>p.leader_connectable);
  if ($("teaching-mode")) {
    $("teaching-mode").disabled = blocked;
    $("leader-port-field").hidden = $("teaching-mode").value !== "leader";
    if ($("manual-connect-extras")) $("manual-connect-extras").hidden = $("teaching-mode").value === "leader";
    if ($("leader-connect-hint")) $("leader-connect-hint").hidden = $("teaching-mode").value !== "leader";
    $("leader-port").disabled = blocked || !state.discovery?.ports.some(p=>p.leader_connectable);
  }
  if ($("refresh-ports")) $("refresh-ports").textContent = state.discovery?.scanning ? "Scanning…" : "↻ Refresh connections";
  document.querySelectorAll("[data-target-point]").forEach(b => b.disabled = blocked || !!b.dataset.locked || !["teach_hold", "teach_follow"].includes(state.phase));
  document.querySelectorAll("[data-note]").forEach(b => {
    const elsewhere = !POSES[b.dataset.note] && !!state.owns && !state.owns.includes(b.dataset.note);
    b.classList.toggle("other-arm", elsewhere);
    b.title = elsewhere ? `Played by the ${ARM_SHORT[arm === "a" ? "b" : "a"]}` : "";
    b.disabled = blocked || elsewhere || !referencesReady() || !(setupIdle() || ["teach_hold","teach_follow"].includes(state.phase));
  });
  document.querySelectorAll("[data-view]").forEach(b => b.disabled = blocked ||
    (b.dataset.view !== "connect" && (!state.connected || (b.dataset.view !== "current" && state.leader_following))) ||
    (b.dataset.view === "notes" && (!setupIdle() || !referencesReady())) ||
    (b.dataset.view === "tune" && (!referencesReady() || tuning())));
  document.querySelectorAll("[data-calibration-target]").forEach(b => b.disabled = blocked || !setupIdle());
  // Stop reaches every follower, so it works whichever arm is shown.
  $("stop").disabled = !online || !owns || !(state?.connected || Object.values(arms || {}).some(a => a.connected));
  document.querySelectorAll("[data-note]").forEach(k => k.classList.toggle("selected", k.dataset.note === (selectedNote || state.selected)));
}
function cancelCountdown() { if (timer) clearInterval(timer); timer = null; $("countdown").hidden = true; }
async function submit(action, args = {}, revision = state.revision) {
  if (needsControllerUpdate() && !recoveryAction(action)) { error(updateRequiredMessage); return; }
  sending = true; error(""); updateButtons();
  const id = crypto.randomUUID();
  const point = action === "teach_capture" ? args.point : action === "teach_set_home" ? "home" : action === "teach_set_rest" ? "rest" : null;
  if (point) sentCaptures[id] = {point, control: args.control || null};
  try { await request("/api/commands", {id, action, args, revision, arm}); if (!POSES[selectedNote]) selectedNote = null; }
  catch (err) { error(err.message); }
  finally { sending = false; updateButtons(); }
}
async function playAfterHold(args) {
  await submit("teach_begin", args);
  for (let i = 0; i < 25 && state.phase !== "teach_hold"; i++) await new Promise(r => setTimeout(r, 200));
  // Speed comes from the shared Arm speed setting.
  if (state.phase === "teach_hold") await submit(args.pose ? `teach_go_${args.pose}` : "teach_play", args.pose ? {} : {control: args.control});
}
function dispatchButton(b) {
  const action = b.dataset.action;
  const args = {};
  const scope = actionScope(b);
  if (!b.dataset.diagnostics) scope.querySelectorAll("[data-confirm]").forEach(c => args[c.dataset.confirm] = confirmed(c));
  if (b.dataset.recovery) args.supported = confirmed($("recovery-supported"));
  if (action === "connect" && $("teaching-mode").value === "leader") { args.prepared = true; args.fixture_unchanged = true; }  // reading only; the saved placement is kept
  if (action === "connect") { args.fixture = $("fixture").value; args.port = $("port").value; args.tool = $("contact-tool").value; args.teaching_mode = $("teaching-mode").value; args.leader_port = $("leader-port").value; }
  if (action === "connect_leader") args.leader_port = $("leader-port").value;
  if (action === "calibrate") args.target = b.dataset.target || "follower";
  if (["calibration_reset","calibration_reload"].includes(action)) args.target = calibrationTarget();
  if (action === "simulate_leader") { args.motor = $("leader-sim-joint").value; args.delta = Number(b.dataset.delta); }
  if (action === "control_start") args.control = b.dataset.control;
  if (action === "tune_start") args.controls = b.dataset.controls.split(",");
  // Every teaching command names the key selected on the map, so the app never falls back to the previous key.
  if (["teach_begin","teach_follow","teach_hold","teach_record","teach_play","teach_capture"].includes(action)) args.control = targetControl();
  if (action === "teach_play") { if (b.dataset.speed) args.speed = Number(b.dataset.speed); args.force = b.dataset.force === "true"; }
  if (action === "teach_play" && $("turn-degrees")) args.turn_degrees = Number($("turn-degrees").value);
  if (action === "teach_play" && $("press-seconds")) args.press_s = Number($("press-seconds").value);
  if (action === "teach_capture") args.point = b.dataset.point;
  if (action === "teach_begin" && b.dataset.follow === "true") args.follow = true;
  if (action === "find_connect_all") { findConnectAll(); return; }
  if (action === "move_leader") { moveLeader(); return; }
  if (action === "teach_begin" && b.dataset.pose) args.pose = b.dataset.pose;
  if (action === "teach_begin" && b.dataset.play) {
    clearConfirmations(scope);
    playAfterHold(args);
    return;
  }
  if (action === "move_home") { args.duration = Number($("home-duration").value); args.hands_clear = args.path_clear; }
  if (action === "home_start") args.control = selectedNote || state.selected;
  if (action === "dial_capture_start") { args.reference = $("dial-reference").value; args.expected_effect = $("dial-effect").value; }
  // Consent is for this attempt only, including canceled countdowns and errors.
  if (!b.dataset.diagnostics && !["refresh_ports","refresh_leader_ports"].includes(action)) clearConfirmations(scope);
  const revision = state.revision;
  const delayed = ["home_start","capture_home","capture_key_clearance","capture_home_return","leader_hold","calibrate","calibration_reset","calibration_reload","calibration_center","control_start","dial_capture_start","dial_capture_contact","dial_capture_turn","dial_capture_lift","dial_capture_return","capture_hover","capture_pressed","capture_touch","retreat_from_press","next","release","disconnect","retry"].includes(action);
  const alignment = action === "leader_resume", homing = action === "move_home";
  if (alignment || homing || ($("delay").checked && delayed)) {
    let left = 5;
    const title = alignment ? "Follower aligns to leader · bring the leader close and clear the path" : homing ? "Follower moves to home · clear hands and the whole path" : b.textContent;
    $("countdown").hidden = false;
    const draw = () => { $("countdown").innerHTML = `<div><strong>${left}</strong> seconds<br><small>${esc(title)}${alignment || homing ? "" : " · hold steady"}</small></div><button id="cancel-countdown" class="secondary">Cancel</button>`; };
    draw(); say(`${title} in five seconds.${alignment || homing ? "" : " Support the arm."}`);
    timer = setInterval(() => {
      if (!online || !owns || document.hidden || needsControllerUpdate() || state.pending || state.worker_alive === false || state.revision !== revision) { cancelCountdown(); updateButtons(); return; }
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
  else if (b.dataset.confirm) { setConfirmation(b, !confirmed(b)); updateButtons(); }
  else if (b.dataset.view) { connectionView = b.dataset.view === "connect"; calibrationView = b.dataset.view === "calibration"; tuneView = b.dataset.view === "tune"; calibrationChoice = null; render(); }
  else if (b.dataset.calibrationTarget) { calibrationChoice = b.dataset.calibrationTarget; render(); }
  else if (b.dataset.action) dispatchButton(b);
  else if (b.dataset.targetPoint) {
    const control = targetControl(), point = b.dataset.targetPoint;
    targetPoint = targetPoint?.control === control && targetPoint.point === point ? null : {control, point};
    renderKey = ""; render();
  }
  else if (b.dataset.arm) {
    const training = !!state && workflowSection() === "notes";
    arm = b.dataset.arm; try { localStorage.setItem("orchid.arm", arm); } catch { /* storage unavailable */ }
    selectedNote = null; targetPoint = null; connectionView = calibrationView = tuneView = false; renderKey = ""; keyboardKey = "";
    renderArms();
    // On the teach screen the leader follows the arm you select: the other arm stops following and holds where it is.
    const chosen = arms?.[arm], holder = Object.entries(arms || {}).some(([id, a]) => id !== arm && a.leader);
    if (training && chosen?.connected && chosen.calibrated && !chosen.leader && holder) moveLeader();
  }
  else if (b.dataset.note) { selectedNote = b.dataset.note; targetPoint = null; connectionView = false; calibrationView = false; tuneView = false; render(); }
});
document.addEventListener("change", event => { if (event.target.id === "teaching-mode") rememberMode(event.target.value); updateButtons(); });
document.addEventListener("input", event => {
  if (event.target.id === "turn-degrees") turnInput[targetControl()] = event.target.value;
  if (event.target.id === "press-seconds") pressInput[targetControl()] = event.target.value;
});
// Arm speed and press hardness are global settings (top bar): dragging previews, letting go saves (no motion).
const topSliders = {
  "arm-speed": {setting: "speed", fallback: 1, label: v => `${Number(v).toFixed(2).replace(/\.?0+$/, "")}×`, toSetting: Number},
  "press-hardness": {setting: "press_hardness", fallback: 0.5, label: v => `${Math.round(v)}%`,
    toSetting: v => Number(v) / 100, fromSetting: v => Math.round(v * 100)},
};
const sliderEditing = {};
function syncSpeed(s) {
  for (const [id, slider] of Object.entries(topSliders)) {
    if (!$(id) || sliderEditing[id]) continue;
    const value = s.teach_settings?.[slider.setting] ?? slider.fallback, shown = slider.fromSetting ? slider.fromSetting(value) : value;
    $(id).value = String(shown);
    $(id + "-value").textContent = slider.label(shown);
  }
}
document.addEventListener("input", event => {
  const slider = topSliders[event.target.id];
  if (!slider) return;
  sliderEditing[event.target.id] = true;
  $(event.target.id + "-value").textContent = slider.label(event.target.value);
});
document.addEventListener("change", async event => {
  const slider = topSliders[event.target.id];
  if (!slider) return;
  event.target.blur();  // so Space presses the highlighted step again
  await submit("teach_settings", {[slider.setting]: slider.toSetting(event.target.value)});
  sliderEditing[event.target.id] = false;
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    cancelCountdown(); clearConfirmations();
    if (online && owns && token) request("/api/heartbeat", {leader_visible:false}).catch(()=>{});
    updateButtons();
  }
});
document.addEventListener("keydown", event => {
  // Space presses the highlighted (primary) button, so one hand can stay on the leader.
  if (event.key !== " " || event.repeat || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  event.preventDefault();  // also stops a focused button from being clicked a second time
  document.activeElement?.blur?.();
  const primary = document.querySelector("#workflow .actions button.primary");
  if (primary && !primary.disabled) primary.click();
});
document.addEventListener("keydown", event => { if (event.key === "Escape") { cancelCountdown(); if (!$("stop").disabled) $("stop").click(); updateButtons(); } });
async function poll() {
  try {
    const session = await request(`/api/session?arm=${arm}`);
    // A restarted app may serve a newer page; reload so the page and controller always match.
    if (instances[arm] && instances[arm] !== session.state.instance_id) { cancelCountdown(); location.reload(); return; }
    instances[arm] = instance = session.state.instance_id; token = session.token; state = session.state; online = state.worker_alive !== false;
    arms = session.arms; connectPlan = session.connect_plan;
    if (arm !== "a" && !arms?.[arm]?.available) { arm = "a"; return; }
    globalThis.OrchidArmView?.setArms(arms);
    renderArms();
    for (const link of document.querySelectorAll("[data-export]")) link.href = `/api/export?arm=${arm}`;
    try { await request("/api/heartbeat", {leader_visible:!document.hidden}); owns = true; operatorError = null; operatorConflictSince = null; }
    catch (err) {
      owns = false; operatorError = err;
      operatorConflictSince = err.status === 409 ? (operatorConflictSince ?? Date.now()) : null;
      cancelCountdown(); clearConfirmations();
    }
    if (firstLoad) { $("delay").checked = !isSim(); firstLoad = false; }
    render();
  } catch {
    online = false; owns = false; cancelCountdown(); clearConfirmations();
    if (state) render(); else { $("connection-warning").hidden = false; $("connection-warning").textContent = "Cannot reach the local Python app. Start python app.py, then keep this page open to reconnect."; }
  } finally { setTimeout(poll, 500); }
}
poll();
