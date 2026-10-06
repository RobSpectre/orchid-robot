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
  Object.assign(elements, {
    'workflow':{querySelectorAll:()=>[consent, optional]},
    'recovery':{querySelectorAll:()=>[recovery]},
    'calibration-tools':{querySelectorAll:()=>[calibrationSupport,unchanged],
      querySelector:s=>s.includes('calibration_unchanged') ? unchanged : calibrationSupport},
    'recovery-supported':recovery, 'stop':button('stop'),
    'delay':{checked:false}, 'speech':{checked:false}, 'countdown':{}, 'error':{}
  });
  const document = {
    getElementById:id=>elements[id], addEventListener:(event,fn)=>listeners[event]=fn,
    querySelectorAll:selector=>selector === '[data-action]' ? [action,disconnect,reset,reload]
      : selector === '[data-confirm]' ? [consent,optional,recovery,calibrationSupport,unchanged] : []
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
  return {consent,optional,recovery,action,disconnect,calibrationSupport,unchanged,reset,reload,elements,commands,context,
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
