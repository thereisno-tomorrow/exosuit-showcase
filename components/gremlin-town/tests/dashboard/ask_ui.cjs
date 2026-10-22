/* Minimal DOM contract test: all fetches are in-memory fakes. No browser/network. */
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
  set innerHTML(_) { throw new Error("Untrusted content used as HTML"); }
  get innerHTML() { throw new Error("HTML access is forbidden"); }
  click() { if (!this.disabled) return this.listeners.click?.(); }
}
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };

(async () => {
  let hooks, count = 0, history = [], requests = [], unknown = true;
  const timers = [];
  const saved = new Map();
  const context = {
    document: {createElement: tag => new Node(tag)},
    crypto: {randomUUID: () => `fixture-key-${++count}`},
    Map, TypeError, SyntaxError, Error,
    sessionStorage: {getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value),
      removeItem: key => saved.delete(key), key: index => [...saved.keys()][index], get length() {return saved.size;}},
    window: {dashboardActions: {register: value => { hooks = value; }}},
    setTimeout: callback => { timers.push(callback); },
    fetch: async (path, options) => {
      if (path === "/auth/session") return {ok: true, json: async () => ({csrf: "fixture-csrf"})};
      assert.equal(path, "/api/ask-diablo");
      assert.equal(options.headers["X-CSRF-Token"], "fixture-csrf");
      requests.push(JSON.parse(options.body));
      if (unknown) throw new TypeError("Uncertain response");
      return {ok: true, json: async () => ({text: "Sent to Diablo", delivery: "queued"})};
    }
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
  const host = new Node("main"), item = {ref: "fixture-thing", record_id: "fixture-version", record_sha256: "a".repeat(64)};
  const api = {capabilities: {ask_diablo: true, writes: false}, request: async path => {
    assert.equal(path, "/api/ask-diablo?ref=fixture-thing"); return {requests: history};
  }};
  const off = new Node("main"); hooks.detail(off, item, {...api, capabilities: {ask_diablo: false}});
  assert.equal(off.children.length, 0);
  hooks.detail(host, item, api);
  const input = host.querySelectorAll("textarea")[0];
  assert.equal(input.value, "");
  const ready = host.querySelectorAll("button").find(x => x.textContent === "Get it ready");
  assert.equal(ready.disabled, true); // no fresh send until earlier requests were read
  await settle();
  input.value = "  Exact words\n<script>fictional</script>  ";
  ready.click(); await settle();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].text, input.value);
  assert.equal(ready.disabled, true);
  ready.click(); await settle(); assert.equal(requests.length, 1);
  unknown = false;
  host.querySelectorAll("button").find(x => x.textContent === "Check request again").click(); await settle();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1], requests[0]); // same key, proof, action and exact words
  history = [{action: "get_ready", text: "Diablo's reply", words: "Words", delivery: "accepted",
              result: {objective: "Reply", text: "<img src=x onerror=alert(1)>"}}];
  await timers.shift()(); await settle();
  assert.equal(host.querySelectorAll("pre")[0].textContent, history[0].result.text);
  host.children[0].isConnected = false;
  process.stdout.write("Ask UI contracts passed\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
