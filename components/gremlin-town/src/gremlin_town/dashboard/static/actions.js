/* Owner-selected controls. All state changes and work rules live on the server. */
(() => {
  "use strict";
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const key = () => crypto.randomUUID();
  const proof = item => ({ref: item.ref, expected: {
    record_id: item.record_id, sha256: item.record_sha256
  }});
  const button = (label, action) => {
    const node = el("button", label, "dashboard-action");
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  async function post(payload) {
    const session = await fetch("/auth/session", {credentials: "same-origin", cache: "no-store"});
    if (!session.ok) throw new Error("Sign in again before changing this item.");
    const auth = await session.json();
    const response = await fetch("/api/actions", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": auth.csrf},
      body: JSON.stringify(payload)
    });
    let result;
    try { result = await response.json(); }
    catch (_) {
      const error = new Error("The save was not confirmed. Check the same save again.");
      error.retryable = response.ok || response.status >= 500;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(result.text || result.detail || "The save was not confirmed.");
      error.retryable = result.retryable === true || response.status >= 500;
      throw error;
    }
    if (!result || !["accepted", "duplicate"].includes(result.status)) {
      const error = new Error("The save was not confirmed. Check the same save again.");
      error.retryable = true;
      throw error;
    }
    return result;
  }
  function sender(host, api) {
    let busy = false;
    const notice = el("div", "", "action-notice");
    notice.setAttribute("role", "status");
    host.append(notice);
    return async function send(payload, item) {
      if (busy) return;
      busy = true;
      host.querySelectorAll("button,input,textarea").forEach(node => { node.disabled = true; });
      notice.replaceChildren(el("span", "Saving…"));
      try {
        const result = await post(payload);
        const savedText = result.schedule?.issue === "needs_split" ? result.schedule.text
          : result.schedule?.status === "wake_failed" ? "Saved, but the schedule could not be updated." : "Saved";
        notice.replaceChildren(el("span", savedText));
        // Refresh is a read. A redraw failure must never be mistaken for a failed save.
        try { await api.refresh(); }
        catch (_) { api.toast?.("Saved. The page could not refresh; reopen it to see the change."); }
        if (item && payload.action !== "undo") {
          let undoPayload = null;
          const undoKey = key();
          const undo = async () => {
            try {
              if (!undoPayload) {
                const fresh = await api.request("/api/work/" + encodeURIComponent(item.ref));
                const current = fresh.item || fresh;
                undoPayload = {action: "undo", operation_id: undoKey, change: result.operation_id, ...proof(current)};
              }
              await post(undoPayload);
              try { await api.refresh(); api.toast?.("Undone"); }
              catch (_) { api.toast?.("Undone. The page could not refresh; reopen it to see the change."); }
            } catch (error) {
              const retry = error.retryable || error instanceof TypeError;
              api.toast?.(error.message || "Undo was not confirmed. Refresh this item.",
                retry ? {label: "Check Undo again", action: undo} : undefined);
            }
          };
          api.toast?.(savedText, {label: "Undo", action: undo});
        } else api.toast?.(savedText);
      } catch (error) {
        notice.replaceChildren(el("span", error.message || "The save was not confirmed. Refresh this item."));
        if (error.retryable || error instanceof TypeError) {
          // Keep the exact original proof and retry key after an uncertain reply.
          notice.append(button("Check save again", () => send(payload, item)));
        }
      } finally {
        busy = false;
        host.querySelectorAll("button,input,textarea").forEach(node => { node.disabled = false; });
      }
    };
  }
  function textForm(host, label, submitLabel, submit, multiline = true) {
    const form = el("form", undefined, "action-form");
    const id = "action-" + key();
    const title = el("label", label); title.htmlFor = id;
    const input = el(multiline ? "textarea" : "input");
    input.id = id; input.maxLength = multiline ? 8192 : 256; input.required = true;
    const save = el("button", submitLabel); save.type = "submit";
    form.append(title, input, save);
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (input.value.trim()) submit(input.value);
    });
    host.append(form);
  }
  function lengths(host, item, send) {
    const group = el("div", undefined, "length-actions");
    group.append(el("span", "Length"));
    [15, 30, 60, 120].forEach(minutes => group.append(button(
      minutes < 60 ? `${minutes} min` : `${minutes / 60} hr`,
      () => send({action: "length", operation_id: key(), ...proof(item), minutes}, item)
    )));
    const form = el("form", undefined, "action-form");
    const id = "minutes-" + key();
    const label = el("label", "Other length in minutes"); label.htmlFor = id;
    const input = el("input"); input.id = id; input.type = "number"; input.min = "1";
    input.max = "525600"; input.step = "1"; input.required = true;
    const save = el("button", "Set length"); save.type = "submit";
    form.append(label, input, save);
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (form.checkValidity()) send({action: "length", operation_id: key(), ...proof(item), minutes: Number(input.value)}, item);
    });
    group.append(form); host.append(group);
  }
  function detail(host, item, api) {
    if (!api.capabilities?.writes) return;
    const send = sender(host, api);
    const group = el("div", undefined, "detail-actions");
    const simple = (label, action) => group.append(button(label,
      () => send({action, operation_id: key(), ...proof(item)}, item)));
    simple(item.state === "done" ? "Mark not done" : "Mark done", item.state === "done" ? "untick" : "tick");
    simple("Do this first today", "today");
    simple("Can do now", "can_do_now");
    group.append(button("Park: Someday", () => send({action: "park", operation_id: key(), ...proof(item), day: "Someday"}, item)));
    const parkForm = el("form", undefined, "action-form");
    const parkId = "park-" + key();
    const label = el("label", "Park until"); label.htmlFor = parkId;
    const day = el("input"); day.type = "date"; day.required = true; day.id = parkId;
    const park = el("button", "Park"); park.type = "submit";
    parkForm.append(label, day, park);
    parkForm.addEventListener("submit", event => { event.preventDefault();
      if (parkForm.checkValidity()) send({action: "park", operation_id: key(), ...proof(item), day: day.value}, item);
    });
    group.append(parkForm); host.append(group);
    lengths(host, item, send);
    textForm(host, "Add a note", "Save note", text => send({action: "note", operation_id: key(), ...proof(item), text}, item));
    textForm(host, "Add steps — separate with a new line, comma, or then", "Save steps", text =>
      send({action: "steps", operation_id: key(), ...proof(item), text}, item));
    for (const step of item.steps || []) {
      const row = el("div", undefined, "step-actions");
      row.append(el("span", step.title), button(step.state === "done" ? "Mark not done" : "Mark done", () =>
        send({action: step.state === "done" ? "untick" : "tick", operation_id: key(), ...proof(step)}, step)));
      host.append(row);
    }
  }
  function quickAdd(host, data, api) {
    if (!api.capabilities?.writes) return;
    const send = sender(host, api);
    textForm(host, "Quick add", "Add for your review", text =>
      send({action: "quick_add", operation_id: key(), text}), false);
  }
  window.dashboardActions.register({detail, day: quickAdd, board: quickAdd});
})();
