const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function consoleHarness() {
  const listeners = {}, commands = [], elements = {};
  function button(id, dataset = {}) {
    const attrs = {'aria-pressed':'false'};
    const children = {'.confirmation-icon':{}, '.confirmation-state':{}};
    return {id, dataset, disabled:false, textContent:id, getAttribute:k=>attrs[k],
      setAttribute:(k,v)=>attrs[k]=v, querySelector:s=>children[s], closest(){return this;}};
  }
  const consent = button('consent', {confirm:'supported'});
  const recovery = button('recovery-supported', {confirm:'supported'});
  const action = button('calibrate', {action:'calibrate'});
  const disconnect = button('disconnect', {action:'disconnect', recovery:'true'});
  const optional = button('placement', {confirm:'fixture_unchanged'});
  const calibrationSupport = button('calibration-support', {confirm:'supported'});
  const unchanged = button('unchanged', {confirm:'calibration_unchanged'});
  const reset = button('reset', {action:'calibration_reset', calibration:'true'});
  const reload = button('reload', {action:'calibration_reload', calibration:'true'});
  const leaderClear = button('leader-clear', {confirm:'hands_clear'});
  const resume = button('resume', {action:'leader_resume', leader:'true'});
  const pause = button('pause', {action:'leader_pause', leader:'true'});
  const capture = button('capture', {action:'capture_pressed'});
  const calibrationNav = button('nav-calibration', {view:'calibration'});
  const trainingNav = button('nav-notes', {view:'notes'});
  const leaderChoice = button('choose-leader', {calibrationTarget:'leader'});
  Object.assign(elements, {
    'workflow':{querySelectorAll:()=>[consent, optional]},
    'recovery':{querySelectorAll:()=>[recovery]},
    'calibration-tools':{querySelectorAll:()=>[calibrationSupport,unchanged],
      querySelector:s=>s.includes('calibration_unchanged') ? unchanged : calibrationSupport},
    'leader-teaching':{querySelectorAll:()=>[leaderClear]},
    'recovery-supported':recovery, 'stop':button('stop'),
    'delay':{checked:false}, 'speech':{checked:false}, 'countdown':{}, 'error':{},
    'connection':{}, 'connection-warning':{}
  });
  const document = {
    getElementById:id=>elements[id], addEventListener:(event,fn)=>listeners[event]=fn,
    querySelectorAll:selector=>selector === '[data-action]' ? [action,disconnect,reset,reload,resume,pause,capture]
      : selector === '[data-confirm]' ? [consent,optional,recovery,calibrationSupport,unchanged,leaderClear]
      : selector === '[data-view]' ? [calibrationNav,trainingNav]
      : selector === '[data-calibration-target]' ? [leaderChoice] : []
  };
  let fail = false;
  const context = vm.createContext({document, crypto:webcrypto, AbortSignal,
    setTimeout:()=>{}, setInterval:()=>1, clearInterval:()=>{},
    fetch:async (path, options) => {
      if (path === '/api/session') return new Promise(()=>{}); // No background poll in this unit harness.
      commands.push(JSON.parse(options.body));
      return fail ? {ok:false,text:async()=>JSON.stringify({detail:'Capture rejected'})}
        : {ok:true,json:async()=>({status:'queued'})};
    }
  });
  vm.runInContext(readFileSync('orchid_demo/static/app.js','utf8'),context);
  vm.runInContext(`state={phase:'connected',connected:true,calibrated:false,worker_alive:true,revision:1,pending:false};
    token='test-token';online=true;owns=true;updateButtons();`,context);
  return {consent,optional,recovery,action,disconnect,calibrationSupport,unchanged,reset,reload,leaderClear,resume,pause,capture,calibrationNav,trainingNav,leaderChoice,elements,commands,context,listeners,
    fail:()=>{fail=true;}, click:b=>listeners.click({target:b})};
}

test('large confirmation toggles unlock the action without sending a motor command',()=>{
  const ui = consoleHarness();
  assert.equal(ui.action.disabled,true);
  ui.click(ui.consent);
  assert.equal(ui.consent.getAttribute('aria-pressed'),'true');
  assert.equal(ui.action.disabled,false);
  assert.equal(ui.commands.length,0);
  ui.click(ui.consent);
  assert.equal(ui.action.disabled,true);
  assert.equal(ui.commands.length,0);
});

test('confirmation applies once and is consumed even if the server rejects the attempt',async()=>{
  const ui = consoleHarness();
  ui.fail();
  ui.click(ui.consent);
  ui.click(ui.action);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].action,'calibrate');
  assert.equal(ui.commands[0].args.supported,true);
  assert.equal(ui.consent.getAttribute('aria-pressed'),'false');
  assert.equal(ui.action.disabled,true);
  assert.equal(ui.elements.error.textContent,'Capture rejected');
});

test('canceling a countdown requires a fresh confirmation',()=>{
  const ui = consoleHarness();
  ui.elements.delay.checked=true;
  ui.click(ui.consent);
  ui.click(ui.action);
  assert.equal(ui.commands.length,0);
  assert.equal(ui.consent.disabled,true);
  ui.click({id:'cancel-countdown',closest(){return this;}});
  assert.equal(ui.consent.disabled,false);
  assert.equal(ui.action.disabled,true);
  assert.equal(ui.commands.length,0);
});

test('recovery consent is independent and consumed on disconnect',async()=>{
  const ui = consoleHarness();
  ui.click(ui.consent);
  assert.equal(ui.disconnect.disabled,true);
  ui.click(ui.recovery);
  assert.equal(ui.disconnect.disabled,false);
  ui.click(ui.disconnect);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].args.supported,true);
  assert.equal(ui.recovery.getAttribute('aria-pressed'),'false');
  assert.equal(ui.disconnect.disabled,true);
});

test('read-only or unavailable console disables confirmation buttons',()=>{
  const ui = consoleHarness();
  vm.runInContext('owns=false;updateButtons();',ui.context);
  assert.equal(ui.consent.disabled,true);
  ui.click(ui.consent);
  assert.equal(ui.consent.getAttribute('aria-pressed'),'false');
  assert.equal(ui.commands.length,0);
});

test('reset has independent support consent and does not require the reload confirmation',async()=>{
  const ui = consoleHarness();
  ui.click(ui.consent);
  assert.equal(ui.reset.disabled,true);
  ui.click(ui.calibrationSupport);
  assert.equal(ui.reset.disabled,false);
  assert.equal(ui.reload.disabled,true); // No saved calibration.
  ui.click(ui.reset);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].action,'calibration_reset');
  assert.equal(ui.commands[0].args.supported,true);
  assert.equal(ui.calibrationSupport.getAttribute('aria-pressed'),'false');
});

test('reload requires a saved reference and both confirmations, consumed after rejection',async()=>{
  const ui = consoleHarness();
  vm.runInContext('state.calibration={saved:true};updateButtons();',ui.context);
  ui.click(ui.calibrationSupport);
  assert.equal(ui.reload.disabled,true);
  ui.click(ui.unchanged);
  assert.equal(ui.reload.disabled,false);
  ui.fail();
  ui.click(ui.reload);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].action,'calibration_reload');
  assert.equal(ui.commands[0].args.calibration_unchanged,true);
  assert.equal(ui.reload.disabled,true);
  assert.equal(ui.unchanged.getAttribute('aria-pressed'),'false');
});

test('reset and reload cannot run during holds and honor a canceled support countdown',()=>{
  const ui = consoleHarness();
  vm.runInContext('state.phase="holding";state.calibration={saved:true};updateButtons();',ui.context);
  ui.click(ui.calibrationSupport); ui.click(ui.unchanged);
  assert.equal(ui.reset.disabled,true);
  assert.equal(ui.reload.disabled,true);
  vm.runInContext('state.phase="calibration_range";updateButtons();',ui.context);
  ui.elements.delay.checked=true;
  ui.click(ui.reload);
  assert.equal(ui.commands.length,0);
  assert.equal(ui.calibrationSupport.disabled,true);
  ui.click({id:'cancel-countdown',closest(){return this;}});
  assert.equal(ui.reload.disabled,true);
  assert.equal(ui.calibrationSupport.disabled,false);
});

test('leader capture requires a paused hold and following needs its own hands-clear confirmation',()=>{
  const ui=consoleHarness();
  vm.runInContext('state.teaching_mode="leader";state.phase="note_ready";updateButtons();',ui.context);
  ui.click(ui.consent);
  assert.equal(ui.capture.disabled,true);
  assert.equal(ui.resume.disabled,true);
  vm.runInContext('state.leader_teaching=true;state.leader_following=false;updateButtons();',ui.context);
  assert.equal(ui.capture.disabled,false);
  assert.equal(ui.resume.disabled,true);
  ui.click(ui.leaderClear);
  assert.equal(ui.resume.disabled,false);
  vm.runInContext('state.leader_following=true;updateButtons();',ui.context);
  assert.equal(ui.capture.disabled,true);
  assert.equal(ui.resume.disabled,true);
  assert.equal(ui.pause.disabled,false);
});

test('hidden page clears consent and immediately revokes visible following permission',async()=>{
  const ui=consoleHarness();
  ui.click(ui.leaderClear);
  vm.runInContext('document.hidden=true;',ui.context);
  ui.listeners.visibilitychange();
  await new Promise(setImmediate);
  assert.equal(ui.leaderClear.getAttribute('aria-pressed'),'false');
  assert.equal(ui.commands[0].leader_visible,false);
});

async function pollHeartbeat(ui, response) {
  ui.context.fetch = async path => {
    if (path === '/api/session') return {ok:true,json:async()=>({token:'test-token',state:{
      instance_id:'test-app',phase:'connected',connected:true,worker_alive:true,revision:1,pending:false
    }})};
    assert.equal(path,'/api/heartbeat');
    if (response instanceof Error) throw response;
    return {ok:response.status === 200,status:response.status,text:async()=>response.message,
      json:async()=>({ok:true})};
  };
  // Exercise the real request/poll/recovery path with just the status and controls rendered.
  vm.runInContext('render=()=>{renderConnection();updateButtons();};',ui.context);
  await vm.runInContext('poll()',ui.context);
}

test('an ownership conflict first explains reload expiry, then shows persistent read-only',async()=>{
  const ui = consoleHarness();
  ui.click(ui.consent);
  await pollHeartbeat(ui,{status:409,message:JSON.stringify({detail:'Another browser is operating this arm.'})});
  assert.equal(ui.elements.connection.textContent,'Waiting for operator control');
  assert.match(ui.elements['connection-warning'].textContent,/Reloading this tab/);
  vm.runInContext('operatorConflictSince=Date.now()-5001;renderConnection();',ui.context);
  assert.equal(ui.elements.connection.textContent,'Read-only window');
  assert.match(ui.elements['connection-warning'].textContent,/Another operator session/);
  assert.equal(ui.elements['connection-warning'].hidden,false);
  assert.equal(ui.consent.getAttribute('aria-pressed'),'false');
  assert.equal(ui.action.disabled,true);
});

test('an expired page session can recover without a second tab or manual takeover',async()=>{
  const ui = consoleHarness();
  await pollHeartbeat(ui,{status:409,message:'Previous operator session is still active'});
  await pollHeartbeat(ui,{status:200});
  assert.equal(ui.elements.connection.textContent,'Follower connected');
  assert.equal(ui.elements['connection-warning'].hidden,true);
  assert.equal(ui.elements['connection-warning'].textContent,'');
  assert.equal(ui.consent.disabled,false);
  assert.equal(ui.action.disabled,true);
});

test('authentication, server and network heartbeat failures report the real error and block actions',async()=>{
  for (const response of [
    {status:403,message:'Invalid operator token'},
    {status:400,message:JSON.stringify({detail:'Heartbeat must contain JSON'})},
    {status:500,message:'Internal Server Error'},
    new TypeError('Failed to fetch')
  ]) {
    const ui = consoleHarness();
    ui.click(ui.consent);
    await pollHeartbeat(ui,response);
    assert.equal(ui.elements.connection.textContent,'Operator control unavailable');
    assert.doesNotMatch(ui.elements['connection-warning'].textContent,/Another operator/);
    const expected = response.status === 400 ? 'Heartbeat must contain JSON' : response.message;
    assert.ok(ui.elements['connection-warning'].textContent.includes(expected));
    assert.equal(ui.elements['connection-warning'].hidden,false);
    assert.equal(ui.consent.getAttribute('aria-pressed'),'false');
    assert.equal(ui.action.disabled,true);
  }
});

test('successful retry clears the warning without restoring a consumed confirmation',async()=>{
  const ui = consoleHarness();
  ui.click(ui.consent);
  await pollHeartbeat(ui,{status:403,message:'Invalid operator token'});
  await pollHeartbeat(ui,{status:200});
  assert.equal(ui.elements.connection.textContent,'Follower connected');
  assert.equal(ui.elements['connection-warning'].hidden,true);
  assert.equal(ui.elements['connection-warning'].textContent,'');
  assert.equal(ui.consent.disabled,false);
  assert.equal(ui.action.disabled,true);
  assert.equal(ui.commands.length,0);
});

test('calibration navigation and leader selection never submit a motor command',()=>{
  const ui = consoleHarness();
  vm.runInContext('state.calibrated=true;state.phase="ready";render=()=>updateButtons();updateButtons();',ui.context);
  ui.click(ui.calibrationNav);
  assert.equal(vm.runInContext('workflowSection()',ui.context),'calibration');
  ui.click(ui.leaderChoice);
  assert.equal(vm.runInContext('calibrationTarget()',ui.context),'leader');
  const html = vm.runInContext('calibrationSetup()',ui.context);
  assert.match(html,/Follower<\/strong><span>Calibration verified/);
  assert.match(html,/Connect leader/);
  assert.doesNotMatch(html,/data-action="calibrate"/);
  assert.equal(ui.commands.length,0);
});

test('both references are required before moving the workflow to training',()=>{
  const ui = consoleHarness();
  vm.runInContext('state.calibrated=true;state.phase="ready";state.teaching_mode="leader";state.leader={connected:true,calibrated:false};updateButtons();',ui.context);
  assert.equal(vm.runInContext('workflowSection()',ui.context),'calibration');
  assert.equal(vm.runInContext('calibrationTarget()',ui.context),'leader');
  assert.equal(ui.trainingNav.disabled,true);
  assert.match(vm.runInContext('calibrationSetup()',ui.context),/data-target="leader"/);
  vm.runInContext('state.leader.calibrated=true;updateButtons();',ui.context);
  assert.equal(ui.trainingNav.disabled,false);
  assert.equal(vm.runInContext('workflowSection()',ui.context),'notes');
});

test('returning during a hold exposes supported release without enabling arm selection',()=>{
  const ui = consoleHarness();
  vm.runInContext(`state.phase="holding";state.calibrated=true;state.selected="C";
    state.catalog={C:{kind:"note",name:"C"}};state.selected_control=state.catalog.C;
    state.keys={C:{status:"empty"}};state.controls={};
    render=()=>{workflow();updateButtons();};updateButtons();`,ui.context);
  ui.click(ui.calibrationNav);
  assert.match(ui.elements.workflow.innerHTML,/Release &amp; return|Release & return/);
  assert.doesNotMatch(ui.elements.workflow.innerHTML,/data-action="calibrate"/);
  assert.equal(ui.leaderChoice.disabled,true);
  assert.equal(ui.commands.length,0);
});

test('reset and reload follow the selected idle arm but stay on the active calibration target',async()=>{
  const ui = consoleHarness();
  vm.runInContext('calibrationChoice="leader";state.leader={connected:true,calibration:{saved:true}};updateButtons();',ui.context);
  ui.click(ui.calibrationSupport);
  ui.click(ui.reset);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].args.target,'leader');
  vm.runInContext('state.phase="calibration_range";state.calibration_target="follower";',ui.context);
  assert.equal(vm.runInContext('calibrationTarget()',ui.context),'follower');
});
