// chttr ui: the pure helpers (no DOM), shared by app.js and the node tests in tests/js.
// Routes live in the URL fragment so the token never reaches a server log or a Referer:
//   #TOKEN                         the default view (home when registered, else meetings)
//   #TOKEN/meetings?q=budget       a search
//   #TOKEN/meeting/<id>?t=152      a meeting, at 00:02:32
//   #TOKEN/actions?owner=dana      action items for one owner
//   #TOKEN/lingo, #TOKEN/dictations?q=…, #TOKEN/<view>/<id>
"use strict";

(function (root) {
  // ---- routes -------------------------------------------------------------------------------
  function parseRoute(hash, known) {
    let s = String(hash || "").replace(/^#/, "");
    let query = "";
    const qi = s.indexOf("?");
    if (qi >= 0) { query = s.slice(qi + 1); s = s.slice(0, qi); }
    const parts = s.split("/");
    const token = parts[0] || "";
    let view = (parts[1] || "").toLowerCase();
    let id = "";
    try { id = parts.length > 2 ? decodeURIComponent(parts.slice(2).join("/")) : ""; } catch { id = ""; }
    if (view === "meeting") view = "meetings";                  // #TOKEN/meeting/<id>
    if (known) {
      const names = known instanceof Set ? known : new Set(known);
      if (!view) view = names.has("home") ? "home" : "meetings";
      else if (!names.has(view)) { view = "meetings"; id = ""; }
    }
    const params = new URLSearchParams(query);
    const t = params.has("t") && params.get("t") !== "" ? Number(params.get("t")) : null;
    return {
      token, view, id,
      t: Number.isFinite(t) && t >= 0 ? t : null,
      q: params.get("q") || "",
      owner: params.get("owner") || "",
    };
  }

  function buildRoute(route) {
    const r = route || {};
    let s = "#" + (r.token || "");
    if (r.view === "meetings" && r.id) s += "/meeting/" + encodeURIComponent(r.id);
    else if (r.view) s += "/" + r.view + (r.id ? "/" + encodeURIComponent(r.id) : "");
    const params = new URLSearchParams();
    if (r.q) params.set("q", r.q);
    if (r.owner) params.set("owner", r.owner);
    if (r.t !== null && r.t !== undefined && Number.isFinite(Number(r.t))) params.set("t", String(Math.round(Number(r.t) * 10) / 10));
    const qs = params.toString().replace(/\+/g, "%20");
    return qs ? `${s}?${qs}` : s;
  }

  // ---- time ---------------------------------------------------------------------------------
  function fmtTs(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map((n) => String(n).padStart(2, "0")).join(":");
  }

  // "1:02:03", "12:40" (minutes:seconds), "95" -> seconds; null when it isn't a time
  function parseTs(text) {
    const m = String(text || "").trim().match(/^(\d+)(?::(\d{1,2}))?(?::(\d{1,2}))?$/);
    if (!m) return null;
    const parts = [m[1], m[2], m[3]].filter((x) => x !== undefined).map(Number);
    while (parts.length < 3) parts.unshift(0);
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }

  // "12 min", "45 s", "1 h 05 min"
  function fmtDuration(seconds) {
    const s = Math.round(Number(seconds) || 0);
    if (s < 60) return `${s} s`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  }

  // ---- search box ---------------------------------------------------------------------------
  // Mirror of library.parse_query: speaker:/from:, after:/before:/on: are filters, the rest is words.
  const FILTER = /(?<!\S)(speaker|from|after|before|on):(?:"([^"]*)"?|(\S*))/gi;

  function isoDay(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function filterDate(value, now) {
    const v = String(value || "").trim().toLowerCase();
    if (v === "today" || v === "yesterday") {
      const d = new Date(now || Date.now());
      if (v === "yesterday") d.setDate(d.getDate() - 1);
      return isoDay(d);
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) return "";
    const [y, mo, da] = v.split("-").map(Number);
    const d = new Date(y, mo - 1, da);
    return d.getFullYear() === y && d.getMonth() === mo - 1 && d.getDate() === da ? v : "";
  }

  function parseQuery(text, now) {
    const out = { words: "", speaker: "", after: "", before: "" };
    const src = String(text || "");
    for (const m of src.matchAll(FILTER)) {
      const key = m[1].toLowerCase();
      const value = (m[2] !== undefined ? m[2] : m[3] || "").trim();
      if (key === "speaker" || key === "from") out.speaker = value;
      else if (key === "on") out.after = out.before = filterDate(value, now);
      else if (key === "after") out.after = filterDate(value, now);
      else out.before = filterDate(value, now);
    }
    out.words = src.replace(FILTER, " ").split(/\s+/).filter(Boolean).join(" ");
    return out;
  }

  // Search as you type: the server drops one-letter words and only prefix-matches from three
  // letters (library.fts_query), so "co" on its way to "contract" would come back empty.
  function tooShortToSearch(pq) {
    return !pq.speaker && !pq.after && !pq.before && pq.words.length > 0 && pq.words.length < 3;
  }

  // ---- transcripts --------------------------------------------------------------------------
  // Mirror of library.turns: consecutive lines by one speaker, less than `gap` s apart, are one turn.
  function turnsFromLines(lines, gap = 3) {
    const out = [];
    for (const u of lines || []) {
      const start = Number(u.start) || 0;
      const end = Math.max(Number(u.end) || start, start);
      const line = { start, end, text: String(u.text || "") };
      const last = out[out.length - 1];
      if (last && last.speaker === (u.speaker || "") && start - last.end < gap) {
        last.lines.push(line);
        last.end = Math.max(last.end, end);
        last.text += " " + line.text;
      } else {
        out.push({ speaker: u.speaker || "", start, end, text: line.text, lines: [line] });
      }
    }
    return out;
  }

  // label -> "var(--cN)", in order of first appearance, cycling through eight colours
  function speakerPalette(labels) {
    const map = new Map();
    for (const label of labels || []) {
      if (!map.has(label)) map.set(label, `var(--c${(map.size % 8) + 1})`);
    }
    return map;
  }

  // "Speaker 2", "In the room 1": voices nobody has named yet
  const isUnknownSpeaker = (label) => /^(speaker|in the room)\s*\d*$/i.test(String(label || "").trim());

  // seconds spoken per speaker, from turns (or lines)
  function talkTime(turns) {
    const out = new Map();
    for (const t of turns || []) out.set(t.speaker, (out.get(t.speaker) || 0) + Math.max(0, (Number(t.end) || 0) - (Number(t.start) || 0)));
    return out;
  }

  // index of the last line that starts at or before `time` (binary search over sorted starts); -1 before the first
  function lineAt(starts, time) {
    let lo = 0, hi = starts.length - 1, found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (starts[mid] <= time + 0.05) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return found;
  }

  // [start, end) of every case-insensitive occurrence of `needle` in `text`
  function findRanges(text, needle) {
    const out = [];
    const hay = String(text || "").toLocaleLowerCase();
    const n = String(needle || "").trim().toLocaleLowerCase();
    if (!n) return out;
    let i = hay.indexOf(n);
    while (i >= 0) {
      out.push([i, i + n.length]);
      i = hay.indexOf(n, i + n.length);
    }
    return out;
  }

  // `,` / `.` step the playback rate through these
  const RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];
  function stepRate(rate, direction) {
    const r = Number(rate) || 1;
    const i = RATES.reduce((best, x, k) => (Math.abs(x - r) < Math.abs(RATES[best] - r) ? k : best), 0);
    return RATES[Math.min(RATES.length - 1, Math.max(0, i + (direction > 0 ? 1 : -1)))];
  }

  // ---- request sequencing -------------------------------------------------------------------
  // const seq = nextSeq("search"); await …; if (!isLatest("search", seq)) return;  (drop stale answers)
  const counters = new Map();
  function nextSeq(channel = "") {
    const n = (counters.get(channel) || 0) + 1;
    counters.set(channel, n);
    return n;
  }
  const isLatest = (channel, n) => (counters.get(channel || "") || 0) === n;

  const lib = {
    parseRoute, buildRoute, fmtTs, parseTs, fmtDuration, parseQuery, tooShortToSearch, filterDate, turnsFromLines, speakerPalette,
    isUnknownSpeaker, talkTime, lineAt, findRanges, stepRate, RATES, nextSeq, isLatest,
  };
  if (root) root.chttrLib = lib;
  if (typeof module !== "undefined" && module.exports) module.exports = lib;
})(typeof window !== "undefined" ? window : null);
