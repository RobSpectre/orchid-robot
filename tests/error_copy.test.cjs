const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function harness({write = async () => {}, fallback = false} = {}) {
  let observe;
  const sources = [], tools = [], writes = [], fallbackCopies = [];
  function element(tag) {
    return {tag, children: [], hidden: false, isConnected: true, textContent: '', attrs: {},
      append(...nodes) { this.children.push(...nodes); },
      after(node) { tools.push(node); },
      setAttribute(k,v) { this.attrs[k] = v; },
      addEventListener(name, fn) { this[name] = fn; },
      focus() { this.focused = true; }, select() { this.selected = true; },
      remove() { this.isConnected = false; }};
  }
  const document = {
    body: element('body'), activeElement: element('button'),
    createElement: element,
    querySelectorAll: () => sources.filter(s => s.isConnected),
    execCommand(name) {
      assert.equal(name, 'copy');
      fallbackCopies.push(document.body.children.at(-1).value);
      return fallback;
    }
  };
  const context = vm.createContext({document, queueMicrotask,
    navigator: {clipboard: {writeText: async text => { writes.push(text); await write(text); }}},
    MutationObserver: class {constructor(fn) {observe = fn;} observe() {}},
    fetch: () => assert.fail('Copying must never contact the controller')});
  vm.runInContext(readFileSync('orchid_demo/static/error-copy.js','utf8'), context);
  const refresh = async () => { observe(); await new Promise(setImmediate); };
  return {document, sources, tools, writes, fallbackCopies, refresh,
    add(id, text) { const s = element('p'); s.id = id; s.textContent = text; sources.push(s); return s; },
    active: () => tools.filter(t => t.isConnected)};
}

test('all error surfaces get a copy control; normal status messages do not',async()=>{
  const ui = harness();
  for (const id of ['error','connection-warning','connection-fault','discovery-warnings','workflow-stop','activity-error']) ui.add(id,'Motor error');
  ui.add('leader-feedback-status','Stopped: wrist_flex failed');
  ui.add('discovery-status','Scan failed: USB missing');
  ui.add('diagnostics-age','Health read unavailable: voltage error');
  const normal = ui.add('leader-feedback-status','Following is disengaged.');
  const empty = ui.add('error','');
  const hidden = ui.add('connection-warning','Hidden error'); hidden.hidden = true;
  await ui.refresh();
  assert.equal(ui.active().length,9);
  normal.textContent='Stopped: new error'; empty.textContent='New error'; hidden.hidden=false;
  await ui.refresh();
  assert.equal(ui.active().length,12);
});

test('copies exact multiline error text and does not duplicate controls on polling',async()=>{
  const ui=harness();
  const message="Follower's elbow: measured 2992, target 2971\n<raw> & \"quotes\"";
  ui.add('error',message);
  await ui.refresh(); await ui.refresh();
  assert.equal(ui.active().length,1);
  const [button,status]=ui.active()[0].children;
  assert.equal(button.type,'button');
  await button.click();
  assert.deepEqual(ui.writes,[message]);
  assert.equal(button.textContent,'Copied ✓');
  assert.equal(status.textContent,'Error copied to clipboard.');
  await ui.refresh();
  assert.equal(button.textContent,'Copied ✓');
});

test('changed or hidden errors reset copy feedback and remove stale controls',async()=>{
  const ui=harness(); const source=ui.add('error','first'); await ui.refresh();
  const [button,status]=ui.active()[0].children;
  await button.click(); source.textContent='second'; await ui.refresh();
  assert.equal(button.textContent,'Copy error'); assert.equal(status.textContent,'');
  source.hidden=true; await ui.refresh(); assert.equal(ui.active().length,0);
  source.hidden=false; await ui.refresh(); assert.equal(ui.active().length,1);
  source.isConnected=false; await ui.refresh(); assert.equal(ui.active().length,0);
});

test('clipboard permission denial uses a local fallback and restores focus',async()=>{
  const ui=harness({write: async()=>{throw Error('Denied');}, fallback:true});
  ui.add('error','motor 4 voltage error'); await ui.refresh();
  await ui.active()[0].children[0].click();
  assert.deepEqual(ui.fallbackCopies,['motor 4 voltage error']);
  assert.equal(ui.document.activeElement.focused,true);
  assert.equal(ui.document.body.children.at(-1).isConnected,false);
  assert.equal(ui.active()[0].children[0].textContent,'Copied ✓');
});

test('blocked clipboard exposes selected text without falsely claiming success',async()=>{
  const ui=harness({write: async()=>{throw Error('Denied');}});
  ui.add('error','motor error'); await ui.refresh();
  const [button,status,manual]=ui.active()[0].children;
  await button.click();
  assert.equal(button.textContent,'Copy error'); assert.equal(button.disabled,false);
  assert.match(status.textContent,/Clipboard blocked/);
  assert.equal(manual.value,'motor error'); assert.equal(manual.hidden,false);
  assert.equal(manual.selected,true); assert.equal(manual.readOnly,true);
});

test('a pending clipboard operation cannot claim a changed error was copied',async()=>{
  let resolve; const ui=harness({write:()=>new Promise(done=>resolve=done)});
  const source=ui.add('error','first'); await ui.refresh();
  const [button,status]=ui.active()[0].children;
  const copying=button.click(); assert.equal(button.disabled,true);
  source.textContent='second'; await ui.refresh(); resolve(); await copying;
  assert.deepEqual(ui.writes,['first']); assert.equal(status.textContent,'');
  assert.equal(button.textContent,'Copy error'); assert.equal(button.disabled,false);
});
