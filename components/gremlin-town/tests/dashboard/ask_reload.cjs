/* Regression from independent review: reload/sign-in/tab-close after lost reply. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = "";
    this.disabled = false; this.value = ""; this.isConnected = true; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute() {}
  querySelectorAll(selector) { return this.children.flatMap(node => node instanceof Node
    ? [...(selector.split(",").includes(node.tag) ? [node] : []), ...node.querySelectorAll(selector)] : []); }
  click() { if (!this.disabled) return this.listeners.click?.(); }
}
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
const code = fs.readFileSync(process.argv[2], "utf8");
const find = (page, label) => page.host.querySelectorAll("button").find(node => node.textContent === label);
const storage = () => {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key), key: index => [...values.keys()][index], get length() { return values.size; }};
};
let sent = [], history = [], counter = 0, authenticated = true, commit = true, lose = true;
function page(saved, freshHash = "a") {
  let hooks;
  const context = {document: {createElement: tag => new Node(tag)}, sessionStorage: saved,
    crypto: {randomUUID: () => `fixture-key-${++counter}`}, Map, TypeError, SyntaxError, Error,
    window: {dashboardActions: {register: value => { hooks = value; }}}, setTimeout() {},
    fetch: async (path, options) => {
      if (path === "/auth/session") return {ok: authenticated, json: async () => ({csrf: "fixture-csrf"})};
      const payload = JSON.parse(options.body); sent.push(payload);
      assert(saved.length > 0, "persist the original request before any POST");
      if (commit && !history.some(entry => entry.operation_id === payload.operation_id)) history.push({
        operation_id: payload.operation_id, action: payload.action, words: payload.text,
        expected: payload.expected, text: "Sent to Diablo", delivery: "queued"});
      if (lose) throw new TypeError("Lost response");
      return {ok: true, json: async () => ({text: "Sent to Diablo", delivery: "queued"})};
    }};
  vm.runInNewContext(code, context);
  const host = new Node("main");
  hooks.detail(host, {ref: "fixture-thing", record_id: "fixture-version", record_sha256: freshHash.repeat(64)},
    {capabilities: {ask_diablo: true}, request: async () => ({requests: history})});
  return {host, context};
}
(async () => {
  // GT committed, response lost, then complete reload with a changed item proof.
  const saved = storage();
  const first = page(saved); await settle();
  first.host.querySelectorAll("textarea")[0].value = "Exact fictional instruction";
  find(first, "Do it").click(); await settle();
  assert.equal(sent.length, 1); assert.equal(saved.length, 1);
  const second = page(saved, "b"); await settle();
  assert.equal(saved.length, 0); // reconciled the same operation in canonical history
  assert.equal(find(second, "Do it").disabled, true);
  assert(find(second, "Check request again"));
  lose = false; find(second, "Check request again").click(); await settle();
  assert.deepEqual(sent[1], sent[0]); assert.equal(history.length, 1);
  assert.equal(find(second, "Do it").disabled, true);
  // Closing the tab loses sessionStorage. Canonical history still blocks a new
  // UUID until the owner explicitly chooses New request.
  const third = page(storage(), "c"); await settle();
  assert.equal(find(third, "Do it").disabled, true);
  find(third, "Check request again").click(); await settle();
  assert.deepEqual(sent[2], sent[0]); assert.equal(history.length, 1);
  find(third, "New request").click(); await settle();
  assert.equal(find(third, "Do it").disabled, false);
  third.host.querySelectorAll("textarea")[0].value = "An explicit second instruction";
  find(third, "Do it").click(); await settle();
  assert.notEqual(sent[3].operation_id, sent[0].operation_id); assert.equal(history.length, 2);

  // Nothing reached GT before the connection broke. Reload must preserve the
  // exact original key/proof/words even though no history can reconcile it yet.
  history = []; sent = []; commit = false; lose = true;
  const uncommitted = storage(); const fourth = page(uncommitted); await settle();
  fourth.host.querySelectorAll("textarea")[0].value = "Recover this exact request";
  find(fourth, "Get it ready").click(); await settle();
  const fifth = page(uncommitted, "b"); await settle();
  assert.equal(find(fifth, "Get it ready").disabled, true);
  authenticated = false;
  find(fifth, "Check request again").click(); await settle();
  assert.equal(uncommitted.length, 1); assert.equal(sent.length, 1);
  authenticated = true; commit = true; lose = false;
  const signedIn = page(uncommitted, "c"); await settle();
  find(signedIn, "Check request again").click(); await settle();
  assert.deepEqual(sent[1], sent[0]); assert.equal(uncommitted.length, 0);

  // A blocked storage write must prevent POST, not silently fall back to memory.
  history = []; sent = [];
  const unavailable = storage(); unavailable.setItem = () => { throw new Error("Storage blocked"); };
  const blocked = page(unavailable); await settle(); find(blocked, "Do it").click(); await settle();
  assert.equal(sent.length, 0); assert.equal(find(blocked, "Do it").disabled, true);

  const clean = storage(); clean.setItem("dashboard.ask.pending.v1:fixture", "fictional request");
  clean.setItem("unrelated", "preserve");
  const logout = page(clean); logout.context.window.dashboardAsk.clearPending();
  assert.equal(clean.length, 1); assert.equal(clean.getItem("unrelated"), "preserve");
  process.stdout.write("Ask reload contracts passed\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
