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
  Object.assign(elements, {
    'workflow':{querySelectorAll:()=>[consent, optional]},
    'recovery':{querySelectorAll:()=>[recovery]},
    'recovery-supported':recovery, 'stop':button('stop'),
    'delay':{checked:false}, 'speech':{checked:false}, 'countdown':{}, 'error':{}
  });
  const document = {
    getElementById:id=>elements[id], addEventListener:(event,fn)=>listeners[event]=fn,
    querySelectorAll:selector=>selector === '[data-action]' ? [action,disconnect]
      : selector === '[data-confirm]' ? [consent,optional,recovery] : []
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
  return {consent,optional,recovery,action,disconnect,elements,commands,context,
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
