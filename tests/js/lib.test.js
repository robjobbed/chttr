// node --test tests/js: the pure helpers behind chttr ui (src/chttr/ui_static/lib.js).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "../../src/chttr/ui_static/lib.js"));

test("timestamps", () => {
  assert.equal(L.fmtTs(0), "00:00:00");
  assert.equal(L.fmtTs(152.9), "00:02:32");
  assert.equal(L.fmtTs(3725), "01:02:05");
  assert.equal(L.fmtTs(-3), "00:00:00");
  assert.equal(L.parseTs("12:40"), 760);
  assert.equal(L.parseTs("1:02:05"), 3725);
  assert.equal(L.parseTs("95"), 95);
  assert.equal(L.parseTs("soon"), null);
  assert.equal(L.fmtDuration(42), "42 s");
  assert.equal(L.fmtDuration(720), "12 min");
  assert.equal(L.fmtDuration(3900), "1 h 05 min");
});

test("parseQuery mirrors library.parse_query", () => {
  assert.deepEqual(L.parseQuery("budget speaker:dana after:2026-10-01"), { words: "budget", speaker: "dana", after: "2026-10-01", before: "" });
  assert.deepEqual(L.parseQuery('from:"Dana Lee" before:2026-10-31 the plan'), { words: "the plan", speaker: "Dana Lee", after: "", before: "2026-10-31" });
  assert.deepEqual(L.parseQuery("on:2026-10-08 launch"), { words: "launch", speaker: "", after: "2026-10-08", before: "2026-10-08" });
  assert.deepEqual(L.parseQuery("after:2026-1 budget"), { words: "budget", speaker: "", after: "", before: "" });
  assert.deepEqual(L.parseQuery("after:2026-02-30"), { words: "", speaker: "", after: "", before: "" });
  assert.deepEqual(L.parseQuery("email:dana@x.com"), { words: "email:dana@x.com", speaker: "", after: "", before: "" });
  const now = new Date(2026, 9, 18, 9, 30);
  assert.deepEqual(L.parseQuery("on:yesterday", now), { words: "", speaker: "", after: "2026-10-17", before: "2026-10-17" });
  assert.equal(L.parseQuery("SPEAKER:Ali").speaker, "Ali");
});

test("turnsFromLines mirrors library.turns", () => {
  const lines = [
    { start: 0, end: 2, speaker: "Dana", text: "Hi." },
    { start: 3.5, end: 5, speaker: "Dana", text: "Shall we start?" },
    { start: 9, end: 10, speaker: "Dana", text: "Hello?" },
    { start: 10.5, end: 12, speaker: "Ali", text: "Yes." },
    { start: 12.2, speaker: "Dana", text: "Good." },
  ];
  const turns = L.turnsFromLines(lines);
  assert.deepEqual(turns.map((t) => [t.speaker, t.start, t.end, t.text]), [
    ["Dana", 0, 5, "Hi. Shall we start?"], ["Dana", 9, 10, "Hello?"], ["Ali", 10.5, 12, "Yes."], ["Dana", 12.2, 12.2, "Good."]]);
  assert.deepEqual(turns[0].lines.map((x) => x.text), ["Hi.", "Shall we start?"]);
  assert.equal(L.turnsFromLines(lines, 10).length, 3);
  assert.deepEqual(L.turnsFromLines([]), []);
});

test("speakerPalette colours by first appearance and wraps after eight", () => {
  const p = L.speakerPalette(["Dana", "Ali", "Dana", "Rob", "a", "b", "c", "d", "e", "f"]);
  assert.equal(p.get("Dana"), "var(--c1)");
  assert.equal(p.get("Ali"), "var(--c2)");
  assert.equal(p.get("Rob"), "var(--c3)");
  assert.equal(p.get("e"), "var(--c8)");
  assert.equal(p.get("f"), "var(--c1)");
  assert.equal(p.size, 9);
});

test("unknown speakers, talk time, the line playing", () => {
  assert.ok(L.isUnknownSpeaker("Speaker 2"));
  assert.ok(L.isUnknownSpeaker("In the room 1"));
  assert.ok(!L.isUnknownSpeaker("Dana"));
  const talk = L.talkTime([{ speaker: "A", start: 0, end: 10 }, { speaker: "B", start: 10, end: 12 }, { speaker: "A", start: 12, end: 15 }]);
  assert.equal(talk.get("A"), 13);
  assert.equal(talk.get("B"), 2);
  const starts = [0, 5, 9.5, 20];
  assert.equal(L.lineAt(starts, -1), -1);
  assert.equal(L.lineAt(starts, 0), 0);
  assert.equal(L.lineAt(starts, 9.47), 2);                     // a hair early still counts
  assert.equal(L.lineAt(starts, 19), 2);
  assert.equal(L.lineAt(starts, 99), 3);
  assert.equal(L.lineAt([], 3), -1);
});

test("findRanges for find-in-transcript", () => {
  assert.deepEqual(L.findRanges("Budget, budget, BUDGET", "budget"), [[0, 6], [8, 14], [16, 22]]);
  assert.deepEqual(L.findRanges("aaaa", "aa"), [[0, 2], [2, 4]]);
  assert.deepEqual(L.findRanges("anything", "  "), []);
  assert.deepEqual(L.findRanges("", "x"), []);
});

test("playback rate steps between 0.75x and 2x", () => {
  assert.equal(L.stepRate(1, 1), 1.25);
  assert.equal(L.stepRate(1, -1), 0.75);
  assert.equal(L.stepRate(0.75, -1), 0.75);
  assert.equal(L.stepRate(2, 1), 2);
  assert.equal(L.stepRate(1.1, 1), 1.25);
  assert.equal(L.stepRate(undefined, 1), 1.25);
});

test("request sequencing drops stale answers per channel", () => {
  const a = L.nextSeq("search");
  const b = L.nextSeq("search");
  const m = L.nextSeq("meeting");
  assert.ok(!L.isLatest("search", a));
  assert.ok(L.isLatest("search", b));
  assert.ok(L.isLatest("meeting", m));                          // channels don't interfere
  assert.ok(b > a);
});
