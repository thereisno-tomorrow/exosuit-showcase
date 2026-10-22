/* Rendering only. Columns, next steps, ranks, dates and completion come from Brain Core. */
(() => {
  "use strict";
  const view = document.querySelector("#view");
  let mode = location.hash === "#work" ? "work" : "day", opened = null, generation = 0;
  const hooks = {};
  const element = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const add = (parent, ...nodes) => { nodes.filter(Boolean).forEach(node => parent.append(node)); return parent; };
  const button = (text, cls, fn) => {
    const node = element("button", cls, text);
    node.type = "button"; node.addEventListener("click", fn); return node;
  };
  const time = value => new Intl.DateTimeFormat("en-GB", {timeZone: "Asia/Singapore", hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date(value));
  function toast(message, undo) {
    document.querySelector("#toast-text").textContent = message;
    const node = document.querySelector("#toast"); node.classList.add("on");
    node.querySelector("button")?.remove();
    if (undo) node.append(button(undo.label, "undo", async () => {
      try { await undo.action(); } catch (error) { toast(error.message); }
    }));
    clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("on"), undo ? 15000 : 5000);
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {...options, credentials: "same-origin", cache: "no-store"});
    if (response.status === 401) { location.assign("/login"); throw new Error("Please sign in again."); }
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "Couldn't load this. Try again.");
    return value;
  }
  const api = {refresh: () => refresh(), toast, request};
  window.dashboardActions = {register(value) {
    for (const [kind, handler] of Object.entries(value)) {
      if (typeof handler !== "function") throw new TypeError("Dashboard action handler must be a function");
      (hooks[kind] ||= []).push(handler);
    }
  }, ...api};

  function tags(item) {
    const row = element("div", "tags");
    const labels = [...(item.parent ? [item.parent.title] : []), ...item.labels];
    labels.forEach(label => row.append(element("span", "tag", label))); return row;
  }
  function card(item, compact = false) {
    const node = button("", compact ? "row" : "card" + (item.due ? " due-" + item.due : ""), () => open(item.ref));
    if (!compact) node.append(tags(item));
    node.append(element("span", "title label", item.title));
    if (item.next.item_ref !== item.ref) node.append(element("div", "fact", "Next: " + item.next.text));
    if (item.schedule_label) node.append(element("div", "fact" + (item.late ? " late" : ""), item.schedule_label));
    item.facts.forEach(fact => node.append(element("div", "fact", fact)));
    if (item.stake) node.append(element("div", "fact stake", item.stake));
    if (item.progress.total) {
      const bar = element("progress", "work-progress");
      bar.value = item.progress.done; bar.max = item.progress.total;
      bar.setAttribute("aria-label", item.progress.text + " steps done");
      add(node, bar, element("div", "fact", item.progress.text + " steps done"));
    }
    return node;
  }
  function notices(data, parent) {
    data.notices.forEach(message => parent.append(element("p", "notice", message)));
  }
  function actions(kind, parent, data) {
    if (!data.capabilities.writes && !data.capabilities.ask_diablo) return;
    for (const handler of hooks[kind] || []) {
      const host = element("div", "dashboard-actions"); parent.append(host);
      handler(host, data, {...api, capabilities: data.capabilities});
    }
  }
  function box(label, rows, cls = "") {
    if (!rows.length) return null;
    const node = element("section", "box " + cls);
    node.append(element("h3", "eyebrow", label)); rows.forEach(item => node.append(card(item, true))); return node;
  }
  function timeline(data) {
    const panel = element("section", "panel"); panel.setAttribute("aria-label", "Timeline");
    add(panel, add(element("h3"), element("span", "eyebrow", "Today"), element("small", "", data.schedule.label)));
    const legend = element("div", "legend");
    [["admin", "Admin"], ["focus", "Deep focus"], ["errands", "Errands"], ["ev", "Calendar"]].forEach(([key, label]) => legend.append(element("span", key === "ev" ? key : "k-" + key, label)));
    panel.append(legend);
    const allDay = data.timeline.filter(slot => slot.all_day);
    allDay.forEach(slot => panel.append(element("div", "box all-day", "All day · " + slot.title)));
    const timed = data.timeline.filter(slot => !slot.all_day);
    const dayStart = new Date(data.timeline_start).getTime();
    // Numeric layout only; the server supplies every calendar and work decision.
    const minute = value => (new Date(value).getTime() - dayStart) / 60000;
    const hourStart = Math.max(0, Math.min(4, Math.floor(Math.min(...timed.map(slot => minute(slot.start)), minute(data.updated_at)) / 60)));
    const hourEnd = Math.min(24, Math.max(18, Math.ceil(Math.max(...timed.map(slot => minute(slot.end)), minute(data.updated_at)) / 60)));
    const grid = element("div", "timeline"), hours = element("div", "hours"), lane = element("div", "lane");
    lane.style.height = `calc(var(--hour) * ${hourEnd - hourStart})`;
    for (let h = hourStart; h <= hourEnd; h++) {
      hours.append(element("div", "", String((h + 4) % 24).padStart(2, "0") + ":00"));
      const line = element("div", "gridline"); line.style.top = `calc(var(--hour) * ${h - hourStart})`; lane.append(line);
    }
    // Side-by-side tracks keep overlapping meetings/tasks legible. This is
    // geometry, not scheduling: no time or work state is changed here.
    const layouts = [];
    const rootStyle = getComputedStyle(document.documentElement);
    const hourPixels = parseFloat(rootStyle.getPropertyValue("--hour"));
    const minimumPixels = parseFloat(rootStyle.getPropertyValue("--slot-min-height"));
    let cluster = [], clusterEnd = -Infinity;
    const finishCluster = () => {
      const ends = [];
      for (const value of cluster) {
        let column = ends.findIndex(end => end <= value.start);
        if (column < 0) column = ends.length;
        ends[column] = value.displayEnd;
        value.column = column;
      }
      cluster.forEach(value => { value.columns = ends.length; layouts.push(value); });
      cluster = [];
    };
    timed.forEach(slot => {
      const start = Math.max(hourStart * 60, minute(slot.start)), end = Math.min(hourEnd * 60, minute(slot.end));
      const height = Math.max((end - start) / 60 * hourPixels - 4, minimumPixels);
      // Short entries keep a readable hit area. Collision tracks use that
      // rendered area, while their anchors and printed times stay unchanged.
      const displayEnd = start + height / hourPixels * 60;
      if (start >= clusterEnd) finishCluster();
      cluster.push({slot, start, end, height, displayEnd}); clusterEnd = Math.max(clusterEnd, displayEnd);
    });
    finishCluster();
    layouts.forEach(({slot, start, end, height, column, columns}) => {
      const kind = slot.kind === "deep_focus" ? "focus" : slot.kind === "errands" ? "errands" : "admin";
      const node = slot.ref ? button("", "slot task k-" + kind, () => open(slot.ref)) : element("div", "slot event");
      node.style.top = `calc(var(--hour) * ${start / 60 - hourStart})`;
      node.style.height = `${height}px`;
      node.style.left = `calc(${column * 100 / columns}% + 10px)`;
      node.style.right = `${(columns - column - 1) * 100 / columns}%`;
      add(node, element("span", "when", `${time(slot.start)} – ${time(slot.end)}`), element("span", "what", slot.title));
      node.setAttribute("aria-label", `${slot.title}, ${time(slot.start)} – ${time(slot.end)}`);
      if (slot.late) node.append(element("span", "late", "After it's due"));
      lane.append(node);
    });
    const now = minute(data.updated_at) / 60 - hourStart;
    if (now >= 0 && now <= hourEnd - hourStart) {
      const line = element("div", "nowline"); line.style.top = `calc(var(--hour) * ${now})`;
      line.append(element("span", "", time(data.updated_at))); lane.append(line);
    }
    add(grid, hours, lane); panel.append(grid); return panel;
  }
  function renderDay(data) {
    const page = element("div");
    add(page, add(element("div", "day-head"), element("h1", "", data.date_label), element("span", "state", `${data.done_count} done today`)));
    notices(data, page);
    const now = element("section", "now"); now.setAttribute("aria-label", "Now");
    add(now, element("span", "eyebrow", "Now · " + time(data.updated_at)),
      element("h2", "", data.now ? data.now.title : data.schedule.available ? "Nothing scheduled now" : "Choose from Can do now"));
    if (data.now) {
      now.append(element("div", "meta", `${data.now.type === "task" ? "Scheduled " : ""}${time(data.now.start)} – ${time(data.now.end)}`));
      if (data.now.ref) now.append(button("Open details", "btn quiet", () => open(data.now.ref)));
    }
    if (data.next) add(now, add(element("div", "then"), element("span", "", "Then"), element("b", "", data.next.title), element("span", "mono", time(data.next.start))));
    page.append(now);
    const side = element("aside", "side");
    add(side, box("Your list", data.today));
    if (!data.today.length) side.append(element("p", "hint", "Nothing picked for today yet."));
    add(side, box("Needs a length", data.needs_length, "fit"), box("Due today", data.due_today, "due-today"), box("Due tomorrow", data.due_tomorrow, "due-tomorrow"));
    data.moved_groups.forEach(group => {
      const moved = element("section", "box moved"); moved.append(element("h3", "eyebrow", group.label));
      group.items.forEach(row => add(moved, card(row.item, true), element("span", "hint", row.schedule.label))); side.append(moved);
    });
    if (!data.schedule.available || !data.now) add(side, box("Can do now", data.ranked.slice(0, 7)));
    add(page, add(element("div", "day-grid"), timeline(data), side));
    actions("day", page, data);
    page.append(element("p", "foot", "Times can shift during the day. The schedule updates by itself."));
    return page;
  }
  function renderBoard(data) {
    const page = element("div"), head = add(element("div", "work-head"), element("h1", "", "All my work"));
    actions("board", head, data); page.append(head); notices(data, page);
    const board = element("div", "board");
    data.columns.forEach(column => {
      const col = element("section", "col c-" + column.key); col.setAttribute("aria-label", column.label);
      add(col, add(element("div", "col-head"), element("h2", "eyebrow", column.label), element("span", "n", column.items.length)));
      const list = element("div", "drop"); column.items.forEach(item => list.append(card(item)));
      if (!column.items.length) list.append(element("p", "empty", "Nothing here"));
      add(col, list); board.append(col);
    });
    add(page, add(element("div", "board-wrap"), board), element("p", "hint", "Tap a card for its details.")); return page;
  }
  function renderDetail(item) {
    const page = element("div", "details");
    page.append(button(mode === "day" ? "← My day" : "← All my work", "back", () => { opened = null; refresh(); }));
    const main = element("div"), aside = element("aside", "actions");
    add(main, tags(item), element("h1", "d-title", item.title), element("span", "pill", item.column_label));
    const facts = element("div", "facts");
    if (item.schedule_label) facts.append(element("div", item.late ? "late" : "", item.schedule_label));
    item.facts.forEach(text => facts.append(element("div", "", text))); main.append(facts);
    if (item.stake) main.append(element("p", "stake", item.stake));
    if (item.steps.length) {
      const section = element("section", "section"), steps = element("ol", "steps");
      section.append(element("h2", "eyebrow", `Steps · ${item.progress.text} done`));
      item.steps.forEach(step => {
        const row = button("", step.state === "done" ? "is-done" : "", () => open(step.ref));
        add(row, element("span", "check", step.state === "done" ? "✓" : ""), element("span", "label", step.title));
        if (step.ref === item.next.item_ref) row.append(element("span", "next-chip", "Next"));
        if (step.notes) row.append(element("span", "detail", step.notes));
        steps.append(add(element("li"), row));
      }); add(section, steps); main.append(section);
    }
    add(main, add(element("section", "section"), element("h2", "eyebrow", "Notes"), element("div", "notes note-body", item.notes || "No notes yet.")));
    actions("detail", aside, item);
    add(page, add(element("div", "d-grid"), main, aside)); return page;
  }
  async function refresh() {
    const mine = ++generation;
    document.querySelector("#tab-day").setAttribute("aria-pressed", mode === "day");
    document.querySelector("#tab-work").setAttribute("aria-pressed", mode === "work");
    view.setAttribute("aria-busy", "true");
    try {
      const data = await request(opened ? "/api/work/" + encodeURIComponent(opened) : "/api/" + mode);
      if (mine !== generation) return;
      if (data.date_label) document.querySelector("#date").textContent = data.date_label;
      view.replaceChildren(opened ? renderDetail(data) : mode === "day" ? renderDay(data) : renderBoard(data));
    } catch (error) {
      if (mine === generation) view.replaceChildren(add(element("section", "box"), element("p", "", error.message), button("Try again", "btn", refresh)));
    } finally { if (mine === generation) view.removeAttribute("aria-busy"); }
  }
  function open(ref) { opened = ref; refresh(); window.scrollTo(0, 0); }
  for (const value of ["day", "work"]) document.querySelector("#tab-" + value).addEventListener("click", () => {
    mode = value; opened = null; history.replaceState(null, "", "#" + value); refresh();
  });
  document.querySelector("#refresh").addEventListener("click", refresh);
  document.querySelector("#sign-out").addEventListener("click", async event => {
    const control = event.currentTarget;
    control.disabled = true;
    try {
      const session = await request("/auth/session");
      await request("/auth/logout", {method: "POST", headers: {"X-CSRF-Token": session.csrf}});
      window.dashboardAsk?.clearPending();
      location.assign("/login");
    } catch (error) {
      control.disabled = false;
      toast(error.message);
    }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  // A minute refresh updates the Now line and ages from fresh server reads, but
  // does not erase a half-written note/steps form installed by the write slice.
  setInterval(() => { if (!document.hidden && !opened && !document.querySelector("input:focus, textarea:focus")) refresh(); }, 60000);
  refresh();
})();
