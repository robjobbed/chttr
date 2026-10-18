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
