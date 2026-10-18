// node --test tests/js: the pure helpers behind the Home page and the Dictations tab,
// and that both scripts register with app.js's view registry only when it is there.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "src", "chttr", "ui_static");
const home = require(path.join(STATIC, "home.js"));
const dictations = require(path.join(STATIC, "dictations.js"));

test("streakLabel", () => {
  assert.equal(home.streakLabel(0), "No streak yet");
  assert.equal(home.streakLabel(undefined), "No streak yet");
  assert.equal(home.streakLabel(1), "1 day");
  assert.equal(home.streakLabel(12), "12 days");
  assert.equal(home.streakLabel(1200), "1,200 days");
});

test("kpiRows: words, speed against typing, time saved, streak", () => {
  const rows = home.kpiRows({ dictations: 31, words: 4210, wpm: 132, saved_minutes: 73.4, streak_days: 4,
    best_day: { date: "2026-10-10", words: 900 } });
  assert.deepEqual(rows.map((r) => r.label), ["Words dictated", "Speaking speed", "Time saved", "Streak"]);
  assert.deepEqual(rows.map((r) => r.value), ["4,210", "132 wpm", "1 h 13 min", "4 days"]);
  assert.equal(rows[0].sub, "31 dictations");
  assert.equal(rows[1].sub, "vs ~40 typing · 3.3× faster");
  assert.equal(rows[3].sub, "Best day: Oct 10 · 900 words");
});

test("kpiRows with nothing dictated yet", () => {
  const rows = home.kpiRows({});
  assert.deepEqual(rows.map((r) => r.value), ["0", "–", "None yet", "No streak yet"]);
  assert.equal(rows[0].sub, "0 dictations");
});

test("fmtMinutes and fmtClock", () => {
  assert.equal(home.fmtMinutes(0), "0 min");
  assert.equal(home.fmtMinutes(0.5), "30 s");
  assert.equal(home.fmtMinutes(12.4), "12 min");
  assert.equal(home.fmtMinutes(120), "2 h");
  assert.equal(home.fmtClock(42), "00:42");
  assert.equal(home.fmtClock(3723), "1:02:03");
});

test("barHeights: proportional, a sliver for small days, zero for empty days", () => {
  const days = [{ words: 0, count: 0 }, { words: 100, count: 2 }, { words: 1, count: 1 }, { words: 50, count: 1 }];
  assert.deepEqual(home.barHeights(days, 64), [0, 64, 3, 32]);
  assert.deepEqual(home.barHeights([{ words: 0, count: 0 }], 64), [0]);
  assert.deepEqual(home.barHeights([], 64), []);
});

test("statusLabel", () => {
  assert.deepEqual(home.statusLabel({ daemon: false }), { cls: "off", text: "Daemon not running" });
  assert.equal(home.statusLabel({ daemon: true, state: "idle" }).text, "Idle");
  assert.equal(home.statusLabel({ daemon: true, state: "transcribing" }).text, "Transcribing");
  assert.equal(home.statusLabel({ daemon: true, state: "recording" }, 1000, 43000).text, "● Recording 00:42");
  assert.equal(home.statusLabel({ daemon: true, state: "recording", meeting: { seconds: 600, title: "Standup" } }).text,
    "● Meeting 10:00 · Standup");
});

test("groupByDay: Today, Yesterday, then dates", () => {
  const now = new Date(2026, 9, 18, 9, 30);
  const at = (d, h) => new Date(2026, 9, d, h).toISOString();
  const groups = dictations.groupByDay([
    { id: "5", ts: at(18, 9) }, { id: "4", ts: at(18, 0) }, { id: "3", ts: at(17, 23) },
    { id: "2", ts: at(10, 12) }, { id: "1", ts: new Date(2025, 11, 31, 12).toISOString() }, { id: "0", ts: "" },
  ], now);
  assert.deepEqual(groups.map((g) => g.label), ["Today", "Yesterday", "Oct 10", "Dec 31, 2025", "Unknown date"]);
  assert.deepEqual(groups.map((g) => g.items.map((e) => e.id)), [["5", "4"], ["3"], ["2"], ["1"], ["0"]]);
  assert.deepEqual(dictations.groupByDay([], now), []);
});

test("groupByDay: yesterday across a month boundary", () => {
  const groups = dictations.groupByDay([{ ts: new Date(2026, 9, 31, 20).toISOString() }], new Date(2026, 10, 1, 8));
  assert.equal(groups[0].label, "Yesterday");
});

test("diffNodesModel", () => {
  assert.deepEqual(dictations.diffNodesModel([["del", "um so"], ["ins", "So"], ["equal", "we ship"]]), [
    { tag: "del", text: "um so" }, { tag: null, text: " " }, { tag: "ins", text: "So" }, { tag: null, text: " " },
    { tag: "span", text: "we ship" }]);
  assert.deepEqual(dictations.diffNodesModel([["equal", "same"]]), [{ tag: "span", text: "same" }]);
  assert.deepEqual(dictations.diffNodesModel([["bogus", "x"], ["del", ""]]), []);
  assert.deepEqual(dictations.diffNodesModel(undefined), []);
  assert.equal(dictations.changed([["equal", "same"]]), false);
  assert.equal(dictations.changed([["equal", "a"], ["ins", "b"]]), true);
});

test("metaLine", () => {
  assert.equal(dictations.metaLine({ app: "Slack", seconds: 6.24, output: "type" }), "Slack · 6.2 s · typed");
  assert.equal(dictations.metaLine({ app: "", seconds: 1, output: "clipboard", kind: "command" }),
    "command · 1.0 s · copied to clipboard");
});

// Run a script the way a browser would (no `module`), with a fake window.
function load(file, win) {
  const listeners = {};
  const fakeWindow = win && Object.assign(win, {
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  });
  const context = { console, setTimeout, clearTimeout, URLSearchParams };
  if (fakeWindow) Object.assign(context, { window: fakeWindow, document: { getElementById: () => null, querySelector: () => null } });
  vm.runInNewContext(fs.readFileSync(path.join(STATIC, file), "utf8"), context, { filename: file });
  return listeners;
}

for (const [file, name] of [["home.js", "home"], ["dictations.js", "dictations"]]) {
  test(`${file} is a no-op without the app, and registers its view when the app is there`, () => {
    load(file, null);                                        // no window at all: nothing happens, nothing throws
    const views = {};
    const registry = { registerView: (n, v) => { views[n] = v; } };
    let listeners = load(file, { chttr: registry });
    assert.deepEqual(Object.keys(views), [name]);
    assert.equal(typeof views[name].detail, "function");
    assert.equal(typeof views[name].list, "function");
    assert.equal(listeners["chttr:ready"], undefined);

    delete views[name];
    const win = {};
    listeners = load(file, win);                             // app.js not ready yet: waits for chttr:ready
    assert.deepEqual(Object.keys(views), []);
    win.chttr = registry;
    listeners["chttr:ready"].forEach((fn) => fn());
    assert.deepEqual(Object.keys(views), [name]);
  });
}

// ---- the views themselves, against a tiny fake DOM and a fake app.js -----------------------------
// Just enough of a DOM for the views: nodes with children, attributes and isConnected.
function fakeDom() {
  class Node {
    constructor(tag) { Object.assign(this, { tag, children: [], parent: null, attrs: {}, className: "", hidden: false, _text: "" }); }
    get isConnected() { let n = this; while (n.parent) n = n.parent; return n === root; }
    append(...kids) { for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false) { if (typeof k === "object") k.parent = this; this.children.push(k); } }
    replaceChildren(...kids) { this.children.forEach((k) => { if (typeof k === "object") k.parent = null; }); this.children = []; this.append(...kids); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((k) => k !== this); this.parent = null; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    removeAttribute(k) { delete this.attrs[k]; }
    set textContent(v) { this.children = [String(v)]; }
    get textContent() { return this.children.map((k) => (typeof k === "object" ? k.textContent : k)).join(""); }
    querySelector() { return null; }
    all() { return [this, ...this.children.filter((k) => typeof k === "object").flatMap((k) => k.all())]; }
  }
  const root = new Node("body");
  const el = (tag, attrs = {}, ...children) => {
    const n = new Node(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n[k] = v;
      else n.setAttribute(k, v === true ? "" : v);
    }
    n.append(...children);
    return n;
  };
  const document = { hidden: false, body: root, createElementNS: (ns, tag) => new Node(tag), getElementById: () => null, querySelector: () => null };
  return { root, el, document };
}

// Fake timers: run them by hand.
function fakeTimers() {
  let next = 1;
  const intervals = new Map(), timeouts = new Map();
  return {
    intervals, timeouts,
    setInterval: (fn) => { intervals.set(next, fn); return next++; },
    clearInterval: (id) => { intervals.delete(id); },
    setTimeout: (fn) => { timeouts.set(next, fn); return next++; },
    clearTimeout: (id) => { timeouts.delete(id); },
    runTimeouts() { const fns = [...timeouts.values()]; timeouts.clear(); fns.forEach((fn) => fn()); },
    runIntervals() { [...intervals.values()].forEach((fn) => fn()); },
  };
}

const flush = () => new Promise((r) => setImmediate(r));

function boot(file, answers) {
  const dom = fakeDom(), timers = fakeTimers(), calls = [], views = {};
  const chttr = {
    el: dom.el, state: { setup: null }, toast: () => {}, navigate: () => {}, openMeeting: () => {},
    registerView: (n, v) => { views[n] = v; },
    api: async (p) => {
      calls.push(p);
      const path = p.split("?")[0];
      if (!(path in answers)) throw new Error("not found");
      return typeof answers[path] === "function" ? answers[path](p) : answers[path];
    },
  };
  const context = { console, URLSearchParams, window: { chttr, addEventListener: () => {} }, document: dom.document,
    navigator: {}, sessionStorage: { setItem() {} }, location: { hash: "" },
    setInterval: timers.setInterval, clearInterval: timers.clearInterval, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout };
  vm.runInNewContext(fs.readFileSync(path.join(STATIC, file), "utf8"), context, { filename: file });
  return { dom, timers, calls, views };
}

const DASHBOARD = { history: { dictations: 0, words: 0, per_day: [], top_apps: [] }, meetings: { recent: [], open_actions: 0 } };

test("Home's status pill polls once it is on screen and stops once it is gone", async () => {
  let status = { daemon: true, state: "idle", meeting: null };
  const { dom, timers, calls, views } = boot("home.js", { "/api/dashboard": DASHBOARD, "/api/setup": {}, "/api/status": () => status });
  const statusCalls = () => calls.filter((p) => p === "/api/status").length;
  const page = await views.home.detail({});
  const pill = page.all().find((n) => /status-pill/.test(n.className));
  assert.equal(statusCalls(), 0);
  timers.runTimeouts();                                   // the first tick, before app.js attached the page: waits
  await flush();
  assert.equal(statusCalls(), 0);
  assert.equal(timers.intervals.size, 1);

  dom.root.append(page);                                  // app.js puts it on screen, then calls mounted()
  views.home.mounted({});
  await flush();
  assert.equal(statusCalls(), 1);
  assert.equal(pill.textContent, "Idle");
  assert.equal(pill.className, "status-pill idle");

  status = { daemon: true, state: "recording", meeting: null };
  timers.runIntervals();
  await flush();
  assert.equal(statusCalls(), 2);
  assert.equal(pill.textContent, "● Recording 00:00");
  status = { daemon: false, state: "idle", meeting: null };
  timers.runIntervals();
  await flush();
  assert.equal(pill.textContent, "Daemon not running");

  page.remove();                                          // another view replaced Home
  timers.runIntervals();
  await flush();
  assert.equal(statusCalls(), 3);
  assert.equal(timers.intervals.size, 0);
});

test("a Home page that never makes it on screen stops polling by itself", async () => {
  const { timers, calls, views } = boot("home.js", { "/api/dashboard": DASHBOARD, "/api/setup": {}, "/api/status": { daemon: true, state: "idle" } });
  await views.home.detail({});
  for (let i = 0; i < 10; i++) timers.runIntervals();
  await flush();
  assert.equal(timers.intervals.size, 0);
  assert.equal(calls.filter((p) => p === "/api/status").length, 0);
});

test("Home asks for /api/setup on every visit, so a new Claude key shows without a reload", async () => {
  let backend = "none";
  const { calls, views } = boot("home.js", { "/api/dashboard": DASHBOARD, "/api/setup": () => ({ cleanup_backend: backend }), "/api/status": {} });
  const cleanupCard = (page) => page.all().some((n) => n.tag === "h2" && n.textContent === "Cleanup");
  assert.equal(cleanupCard(await views.home.detail({})), true);
  backend = "claude";
  assert.equal(cleanupCard(await views.home.detail({})), false);
  assert.equal(calls.filter((p) => p === "/api/setup").length, 2);
});

const ENTRIES = [
  { id: "2", ts: new Date().toISOString(), app: "foot", kind: "command", seconds: 1, output: "paste", raw: "make it shorter", text: "Short.", typed: true, has_audio: false },
  { id: "1", ts: new Date().toISOString(), app: "Slack", kind: "dictate", seconds: 2, output: "", raw: "polish failed", text: "polish failed", typed: false, has_audio: false },
  { id: "0", ts: new Date().toISOString(), app: "Slack", kind: "dictate", seconds: 4, output: "type", raw: "um ping the vendor", text: "Ping the vendor.", typed: true, has_audio: false },
];

test("Dictations: searches dictations, rows carry data-id for app.js's highlight", async () => {
  const { views } = boot("dictations.js", { "/api/dictations": ENTRIES, "/api/setup": {} });
  const v = views.dictations;
  assert.equal(v.placeholder, "Search your dictations…");
  assert.equal(v.searchLabel, "Search dictations");
  const box = v.list({ id: "0", q: "" });
  await flush();
  const rows = box.all().filter((n) => n.attrs["data-id"] !== undefined);
  assert.deepEqual(rows.map((n) => n.attrs["data-id"]), ["2", "1", "0"]);
  assert.equal(rows[2].attrs["aria-current"], "true");
  assert.match(rows[1].textContent, /nothing typed/);
  // opening another dictation with the same search keeps the list; different words rebuild it
  const key = v.listKey({ q: "" });
  assert.equal(v.listKey({ q: "", id: "2" }), key);
  assert.notEqual(v.listKey({ q: "vendor" }), key);
});

test("Dictations: the heard-vs-typed diff only for dictations chttr typed", async () => {
  const byId = Object.fromEntries(ENTRIES.map((e) => [e.id, e]));
  const detail = { ...byId["0"], words: 3, diff: [["del", "um"], ["equal", "Ping the vendor."]] };
  const { views } = boot("dictations.js", {
    "/api/dictation/0": detail,
    "/api/dictation/1": { ...byId["1"], words: 2, diff: [] },
    "/api/dictation/2": { ...byId["2"], words: 1, diff: [] },
  });
  const show = async (id) => {
    const nodes = (await views.dictations.detail({ id })).all();
    return { tags: nodes.map((n) => n.tag), text: nodes[0].textContent };
  };
  const typed = await show("0");
  assert.ok(typed.tags.includes("del"));
  assert.match(typed.text, /What chttr heard/);
  assert.match(typed.text, /typed/);

  const command = await show("2");
  assert.ok(!command.tags.includes("del") && !command.tags.includes("ins"));
  assert.match(command.text, /You said\s*make it shorter/);
  assert.doesNotMatch(command.text, /dropped by cleanup/);

  const failed = await show("1");
  assert.ok(!failed.tags.includes("del"));
  assert.match(failed.text, /Nothing was typed for this one/);
  assert.doesNotMatch(failed.text, /What chttr heard|· typed/);
});
