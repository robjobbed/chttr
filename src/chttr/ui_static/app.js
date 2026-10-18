// chttr meeting library and Lingo. Talks only to the local chttr server that served it; the
// access token comes from the URL fragment (never sent in Referer headers), followed by the
// route: #TOKEN/meeting/<id>?t=152 (see lib.js). Every change of place goes through
// chttr.navigate(), so refresh, Back and Forward work and a link opens the same thing.
//
// Views plug in with chttr.registerView(name, view), where a view is
//   { title, list(route) -> Node | null (null hides the left pane), detail(route) -> Promise<Node | Node[]>,
//     search?(q), onKey?(event) -> bool, update?(route), placeholder?, noSearch? }
// Meetings, Action items and Lingo are built in; other scripts register theirs before
// DOMContentLoaded, or on the "chttr:ready" event.
"use strict";

(function () {
  const L = window.chttrLib;
  const $ = (sel) => document.querySelector(sel);
  const TOKEN = L.parseRoute(location.hash).token;
  const state = {
    meetings: [], styles: [], setup: null, current: null, route: null, view: "", loaded: false,
    meetingsError: "", hits: [], facet: "", reader: null,
    held: "", forceSearch: false,                             // a search too short to run yet; Enter runs it anyway
  };
  const views = new Map();
  const rendered = { listKey: null, detailKey: null };
  let started = false;

  // ---- basics ---------------------------------------------------------------------------------
  async function api(path, options = {}) {
    let res;
    try {
      res = await fetch(path, { ...options, headers: { "X-Chttr-Token": TOKEN, "Content-Type": "application/json", ...(options.headers || {}) } });
    } catch {
      throw new Error("Lost the connection to chttr. Open this page again from the chttr menu.");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "style" && typeof v === "object") for (const [p, x] of Object.entries(v)) node.style.setProperty(p, x);
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "value" && "value" in node) node.value = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c);
    return node;
  }

  // "**word**" markers from the search snippets -> <mark>, without ever using innerHTML
  function highlighted(text) {
    return String(text).split("**").map((part, i) => (i % 2 ? el("mark", {}, part) : part));
  }

  const ts = L.fmtTs;
  const when = (iso) => (iso ? new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
  const day = (iso) => (iso ? new Date(iso + "T12:00").toLocaleDateString([], { month: "short", day: "numeric" }) : "");
  const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
  const enc = encodeURIComponent;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const store = {
    get(key, area = localStorage) { try { return area.getItem(key); } catch { return null; } },
    set(key, value, area = localStorage) { try { area.setItem(key, value); } catch { /* private window */ } },
    del(key, area = localStorage) { try { area.removeItem(key); } catch { /* private window */ } },
  };

  // Visual toast plus the screen-reader status region. toast(msg), toast(msg, true) for an
  // error, or toast(msg, { error, action: { label, run }, ms }).
  function toast(message, opts = {}) {
    if (typeof opts === "boolean") opts = { error: opts };
    const t = $("#toast");
    const action = opts.action
      ? el("button", { type: "button", onclick: () => { t.hidden = true; opts.action.run(); } }, opts.action.label)
      : null;
    t.replaceChildren(...[el("span", {}, message), action].filter(Boolean));
    t.className = "toast" + (opts.error ? " error" : "");
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (t.hidden = true), opts.ms || (opts.error ? 6000 : action ? 6000 : 2500));
    const status = $("#status");
    status.textContent = "";
    setTimeout(() => (status.textContent = message), 30);       // a fresh write is announced even when repeated
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const area = el("textarea", { class: "sr-only", "aria-hidden": "true" }, text);
      document.body.append(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      area.remove();
      return ok;
    }
  }

  // ---- building blocks ------------------------------------------------------------------------
  function emptyState({ icon = "●", title, body, actions = [] }) {
    return el("div", { class: "empty-state" },
      el("div", { class: "icon", "aria-hidden": "true" }, icon),
      el("h2", {}, title),
      body ? (typeof body === "string" ? el("p", {}, body) : body) : null,
      actions.length ? el("div", { class: "actions-row" }, actions.map((a) =>
        el("button", { type: "button", class: a.primary ? "primary" : "button", onclick: (e) => a.run(e.currentTarget) }, a.label))) : null);
  }

  const skeleton = (rows = 6) => el("div", { "aria-hidden": "true" },
    Array.from({ length: rows }, () => el("div", { class: "skeleton" }, el("span"), el("span"), el("span"))));

  const errorCard = (title, message, retry) => el("div", { class: "card-error", role: "alert" },
    el("strong", {}, title), el("p", {}, message),
    retry ? el("button", { type: "button", class: "button", onclick: retry }, "Try again") : null);

  const hotkey = (name, fallback) => (state.setup && state.setup.hotkeys && state.setup.hotkeys[name]) || fallback;

  // ---- router ---------------------------------------------------------------------------------
  function registerView(name, view) {
    views.set(name, { title: name, ...view });
    if (started) {
      setupTabs();
      const wanted = (L.parseRoute(location.hash).view || "");
      if (wanted === name || (!wanted && name === "home")) { rendered.listKey = rendered.detailKey = null; render(); }
    }
  }

  const currentView = () => views.get(state.view) || views.get("meetings");

  function navigate(route, { replace = false, silent = false } = {}) {
    const next = { view: state.view || "meetings", id: "", t: null, q: "", owner: "", ...route, token: TOKEN };
    const hash = L.buildRoute(next);
    if (hash === location.hash) {
      if (!silent) render();
      return;
    }
    if (replace || silent) {
      history.replaceState(null, "", hash);
      if (silent) state.route = L.parseRoute(hash, new Set(views.keys()));
      else render();
    } else {
      location.hash = hash;                                 // -> hashchange -> render()
    }
  }

  const setView = (name) => navigate({ view: name, q: name === state.view ? state.route.q : "" });
  const openMeeting = (id, at = null) => navigate({ view: "meetings", id, t: at, q: state.view === "meetings" && state.route ? state.route.q : "" });

  function refresh({ list = true, detail = false } = {}) {
    if (list) rendered.listKey = null;
    if (detail) rendered.detailKey = null;
    render();
  }

  async function render() {
    const route = L.parseRoute(location.hash, new Set(views.keys()));
    const view = views.get(route.view) || views.get("meetings");
    state.route = route;
    state.view = route.view;
    chrome(route, view);

    const listKey = `${route.view}|${view.listKey ? view.listKey(route) : route.q}`;
    let hasList = !$("#layout").classList.contains("one-pane");
    if (listKey !== rendered.listKey) {
      rendered.listKey = listKey;
      const node = view.list ? view.list(route) : null;
      hasList = node !== null && node !== undefined;
      $("#layout").classList.toggle("one-pane", !hasList);
      $("#list").replaceChildren(...(hasList ? [node].flat() : []));
      $("#list").removeAttribute("aria-activedescendant");
    } else if (view.listUpdate) {
      view.listUpdate(route);
    }
    markActive(route);
    const backable = hasList && (Boolean(route.id) || Boolean(view.detailFirst));
    $("#layout").classList.toggle("has-detail", Boolean(route.id) || !hasList || Boolean(view.detailFirst));

    const detailKey = `${route.view}|${view.detailKey ? view.detailKey(route) : route.id}`;
    if (detailKey === rendered.detailKey) {
      if (view.update) view.update(route);
      return;
    }
    rendered.detailKey = detailKey;
    if (state.reader && route.view !== "meetings") closeReader();
    const seq = L.nextSeq("detail");
    const detail = $("#detail");
    detail.setAttribute("aria-busy", "true");
    let out;
    try {
      out = await view.detail(route);
    } catch (e) {
      out = errorCard("Something went wrong", e.message, () => refresh({ list: false, detail: true }));
    }
    if (!L.isLatest("detail", seq)) return;                  // a newer navigation won
    detail.removeAttribute("aria-busy");
    if (out === null || out === undefined) return;            // the view rendered in place
    const back = backable
      ? el("button", { type: "button", class: "back button", onclick: () => {
        if (state.route.id) navigate({ view: route.view, q: state.route.q });
        else $("#layout").classList.remove("has-detail");      // narrow window: show the list again
      } }, `‹ ${view.backLabel || view.title}`)
      : null;
    detail.replaceChildren(...[back, out].flat(Infinity).filter(Boolean));
    if (!view.keepScroll) detail.scrollTop = 0;
    if (view.mounted) view.mounted(route);
  }

  function chrome(route, view) {
    const meeting = route.view === "meetings" && state.current && state.current.id === route.id ? state.current.title : "";
    document.title = `chttr · ${meeting || view.title}`;
    document.querySelectorAll(".tab[data-view]").forEach((t) => {
      const on = t.dataset.view === route.view;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      t.classList.toggle("active", on);
      if (on) $("#detail").setAttribute("aria-labelledby", t.id);
    });
    const search = $("#search");
    search.hidden = Boolean(view.noSearch);
    const q = $("#q");
    q.placeholder = view.placeholder || "Search every meeting… speaker:dana after:2026-10-01";
    q.setAttribute("aria-label", view.searchLabel || "Search meetings");
    if (document.activeElement !== q && q.value !== route.q) q.value = route.q;
    $("#list").setAttribute("aria-label", view.listLabel ? view.listLabel(route) : view.title);
  }

  function markActive(route) {
    document.querySelectorAll("#list .item[data-id]").forEach((item) => {
      const on = Boolean(route.id) && item.dataset.id === route.id;
      item.classList.toggle("active", on);
      if (on) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    });
  }

  function setupTabs() {
    const tabs = [...document.querySelectorAll(".tab[data-view]")];
    for (const t of tabs) {
      t.hidden = !views.has(t.dataset.view);
      if (!t.id) t.id = `tab-${t.dataset.view}`;
      t.setAttribute("aria-controls", "detail");
      if (t.dataset.bound) continue;
      t.dataset.bound = "1";
      t.addEventListener("click", () => setView(t.dataset.view));
      t.addEventListener("keydown", (e) => {
        const shown = tabs.filter((x) => !x.hidden);
        const i = shown.indexOf(t);
        const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: shown.length - 1 }[e.key];
        if (to === undefined) return;
        e.preventDefault();
        e.stopPropagation();
        const next = shown[(to + shown.length) % shown.length];
        next.focus();
        setView(next.dataset.view);
      });
    }
  }

  // ---- search box -----------------------------------------------------------------------------
  function bindSearch() {
    const q = $("#q");
    let timer;
    const apply = () => {
      clearTimeout(timer);
      const text = q.value;
      const view = currentView();
      if (view.noSearch) return;
      if (view.search) {
        navigate({ ...state.route, q: text }, { silent: true });
        view.search(text);
      } else if (state.view !== "meetings") {
        if (text.trim()) navigate({ view: "meetings", q: text });
      } else {
        navigate({ ...state.route, q: text }, { replace: true });
      }
    };
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 200); });
    q.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        q.value = "";
        apply();
        q.blur();
      } else if (e.key === "Enter") {
        e.preventDefault();
        state.forceSearch = true;                              // Enter searches a short word as it is
        apply();
        if (state.forceSearch && state.held && state.results) state.searching = runSearch(state.held, state.results);
        state.forceSearch = false;
        openTopHit();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        q.blur();
        moveFocus(1);
      }
    });
    $("#search").addEventListener("submit", (e) => e.preventDefault());
  }

  async function openTopHit() {
    if (state.view !== "meetings") return;
    await (state.searching || Promise.resolve());
    const h = state.hits[0];
    if (h && h.meeting_id) openMeeting(h.meeting_id, h.kind === "line" ? h.start : null);
  }

  // ---- list keyboard focus --------------------------------------------------------------------
  function moveFocus(step) {
    const items = [...document.querySelectorAll("#list .item")];
    if (!items.length) return false;
    let i = items.findIndex((x) => x.classList.contains("focus"));
    if (i < 0) i = items.findIndex((x) => x.classList.contains("active"));
    i = i < 0 ? (step > 0 ? 0 : items.length - 1) : Math.min(items.length - 1, Math.max(0, i + step));
    items.forEach((x) => x.classList.remove("focus"));
    const item = items[i];
    item.classList.add("focus");
    if (!item.id) item.id = `item-${L.nextSeq("item-id")}`;
    $("#list").setAttribute("aria-activedescendant", item.id);
    item.focus({ preventScroll: true });
    item.scrollIntoView({ block: "nearest" });
    return true;
  }

  // ---- daemon banner --------------------------------------------------------------------------
  function banner() {
    const b = $("#banner");
    const down = state.setup && state.setup.daemon === false && store.get("chttrBannerDismissed", sessionStorage) !== "1";
    b.hidden = !down;
    if (!down) { b.replaceChildren(); return; }
    b.replaceChildren(
      el("span", { class: "grow" }, el("strong", {}, "chttr isn't running."), " Hotkeys, dictation and meeting recording are off until it starts."),
      el("button", { type: "button", class: "primary", onclick: (e) => startDaemon(e.currentTarget) }, "Start chttr"),
      el("button", { type: "button", class: "x", "aria-label": "Dismiss", onclick: () => { store.set("chttrBannerDismissed", "1", sessionStorage); banner(); } }, "×"));
  }

  async function startDaemon(button) {
    if (button) button.disabled = true;
    toast("Starting chttr…");
    try {
      await api("/api/daemon/start", { method: "POST" });
    } catch (e) {
      toast(e.message, true);
      if (button) button.disabled = false;
      return false;
    }
    for (let i = 0; i < 30; i++) {
      await sleep(500);
      try {
        const setup = await api("/api/setup");
        if (setup.daemon) {
          state.setup = setup;
          banner();
          toast("chttr is running.");
          refresh({ list: true, detail: true });
          return true;
        }
      } catch { /* keep waiting */ }
    }
    if (button) button.disabled = false;
    toast("chttr is taking a while to start. See what's wrong with: chttr doctor", true);
    return false;
  }

  async function recordNow(button) {
    if (button) button.disabled = true;
    try {
      await api("/api/daemon/meeting-start", { method: "POST" });
      toast(`Recording the meeting. Stop it with ${hotkey("meeting", "Super+Alt+Insert")} or from the chttr menu.`, { ms: 6000 });
    } catch (e) {
      toast(e.message, true);
    }
    if (button) button.disabled = false;
  }

  // ---- theme ----------------------------------------------------------------------------------
  // The server paints the theme into the page; this keeps it current after an Omarchy theme switch.
  let themeVars = [];
  function applyTheme(t) {
    const rootEl = document.documentElement;
    if (!t || t.mode === "auto") rootEl.removeAttribute("data-theme");
    else rootEl.setAttribute("data-theme", t.mode);
    for (const k of themeVars) rootEl.style.removeProperty(k);
    rootEl.style.cssText.split(";").map((x) => x.split(":")[0].trim()).filter((k) => k.startsWith("--"))
      .forEach((k) => rootEl.style.removeProperty(k));
    themeVars = Object.keys((t && t.vars) || {});
    for (const [k, v] of Object.entries((t && t.vars) || {})) rootEl.style.setProperty(k, v);
  }

  async function onVisible() {
    if (document.visibilityState !== "visible" || !started) return;
    api("/api/theme").then(applyTheme).catch(() => {});
    try {
      const fresh = await api("/api/meetings");
      const sig = (ms) => ms.map((m) => `${m.id}:${m.title}:${m.speakers.join(",")}`).join("|");
      if (sig(fresh) !== sig(state.meetings)) {
        state.meetings = fresh;
        state.meetingsError = "";
        if (state.view === "meetings" && !state.route.q) refresh({ list: true });
      }
    } catch { /* the next look will tell */ }
  }

  // ---- meetings view: list --------------------------------------------------------------------
  function meetingItem(m) {
    return el("button", { type: "button", class: "item", "data-id": m.id, onclick: () => openMeeting(m.id) },
      el("div", { class: "title" }, m.title),
      el("div", { class: "meta" }, [when(m.when), `${m.minutes} min`, m.speakers.slice(0, 4).join(", ")].filter(Boolean).join(" · ")),
      m.summary ? el("div", { class: "snip meta" }, m.summary.length > 140 ? m.summary.slice(0, 139) + "…" : m.summary) : null);
  }

  function meetingList() {
    if (!state.loaded) return skeleton();
    if (state.meetingsError) {
      return errorCard("Couldn't load your meetings", state.meetingsError, async () => {
        await loadMeetings();
        refresh({ list: true, detail: true });
      });
    }
    if (!state.meetings.length) return el("div", { class: "section-label" }, "No meetings yet");
    return [el("div", { class: "section-label" }, plural(state.meetings.length, "meeting")), ...state.meetings.map(meetingItem)];
  }

  function describeFilters(pq) {
    return [pq.speaker && `speaker ${pq.speaker}`, pq.after && pq.after === pq.before ? `on ${day(pq.after)}` : null,
      pq.after && pq.after !== pq.before && `after ${day(pq.after)}`, pq.before && pq.after !== pq.before && `before ${day(pq.before)}`]
      .filter(Boolean).join(" · ");
  }

  function searchList(route) {
    const box = el("div", { class: "search-results" }, skeleton(4));
    state.results = box;
    state.searching = runSearch(route.q, box);
    return box;
  }

  async function runSearch(q, box) {
    const seq = L.nextSeq("search");
    const force = state.forceSearch;
    state.forceSearch = false;
    state.held = "";
    if (q !== state.lastQ) state.facet = "";                    // a new search starts across every meeting
    state.lastQ = q;
    const pq = L.parseQuery(q);
    if (!pq.words && !pq.speaker && !pq.after && !pq.before) {
      state.hits = [];
      box.replaceChildren(...[meetingList()].flat());
      return;
    }
    if (!force && L.tooShortToSearch(pq)) {                     // "co" while typing "contract": not "Nothing matches"
      state.held = q;
      state.hits = [];
      box.replaceChildren(...[el("div", { class: "section-label" }, `Keep typing… Enter searches for “${pq.words}”`), meetingList()].flat());
      markActive(state.route);
      return;
    }
    let res;
    try {
      res = await api(`/api/search?q=${enc(q)}&prefix_last=1&facets=1${state.facet ? `&meeting=${enc(state.facet)}` : ""}`);
    } catch (e) {
      if (L.isLatest("search", seq)) box.replaceChildren(errorCard("Search failed", e.message, () => refresh()));
      return;
    }
    if (!L.isLatest("search", seq)) return;                    // a newer search is on its way
    state.hits = res.hits || [];
    const facets = res.facets || [];
    const filters = describeFilters(pq);
    if (!state.hits.length) {
      box.replaceChildren(emptyState({
        icon: "∅", title: `Nothing matches “${pq.words || filters}”`,
        body: el("p", {}, "Try fewer or different words. Filters narrow a search: ", el("kbd", {}, "speaker:dana"), " ",
          el("kbd", {}, "after:2026-10-01"), " ", el("kbd", {}, "before:…"), " ", el("kbd", {}, "on:today")),
      }));
      return;
    }
    const lines = facets.reduce((n, f) => n + f.count, 0);
    const label = lines ? `${plural(lines, "line")} in ${plural(facets.length, "meeting")}` : plural(state.hits.length, "match", "matches");
    box.replaceChildren(...[
      el("div", { class: "section-label" }, label + (filters ? ` · ${filters}` : "")),
      facets.length > 1 ? el("div", { class: "facets", role: "group", "aria-label": "Narrow to one meeting" }, facets.slice(0, 12).map((f) =>
        el("button", { type: "button", class: "chip", "aria-pressed": state.facet === f.meeting_id ? "true" : "false",
          title: when(f.when), onclick: () => { state.facet = state.facet === f.meeting_id ? "" : f.meeting_id; state.searching = runSearch(q, box); } },
        `${f.title} · ${f.count}`))) : null,
      ...state.hits.map((h) =>
        el("button", { type: "button", class: "item", "data-id": h.meeting_id, onclick: () => openMeeting(h.meeting_id, h.kind === "line" ? h.start : null) },
          el("div", { class: "title" }, h.title),
          el("div", { class: "meta" }, `${when(h.when)}${h.kind === "line" ? ` · ${ts(h.start)} · ${h.speaker}` : " · notes"}`),
          h.snippet ? el("div", { class: "snip" }, ...highlighted(h.snippet)) : null)),
    ].filter(Boolean));
    markActive(state.route);
  }

  // ---- meetings view: detail ------------------------------------------------------------------
  function meetingsEmpty() {
    const s = state.setup || {};
    const ways = el("ul", {},
      el("li", {}, s.detect_calls === false
        ? "Turn on detect_calls in [meeting] settings, and chttr offers to record your calls."
        : "Join a Google Meet, Zoom or Teams call: chttr asks whether to record it."),
      el("li", {}, "Press ", el("kbd", {}, hotkey("meeting", "Super+Alt+Insert")), " to start and stop recording any meeting."),
      el("li", {}, "Or run ", el("kbd", {}, "chttr meeting start"), " in a terminal."));
    const actions = s.daemon === false
      ? [{ label: "Start chttr", primary: true, run: startDaemon }]
      : [{ label: "Record now", primary: true, run: recordNow }];
    return emptyState({ icon: "●", title: "No meetings yet", body: el("div", {}, el("p", {}, "Three ways to record one:"), ways), actions });
  }

  async function loadMeeting(id) {
    const seq = L.nextSeq("meeting");
    const m = await api(`/api/meeting/${enc(id)}`);
    if (!L.isLatest("meeting", seq)) return null;              // another meeting was opened meanwhile
    return m;
  }

  const meetingsView = {
    title: "Meetings",
    listLabel: (route) => (route.q ? "Search results" : "Meetings"),
    keepScroll: false,
    listKey: (route) => (route.q.trim() ? "search" : `all:${state.loaded}:${state.meetings.length > 0}`),
    list(route) {
      if (route.q.trim()) return searchList(route);
      if (state.loaded && !state.meetings.length && !state.meetingsError) return null;   // the empty state gets the room
      return el("div", {}, ...[meetingList()].flat());
    },
    listUpdate(route) {                                        // typing: the old results stay until the new ones arrive
      if (route.q.trim() && state.results && route.q !== state.lastQ) state.searching = runSearch(route.q, state.results);
    },
    detailKey: (route) => route.id,
    async detail(route) {
      if (state.reader && state.reader.id !== route.id) closeReader();   // stop the old meeting while the new one loads
      if (!route.id) {
        closeReader();
        if (!state.loaded) return el("div", { class: "empty" }, "Loading…");
        if (!state.meetings.length && !state.meetingsError) return meetingsEmpty();
        if (!route.q && state.meetings.length && matchMedia("(min-width: 761px)").matches) {
          setTimeout(() => navigate({ view: "meetings", id: state.meetings[0].id }, { replace: true }), 0);
        }
        return el("div", { class: "empty" }, "Pick a meeting, or search across all of them. Press ", el("kbd", {}, "?"), " for the keys.");
      }
      let m;
      try {
        m = await loadMeeting(route.id);
      } catch (e) {
        closeReader();
        return emptyState({ icon: "?", title: "That meeting isn't here", body: e.message,
          actions: [{ label: "All meetings", run: () => navigate({ view: "meetings" }) }] });
      }
      if (!m || state.view !== "meetings" || state.route.id !== route.id) return null;   // you moved on meanwhile
      state.current = m;
      document.title = `chttr · ${m.title}`;
      return buildReader(m, route.t);
    },
    mounted(route) { if (state.reader) state.reader.arrive(route.t); },
    update(route) {                                            // same meeting, another moment (a second search hit)
      if (state.reader && route.t !== null && route.t !== state.reader.lastT) state.reader.seek(route.t, { play: false, mark: true });
    },
    onKey: (e) => (state.reader ? state.reader.onKey(e) : false),
  };

  // ---- the meeting reader ---------------------------------------------------------------------
  function closeReader(keepAudio = false) {
    if (state.reader) state.reader.destroy(keepAudio);
    state.reader = null;
  }

  function renderNotes(notes) {
    const out = [];
    for (const sec of notes.sections || []) {
      const items = (sec.items || []).filter((x) => String(x).trim());
      if (!items.length) continue;
      out.push(el("h2", {}, sec.title));
      if (sec.kind === "paragraph") out.push(...items.map((x) => el("p", {}, x)));
      else if (sec.kind === "quotes") out.push(...items.map((x) => el("blockquote", {}, x)));
      else out.push(el("ul", {}, items.map((x) => el("li", {}, x))));
    }
    const actions = notes.action_items || [];
    if (actions.length) {
      out.push(el("h2", {}, notes.actions_heading || "Action items"));
      out.push(el("ul", { class: "actions" }, actions.map((a) =>
        el("li", {}, a.owner ? el("span", { class: "owner" }, `${a.owner}: `) : null, a.task, a.due ? el("span", { class: "due" }, ` (due ${a.due})`) : null))));
    }
    if (notes.error) out.push(el("p", { class: "meta" }, `Notes could not be written: ${notes.error}`));
    return out;
  }

  function styleControl(m) {
    if (m.notes_available === false) return el("span", { class: "hint" }, "Notes need Claude or Ollama (chttr connect claude)");
    if (!state.styles.length) return null;
    const current = (m.notes && m.notes.style) || "";
    const known = state.styles.some((s) => s.id === current);
    const select = el("select", { "aria-label": "Notes style", onchange: () => restyle(m.id, select) },
      !current ? el("option", { value: "", disabled: true, selected: true }, "No notes yet") : null,
      current && !known ? el("option", { value: current, selected: true, disabled: true },
        current === "lite" ? "On-device notes" : (m.notes.style_name || "Custom")) : null,
      state.styles.map((s) => el("option", { value: s.id, selected: s.id === current, title: s.description }, s.name)));
    return select;
  }

  async function restyle(id, select) {
    const choice = select.value;
    const name = (state.styles.find((s) => s.id === choice) || {}).name || choice;
    select.disabled = true;
    toast(`Rewriting the notes as ${name}… this can take a minute.`, { ms: 60000 });
    try {
      const m = await api(`/api/meeting/${enc(id)}/style`, { method: "POST", body: JSON.stringify({ style: choice }) });
      if (!state.current || state.current.id !== id) return;
      state.current = { ...state.current, ...m };
      rebuildReader();
      toast(`Notes rewritten as ${name}.`);
    } catch (e) {
      toast(e.message, true);
      if (state.current && state.current.id === id && select.isConnected) select.replaceWith(styleControl(state.current));
    }
  }

  function rebuildReader() {
    const m = state.current;
    const keep = state.reader ? state.reader.snapshot() : null;
    const node = buildReader(m, null, keep);
    $("#detail").replaceChildren(...[$("#detail .back"), node].filter(Boolean));
    if (state.reader) state.reader.restore(keep);
  }

  // Speaker chips: name · minutes, a thin bar for the share of talk time, inline rename.
  function speakerChips(m, palette, talk, total) {
    const labels = Object.keys(m.speakers || {});
    return el("div", { class: "chips", role: "group", "aria-label": "Speakers" }, labels.map((label) => {
      const sp = m.speakers[label];
      const secs = talk.get(label) || (sp.minutes || 0) * 60;
      const share = total ? Math.round((secs / total) * 100) : 0;
      const unknown = sp.source === "unknown" || L.isUnknownSpeaker(label);
      return el("button", {
        type: "button", class: "chip speaker" + (unknown ? " unknown" : ""), "data-label": label,
        style: { "--who": palette.get(label) || "var(--c8)", "--share": `${share}%` },
        title: `${unknown ? "Not named yet" : sp.source || ""}${sp.email ? " · " + sp.email : ""} · ${share}% of the talking · click to rename (r)`,
        "aria-label": `${label}, ${L.fmtDuration(secs)}, ${share}% of the talking. Rename`,
        onclick: (e) => renameForm(m.id, label, e.currentTarget, unknown),
      }, el("span", { class: "dot", "aria-hidden": "true" }), `${label} · ${L.fmtDuration(secs)}`, el("span", { class: "bar", "aria-hidden": "true" }));
    }));
  }

  function renameForm(id, label, chip, unknown) {
    const input = el("input", { value: unknown ? "" : label, placeholder: "Who is this?", "aria-label": `New name for ${label}`, autocomplete: "off" });
    const cancel = () => { form.replaceWith(chip); chip.focus(); };
    const form = el("form", { class: "rename-form", onsubmit: async (e) => {
        e.preventDefault();
        const name = input.value.trim();
        if (!name || name === label) { cancel(); return; }
        form.querySelectorAll("button, input").forEach((x) => (x.disabled = true));
        try {
          const m = await api(`/api/meeting/${enc(id)}/rename`, { method: "POST", body: JSON.stringify({ old: label, new: name }) });
          if (state.current && state.current.id === id) {
            state.current = { ...state.current, ...m };
            rebuildReader();
          }
          toast(`Renamed to ${name}; chttr will recognise that voice next time.`);
          loadMeetings().then(() => { if (state.view === "meetings" && !state.route.q) refresh({ list: true }); });
        } catch (err) {
          toast(err.message, true);
          form.querySelectorAll("button, input").forEach((x) => (x.disabled = false));
          input.focus();
        }
      },
      onkeydown: (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); } } },
      el("span", { class: "sr-only" }, `Rename ${label}`), input,
      el("button", { type: "submit", class: "primary" }, "Save"),
      el("button", { type: "button", class: "cancel", onclick: cancel }, "Cancel"));
    chip.replaceWith(form);
    input.focus();
    input.select();
  }

  function buildReader(m, at, keep = null) {
    closeReader(Boolean(keep && keep.audio));
    const turns = m.turns && m.turns.length ? m.turns : L.turnsFromLines(m.transcript || []);
    const palette = L.speakerPalette([...turns.map((t) => t.speaker), ...Object.keys(m.speakers || {})]);
    const talk = new Map(Object.entries(m.talk_seconds || {}));
    if (!talk.size) for (const [k, v] of L.talkTime(turns)) talk.set(k, v);
    const total = [...talk.values()].reduce((a, b) => a + b, 0);

    const audio = m.has_audio
      ? (keep && keep.audio) || el("audio", { controls: true, preload: "metadata", src: `/api/meeting/${enc(m.id)}/audio?token=${enc(TOKEN)}` })
      : null;
    const rateLabel = el("span", { class: "rate", "aria-live": "off", title: "Playback speed (, and .)" }, "1×");
    const setRate = (r) => { if (!audio) return; audio.playbackRate = r; rateLabel.textContent = `${r}×`; store.set("chttrRate", String(r)); };
    if (audio) setRate(Number(store.get("chttrRate")) || 1);

    // transcript: turns of <span data-start> utterances
    const spans = [];
    const turnNodes = [];
    const transcript = el("div", { class: "transcript" + (audio ? " has-audio" : "") }, turns.map((t, ti) => {
      const color = palette.get(t.speaker) || "var(--c8)";
      const body = el("div", { class: "text" });
      t.lines.forEach((line, i) => {
        const span = el("span", { class: "u", "data-start": String(line.start), "data-turn": String(ti) }, line.text);
        span.dataset.text = line.text;
        spans.push(span);
        if (i) body.append(" ");
        body.append(span);
      });
      const stamp = el("button", { type: "button", class: "t", "aria-label": `${audio ? "Play from" : "Go to"} ${ts(t.start)}`,
        onclick: () => reader.seek(t.start, { play: true, mark: !audio }) }, ts(t.start));
      const node = el("div", { class: "turn", style: { "--who": color } }, stamp,
        el("div", { class: "body" }, el("div", { class: "who" }, t.speaker || "Unknown"), body));
      turnNodes.push({ start: t.start, stamp, node });
      return node;
    }));
    const starts = spans.map((s) => Number(s.dataset.start));
    transcript.addEventListener("click", (e) => {
      const span = e.target.closest(".u");
      if (!span || !audio) return;
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) return;                   // selecting text to copy, not seeking
      reader.seek(Number(span.dataset.start), { play: true });
    });

    // find in transcript
    const findInput = el("input", { type: "search", placeholder: "Find in transcript", "aria-label": "Find in transcript" });
    const findCount = el("span", { class: "count", "aria-live": "polite" });
    const findBox = el("div", { class: "find", hidden: true, role: "search" }, findInput, findCount,
      el("button", { type: "button", "aria-label": "Previous match", title: "Previous (Shift+Enter)", onclick: () => reader.findStep(-1) }, "↑"),
      el("button", { type: "button", "aria-label": "Next match", title: "Next (Enter)", onclick: () => reader.findStep(1) }, "↓"),
      el("button", { type: "button", "aria-label": "Close find", title: "Close (Esc)", onclick: () => reader.closeFind() }, "×"));
    let findTimer;
    findInput.addEventListener("input", () => { clearTimeout(findTimer); findTimer = setTimeout(() => reader.find(findInput.value), 120); });
    findInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); reader.findStep(e.shiftKey ? -1 : 1); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); reader.closeFind(); }
    });

    const lite = m.notes && m.notes.style === "lite";
    const exportLink = (fmt, label, extra = {}) => el("a", { class: "button", href: `/api/meeting/${enc(m.id)}/export?format=${fmt}&token=${enc(TOKEN)}${extra.inline ? "&inline=1" : ""}`,
      ...(extra.inline ? { target: "_blank", rel: "noopener" } : { download: "" }) }, label);
    const node = el("article", { class: "reader", "aria-label": m.title },
      el("h1", {}, m.title),
      el("div", { class: "sub" }, el("span", {}, `${when(m.when)} · ${m.minutes} min`), styleControl(m)),
      el("div", { class: "toolbar", role: "toolbar", "aria-label": "Meeting" },
        el("button", { type: "button", class: "button", onclick: () => reader.copyLink() }, "Copy link to moment"),
        m.markdown ? exportLink("md", "Open .md", { inline: true }) : null,
        exportLink("json", "Export JSON"), exportLink("csv", "Export CSV"),
        el("button", { type: "button", class: "button", onclick: () => reader.openFind(), "aria-keyshortcuts": "f" }, "Find")),
      lite ? el("div", { class: "lite-banner" }, "On-device notes · add a key for full notes (chttr connect claude)") : null,
      speakerChips(m, palette, talk, total),
      audio ? el("div", { class: "player" }, audio, rateLabel) : null,
      el("div", { class: "notes" }, renderNotes(m.notes || {})),
      el("h2", {}, "Transcript"),
      findBox,
      turns.length ? transcript : el("p", { class: "meta" }, "No transcript for this meeting."));

    // follow-along: the line being played lights up and stays in view, unless you scrolled lately
    // (wheel, keys, the scrollbar, middle-click, a find match). Scroll events arrive on the next
    // frame, so the reader's own scrolls are marked until the frame after that.
    let playing = -1;
    let userScrolled = 0;
    let lastRouteWrite = 0;
    let autoScrolling = false;
    let autoFrame = 0;
    const detail = $("#detail");
    const noteScroll = () => { if (!autoScrolling) userScrolled = Date.now(); };
    const autoScroll = (fn) => {
      autoScrolling = true;
      fn();
      cancelAnimationFrame(autoFrame);
      autoFrame = requestAnimationFrame(() => { autoFrame = requestAnimationFrame(() => (autoScrolling = false)); });
    };
    const show = (span, block) => autoScroll(() => span.scrollIntoView({ block }));
    detail.addEventListener("scroll", noteScroll, { passive: true });
    const markPlaying = (time, scroll) => {
      const i = L.lineAt(starts, time);
      if (i === playing) return;
      if (playing >= 0 && spans[playing]) spans[playing].classList.remove("playing");
      playing = i;
      if (i >= 0) {
        spans[i].classList.add("playing");
        if (scroll && Date.now() - userScrolled > 3000) show(spans[i], "nearest");
      }
    };
    // the moment goes into the URL, so a refresh resumes about here; only while this meeting is the one shown
    const writeT = (t) => {
      reader.lastT = t;
      if (state.view === "meetings" && state.route.id === m.id) navigate({ ...state.route, t }, { silent: true });
    };
    const onTime = () => {
      markPlaying(audio.currentTime, true);
      if (Date.now() - lastRouteWrite > 5000 && audio.currentTime > 0) {
        lastRouteWrite = Date.now();
        writeT(Math.floor(audio.currentTime));
      }
    };
    if (audio) audio.addEventListener("timeupdate", onTime);

    let marks = [];
    let markAt = -1;
    const clearHit = () => spans.forEach((s) => s.classList.remove("hit"));

    const reader = {
      id: m.id, audio, lastT: at,
      position() {
        if (audio && audio.currentTime > 0) return audio.currentTime;
        const hit = spans.find((s) => s.classList.contains("hit"));
        return hit ? Number(hit.dataset.start) : (state.route.t || 0);
      },
      // jump to a moment: seek the audio (playing it when asked) and/or mark and show the line
      seek(time, { play = false, mark = false } = {}) {
        const t = Math.max(0, Number(time) || 0);
        if (audio) {
          const go = () => { audio.currentTime = t; if (play) audio.play().catch(() => {}); };
          if (audio.readyState >= 1) go(); else audio.addEventListener("loadedmetadata", go, { once: true });
          markPlaying(t, false);
        }
        if (mark || !audio) {
          clearHit();
          const i = L.lineAt(starts, t);
          if (i >= 0) {
            spans[i].classList.add("hit");
            show(spans[i], "center");
          }
        } else if (playing >= 0) show(spans[playing], "nearest");
        userScrolled = 0;
        writeT(Math.round(t));
      },
      arrive(time) {                                          // first paint: show the moment from the URL
        autoScroll(() => {});                                 // render() just reset the scroll: not the user's
        if (time === null || time === undefined) return;
        clearHit();
        const i = L.lineAt(starts, time);
        if (i >= 0) {
          spans[i].classList.add("hit");
          show(spans[i], "center");
        }
        if (audio) {
          const go = () => (audio.currentTime = time);
          if (audio.readyState >= 1) go(); else audio.addEventListener("loadedmetadata", go, { once: true });
        }
      },
      copyLink() {
        const t = Math.floor(reader.position());
        const url = location.origin + location.pathname + L.buildRoute({ ...state.route, t, token: TOKEN });
        copyText(url).then((ok) => toast(ok ? `Link to ${ts(t)} copied. It opens while this chttr ui is running.` : "Couldn't copy the link.", !ok));
      },
      turnStep(step) {
        const now = reader.position();
        let i = turnNodes.findIndex((x) => x.start > now + 0.5);
        if (i < 0) i = turnNodes.length;
        let target = step > 0 ? i : i - 2;                    // i - 1 is the turn we are in
        if (step < 0 && audio && now - (turnNodes[i - 1] || {}).start > 2) target = i - 1;   // back to its start first
        target = Math.max(0, Math.min(turnNodes.length - 1, target));
        const t = turnNodes[target];
        if (!t) return;
        reader.seek(t.start, { play: Boolean(audio && !audio.paused), mark: !audio });
        if (!audio) t.stamp.focus({ preventScroll: true });
      },
      openFind() {
        findBox.hidden = false;
        findInput.focus();
        findInput.select();
      },
      closeFind() {
        findBox.hidden = true;
        findInput.value = "";
        reader.find("");
        detail.focus({ preventScroll: true });
      },
      find(needle) {
        marks = [];
        markAt = -1;
        for (const span of spans) {
          const text = span.dataset.text;
          const ranges = L.findRanges(text, needle);
          if (!ranges.length) {
            if (span.childElementCount) span.textContent = text;
            continue;
          }
          const parts = [];
          let pos = 0;
          for (const [a, b] of ranges) {
            if (a > pos) parts.push(text.slice(pos, a));
            const mk = el("mark", {}, text.slice(a, b));
            marks.push(mk);
            parts.push(mk);
            pos = b;
          }
          if (pos < text.length) parts.push(text.slice(pos));
          span.replaceChildren(...parts);
        }
        findCount.textContent = needle.trim() ? (marks.length ? `0 / ${marks.length}` : "No matches") : "";
        if (marks.length) reader.findStep(1);
      },
      findStep(step) {
        if (!marks.length) return;
        if (markAt >= 0) marks[markAt].classList.remove("current");
        markAt = (markAt + step + marks.length) % marks.length;
        marks[markAt].classList.add("current");
        marks[markAt].scrollIntoView({ block: "center" });
        findCount.textContent = `${markAt + 1} / ${marks.length}`;
      },
      rename() {
        const chips = [...node.querySelectorAll(".chip.speaker")];
        const chip = (document.activeElement && document.activeElement.closest(".chip.speaker")) ||
          chips.find((c) => c.classList.contains("unknown")) || chips[0];
        if (chip) chip.click();
      },
      onKey(e) {
        const inPlayer = e.target.closest && e.target.closest("audio");
        if (e.key === "f") { reader.openFind(); return true; }
        if (e.key === "r") { reader.rename(); return true; }
        if (e.key === "Escape" && !findBox.hidden) { reader.closeFind(); return true; }
        if (e.key === "[" || e.key === "]") { reader.turnStep(e.key === "]" ? 1 : -1); return true; }
        if (!audio || inPlayer) return false;
        if (e.key === " ") {
          if (audio.paused) audio.play().catch(() => {}); else audio.pause();
          return true;
        }
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          const d = (e.shiftKey ? 30 : 5) * (e.key === "ArrowRight" ? 1 : -1);
          audio.currentTime = Math.max(0, Math.min(audio.duration || Infinity, audio.currentTime + d));
          userScrolled = 0;
          markPlaying(audio.currentTime, true);
          return true;
        }
        if (e.key === "," || e.key === ".") {
          setRate(L.stepRate(audio.playbackRate, e.key === "." ? 1 : -1));
          toast(`Playback speed ${audio.playbackRate}×`, { ms: 1200 });
          return true;
        }
        return false;
      },
      snapshot: () => ({ audio, time: audio ? audio.currentTime : 0, paused: audio ? audio.paused : true, scroll: detail.scrollTop }),
      restore(keep) {
        if (!keep) return;
        autoScroll(() => (detail.scrollTop = keep.scroll));
        if (audio && keep.audio === audio) markPlaying(audio.currentTime, false);
      },
      destroy(keepAudio) {                                     // keepAudio: a rebuild (rename, new notes) keeps playing
        detail.removeEventListener("scroll", noteScroll);
        cancelAnimationFrame(autoFrame);
        if (audio) {
          audio.removeEventListener("timeupdate", onTime);
          if (!keepAudio) audio.pause();
        }
      },
    };
    state.reader = reader;
    return node;
  }

  // ---- action items ---------------------------------------------------------------------------
  const actionsView = {
    title: "Action items",
    noSearch: true,
    list: () => null,
    detailKey: () => "all",
    async detail(route) {
      const filter = el("input", { class: "filter", type: "search", placeholder: "Filter by owner…", "aria-label": "Filter by owner", value: route.owner });
      const holder = el("div", {}, skeleton(3));
      let timer;
      filter.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          navigate({ view: "actions", owner: filter.value.trim() }, { silent: true });
          fillActions(holder, filter.value.trim());
        }, 250);
      });
      actionsView.filter = filter;
      await fillActions(holder, route.owner);
      return [el("h1", {}, "Action items"), filter, holder];
    },
    update(route) {
      if (actionsView.filter && actionsView.filter.value.trim() !== route.owner && document.activeElement !== actionsView.filter) {
        actionsView.filter.value = route.owner;
        fillActions(actionsView.filter.nextElementSibling, route.owner);
      }
    },
  };

  async function fillActions(holder, owner) {
    const seq = L.nextSeq("actions");
    let items;
    try {
      items = await api(`/api/actions?owner=${enc(owner)}`);
    } catch (e) {
      if (L.isLatest("actions", seq)) holder.replaceChildren(errorCard("Couldn't load the action items", e.message, () => fillActions(holder, owner)));
      return;
    }
    if (!L.isLatest("actions", seq)) return;
    if (!items.length) {
      const noNotes = state.setup && state.setup.notes_available === false;
      holder.replaceChildren(owner
        ? emptyState({ icon: "∅", title: `No action items for “${owner}”`, body: "Check the spelling, or clear the filter to see everyone's." })
        : emptyState({ icon: "☐", title: "No action items yet", body: noNotes
          ? "Action items come from meeting notes, and notes need Claude or Ollama. Add a key with: chttr connect claude"
          : "When a meeting's notes list action items, every one of them shows up here, with who owns it." }));
      return;
    }
    const meetings = new Set(items.map((a) => a.meeting_id)).size;
    holder.replaceChildren(el("table", {},
      el("caption", {}, `${plural(items.length, "action item")} from ${plural(meetings, "meeting")}${owner ? ` · owner matches “${owner}”` : ""}`),
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Owner"), el("th", { scope: "col" }, "Task"), el("th", { scope: "col" }, "Due"), el("th", { scope: "col" }, "Meeting"))),
      el("tbody", {}, items.map((a) => el("tr", {}, el("td", {}, a.owner || "–"), el("td", {}, a.task), el("td", {}, a.due || ""),
        el("td", {}, el("button", { type: "button", class: "link", onclick: () => openMeeting(a.meeting_id) }, `${a.meeting} · ${when(a.when)}`)))))));
  }

  // ---- Lingo: how chttr spells your words -----------------------------------------------------
  // Pick a past dictation, click the words that came out wrong (neighbouring words extend the
  // selection; shift-click selects a range), type the right spelling. The daemon reads the
  // dictionary on every dictation, so a fix applies to the next one.
  const lingo = { words: [], recent: [], pick: null, from: -1, to: -1, draft: "", editing: null, list: null };

  const tokens = (s) => s.split(/\s+/).filter(Boolean);
  const trimPunctuation = (s) => s.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");

  async function lingoDo(action, body, message, opts = {}) {
    try {
      Object.assign(lingo, await api(`/api/lingo/${action}`, { method: "POST", body: JSON.stringify(body) }));
      lingo.editing = null;
      if (message) toast(message, opts);
      renderLingo();
      return true;
    } catch (e) {
      toast(e.message, true);
      return false;
    }
  }

  function renderLingoList() {
    if (!lingo.list) return;
    lingo.list.replaceChildren(
      el("div", { class: "section-label" }, lingo.recent.length ? "Recent dictations" : "No dictations yet"),
      ...lingo.recent.map((d, i) =>
        el("button", { type: "button", class: "item" + (lingo.pick === i ? " active" : ""), "aria-current": lingo.pick === i ? "true" : null, onclick: () => pickDictation(i) },
          el("div", { class: "snip" }, d.heard.length > 140 ? d.heard.slice(0, 139) + "…" : d.heard),
          el("div", { class: "meta" }, [when(d.ts), d.app].filter(Boolean).join(" · ")))));
  }

  function pickDictation(i) {
    Object.assign(lingo, { pick: i, from: -1, to: -1, draft: "" });
    renderLingo();
    $("#layout").classList.add("has-detail");
  }

  function pickWord(i, extend) {
    const { from, to } = lingo;
    const before = selectedPhrase();
    if (from < 0) lingo.from = lingo.to = i;
    else if (extend) [lingo.from, lingo.to] = [Math.min(from, i), Math.max(to, i)];
    else if (i === from - 1) lingo.from = i;
    else if (i === to + 1) lingo.to = i;
    else if (i === from && i === to) lingo.from = lingo.to = -1;
    else if (i === from) lingo.from = i + 1;
    else if (i === to) lingo.to = i - 1;
    else lingo.from = lingo.to = i;
    const after = selectedPhrase();
    // keep what you typed; prefill only a new selection, or one you haven't typed over yet
    const untouched = lingo.draft === before;
    if (!before || !after || untouched) lingo.draft = after;
    renderLingo();
    const input = document.getElementById("lingo-right");
    if (input) {
      input.focus();
      if (lingo.draft === after) input.select();
      else input.setSelectionRange(input.value.length, input.value.length);
    }
  }

  function selectedPhrase() {
    if (lingo.pick === null || lingo.from < 0) return "";
    return trimPunctuation(tokens(lingo.recent[lingo.pick].heard).slice(lingo.from, lingo.to + 1).join(" "));
  }

  function fixPanel() {
    if (lingo.pick === null) {
      return lingo.recent.length
        ? el("p", { class: "meta" }, "Pick a dictation on the left.")
        : emptyState({ icon: "✎", title: "Dictate something first",
          body: el("p", {}, "Hold ", el("kbd", {}, hotkey("dictate", "Insert")), ", talk, and let go. Your dictations show up here, ready to fix.") });
    }
    const d = lingo.recent[lingo.pick];
    const words = tokens(d.heard).map((w, i) => {
      const on = lingo.from >= 0 && i >= lingo.from && i <= lingo.to;
      return el("button", { type: "button", class: "word" + (on ? " on" : ""), "aria-pressed": on ? "true" : "false",
        onclick: (e) => pickWord(i, e.shiftKey) }, w);
    });
    const heard = selectedPhrase();
    const now = el("div", { class: "meta now" });
    api("/api/lingo/try", { method: "POST", body: JSON.stringify({ text: d.heard }) })
      .then((r) => { now.textContent = r.typed !== d.heard ? `With your words, chttr now types: “${r.typed}”` : ""; })
      .catch(() => {});
    const form = heard ? el("form", { class: "lingo-form", onsubmit: (e) => {
        e.preventDefault();
        const word = $("#lingo-right").value.trim();
        if (!word) return;
        if (word === heard) { toast("That's how chttr already spells it."); return; }
        Object.assign(lingo, { from: -1, to: -1, draft: "" });
        lingoDo("learn", { word, heard: [heard] }, `From now on, “${heard}” is typed as “${word}”.`);
      } },
      el("span", {}, "“", el("strong", {}, heard), "” should be"),
      el("input", { id: "lingo-right", value: lingo.draft, "aria-label": "Correct spelling", autocomplete: "off", oninput: (e) => (lingo.draft = e.target.value) }),
      el("button", { class: "primary", type: "submit" }, "Always spell it this way"))
      : el("p", { class: "meta" }, "Click the word, or words, that came out wrong.");
    return [el("div", { class: "words", role: "group", "aria-label": "Words in this dictation" }, words), form, now];
  }

  function addPanel() {
    const heard = el("input", { placeholder: "what chttr hears (optional)", "aria-label": "What chttr hears", autocomplete: "off" });
    const word = el("input", { placeholder: "how to spell it", "aria-label": "Correct spelling", autocomplete: "off" });
    return el("form", { class: "lingo-form", onsubmit: (e) => {
        e.preventDefault();
        if (!word.value.trim()) { word.focus(); return; }
        const h = heard.value.trim();
        lingoDo("learn", { word: word.value.trim(), heard: h ? [h] : [] },
          h ? `From now on, “${h}” is typed as “${word.value.trim()}”.` : `Added “${word.value.trim()}”.`);
      } },
      el("span", {}, "When I say"), heard, el("span", {}, "type"), word, el("button", { class: "primary", type: "submit" }, "Add"));
  }

  // one small inline form (rename a spelling, add a way it's heard); Esc or Cancel closes it
  function inlineForm({ label, value = "", placeholder = "", submit, onDone }) {
    const input = el("input", { value, placeholder, "aria-label": label, autocomplete: "off" });
    const close = () => { lingo.editing = null; renderLingo(); };
    const form = el("form", { class: "inline-form", onsubmit: async (e) => {
        e.preventDefault();
        const v = input.value.trim();
        if (!v || v === value) { close(); return; }
        await submit(v);
        if (onDone) onDone();
      },
      onkeydown: (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); } } },
      input, el("button", { type: "submit", class: "primary" }, "Save"), el("button", { type: "button", class: "link", onclick: close }, "Cancel"));
    setTimeout(() => { input.focus(); input.select(); }, 0);
    return form;
  }

  function wordsTable() {
    if (!lingo.words.length) return el("p", { class: "meta" }, "No words yet. Fix one from a dictation above, or add one.");
    const editing = (w, kind) => lingo.editing && lingo.editing.word === w && lingo.editing.kind === kind;
    const edit = (w, kind) => { lingo.editing = { word: w, kind }; renderLingo(); };
    const forget = (w) => {
      const saved = { word: w.word, heard: [...w.heard] };
      lingoDo("forget", { word: w.word }, `Removed “${w.word}”.`, {
        action: { label: "Undo", run: () => lingoDo("learn", saved, `“${w.word}” is back.`) }, ms: 6000,
      });
    };
    return el("table", { class: "lingo-words" },
      el("caption", { class: "sr-only" }, "Your words and the ways chttr hears them"),
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Spelled"), el("th", { scope: "col" }, "When chttr hears"), el("th", { scope: "col" }, el("span", { class: "sr-only" }, "Actions")))),
      el("tbody", {}, lingo.words.map((w) => el("tr", {},
        el("td", {}, editing(w.word, "rename")
          ? inlineForm({ label: `New spelling for ${w.word}`, value: w.word,
            submit: (v) => lingoDo("rename", { old: w.word, new: v }, `Now spelled “${v}”.`) })
          : el("button", { type: "button", class: "spelling", title: "Change the spelling", onclick: () => edit(w.word, "rename") }, w.word)),
        el("td", {}, el("div", { class: "chips" },
          w.heard.map((h) => el("span", { class: "chip heard" }, h,
            el("button", { type: "button", class: "x", title: `Stop changing “${h}”`, "aria-label": `Remove “${h}”`, onclick: () => lingoDo("unhear", { heard: h }) }, "×"))),
          w.vocabulary && /^\p{L}{3,}$/u.test(w.word) ? el("span", { class: "chip note", title: "Single words that sound like this are corrected too" }, "≈ sound-alikes") : null,
          editing(w.word, "heard")
            ? inlineForm({ label: `What chttr types instead of ${w.word}`, placeholder: "what chttr hears",
              submit: (v) => lingoDo("learn", { word: w.word, heard: [v] }, `“${v}” is typed as “${w.word}” from now on.`) })
            : el("button", { type: "button", class: "chip add", onclick: () => edit(w.word, "heard") }, "+ add"))),
        el("td", { class: "right" }, el("button", { type: "button", class: "link danger", onclick: () => forget(w) }, "Remove"))))));
  }

  function tryPanel() {
    const out = el("div", { class: "try-out", "aria-live": "polite" });
    const input = el("input", { class: "filter wide", placeholder: "Type what chttr heard, e.g. my name is so rob", "aria-label": "Try a sentence",
      oninput: (e) => {
        clearTimeout(tryPanel.t);
        const text = e.target.value;
        tryPanel.t = setTimeout(() => {
          if (!text.trim()) { out.textContent = ""; return; }
          api("/api/lingo/try", { method: "POST", body: JSON.stringify({ text }) })
            .then((r) => { out.textContent = `→ ${r.typed}`; }).catch((x) => toast(x.message, true));
        }, 200);
      } });
    return [input, out];
  }

  function lingoNodes() {
    return [
      el("h1", {}, "Lingo"),
      el("div", { class: "sub" }, "Teach chttr how you spell things. Fixes apply from your next dictation, everywhere."),
      el("h2", {}, "Fix a dictation"), ...[fixPanel()].flat(),
      el("h2", {}, "Add a spelling"), addPanel(),
      el("h2", {}, "Your words"), wordsTable(),
      el("p", { class: "meta" }, "Single words that sound like one of your words (Sorov, Suhrab → Sohrab) are corrected automatically. Phrases need each way chttr hears them."),
      el("h2", {}, "Try it"), ...tryPanel(),
    ];
  }

  function renderLingo() {
    if (state.view !== "lingo") return;
    renderLingoList();
    const detail = $("#detail");
    const top = detail.scrollTop;
    detail.replaceChildren(...[$("#detail .back"), ...lingoNodes()].filter(Boolean));
    detail.scrollTop = top;
  }

  const lingoView = {
    title: "Lingo",
    backLabel: "Dictations",
    detailFirst: true,
    noSearch: true,
    listLabel: () => "Recent dictations",
    listKey: () => "",
    list() {
      lingo.list = el("div", {}, skeleton(5));
      return lingo.list;
    },
    detailKey: () => "",
    async detail() {
      Object.assign(lingo, await api("/api/lingo"));
      // the Dictations view hands over the dictation to fix by its timestamp
      const want = store.get("chttrLingoPick", sessionStorage);
      if (want) {
        store.del("chttrLingoPick", sessionStorage);
        const i = lingo.recent.findIndex((d) => d.ts === want);
        if (i >= 0) Object.assign(lingo, { pick: i, from: -1, to: -1, draft: "" });
      }
      if (lingo.pick === null && lingo.recent.length) lingo.pick = 0;
      if (lingo.pick !== null && lingo.pick >= lingo.recent.length) lingo.pick = lingo.recent.length ? 0 : null;
      renderLingoList();
      return lingoNodes();
    },
  };

  // ---- keyboard -------------------------------------------------------------------------------
  const KEYS = [
    ["Anywhere", [["/ or s", "Search"], ["Esc", "Clear the search, close a box"], ["j k or ↓ ↑", "Move through the list"], ["Enter", "Open"],
      ["g then h m a l d", "Home, Meetings, Action items, Lingo, Dictations"], ["?", "These keys"]]],
    ["Reading a meeting", [["Space", "Play or pause"], ["← →", "Back or forward 5 seconds"], ["Shift ← →", "30 seconds"],
      ["[ ]", "Previous or next turn"], [", .", "Slower or faster (0.75× to 2×)"], ["f or Ctrl+F", "Find in the transcript"], ["r", "Rename a speaker"]]],
  ];
  const G_VIEWS = { h: "home", m: "meetings", a: "actions", l: "lingo", d: "dictations" };

  function toggleHelp() {
    const d = $("#help");
    if (d.open) { d.close(); return; }
    d.replaceChildren(
      el("button", { type: "button", class: "button close", onclick: () => d.close() }, "Close"),
      el("h2", { id: "help-title" }, "Keyboard shortcuts"),
      ...KEYS.flatMap(([group, keys]) => [el("h3", {}, group), el("dl", {}, keys.map(([k, what]) => [el("dt", {}, k.split(" ").map((x) =>
        ["or", "then"].includes(x) ? ` ${x} ` : el("kbd", {}, x))), el("dd", {}, what)]))]));
    d.showModal();
  }

  let gPending = 0;
  function onKey(e) {
    if (e.defaultPrevented || e.isComposing) return;
    const t = e.target;
    if (t.closest && t.closest("input, select, textarea, audio, video, [contenteditable=''], [contenteditable='true']")) return;
    if ($("#help").open) {
      if (e.key === "?") { e.preventDefault(); toggleHelp(); }
      return;
    }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "f" && state.reader && state.view === "meetings") {
      e.preventDefault();
      state.reader.openFind();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (gPending && Date.now() - gPending < 1500) {
      gPending = 0;
      const name = G_VIEWS[e.key];
      if (name && views.has(name)) { e.preventDefault(); setView(name); }
      return;
    }
    gPending = 0;
    const view = currentView();
    const onButton = t.closest && t.closest("button, a, summary, [role=tab]");
    const pressesControl = onButton && (e.key === " " || e.key === "Enter");   // the focused control's own key
    if (!pressesControl && view.onKey && view.onKey(e)) { e.preventDefault(); return; }
    switch (e.key) {
      case "/": case "s":
        if (!$("#search").hidden) { e.preventDefault(); $("#q").focus(); $("#q").select(); }
        return;
      case "j": case "ArrowDown":
        if (moveFocus(1)) e.preventDefault();
        return;
      case "k": case "ArrowUp":
        if (moveFocus(-1)) e.preventDefault();
        return;
      case "Enter": {
        const item = $("#list .item.focus");
        if (item && !onButton) { e.preventDefault(); item.click(); }
        return;
      }
      case "g":
        gPending = Date.now();
        return;
      case "?":
        e.preventDefault();
        toggleHelp();
        return;
      case "Escape":
        document.querySelectorAll("#list .item.focus").forEach((x) => x.classList.remove("focus"));
        if (matchMedia("(max-width: 760px)").matches && state.route && state.route.id) navigate({ view: state.view, q: state.route.q });
        return;
    }
  }

  // ---- start ----------------------------------------------------------------------------------
  async function loadMeetings() {
    try {
      state.meetings = await api("/api/meetings");
      state.meetingsError = "";
    } catch (e) {
      state.meetingsError = e.message;
    }
  }

  async function start() {
    started = true;
    if (!TOKEN) {
      $("#detail").replaceChildren(emptyState({ icon: "!", title: "This page needs its link",
        body: el("p", {}, "Open it from the chttr menu (Meeting library), or with ", el("kbd", {}, "chttr ui"), ", which prints the link.") }));
      return;
    }
    setupTabs();
    bindSearch();
    document.addEventListener("keydown", onKey);
    $("#help-button").addEventListener("click", toggleHelp);
    $("#help").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });   // the backdrop
    window.addEventListener("hashchange", render);
    document.addEventListener("visibilitychange", onVisible);
    setInterval(() => api("/api/ping").catch(() => {}), 60000);   // keeps a `--idle-timeout` server alive while open
    $("#list").replaceChildren(skeleton());

    const [setup, , styles] = await Promise.allSettled([api("/api/setup"), loadMeetings(), api("/api/styles")]);
    state.setup = setup.status === "fulfilled" ? setup.value : null;
    state.styles = styles.status === "fulfilled" ? styles.value : [];
    state.loaded = true;
    window.dispatchEvent(new CustomEvent("chttr:ready"));
    setupTabs();                                              // views registered on chttr:ready get their tabs
    banner();
    render();
  }

  registerView("meetings", meetingsView);
  registerView("actions", actionsView);
  registerView("lingo", lingoView);

  // the API other scripts build on, there before anything renders
  window.chttr = {
    api, el, toast, ts, when, state, navigate, registerView, openMeeting, setView, emptyState, skeleton, errorCard,
    copyText, refresh, startDaemon, lib: L, get route() { return state.route; },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else setTimeout(start, 0);
})();
