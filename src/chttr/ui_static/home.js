// chttr Home: how much you dictated, how fast, your streak, the last 30 days, recent meetings and
// what the daemon is doing right now. Registers itself with app.js's view registry
// (window.chttr.registerView); does nothing without it. The pure helpers are tested with node.
"use strict";

(function () {
  const TYPING_WPM = 40;
  const STRIP_HEIGHT = 64;           // px, the tallest bar in the 30-day strip
  const POLL_MS = 2000;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // ---- pure helpers -----------------------------------------------------------------------
  const plural = (n, one, many = one + "s") => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;

  function streakLabel(days) {
    days = Math.max(0, Math.floor(Number(days) || 0));
    return days ? plural(days, "day") : "No streak yet";
  }

  function fmtMinutes(min) {
    min = Math.max(0, Number(min) || 0);
    if (min < 1) return min * 60 >= 1 ? `${Math.round(min * 60)} s` : "0 min";
    if (min < 60) return `${Math.round(min)} min`;
    const h = Math.floor(min / 60), m = Math.round(min % 60);
    return m ? `${h} h ${String(m).padStart(2, "0")} min` : `${h} h`;
  }

  function fmtClock(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const mmss = `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
    return h ? `${h}:${mmss}` : mmss;
  }

  function shortDate(iso) {
    const [y, m, d] = String(iso || "").split("-").map(Number);
    return y && m && d ? `${MONTHS[m - 1]} ${d}` : "";
  }

  // The four headline numbers, as {key, label, value, sub} rows.
  function kpiRows(summary, typingWpm = TYPING_WPM) {
    const s = summary || {};
    const words = Number(s.words) || 0, wpm = Number(s.wpm) || 0, saved = Number(s.saved_minutes) || 0;
    const best = s.best_day;
    return [
      { key: "words", label: "Words dictated", value: words.toLocaleString("en-US"),
        sub: plural(Number(s.dictations) || 0, "dictation") },
      { key: "speed", label: "Speaking speed", value: wpm ? `${wpm} wpm` : "–",
        sub: wpm ? `vs ~${typingWpm} typing${wpm > typingWpm ? ` · ${(wpm / typingWpm).toFixed(1)}× faster` : ""}` : "Dictate something to see it" },
      { key: "saved", label: "Time saved", value: saved > 0 ? fmtMinutes(saved) : "None yet",
        sub: saved > 0 ? `against typing at ${typingWpm} wpm` : "Longer dictations save the most" },
      { key: "streak", label: "Streak", value: streakLabel(s.streak_days),
        sub: best && best.words ? `Best day: ${shortDate(best.date)} · ${plural(best.words, "word")}` : "Dictate on consecutive days" },
    ];
  }

  // Bar heights in px for the strip: proportional to words, a sliver for any day with a dictation.
  function barHeights(perDay, max = STRIP_HEIGHT) {
    const days = perDay || [];
    const peak = Math.max(0, ...days.map((d) => Number(d.words) || 0));
    return days.map((d) => {
      const w = Number(d.words) || 0, n = Number(d.count) || 0;
      if (!w && !n) return 0;
      return Math.max(3, Math.round((max * w) / (peak || 1)));
    });
  }

  // The status pill: {cls, text} from /api/status. `since` is when we first saw this recording.
  function statusLabel(status, since = null, now = Date.now()) {
    if (!status || status.daemon === false) return { cls: "off", text: "Daemon not running" };
    const meeting = status.meeting;
    if (meeting) {
      const secs = Number(meeting.seconds) || 0;
      return { cls: "meeting", text: `● Meeting ${fmtClock(secs)}${meeting.title ? ` · ${meeting.title}` : ""}` };
    }
    if (status.state === "recording") return { cls: "recording", text: `● Recording ${fmtClock(since ? (now - since) / 1000 : 0)}` };
    if (status.state === "transcribing") return { cls: "busy", text: "Transcribing" };
    if (status.state === "loading") return { cls: "busy", text: "Loading the speech model" };
    return { cls: "idle", text: "Idle" };
  }

  const helpers = { streakLabel, fmtMinutes, fmtClock, shortDate, kpiRows, barHeights, statusLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // ---- the view --------------------------------------------------------------------------------
  const SVG = "http://www.w3.org/2000/svg";
  let pollTimer = null;
  let kickPill = null;              // the current pill's first poll, for view.mounted()

  // Asked afresh on every render, so `chttr connect claude` or a new hotkey shows without a reload.
  function getSetup(c) {
    return c.api("/api/setup").catch(() => (c.state && c.state.setup) || {});
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // no clipboard API (or no permission): select a hidden textarea and copy that
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      try {
        area.select();
        return document.execCommand("copy");
      } catch {
        return false;
      } finally {
        area.remove();
      }
    }
  }

  function kpiGrid(c, summary, typingWpm) {
    return c.el("div", { class: "kpis" }, kpiRows(summary, typingWpm).map((k) =>
      c.el("div", { class: `kpi kpi-${k.key}` },
        c.el("div", { class: "kpi-label" }, k.label),
        c.el("div", { class: "kpi-value" }, k.value),
        c.el("div", { class: "kpi-sub" }, k.sub))));
  }

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
    return node;
  }

  function activityStrip(c, perDay) {
    const days = perDay || [];
    const heights = barHeights(days, STRIP_HEIGHT);
    const slot = 10, gap = 3, width = days.length * slot;
    const chart = svg("svg", { class: "strip", viewBox: `0 0 ${width} ${STRIP_HEIGHT}`, preserveAspectRatio: "none",
      role: "img", "aria-label": `Words dictated per day, last ${days.length} days` });
    days.forEach((d, i) => {
      const h = heights[i];
      const bar = svg("rect", { x: i * slot + gap / 2, y: STRIP_HEIGHT - (h || 2), width: slot - gap, height: h || 2, rx: 1.5,
        class: (h ? "bar" : "bar zero") + (i === days.length - 1 ? " today" : "") });
      const tip = svg("title");
      tip.textContent = `${shortDate(d.date)}: ${d.count ? `${plural(d.words, "word")} in ${plural(d.count, "dictation")}` : "nothing dictated"}`;
      bar.append(tip);
      chart.append(bar);
    });
    const active = days.filter((d) => d.count).length;
    return c.el("div", { class: "card wide" },
      c.el("div", { class: "card-head" }, c.el("h2", {}, `Last ${days.length} days`),
        c.el("span", { class: "meta" }, active ? `active on ${plural(active, "day")}` : "nothing yet")),
      chart,
      c.el("div", { class: "strip-axis meta" }, c.el("span", {}, days.length ? shortDate(days[0].date) : ""), c.el("span", {}, "Today")));
  }

  function topApps(c, apps) {
    const top = (apps || []).filter((a) => a.words > 0);
    const peak = Math.max(1, ...top.map((a) => a.words));
    return c.el("div", { class: "card" },
      c.el("h2", {}, "Top apps"),
      top.length ? c.el("ul", { class: "hbars" }, top.map((a) =>
        c.el("li", {},
          c.el("span", { class: "hbar-name", title: a.app }, a.app),
          c.el("span", { class: "hbar-track" }, c.el("span", { class: "hbar-fill", style: `width: ${Math.max(2, Math.round((100 * a.words) / peak))}%` })),
          c.el("span", { class: "hbar-n meta" }, a.words.toLocaleString("en-US")))))
        : c.el("p", { class: "meta" }, "Where you dictate shows up here."));
  }

  function recentMeetings(c, meetings, setup) {
    const recent = (meetings && meetings.recent) || [];
    const key = setup && setup.hotkeys && setup.hotkeys.meeting;
    return c.el("div", { class: "card" },
      c.el("div", { class: "card-head" }, c.el("h2", {}, "Recent meetings"),
        meetings && meetings.meetings_total ? c.el("button", { class: "link", type: "button", onclick: () => c.navigate({ view: "meetings" }) },
          `All ${meetings.meetings_total}`) : null),
      recent.length ? c.el("div", { class: "meeting-cards" }, recent.map((m) =>
        c.el("button", { class: "meeting-card", type: "button", onclick: () => c.openMeeting(m.id) },
          c.el("div", { class: "title" }, m.title || "Meeting"),
          c.el("div", { class: "meta" }, [c.when ? c.when(m.when) : m.when, m.minutes ? `${m.minutes} min` : ""].filter(Boolean).join(" · ")),
          (m.speakers || []).length ? c.el("div", { class: "meta people" }, m.speakers.slice(0, 4).join(", ")) : null)))
        : c.el("p", { class: "meta" }, key ? ["No meetings yet. Press ", c.el("kbd", {}, key), " to record one, or just join a call."]
          : "No meetings yet."));
  }

  function actionsCard(c, meetings) {
    const n = (meetings && meetings.open_actions) || 0;
    return c.el("button", { class: "card stat-card", type: "button", onclick: () => c.navigate({ view: "actions" }),
      "aria-label": `${plural(n, "open action item")}; show them` },
      c.el("h2", {}, "Open action items"),
      c.el("div", { class: "kpi-value" }, n.toLocaleString("en-US")),
      c.el("div", { class: "meta" }, n ? "from your meetings · see all →" : "Action items from meeting notes land here"));
  }

  function cleanupCard(c) {
    const cmd = "chttr connect claude";
    return c.el("div", { class: "card wide hint-card" },
      c.el("h2", {}, "Cleanup"),
      c.el("p", {}, "Dictation is cleaned up locally. Add a Claude key for Wispr-style cleanup: ", c.el("code", {}, cmd)),
      c.el("button", { class: "primary", type: "button",
        onclick: async () => c.toast((await copyText(cmd)) ? "Copied. Paste it into a terminal." : "Couldn't copy; select the command instead.", false) },
      "Copy command"));
  }

  function highlights(c, h) {
    const bits = [];
    if (h.longest_dictation) bits.push(`Longest dictation ${h.longest_dictation.seconds.toFixed(1)} s`);
    if (h.seconds) bits.push(`${fmtMinutes(h.seconds / 60)} of talking`);
    return bits.length ? c.el("p", { class: "meta highlights" }, bits.join(" · ")) : null;
  }

  // ---- live status pill --------------------------------------------------------------------------
  // The pill is built before app.js puts the Home page on screen, so a tick that finds it detached
  // waits for it. It stops polling once the pill has been shown and then replaced, or when it never
  // shows up at all (a newer render won).
  function statusPill(c) {
    const pill = c.el("span", { class: "status-pill", title: "What chttr is doing right now" }, "Checking…");   // not a live region: it ticks every 2 s
    let since = null, last = "", seen = false, misses = 0, busy = false, polled = false, timer = null;
    const stop = () => {
      clearInterval(timer);
      if (pollTimer === timer) pollTimer = null;
      if (kickPill === kick) kickPill = null;
    };
    const tick = async () => {
      if (pill.isConnected) seen = true;
      else {
        if (seen || ++misses > 5) stop();
        return;
      }
      if (busy || document.hidden) return;
      busy = polled = true;
      let st;
      try {
        st = await c.api("/api/status");
      } catch (e) {
        if (/not found/i.test(e.message)) { pill.hidden = true; stop(); return; }   // a server without /api/status
        st = { daemon: false };
      } finally {
        busy = false;
      }
      if (st && st.state === "recording" && !st.meeting) since = since || Date.now();
      else since = null;
      const { cls, text } = statusLabel(st, since);
      pill.className = `status-pill ${cls}`;
      if (text !== last) pill.textContent = last = text;
    };
    const kick = () => { if (!polled) tick(); };     // the first poll, as soon as the pill is on screen
    clearInterval(pollTimer);
    pollTimer = timer = setInterval(tick, POLL_MS);
    kickPill = kick;
    setTimeout(kick, 0);
    return pill;
  }

  async function renderHome(c) {
    const [data, setup] = await Promise.all([c.api("/api/dashboard"), getSetup(c)]);
    const h = data.history || {};
    const typingWpm = data.typing_wpm || TYPING_WPM;
    const dictateKey = setup.hotkeys && setup.hotkeys.dictate;
    return c.el("div", { class: "home-view" },
      c.el("div", { class: "home-head" }, c.el("h1", {}, "Home"), statusPill(c)),
      h.dictations ? highlights(c, h) : c.el("p", { class: "meta" },
        dictateKey ? ["Hold ", c.el("kbd", {}, dictateKey), " and talk; chttr types it where your cursor is."]
          : "Hold your dictation key and talk; chttr types it where your cursor is."),
      kpiGrid(c, h, typingWpm),
      c.el("div", { class: "cards" },
        activityStrip(c, h.per_day),
        topApps(c, h.top_apps),
        actionsCard(c, data.meetings),
        recentMeetings(c, data.meetings, setup),
        setup.cleanup_backend === "none" ? cleanupCard(c) : null));
  }

  function register() {
    const c = window.chttr;
    if (!c || typeof c.registerView !== "function" || register.done) return;
    register.done = true;
    c.registerView("home", {
      title: "Home",
      list() { return null; },                       // no list: app.js hides the left pane
      detail() { return renderHome(c); },
      mounted() { if (kickPill) kickPill(); },
    });
  }

  if (window.chttr) register();
  else window.addEventListener("chttr:ready", register, { once: true });
})();
