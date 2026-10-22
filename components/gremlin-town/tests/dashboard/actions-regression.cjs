// Independent synthetic UI review. Run: node this-file path/to/actions.js
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.textContent = ''; this.value = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute() {}
  addEventListener(type, fn) { this.events[type] = fn; }
  querySelectorAll(selector) {
    const tags = selector.split(',');
    return this.children.flatMap(child => [...(tags.includes(child.tag) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  checkValidity() { return true; }
}
async function scenario(mode) {
  let plugin, counter = 0, refreshes = 0, undos = 0;
  const requests = [], toasts = [];
  const item = {ref: 'synthetic-ref', record_id: 'synthetic-version', record_sha256: 'a'.repeat(64), state: 'open', steps: []};
  const api = {
    capabilities: {writes: true},
    request: async () => ({item}),
    refresh: async () => { if (++refreshes === 2 && mode === 'undo-redraw') throw Error('Synthetic redraw failed'); },
    toast: (message, options) => toasts.push({message, options})
  };
  const context = {
    window: {dashboardActions: {register: value => plugin = value}},
    document: {createElement: tag => new Element(tag)},
    crypto: {randomUUID: () => `synthetic-${++counter}`}, TypeError, SyntaxError,
    fetch: async (url, options) => {
      if (url === '/auth/session') return {ok: true, json: async () => ({csrf: 'synthetic-csrf'})};
      const payload = JSON.parse(options.body); requests.push(payload);
      if (mode === 'bad-json') return {ok: false, status: 502, json: async () => { throw new SyntaxError('Synthetic non-JSON response'); }};
      if (payload.action === 'undo' && mode === 'undo-retry' && ++undos === 1) throw new TypeError('Synthetic response lost');
      return {ok: true, status: 200, json: async () => ({status: 'accepted', operation_id: payload.operation_id,
        ...(mode === 'needs-split' ? {schedule: {status: 'needs_split', issue: 'needs_split',
          text: 'Saved. Not scheduled · split this into shorter tasks'}} : {})})};
    }
  };
  vm.runInNewContext(source, context);
  const host = new Element('div'); plugin.detail(host, item, api);
  await host.querySelectorAll('button').find(node => node.textContent === 'Mark done').events.click();
  if (mode === 'bad-json') {
    assert(host.querySelectorAll('button').some(node => node.textContent === 'Check save again'), 'Non-JSON uncertain response must offer the exact save retry');
    return;
  }
  if (mode === 'needs-split') {
    assert.match(toasts.at(-1).message, /split this into shorter tasks/);
    assert.equal(toasts.at(-1).options.label, 'Undo');
    return;
  }
  const undo = toasts.find(entry => entry.options?.label === 'Undo').options.action;
  await undo();
  if (mode === 'undo-retry') {
    await undo();
    const sent = requests.filter(payload => payload.action === 'undo');
    assert.equal(sent.length, 2);
    assert.deepEqual(sent[0], sent[1], 'Uncertain Undo must preserve the exact original request');
  } else {
    assert.match(toasts.at(-1).message, /Undone/i, 'A redraw failure must preserve the confirmed Undo receipt');
  }
}
(async () => {
  let failed = 0;
  for (const mode of ['bad-json', 'undo-redraw', 'undo-retry', 'needs-split']) {
    try { await scenario(mode); console.log(`PASS ${mode}`); }
    catch (error) { failed++; console.log(`FAIL ${mode}: ${error.message}`); }
  }
  process.exitCode = failed ? 1 : 0;
})();
