/* Offline browser regressions. All URLs are intercepted; unknown URLs abort.
 * Usage: node this-file <playwright module> <rendered page.html> [asset directory]
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require(process.argv[2]);
const html = fs.readFileSync(process.argv[3], "utf8");
const assets = process.argv[4] || path.resolve(__dirname, "../../src/gremlin_town/dashboard/static");
const capabilities = {writes: true, ask_diablo: true};
const item = {ref: "fake-task", title: "Test task", state: "open", column_label: "Today", labels: [],
  parent: null, next: {item_ref: "fake-task"}, progress: {done: 0, total: 0}, facts: [],
  notes: "Invented text", steps: [], capabilities};
const schedule = {available: true, label: "Schedule updated 11:00"};
const common = {date_label: "Fri 2 Oct", notices: [], capabilities, schedule};
const timeline = [
  {ref: "fake-task", title: "First fifteen minutes", type: "task", kind: "admin", start: "2026-10-02T11:00:00+08:00", end: "2026-10-02T11:15:00+08:00"},
  {ref: null, title: "A short meeting", type: "event", kind: "calendar", start: "2026-10-02T11:15:00+08:00", end: "2026-10-02T11:30:00+08:00"},
  {ref: "fake-task", title: "Next fifteen minutes", type: "task", kind: "admin", start: "2026-10-02T11:30:00+08:00", end: "2026-10-02T11:45:00+08:00"},
];
const day = {...common, updated_at: "2026-10-02T11:05:00+08:00", timeline,
  timeline_start: "2026-10-02T04:00:00+08:00", timeline_end: "2026-10-03T04:00:00+08:00",
  now: timeline[0], next: timeline[1], done_count: 0, today: [item], needs_length: [],
  due_today: [], due_tomorrow: [], moved_groups: [], ranked: []};
const board = {...common, columns: [{key: "today", label: "Today", items: [item]}]};
const policy = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'";

(async () => {
  const browser = await chromium.launch({headless: true,
    args: ["--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND"]});
  try {
    for (const [width, height] of [[1440, 1000], [390, 844]]) {
      const errors = [], authRequests = [];
      let refuseLogout = true;
      const context = await browser.newContext({viewport: {width, height}, reducedMotion: "reduce"});
      await context.route("**/*", async route => {
        const request = route.request(), url = new URL(request.url());
        if (url.origin !== "https://dashboard.test") {
          errors.push("Unexpected external request"); return route.abort();
        }
        const headers = {"Content-Security-Policy": policy};
        if (url.pathname === "/app") return route.fulfill({contentType: "text/html", body: html, headers});
        if (url.pathname === "/login") return route.fulfill({contentType: "text/html", body: "<h1>Signed out</h1>", headers});
        if (["/assets/dashboard.js", "/assets/dashboard.css"].includes(url.pathname)) {
          return route.fulfill({contentType: url.pathname.endsWith(".js") ? "text/javascript" : "text/css",
            body: fs.readFileSync(path.join(assets, url.pathname.split("/").pop())), headers});
        }
        if (url.pathname === "/auth/session") {
          authRequests.push("session");
          return route.fulfill({contentType: "application/json", body: JSON.stringify({authenticated: true, csrf: "fake-csrf"}), headers});
        }
        if (url.pathname === "/auth/logout") {
          assert.equal(request.method(), "POST");
          const requestHeaders = await request.allHeaders();
          assert.equal(requestHeaders.origin, "https://dashboard.test");
          assert.equal(requestHeaders["x-csrf-token"], "fake-csrf");
          authRequests.push("logout");
          return route.fulfill({status: refuseLogout ? 403 : 200, contentType: "application/json",
            body: JSON.stringify(refuseLogout ? {detail: "Try signing out again."} : {ok: true}), headers});
        }
        const data = url.pathname === "/api/day" ? day : url.pathname === "/api/work" ? board
          : url.pathname === "/api/work/fake-task" ? item : undefined;
        if (!data) { errors.push("Unexpected local request"); return route.abort(); }
        return route.fulfill({contentType: "application/json", body: JSON.stringify(data), headers});
      });
      const page = await context.newPage();
      page.on("pageerror", error => errors.push(error.message));
      await page.goto("https://dashboard.test/app");
      await page.getByRole("heading", {name: "Fri 2 Oct"}).waitFor();
      const rectangles = await page.locator(".lane .slot").evaluateAll(nodes => nodes.map(node => {
        const r = node.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
      }));
      assert.equal(rectangles.length, 3);
      for (let a = 0; a < rectangles.length; a++) for (let b = a + 1; b < rectangles.length; b++) {
        const x = rectangles[a], y = rectangles[b];
        assert.ok(!(x.left < y.right && y.left < x.right && x.top < y.bottom && y.top < x.bottom),
          "Consecutive short entries must not cover each other");
      }
      // A larger hit area never changes the event's printed duration.
      assert.equal(await page.locator(".lane .slot .when").nth(0).textContent(), "11:00 – 11:15");
      assert.equal(await page.locator(".lane .slot .when").nth(1).textContent(), "11:15 – 11:30");
      await page.evaluate(() => {
        for (const name of ["Write controls", "Ask controls"]) window.dashboardActions.register({
          detail(host) { const node = document.createElement("h2"); node.textContent = name; host.replaceChildren(node); }
        });
      });
      await page.getByRole("button", {name: "Open details", exact: true}).click();
      await page.getByRole("heading", {name: "Test task", exact: true}).waitFor();
      assert.equal(await page.getByRole("heading", {name: "Write controls"}).count(), 1);
      assert.equal(await page.getByRole("heading", {name: "Ask controls"}).count(), 1);
      await page.getByRole("button", {name: "Sign out", exact: true}).click();
      await page.getByText("Try signing out again.", {exact: true}).waitFor();
      assert.equal(new URL(page.url()).pathname, "/app");
      assert.equal(await page.getByRole("button", {name: "Sign out", exact: true}).isEnabled(), true);
      refuseLogout = false;
      await page.getByRole("button", {name: "Sign out", exact: true}).click();
      await page.getByRole("heading", {name: "Signed out"}).waitFor();
      assert.deepEqual(authRequests, ["session", "logout", "session", "logout"]);
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log("Desktop and phone: short-entry collisions, composed actions, and CSRF sign-out passed offline.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
