/* Calibration guidance and measured motor status, separate from motion controls. */
(function() {
  'use strict';
  const $=id=>document.getElementById(id);
  const names={shoulder_pan:'Base rotation',shoulder_lift:'Shoulder',elbow_flex:'Elbow',wrist_flex:'Wrist bend',wrist_roll:'Wrist rotation',gripper:'Gripper'};
  const guidance={
    shoulder_pan:['Turn the base left and right.','Support the upper arm and rotate the base gently through the usable sweep. Keep the cables slack and the whole arm clear of Orchid.'],
    shoulder_lift:['Raise and lower the upper arm.','Support the elbow and tool end while moving the shoulder. Keep the base secure; stop before the arm strains or touches the table.'],
    elbow_flex:['Bend and extend the elbow.','Support both sides of the elbow. Fold and unfold gently through the usable range without forcing the joint or its reseated attachment.'],
    wrist_flex:['Tilt the wrist up and down.','Support the gripper and forearm. Flex the wrist gently in both directions with the rubber tips clear of every surface.'],
    gripper:['Open and close the gripper gently.','Keep fingers clear of the jaws. Record the usable opening without crushing the rubber tips or forcing the mechanism. Set the final contact opening after calibration.']
  };
  let selected='shoulder_pan';
  function calibration(s) {
    const active=s.phase==='calibration_midpoint'?1:s.phase==='calibration_range'?s.range_index+2:s.phase==='calibration_review'?7:s.calibrated?8:0;
    const labels=['Prepare','Midpoint','Base','Shoulder','Elbow','Wrist bend','Gripper','Verify'];
    return `<div class="calibration-guide"><div class="guide-heading"><span>MOTOR CALIBRATION</span><strong>${active===8?'Verified':`Step ${active+1} / 8`}</strong></div><ol class="calibration-steps">${labels.map((text,i)=>`<li class="${i<active?'done':i===active?'current':''}" ${i===active?'aria-current="step"':''}><b>${i<active?'✓':i+1}</b><span>${text}</span></li>`).join('')}</ol><p>Support → torque off → midpoint → five joint sweeps → verified save. Wrist rotation is not swept.</p></div>`;
  }
  function init() {
    $('motors').innerHTML=Object.entries(names).map(([name,label],i)=>`<div class="motor-detail" id="motor-${name}"><div class="motor-top"><button type="button" class="motor-select" data-joint="${name}" aria-label="Highlight ${label} in 3D"><span>${String(i+1).padStart(2,'0')}</span> ${label}</button><span class="motor-torque">UNKNOWN</span></div><div class="motor-reading"><strong class="motor-ticks">—</strong><span>ticks</span><span class="motor-angle">No midpoint</span></div><meter class="motor-range" min="0" max="4095" value="2047" aria-label="${label} position in recorded range"></meter><div class="motor-limits"><span class="motor-min">—</span><span class="motor-limit-label">No range</span><span class="motor-max">—</span></div><div class="motor-health"><span class="motor-volts">— V</span><span class="motor-temp">— °C</span><span class="motor-error">No powered target</span></div></div>`).join('');
    document.querySelectorAll('[data-joint]').forEach(b=>b.addEventListener('click',()=>{selected=b.dataset.joint;if(window.OrchidPanels.last)update(...window.OrchidPanels.last);}));
  }
  function update(s,online) {
    window.OrchidPanels.last=[s,online];
    const fresh=online&&s.connected&&s.phase!=='fault'&&s.feedback_at&&Date.now()/1000-s.feedback_at<1.5;
    const target=window.OrchidArmGuide.target(s,selected);
    $('motor-status-label').textContent=fresh?(s.mode==='simulation'?'Simulated readback':'Live readback'):'Readback unavailable';
    for(const [name,label] of Object.entries(names)) {
      const row=$('motor-'+name),data=s.motor_status?.[name]||{};
      row.classList.toggle('active',target.active===name);row.classList.toggle('stale',!fresh);
      row.querySelector('.motor-select').setAttribute('aria-pressed',String(target.active===name));
      row.querySelector('.motor-select').disabled=s.phase?.startsWith('calibration_');
      const torque=row.querySelector('.motor-torque');torque.textContent=!fresh||data.torque_enabled===null||data.torque_enabled===undefined?'UNKNOWN':data.torque_enabled?'ON':'OFF';torque.classList.toggle('on',fresh&&data.torque_enabled===true);
      row.querySelector('.motor-ticks').textContent=data.position_ticks??'—';
      row.querySelector('.motor-angle').textContent=data.degrees_from_midpoint!==null&&data.degrees_from_midpoint!==undefined?`${data.degrees_from_midpoint.toFixed(1)}° from midpoint`:'No midpoint reference';
      const meter=row.querySelector('meter');const validRange=Number.isFinite(data.range_min)&&data.range_max>data.range_min;
      meter.min=validRange?data.range_min:0;meter.max=validRange?data.range_max:4095;meter.value=data.position_ticks??2047;meter.classList.toggle('unreferenced',!validRange);
      meter.setAttribute('aria-valuetext',`${label}: ${data.position_ticks??'unknown'} ticks; ${data.limit_status||'unreferenced'}`);
      row.querySelector('.motor-min').textContent=data.range_min??'—';row.querySelector('.motor-max').textContent=data.range_max??'—';
      row.querySelector('.motor-limit-label').textContent=({recording:'Recorded range',pending_range:name==='wrist_roll'?'No sweep needed':'Awaiting sweep',unreferenced:'No verified range',within:'Within working margin',near_limit:'Near calibrated limit',outside:'Outside calibrated range'})[data.limit_status]||'No verified range';
      row.classList.toggle('limit-warning',['near_limit','outside'].includes(data.limit_status));
      row.querySelector('.motor-volts').textContent=Number.isFinite(data.voltage_v)?`${data.voltage_v.toFixed(1)} V`:'— V';
      row.querySelector('.motor-temp').textContent=Number.isFinite(data.temperature_c)?`${data.temperature_c} °C`:'— °C';
      row.querySelector('.motor-error').textContent=fresh&&Number.isFinite(data.tracking_error_ticks)?`Goal ${data.target_ticks} · Δ ${data.tracking_error_ticks} ticks`:'No live powered target';
    }
    $('diagnostics-age').textContent=s.diagnostics_error?`Health read unavailable: ${s.diagnostics_error}`:s.diagnostics_at?`Voltage / temperature snapshot · ${new Date(s.diagnostics_at*1000).toLocaleTimeString()} · ${Math.floor(Math.max(0,Date.now()/1000-s.diagnostics_at))}s ago`:'Voltage and temperature have not been sampled. Refresh while idle with torque off.';
    $('arm-canvas').setAttribute('aria-label',`Read-only 3D SO101 arm view. ${target.title}. ${fresh&&s.pose_angles?'Encoder-based pose estimate.':'No live referenced pose.'} Drag to orbit, or use arrow keys. Plus and minus zoom. Camera controls never move the arm.`);
    window.OrchidArmView?.update(s,online,selected);
    const range=s.ranges?.[s.range_motor];
    if($('range-span'))$('range-span').textContent=range?`${range.max-range.min} ticks recorded`:'Waiting for movement';
    if($('range-current'))$('range-current').textContent=s.position?.[s.range_motor]??'—';
    if($('range-readiness'))$('range-readiness').textContent=range&&range.max-range.min>32?'Travel recorded. Confirm that both directions are represented.':'Move gently in both directions. A span greater than 32 ticks is required; never force travel to meet it.';
  }
  window.OrchidPanels={calibration,guidance,update,last:null};init();
})();
