/* Display-only calibration targets. Motor IDs follow the SO101 bus mapping. */
(function(root) {
  'use strict';
  const motors = {
    shoulder_pan:{id:1,label:'Base rotation',action:'Turn the base left and right.',landmark:'The lowest servo, mounted in the base.',camera:[-.95,.48]},
    shoulder_lift:{id:2,label:'Shoulder',action:'Raise and lower the upper arm.',landmark:'The servo just above the base, at the upper arm’s first hinge.',camera:[-1.15,.3]},
    elbow_flex:{id:3,label:'Elbow',action:'Bend and extend the elbow.',landmark:'The middle hinge joining the upper arm and forearm.',camera:[-1.2,.35]},
    wrist_flex:{id:4,label:'Wrist bend',action:'Tilt the wrist up and down.',landmark:'The hinge at the end of the forearm, before the rotating wrist.',camera:[-1.05,.48]},
    wrist_roll:{id:5,label:'Wrist rotation',action:'No sweep is needed for this joint.',landmark:'The servo immediately behind the gripper. Calibration uses its full encoder range.',camera:[-1.1,.5]},
    gripper:{id:6,label:'Gripper',action:'Open and close the jaws gently.',landmark:'The servo inside the gripper and the moving finger’s hinge.',camera:[-1.35,.48]}
  };
  function target(state, selected='shoulder_pan') {
    const phase=state?.phase;
    if(phase==='fault'||phase==='failed') return {mode:'paused',active:null,badge:'PAUSED',title:'Support the arm',action:'Resolve the stopped session before continuing.',landmark:'The view is for identification only.'};
    if(phase==='calibration_midpoint') return {mode:'midpoint',active:null,badge:'1–6',title:'Position all six motors',action:'Center the joints; half-open the gripper.',landmark:'Use the numbered markers to locate each joint. This is a reference pose until midpoint capture.'};
    if(phase==='calibration_review') return {mode:'review',active:null,badge:'1–6',title:'Review all six motors',action:'Check the recorded travel before saving.',landmark:'Motor 5 · wrist rotation uses the full range; it is not swept.'};
    if((phase==='connected'&&!state.calibrated)||phase==='calibration_prepare') return {mode:'prepare',active:null,badge:'SO101',title:'Locate the six motors',action:'Support the arm and clear the workspace.',landmark:'Begin calibration to capture the midpoint, then follow one highlighted joint at a time.'};
    const active=phase==='calibration_range' ? state.range_motor : selected;
    const motor=motors[active]||motors.shoulder_pan;
    return {mode:phase==='calibration_range'?'sweep':'inspect',active:motors[active]?active:'shoulder_pan',
      badge:String(motor.id).padStart(2,'0'),title:`Motor ${motor.id} · ${motor.label}`,
      action:phase==='calibration_range'?motor.action:'Inspect this motor in the 3D view.',landmark:motor.landmark,camera:motor.camera};
  }
  const api={motors,target};
  if(typeof module!=='undefined')module.exports=api;else root.OrchidArmGuide=api;
})(typeof window==='undefined'?globalThis:window);
