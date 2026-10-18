// node --test tests/js: the URL fragment routes of chttr ui (src/chttr/ui_static/lib.js).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "../../src/chttr/ui_static/lib.js"));

const KNOWN = ["meetings", "actions", "lingo"];

test("parses every documented route", () => {
  assert.deepEqual(L.parseRoute("#TOKEN"), { token: "TOKEN", view: "", id: "", t: null, q: "", owner: "" });
  assert.equal(L.parseRoute("#TOKEN", KNOWN).view, "meetings");
  assert.equal(L.parseRoute("#TOKEN", [...KNOWN, "home"]).view, "home");
  assert.equal(L.parseRoute("#TOKEN/home", [...KNOWN, "home"]).view, "home");
  assert.deepEqual(L.parseRoute("#TOKEN/meetings?q=budget%20review"), { token: "TOKEN", view: "meetings", id: "", t: null, q: "budget review", owner: "" });
  assert.deepEqual(L.parseRoute("#TOKEN/meeting/2026-10-01-140000?t=152"), { token: "TOKEN", view: "meetings", id: "2026-10-01-140000", t: 152, q: "", owner: "" });
  assert.equal(L.parseRoute("#TOKEN/actions?owner=Dana").owner, "Dana");
  assert.equal(L.parseRoute("#TOKEN/lingo").view, "lingo");
  assert.deepEqual(L.parseRoute("#TOKEN/dictations?q=vendor", [...KNOWN, "dictations"]), { token: "TOKEN", view: "dictations", id: "", t: null, q: "vendor", owner: "" });
  assert.equal(L.parseRoute("TOKEN/lingo").token, "TOKEN");                 // location.hash without its #
});

test("t is a number, or null when it isn't one", () => {
  assert.strictEqual(L.parseRoute("#T/meeting/x?t=12.5").t, 12.5);
  assert.strictEqual(L.parseRoute("#T/meeting/x?t=0").t, 0);
  assert.strictEqual(L.parseRoute("#T/meeting/x?t=soon").t, null);
  assert.strictEqual(L.parseRoute("#T/meeting/x?t=").t, null);
  assert.strictEqual(L.parseRoute("#T/meeting/x?t=-4").t, null);
});

test("an unknown view falls back to meetings", () => {
  assert.equal(L.parseRoute("#T/nonsense/42", KNOWN).view, "meetings");
  assert.equal(L.parseRoute("#T/nonsense/42", KNOWN).id, "");
  assert.equal(L.parseRoute("#T/home", KNOWN).view, "meetings");           // home.js not loaded
  assert.equal(L.parseRoute("#T/LINGO", KNOWN).view, "lingo");
});

test("routes round-trip", () => {
  const cases = [
    { token: "abc_-9", view: "meetings", id: "", t: null, q: "", owner: "" },
    { token: "abc", view: "meetings", id: "", t: null, q: "speaker:dana after:2026-10-01 budget", owner: "" },
    { token: "abc", view: "meetings", id: "2026-10-01-140000", t: 152, q: "", owner: "" },
    { token: "abc", view: "meetings", id: "2026-10-01-140000", t: 7.5, q: "contract & co", owner: "" },
    { token: "abc", view: "actions", id: "", t: null, q: "", owner: "Dana Lee" },
    { token: "abc", view: "lingo", id: "", t: null, q: "", owner: "" },
    { token: "abc", view: "dictations", id: "17", t: null, q: "a/b?c#d", owner: "" },
  ];
  for (const r of cases) assert.deepEqual(L.parseRoute(L.buildRoute(r)), r);
});

test("buildRoute writes the short forms", () => {
  assert.equal(L.buildRoute({ token: "T", view: "meetings", id: "2026-10-01-140000", t: 152 }), "#T/meeting/2026-10-01-140000?t=152");
  assert.equal(L.buildRoute({ token: "T", view: "meetings", q: "a b" }), "#T/meetings?q=a%20b");
  assert.equal(L.buildRoute({ token: "T", view: "actions", owner: "dana" }), "#T/actions?owner=dana");
  assert.equal(L.buildRoute({ token: "T" }), "#T");
  assert.equal(L.buildRoute({ token: "T", view: "meetings", id: "x", t: 12.345 }), "#T/meeting/x?t=12.3");
});
