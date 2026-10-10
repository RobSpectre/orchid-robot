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
  const homeCapture = button('home-capture', {action:'capture_home'});
  const homeStart = button('home-start', {action:'home_start'});
  const leaderHomeCapture = button('leader-home-capture', {action:'capture_leader_home'});
  const teachControl = button('teach-control', {action:'control_start'});
  const homeHold = button('home-hold', {action:'leader_hold'});
  const homeReturn = button('home-return', {action:'capture_home_return'});
  const connectionNav = button('nav-connect', {view:'connect'});
  const connectionDisconnect = button('connection-disconnect', {action:'disconnect', connection:'true'});
  const unavailableSupport = button('unavailable-support', {confirm:'supported'});
  const powerDisconnected = button('power-disconnected', {confirm:'motor_power_disconnected'});
  const forget = button('forget', {action:'forget_connection', unavailable:'true'});
  const stay = button('stay', {view:'current'});
  const calibrationNav = button('nav-calibration', {view:'calibration'});
  const trainingNav = button('nav-notes', {view:'notes'});
  const leaderChoice = button('choose-leader', {calibrationTarget:'leader'});
  Object.assign(elements, {
    'workflow':{querySelectorAll:()=>[consent, optional]},
    'connection-release':{querySelectorAll:()=>[consent]},
    'unavailable-tools':{querySelectorAll:()=>[unavailableSupport,powerDisconnected]},
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
    querySelectorAll:selector=>selector === '[data-action]' ? [action,disconnect,reset,reload,resume,pause,capture,connectionDisconnect,forget,homeCapture,homeStart,leaderHomeCapture,teachControl,homeHold,homeReturn]
      : selector === '[data-confirm]' ? [consent,optional,recovery,calibrationSupport,unchanged,leaderClear,unavailableSupport,powerDisconnected]
      : selector === '[data-view]' ? [connectionNav,calibrationNav,trainingNav,stay]
      : selector === '[data-calibration-target]' ? [leaderChoice] : []
  };
  let fail = false, interval;
  const context = vm.createContext({document, crypto:webcrypto, AbortSignal,
    setTimeout:()=>{}, setInterval:fn=>{interval=fn;return 1;}, clearInterval:()=>{interval=null;},
    fetch:async (path, options) => {
      if (path.startsWith('/api/session')) return new Promise(()=>{}); // No background poll in this unit harness.
      commands.push(JSON.parse(options.body));
      return fail ? {ok:false,text:async()=>JSON.stringify({detail:'Capture rejected'})}
        : {ok:true,json:async()=>({status:'queued'})};
    }
  });
  vm.runInContext(readFileSync('orchid_demo/static/app.js','utf8'),context);
  vm.runInContext(`state={phase:'connected',connected:true,calibrated:false,worker_alive:true,revision:1,pending:false};
    token='test-token';online=true;owns=true;updateButtons();`,context);
  return {consent,optional,recovery,action,disconnect,calibrationSupport,unchanged,reset,reload,leaderClear,resume,pause,capture,homeCapture,homeStart,leaderHomeCapture,teachControl,homeHold,homeReturn,connectionNav,connectionDisconnect,unavailableSupport,powerDisconnected,forget,stay,calibrationNav,trainingNav,leaderChoice,elements,commands,context,listeners,
    tick:()=>interval?.(), fail:()=>{fail=true;}, click:b=>listeners.click({target:b})};
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

test('leader mode teaches by record and replay without a home pose',()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.teaching_mode="leader";state.calibrated=true;state.leader={calibrated:true};
    state.selected="C";state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};state.selected_control=state.catalog.C;
    state.keys={C:{status:"empty"}};state.controls={};state.torque={gripper:0};workflow();`,ui.context);
  const ready=ui.elements.workflow.innerHTML;
  assert.match(ready,/Ready to teach C/);
  assert.match(ready,/data-action="teach_begin"/);
  assert.doesNotMatch(ready,/data-action="(home_start|capture_home|move_home|leader_hold)"/);
  assert.doesNotMatch(ready,/data-confirm/);  // nothing to confirm while the follower is limp
  vm.runInContext('state.torque={gripper:1};workflow();',ui.context);
  assert.match(ui.elements.workflow.innerHTML,/data-confirm="supported"/);  // configure() blinks torque off
});

test('the connect screen is one button that finds and connects every arm, and hand-guide mode says the leader is inactive',()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.phase="disconnected";state.mode="hardware";state.fixture={label:"x",id:"",tool:"rubber_gloved_tips"};
    state.discovery={ports:[]};state.selected="C";state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};
    state.selected_control=state.catalog.C;state.keys={C:{status:"empty"}};state.controls={};workflow();`,ui.context);
  const page = ui.elements.workflow.innerHTML;
  assert.match(page,/data-action="find_connect_all"[^>]*>Find and connect all arms/);
  assert.doesNotMatch(page,/<select/);  // no port, leader or teaching-mode pickers
  vm.runInContext(`state.phase="ready";state.teaching_mode="manual";state.calibrated=true;state.torque={};workflow();`,ui.context);
  assert.match(ui.elements.workflow.innerHTML,/leader arm is not connected/);
  vm.runInContext(`state.phase="note_ready";workflow();`,ui.context);
  assert.match(ui.elements.workflow.innerHTML,/leader arm is not connected/);
});

test('keys are taught as home, hover, touch and press points',()=>{
  const ui=consoleHarness();
  const render=(phase,teach,status='empty')=>{
    vm.runInContext(`state.teaching_mode="leader";state.phase=${JSON.stringify(phase)};state.message="m";
      state.selected="C";state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};state.selected_control=state.catalog.C;
      state.keys={C:{status:${JSON.stringify(status)},recorded:${status!=='empty'}}};state.controls={};
      state.teach=${JSON.stringify({points_for:'C', ...teach})};`,ui.context);
    return vm.runInContext('teachWorkflow(state)',ui.context);
  };
  const primary=html=>(html.match(/<div class="actions"><button data-action="([a-z_]+)" class="primary"(?: data-point="(\w+)")?/)||[]).slice(1).join(':');
  assert.equal(primary(render('teach_hold',{mode:'holding',points:[]})),'teach_set_home:');
  assert.equal(primary(render('teach_hold',{mode:'holding',points:['home']})),'teach_follow:');
  assert.equal(primary(render('teach_follow',{mode:'following',points:['home']})),'teach_capture:hover');
  const pressing=render('teach_follow',{mode:'following',points:['home','hover','touch']});
  assert.equal(primary(pressing),'teach_capture:press');
  assert.match(pressing,/Capture press &amp; return home|Capture press & return home/);
  assert.match(pressing,/data-target-point="hover" class="teach-point done"/);  // tap a captured step to retrain it
  assert.match(pressing,/<span class="teach-point done"><strong>✓ Home/);
  assert.doesNotMatch(render('teach_follow',{mode:'aligning',points:['home']}),/class="primary" data-point/);
  const taught=render('teach_hold',{mode:'holding',points:['home','hover','touch','press']},'testing');
  assert.equal(primary(taught),'teach_play:');
  assert.match(taught,/id="press-seconds"/);
  assert.doesNotMatch(taught,/play-speed/);  // speed is the global slider in the top bar
  assert.doesNotMatch(taught,/teach_record/);
  assert.match(render('teach_play',{mode:'playing',returning:true,points:['home','hover','touch','press']}),/Returning to home/);
  const played=render('teach_hold',{mode:'holding',played:'C',points:['home','hover','touch','press']},'registered');
  assert.equal(primary(played),'teach_play:');  // Play again; no separate "sounded right" step
  assert.doesNotMatch(played,/teach_verify/);
  // Another key starts from the shared home.
  vm.runInContext('state.teach.points_for="D";state.teach.home_saved=true;',ui.context);
  assert.deepEqual(Array.from(vm.runInContext('pointsFor(state,"C")',ui.context)),['home','hover','touch','press']);  // taught
  vm.runInContext('state.keys.C={status:"empty",recorded:false};',ui.context);
  assert.deepEqual(Array.from(vm.runInContext('pointsFor(state,"C")',ui.context)),['home']);  // untaught: shared home only
});

test('a taught key offers Play, even while another key is being taught',()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.teaching_mode="leader";state.phase="teach_follow";state.message="m";state.selected="C#";
    state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"},"C#":{id:"C#",label:"C#",kind:"key",name:"C#"}};
    state.selected_control=state.catalog["C#"];state.keys={C:{status:"registered",recorded:true},"C#":{status:"empty",recorded:false}};
    state.controls={};state.teach={mode:"following",points_for:"C#",points:["home","hover"],home_saved:true};
    selectedNote="C";`,ui.context);
  const html=vm.runInContext('teachWorkflow(state)',ui.context);
  assert.match(html,/<div class="actions"><button data-action="teach_play" class="primary"/);
  assert.doesNotMatch(html,/class="primary" data-point/);  // Space must not re-capture C's hover
  vm.runInContext(`selectedNote=null;state.phase="ready";state.calibrated=true;state.leader={calibrated:true};state.torque={};
    state.selected="C";state.selected_control=state.catalog.C;state.teach=null;workflow();`,ui.context);
  assert.match(ui.elements.workflow.innerHTML,/data-action="teach_begin" class="primary" data-control="C" data-play="1">Play C/);
});

test('a re-teach in progress offers Cancel re-teach',()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.teaching_mode="leader";state.phase="teach_follow";state.message="m";state.selected="C";
    state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};state.selected_control=state.catalog.C;
    state.keys={C:{status:"registered",recorded:true}};state.controls={};
    state.teach={mode:"following",points_for:"C",points:["home","hover"],home_saved:true,reteaching:"C"};selectedNote=null;`,ui.context);
  assert.match(vm.runInContext('teachWorkflow(state)',ui.context),/data-action="teach_cancel_reteach"[^>]*>✕ Cancel re-teach/);
  vm.runInContext('state.teach.reteaching=null;',ui.context);
  assert.doesNotMatch(vm.runInContext('teachWorkflow(state)',ui.context),/teach_cancel_reteach/);
});

test('the Home button sets and visits the one home',()=>{
  const ui=consoleHarness();
  const render=(phase,teach)=>{
    vm.runInContext(`state.teaching_mode="leader";state.phase=${JSON.stringify(phase)};state.message="m";state.selected="C";
      state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};state.selected_control=state.catalog.C;
      state.keys={C:{status:"registered",recorded:true}};state.controls={};state.teach=${JSON.stringify(teach)};selectedNote="home";`,ui.context);
    return vm.runInContext('teachWorkflow(state)',ui.context);
  };
  const primary=html=>(html.match(/<div class="actions"><button data-action="([a-z_]+)" class="primary"/)||[])[1];
  assert.equal(primary(render('teach_follow',{mode:'following',home_saved:true})),'teach_set_home');
  assert.match(render('teach_follow',{mode:'following',home_saved:true}),/teach_go_home/);
  assert.equal(primary(render('teach_hold',{mode:'holding',home_saved:true})),'teach_follow');
  assert.doesNotMatch(render('teach_hold',{mode:'holding',home_saved:false}),/teach_go_home/);
  assert.equal(vm.runInContext('targetControl()',ui.context),'C');  // home commands never send "home" as a key
  // A key shows home as a marker, not a button to tap.
  vm.runInContext('selectedNote=null;',ui.context);
  const key=render('teach_follow',{mode:'following',points_for:'C',points:['home','hover','touch','press'],home_saved:true});
  assert.doesNotMatch(key,/data-point="home"/);
});

test('the Rest button sets and visits the rest pose',()=>{
  const ui=consoleHarness();
  const render=(phase,teach)=>{
    vm.runInContext(`state.teaching_mode="leader";state.phase=${JSON.stringify(phase)};state.message="m";state.selected="C";
      state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};state.selected_control=state.catalog.C;
      state.keys={C:{status:"registered",recorded:true}};state.controls={};state.teach=${JSON.stringify(teach)};selectedNote="rest";`,ui.context);
    return vm.runInContext('teachWorkflow(state)',ui.context);
  };
  const primary=html=>(html.match(/<div class="actions"><button data-action="([a-z_]+)" class="primary"/)||[])[1];
  assert.equal(primary(render('teach_follow',{mode:'following',rest_saved:true})),'teach_set_rest');
  assert.match(render('teach_follow',{mode:'following',rest_saved:true}),/teach_go_rest/);
  assert.doesNotMatch(render('teach_hold',{mode:'holding',rest_saved:false}),/teach_go_rest/);
  assert.equal(vm.runInContext('targetControl()',ui.context),'C');
  assert.match(render('teach_play',{mode:'ramping',going_rest:true}),/Moving to rest/);
  vm.runInContext('selectedNote=null;state.teach.points_for="C";state.teach.points=["home","hover","touch","press"];',ui.context);
  assert.match(vm.runInContext('teachWorkflow(state)',ui.context),/Returning to rest/);  // a key's panel after it played
});

test('a redo shows "captured" on its box even though the point count went down',()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.teaching_mode="leader";state.phase="teach_follow";state.message="m";state.selected="D";
    state.catalog={D:{id:"D",label:"D",kind:"key",name:"D"}};state.selected_control=state.catalog.D;
    state.keys={D:{status:"registered",recorded:true}};state.controls={};
    state.teach={mode:"following",points_for:"D",points:["home","hover"],home_saved:true};
    flash={point:"hover",control:"D",until:Date.now()+2000};`,ui.context);
  const html=vm.runInContext('teachWorkflow(state)',ui.context);
  assert.match(html,/data-target-point="hover" class="teach-point done flash"[^>]*><strong>✓ Hover<\/strong><small>captured ✓/);
  vm.runInContext('flash=null;',ui.context);
  assert.match(vm.runInContext('teachWorkflow(state)',ui.context),/✓ Hover<\/strong><small>tap to retrain/);
});

test('tapping a taught step selects it for retraining instead of capturing',()=>{
  const ui=consoleHarness();
  const render=()=>{
    vm.runInContext(`state.teaching_mode="leader";state.phase="teach_follow";state.message="m";state.selected="C";
      state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};state.selected_control=state.catalog.C;
      state.keys={C:{status:"registered",recorded:true}};state.controls={};
      state.teach={mode:"following",points_for:"C",points:["home","hover","touch","press"],home_saved:true};`,ui.context);
    return vm.runInContext('teachWorkflow(state)',ui.context);
  };
  const primary=html=>(html.match(/<div class="actions"><button data-action="([a-z_]+)" class="primary"(?: data-point="(\w+)")?/)||[]).slice(1).join(':');
  assert.equal(primary(render()),'teach_play:');  // all taught: Space plays
  assert.doesNotMatch(render(),/data-action="teach_capture" data-point="hover"|data-target-point="hover"[^>]*data-action/);  // boxes never capture
  vm.runInContext('targetPoint={control:"C",point:"hover"};',ui.context);
  const retraining=render();
  assert.equal(primary(retraining),'teach_capture:hover');  // now Space recaptures hover only
  assert.match(retraining,/Recapture hover/);
  assert.match(retraining,/data-target-point="touch" class="teach-point done"/);  // touch and press stay taught
  assert.match(retraining,/data-target-point="press" class="teach-point done"/);
  vm.runInContext('targetPoint={control:"C",point:"press"};',ui.context);
  assert.match(render(),/Recapture press &amp; return home|Recapture press & return home/);
  vm.runInContext('targetPoint={control:"D",point:"hover"};',ui.context);
  assert.equal(primary(render()),'teach_play:');  // a selection for another key does not apply here
});

test('Follow the leader sends the key selected on the map',async()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.teaching_mode="leader";state.phase="teach_hold";state.selected="C";state.revision=1;
    state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"},D:{id:"D",label:"D",kind:"key",name:"D"}};
    state.keys={C:{status:"registered",recorded:true},D:{status:"empty",recorded:false}};state.controls={};
    state.teach={mode:"holding",points_for:"C",points:["home","hover","touch","press"],home_saved:true};selectedNote="D";
    dispatchButton({dataset:{action:"teach_follow"}});`,ui.context);
  await new Promise(setImmediate);
  const sent=ui.commands.find(c=>c.action==='teach_follow');
  assert.equal(sent.args.control,'D');
});

test('re-centring the wrist roll is offered with both arms connected and needs support',()=>{
  const ui=consoleHarness();
  ui.elements['calibration-tools-content']={};
  vm.runInContext(`state.teaching_mode="leader";state.phase="ready";state.connected=true;state.calibrated=true;
    state.calibration={};state.leader={connected:true,calibrated:true,calibration:{}};calibrationTools();`,ui.context);
  const html=ui.elements['calibration-tools-content'].innerHTML;
  assert.match(html,/data-action="recenter_wrist_roll"[^>]*data-calibration="true"/);
  assert.match(html,/data-confirm="supported"/);
  vm.runInContext('state.leader.connected=false;calibrationTools();',ui.context);
  assert.doesNotMatch(ui.elements['calibration-tools-content'].innerHTML,/recenter_wrist_roll/);
  vm.runInContext('arms={a:{available:true},b:{available:true}};calibrationTools();',ui.context);  // two followers: this one alone
  assert.match(ui.elements['calibration-tools-content'].innerHTML,/this follower’s wrist-roll zero[^]*shared leader is not changed/);
});

test('a taught key has a press length and the shared speed, and Play sends the press length',async()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.teaching_mode="leader";state.phase="teach_hold";state.message="m";state.selected="C";state.revision=1;
    state.catalog={C:{id:"C",label:"C",kind:"key",name:"C"}};state.selected_control=state.catalog.C;
    state.keys={C:{status:"registered",recorded:true,press_s:0.8}};state.controls={};state.teach_settings={speed:2,press_s:0.3};
    state.teach={mode:"holding",points_for:"C",points:["home","hover","touch","press"],home_saved:true};selectedNote=null;targetPoint=null;`,ui.context);
  const html=vm.runInContext('teachWorkflow(state)',ui.context);
  assert.match(html,/id="press-seconds"[^>]*value="0.8"/);
  assert.doesNotMatch(html,/½ speed|data-speed/);
  ui.elements['press-seconds']={value:"1.5"};
  vm.runInContext('dispatchButton({dataset:{action:"teach_play"}});',ui.context);
  await new Promise(setImmediate);
  const sent=ui.commands.find(c=>c.action==='teach_play');
  assert.equal(sent.args.press_s,1.5);
  assert.equal('speed' in sent.args,false);  // the shared speed setting applies
});

test('the global arm speed slider shows the saved speed and saves when released',async()=>{
  const ui=consoleHarness();
  const slider={id:'arm-speed',value:'1',disabled:false,blur(){}}, out={textContent:''};
  ui.elements['arm-speed']=slider; ui.elements['arm-speed-value']=out;
  vm.runInContext(`syncSpeed({teach_settings:{speed:2.5,press_s:0.3}})`,ui.context);
  assert.equal(slider.value,'2.5'); assert.equal(out.textContent,'2.5×');
  slider.value='3'; ui.listeners.input({target:slider});
  assert.equal(out.textContent,'3×');
  vm.runInContext(`syncSpeed({teach_settings:{speed:1}})`,ui.context);
  assert.equal(slider.value,'3');  // a poll does not snap the slider back while it is being dragged
  await ui.listeners.change({target:slider});
  assert.deepEqual(ui.commands.at(-1).action,'teach_settings');
  assert.deepEqual(ui.commands.at(-1).args,{speed:3});
  vm.runInContext(`syncSpeed({teach_settings:{speed:3}})`,ui.context);
  assert.equal(out.textContent,'3×');
  const page=readFileSync('orchid_demo/static/index.html','utf8');
  assert.match(page,/id="arm-speed" type="range" min="0.25" max="3"/);
});

test('the dial is taught as hover, open, lower and grip, with a turn angle per direction',()=>{
  const ui=consoleHarness();
  const render=(phase,teach,status='empty',extra={})=>{
    vm.runInContext(`state.teaching_mode="leader";state.phase=${JSON.stringify(phase)};state.message="m";state.selected="voicing.cw";
      state.catalog={"voicing.cw":{id:"voicing.cw",label:"Clockwise",kind:"dial",direction:"cw",name:"Voicing · clockwise"}};
      state.selected_control=state.catalog["voicing.cw"];state.keys={};
      state.controls={"voicing.cw":{status:${JSON.stringify(status)},recorded:${status!=='empty'},...${JSON.stringify(extra)}}};
      state.teach=${JSON.stringify({points_for:'voicing.cw', ...teach})};selectedNote=null;targetPoint=null;`,ui.context);
    return vm.runInContext('teachWorkflow(state)',ui.context);
  };
  const primary=html=>(html.match(/<div class="actions"><button data-action="([a-z_]+)" class="primary"(?: data-point="(\w+)")?/)||[]).slice(1).join(':');
  const html=render('teach_follow',{mode:'following',points:['home']});
  for (const step of ['hover','open','lower','grip']) assert.match(html,new RegExp(`data-target-point="${step}"`));
  assert.doesNotMatch(html,/data-target-point="(touch|press|turn)"|teach_record|turn-degrees/);
  assert.equal(primary(html),'teach_capture:hover');
  const gripping=render('teach_follow',{mode:'following',points:['home','hover','open','lower']});
  assert.equal(primary(gripping),'teach_capture:grip');
  assert.match(gripping,/Capture grip &amp; let go|Capture grip & let go/);
  const taught=render('teach_hold',{mode:'holding',points:['home','hover','open','lower','grip']},'registered',{turn_degrees:35});
  assert.equal(primary(taught),'teach_play:');
  assert.match(taught,/id="turn-degrees"[^>]*value="35"/);
});

test('saving a leader-positioned home requires its positioning phase and a paused hold',()=>{
  const ui=consoleHarness();
  vm.runInContext('state.teaching_mode="leader";state.phase="home_positioning";state.leader_teaching=true;state.leader_following=true;updateButtons();',ui.context);
  ui.click(ui.consent);
  assert.equal(ui.leaderHomeCapture.disabled,true);
  vm.runInContext('state.leader_following=false;updateButtons();',ui.context);
  assert.equal(ui.leaderHomeCapture.disabled,false);
  vm.runInContext('state.phase="home_approach";updateButtons();',ui.context);
  assert.equal(ui.leaderHomeCapture.disabled,true);
});

test('teaching can prepare away from home but capture requires measured alignment',()=>{
  const ui=consoleHarness();
  vm.runInContext('state.teaching_mode="leader";state.home_ready=true;state.at_home=false;updateButtons();',ui.context);
  ui.click(ui.consent);
  assert.equal(ui.teachControl.disabled,false);
  assert.equal(ui.homeHold.disabled,true);
  vm.runInContext('state.at_home=true;updateButtons();',ui.context);
  assert.equal(ui.teachControl.disabled,false);
  assert.equal(ui.homeHold.disabled,false);
  vm.runInContext('state.phase="home_return";state.leader_teaching=true;state.leader_following=true;updateButtons();',ui.context);
  assert.equal(ui.homeReturn.disabled,true);
  vm.runInContext('state.leader_following=false;updateButtons();',ui.context);
  assert.equal(ui.homeReturn.disabled,false);
  vm.runInContext('state.at_home=false;updateButtons();',ui.context);
  assert.equal(ui.homeReturn.disabled,true);
});

async function pollHeartbeat(ui, response) {
  ui.context.fetch = async path => {
    if (path.startsWith('/api/session')) return {ok:true,json:async()=>({token:'test-token',state:{
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

test('step 01 opens a supported disconnect screen from calibration, training and faults',()=>{
  for (const phase of ['connected','ready','calibration_midpoint','calibration_range','calibration_review','note_ready','holding','fault']) {
    const ui = consoleHarness();
    vm.runInContext(`state.phase=${JSON.stringify(phase)};state.calibrated=true;state.selected="C";
      state.catalog={C:{kind:"note",name:"C"}};state.selected_control=state.catalog.C;
      state.keys={C:{status:"empty"}};state.controls={};
      render=()=>{clearConfirmations();workflow();updateButtons();};updateButtons();`,ui.context);
    ui.click(ui.consent);
    ui.click(ui.connectionNav);
    assert.equal(vm.runInContext('workflowSection()',ui.context),'connect');
    assert.match(ui.elements.workflow.innerHTML,/Return to connections/);
    assert.match(ui.elements.workflow.innerHTML,/Release & return to connections/);
    assert.doesNotMatch(ui.elements.workflow.innerHTML,/data-action="calibrate"|data-action="control_start"/);
    if (phase.startsWith('calibration_')) assert.match(ui.elements.workflow.innerHTML,/previous motor settings restored and verified/);
    assert.equal(ui.connectionDisconnect.disabled,true);
    assert.equal(ui.consent.getAttribute('aria-pressed'),'false');
    assert.equal(ui.commands.length,0);
    // Cancel navigation leaves the worker on exactly the same step.
    vm.runInContext('render=()=>updateButtons();',ui.context);
    ui.click(ui.stay);
    assert.equal(vm.runInContext('connectionView',ui.context),false);
    assert.equal(vm.runInContext('state.phase',ui.context),phase);
    assert.equal(ui.commands.length,0);
  }
});

test('return to connections requires paused following and consumes fresh support consent',async()=>{
  const ui = consoleHarness();
  vm.runInContext('state.phase="note_ready";state.leader_following=true;render=()=>updateButtons();updateButtons();',ui.context);
  assert.equal(ui.connectionNav.disabled,false);
  ui.click(ui.connectionNav);
  ui.click(ui.consent);
  assert.equal(ui.connectionDisconnect.disabled,true);
  assert.equal(ui.commands.length,0);
  vm.runInContext('state.leader_following=false;clearConfirmations();updateButtons();',ui.context);
  assert.equal(ui.connectionDisconnect.disabled,true);
  ui.click(ui.consent);
  ui.click(ui.connectionDisconnect);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].action,'disconnect');
  assert.equal(ui.commands[0].args.supported,true);
  assert.equal(ui.consent.getAttribute('aria-pressed'),'false');
});

test('connection navigation observes operation, countdown and ownership locks',()=>{
  for (const block of ['state.pending=true','owns=false','online=false','timer=1','state.worker_alive=false']) {
    const ui = consoleHarness();
    vm.runInContext(`${block};updateButtons();`,ui.context);
    assert.equal(ui.connectionNav.disabled,true);
    ui.click(ui.connectionNav);
    assert.equal(vm.runInContext('connectionView',ui.context),false);
    assert.equal(ui.commands.length,0);
  }
});

test('forgetting a failed connection requires its own support and power confirmations',async()=>{
  const ui = consoleHarness();
  vm.runInContext('state.phase="fault";updateButtons();',ui.context);
  ui.click(ui.consent);
  assert.equal(ui.connectionDisconnect.disabled,false);
  assert.equal(ui.forget.disabled,true);
  ui.click(ui.unavailableSupport);
  assert.equal(ui.forget.disabled,true);
  ui.click(ui.powerDisconnected);
  assert.equal(ui.forget.disabled,false);
  vm.runInContext('state.phase="connected";updateButtons();',ui.context);
  assert.equal(ui.forget.disabled,true);
  vm.runInContext('state.phase="fault";updateButtons();',ui.context);
  ui.click(ui.forget);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].action,'forget_connection');
  assert.deepEqual(ui.commands[0].args,{supported:true,motor_power_disconnected:true});
  assert.equal(ui.unavailableSupport.getAttribute('aria-pressed'),'false');
  assert.equal(ui.powerDisconnected.getAttribute('aria-pressed'),'false');
});

test('unavailable connection recovery respects operator and request locks',()=>{
  for (const block of ['state.pending=true','owns=false','online=false','timer=1','state.worker_alive=false']) {
    const ui = consoleHarness();
    vm.runInContext('state.phase="fault";updateButtons();',ui.context);
    ui.click(ui.unavailableSupport);
    ui.click(ui.powerDisconnected);
    vm.runInContext(`${block};updateButtons();`,ui.context);
    assert.equal(ui.forget.disabled,true);
    ui.click(ui.forget);
    assert.equal(ui.commands.length,0);
  }
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

test('Train controls is reachable again from Correct keys while the arm holds, but not mid-correction',()=>{
  const ui = consoleHarness();
  vm.runInContext('state.calibrated=true;state.phase="teach_hold";state.teaching_mode="manual";tuneView=true;render=()=>updateButtons();updateButtons();',ui.context);
  assert.equal(vm.runInContext('workflowSection()',ui.context),'tune');
  assert.equal(ui.trainingNav.disabled,false);
  ui.click(ui.trainingNav);
  assert.equal(vm.runInContext('workflowSection()',ui.context),'notes');
  vm.runInContext('tuneView=true;state.tune={status:"running"};updateButtons();',ui.context);
  assert.equal(ui.trainingNav.disabled,true);  // Stop correcting stays in view while a key is being corrected
  assert.equal(ui.commands.length,0);
});

test('Correct keys offers the keys whose last run failed, not keys that passed but sit off the pattern',()=>{
  const ui = consoleHarness();
  const html = vm.runInContext(`state.calibrated=true;state.phase="teach_hold";state.teaching_mode="manual";state.key_check_available=true;
    state.owns=notes.slice();state.keys=Object.fromEntries(notes.map(k=>[k,{status:"registered",recorded:true}]));state.controls={};
    state.tune_limits={presses:5,rounds:4,margin:1,depth:0.75,limit_deg:3,speed:0.5,hardness:0.25};
    state.tune_results={"C#":{status:"failed",kind:"correct"},"F#":{status:"failed",kind:"test",clean:2,presses:5},A:{status:"done",kind:"correct"}};
    state.key_layout={fitted:true,pitch_mm:17,keys:{A:{text:"A is 5 mm toward A#"},B:{text:"B is 4 mm toward A#"}}};
    tuneSection();`,ui.context);
  assert.match(html,/data-controls="C#,F#"[^>]*>Correct the 2 keys that failed \(C# F#\)/);
  assert.doesNotMatch(html,/off the pattern \(/);  // A passed; B is only offered once nothing has failed
  assert.match(html,/✗ 2\/5 clean/);
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


test('engaging leader always counts down before alignment even with capture delay off',async()=>{
  const ui=consoleHarness();
  vm.runInContext('state.phase="home_approach";state.leader_teaching=true;updateButtons();',ui.context);
  ui.click(ui.leaderClear); ui.click(ui.resume);
  assert.equal(ui.elements.delay.checked,false);
  assert.match(ui.elements.countdown.innerHTML,/Follower aligns to leader/);
  for (let i=0;i<4;i++) { ui.tick(); assert.equal(ui.commands.length,0); }
  ui.tick(); await new Promise(setImmediate);
  assert.equal(ui.commands.length,1);
  assert.equal(ui.commands[0].action,'leader_resume');
  assert.equal(ui.commands[0].args.hands_clear,true);
});

for (const interruption of ['cancel','stop','hidden','offline','revision','pending']) {
  test(`leader countdown prevents engagement after ${interruption}`,async()=>{
    const ui=consoleHarness();
    vm.runInContext('state.phase="home_approach";state.leader_teaching=true;updateButtons();',ui.context);
    ui.click(ui.leaderClear); ui.click(ui.resume); ui.tick();
    if (interruption==='cancel') ui.click({id:'cancel-countdown',closest(){return this;}});
    else if (interruption==='stop') ui.click(ui.elements.stop);
    else vm.runInContext({hidden:'document.hidden=true',offline:'online=false',revision:'state.revision++',pending:'state.pending=true'}[interruption],ui.context);
    for(let i=0;i<6;i++) ui.tick();
    await new Promise(setImmediate);
    assert.equal(ui.commands.filter(c=>c.action==='leader_resume').length,0);
    assert.equal(ui.leaderClear.getAttribute('aria-pressed'),'false');
  });
}

test('old controller cannot run the new teaching flow; recovery remains available',async()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.mode="hardware";state.teaching_mode="leader";
    state.home_ready=true;state.leader_teaching=true;state.leader_following=true;
    updateButtons();renderConnection();`,ui.context);
  ui.click(ui.consent); ui.click(ui.recovery);
  assert.equal(ui.teachControl.disabled,true);
  assert.equal(ui.action.disabled,true);
  assert.equal(ui.pause.disabled,false);
  assert.equal(ui.disconnect.disabled,false);
  assert.equal(ui.elements.stop.disabled,false);
  assert.equal(ui.elements['connection-warning'].hidden,false);
  assert.match(ui.elements['connection-warning'].textContent,/Update not loaded/);
  await vm.runInContext('submit("control_start", {supported:true,control:"C"})',ui.context);
  assert.equal(ui.commands.length,0);
  ui.click(ui.disconnect);
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].action,'disconnect');
});

test('matching controller enables teaching away from home and clears the update banner',()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.mode="hardware";state.teaching_mode="leader";state.home_ready=true;state.at_home=false;
    state.teaching_workflow_version="leader-record-replay-v1";updateButtons();renderConnection();`,ui.context);
  ui.click(ui.consent);
  assert.equal(ui.teachControl.disabled,false);
  assert.equal(ui.elements['connection-warning'].hidden,true);
});

test('off-home arrival allows leader engagement and explicit home acceptance',async()=>{
  const ui=consoleHarness();
  vm.runInContext(`state.phase="home_arrival";state.teaching_mode="leader";
    state.leader_teaching=true;state.leader_following=false;state.at_home=false;updateButtons();`,ui.context);
  ui.click(ui.leaderClear);
  assert.equal(ui.resume.disabled,false);
  ui.click(ui.resume);
  for(let i=0;i<5;i++) ui.tick();
  await new Promise(setImmediate);
  assert.equal(ui.commands[0].action,'leader_resume');
  vm.runInContext('state.leader_following=true;updateButtons();',ui.context);
  assert.equal(ui.pause.disabled,false);
  assert.equal(ui.leaderHomeCapture.disabled,true);
  vm.runInContext('state.leader_following=false;updateButtons();',ui.context);
  ui.click(ui.consent);
  assert.equal(ui.leaderHomeCapture.disabled,false);
});
