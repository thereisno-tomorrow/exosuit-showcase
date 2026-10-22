/* Ask Diablo: text nodes only; the hub owns delivery, identity and retries. */
(() => {
  "use strict";
  const pending = new Map();
  const storagePrefix = "dashboard.ask.pending.v1:";
  const storageKey = ref => storagePrefix + encodeURIComponent(ref);
  function loadPending(ref) {
    const saved = sessionStorage.getItem(storageKey(ref));
    if (saved !== null) {
      const payload = JSON.parse(saved);
      if (!payload || payload.ref !== ref || typeof payload.operation_id !== "string") {
        throw new Error("Couldn't recover the earlier request. Reopen the dashboard before sending.");
      }
      pending.set(ref, payload);
    }
    return pending.get(ref);
  }
  function remember(payload) {
    // Persist before POST. If tab storage is unavailable, no request is sent.
    sessionStorage.setItem(storageKey(payload.ref), JSON.stringify(payload));
    pending.set(payload.ref, payload);
  }
  function forget(ref) {
    sessionStorage.removeItem(storageKey(ref));
    pending.delete(ref);
  }
  window.dashboardAsk = {clearPending() {
    // The host calls this only after explicit logout succeeds. No auth secrets
    // are stored here. Closed-tab recovery uses the canonical GT history.
    for (let index = sessionStorage.length - 1; index >= 0; index--) {
      const key = sessionStorage.key(index);
      if (key?.startsWith(storagePrefix)) sessionStorage.removeItem(key);
    }
    pending.clear();
  }};
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (label, action) => {
    const node = el("button", label, "dashboard-action");
    node.type = "button"; node.addEventListener("click", action); return node;
  };
  async function post(payload) {
    const session = await fetch("/auth/session", {credentials: "same-origin", cache: "no-store"});
    if (!session.ok) {
      const error = new Error("Sign in again, then check the same request.");
      error.uncertain = true; throw error;
    }
    const auth = await session.json();
    const response = await fetch("/api/ask-diablo", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": auth.csrf},
      body: JSON.stringify(payload)
    });
    const value = await response.json();
    if (!response.ok) {
      const error = new Error(value.text || value.detail || "Couldn't confirm the request.");
      error.uncertain = response.status === 401 || response.status === 403 || response.status >= 500 || value.retryable === true;
      throw error;
    }
    return value;
  }
  function detail(host, item, api) {
    if (!api.capabilities?.ask_diablo) return;
    const group = el("section", undefined, "ask-diablo");
    group.append(el("h3", "Ask Diablo"));
    const label = el("label", "Your words (optional)");
    const input = el("textarea"); input.id = "ask-" + crypto.randomUUID();
    input.maxLength = 4000; label.htmlFor = input.id;
    const controls = el("div", undefined, "ask-controls");
    const status = el("div", "", "action-notice"); status.setAttribute("role", "status");
    const replies = el("div", undefined, "diablo-replies");
    group.append(label, input, controls, status, replies); host.append(group);
    const next = el("div", undefined, "ask-next"); group.append(next);
    let busy = false, loaded = false, readFailed = false, storageFailed = false;
    let hasHistory = false, newRequest = false;
    const setEnabled = () => {
      const blocked = busy || !loaded || storageFailed || pending.has(item.ref) || (hasHistory && !newRequest);
      controls.querySelectorAll("button").forEach(node => { node.disabled = blocked; });
      input.disabled = blocked;
      next.replaceChildren();
      if (loaded && hasHistory && !newRequest && !pending.has(item.ref) && !busy && !storageFailed) {
        next.append(button("New request", () => { newRequest = true; input.value = ""; setEnabled(); }));
      }
    };
    function showUncertain(payload, message) {
      status.replaceChildren(el("p", message), button("Check request again", () => send(payload)));
      setEnabled();
    }
    async function send(payload) {
      if (busy) return;
      try { remember(payload); }
      catch (_) {
        storageFailed = true;
        status.replaceChildren(el("p", "Couldn't keep this request safe for a retry. Nothing was sent. Reopen the dashboard."));
        setEnabled(); return;
      }
      busy = true; setEnabled();
      status.replaceChildren(el("p", "Sending to Diablo…"));
      try {
        const value = await post(payload);
        forget(item.ref); hasHistory = true; newRequest = false;
        status.replaceChildren(el("p", value.text)); input.value = "";
        // Failure to redraw is not a failed send. It must never offer a new send.
        try { await refreshReplies(); }
        catch (_) { status.append(el("p", "Sent to Diablo. Reopen this thing to check his reply.")); }
      } catch (error) {
        if (error.uncertain || error instanceof TypeError || error instanceof SyntaxError) {
          showUncertain(payload, error.message || "Couldn't confirm the request.");
        } else {
          forget(item.ref);
          status.replaceChildren(el("p", error.message || "Couldn't send the request. Reopen this thing."));
        }
      } finally { busy = false; setEnabled(); }
    }
    function start(action) {
      if (busy || !loaded || storageFailed || pending.has(item.ref) || (hasHistory && !newRequest)) return;
      send({operation_id: crypto.randomUUID(), action, text: input.value,
            ref: item.ref, expected: {record_id: item.record_id, sha256: item.record_sha256}});
    }
    controls.append(button("Get it ready", () => start("get_ready")), button("Do it", () => start("do_it")));
    async function refreshReplies() {
      const value = await api.request("/api/ask-diablo?ref=" + encodeURIComponent(item.ref));
      const original = pending.get(item.ref);
      const known = original && value.requests.find(request => request.operation_id === original.operation_id
        && request.action === original.action && request.words === original.text
        && request.expected?.record_id === original.expected.record_id
        && request.expected?.sha256 === original.expected.sha256);
      if (known && !busy) {
        forget(item.ref); newRequest = false;
        status.replaceChildren(el("p", known.text));
      }
      if (readFailed && !busy && !pending.has(item.ref)) status.replaceChildren();
      readFailed = false;
      hasHistory = value.requests.length > 0;
      replies.replaceChildren();
      for (const request of value.requests) {
        const row = el("article", undefined, "diablo-reply");
        row.append(el("strong", request.text));
        row.append(el("p", request.action === "get_ready" ? "Get it ready" : "Do it"));
        if (request.words) row.append(el("p", request.words));
        if (request.delivery === "queued" || request.delivery === "sending") row.append(el("small", "Queued for delivery"));
        if (request.result) {
          if (request.result.objective) row.append(el("h4", request.result.objective));
          row.append(el("pre", request.result.text));
        }
        if (request.expected && !busy && !pending.has(item.ref)) {
          row.append(button("Check request again", () => send({operation_id: request.operation_id,
            action: request.action, text: request.words, ref: item.ref, expected: request.expected})));
        }
        replies.append(row);
      }
      loaded = true; setEnabled();
    }
    async function poll() {
      if (!group.isConnected) return;
      try { await refreshReplies(); }
      catch (_) {
        readFailed = true;
        if (!busy && !pending.has(item.ref)) status.replaceChildren(el("p", loaded
          ? "Couldn't check Diablo's reply. Checking again shortly."
          : "Couldn't check earlier requests. Reopen this thing before sending."));
      }
      if (group.isConnected) setTimeout(poll, 5000);
    }
    setEnabled();
    try {
      const original = loadPending(item.ref);
      if (original) showUncertain(original, "The earlier request needs checking.");
    } catch (_) {
      storageFailed = true;
      status.replaceChildren(el("p", "Couldn't recover the earlier request. Reopen the dashboard before sending."));
      setEnabled();
    }
    poll();
  }
  // The host registry composes handlers; it must not replace the write controls.
  window.dashboardActions.register({detail});
})();
