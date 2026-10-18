// chttr Dictations: every dictation, newest first, searchable from the header. A dictation shows
// what chttr typed, what it heard (words cleanup dropped struck through, words it added
// highlighted), replay when [history] keep_audio_days kept the audio, copy, and a jump into Lingo.
// Registers itself with app.js's view registry; does nothing without it. Pure helpers are node-tested.
"use strict";

(function () {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const OUTPUT = { type: "typed", paste: "pasted", clipboard: "copied to clipboard" };
  const LIMIT = 500;

  // ---- pure helpers -----------------------------------------------------------------------
  const dayKey = (d) => `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;

  // Entries (already newest first) -> [{key, label, items}] with 'Today', 'Yesterday', 'Oct 10'
  // (and the year once it isn't this year's), in local time.
  function groupByDay(entries, now = new Date()) {
    const today = dayKey(now);
    const y = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    const yesterday = dayKey(y);
    const groups = [];
    for (const e of entries || []) {
      const when = new Date(e.ts);
      const valid = e.ts && !Number.isNaN(when.getTime());
      const key = valid ? dayKey(when) : "unknown";
      let label = "Unknown date";
      if (key === today) label = "Today";
      else if (key === yesterday) label = "Yesterday";
      else if (valid) {
        label = `${MONTHS[when.getMonth()]} ${when.getDate()}`;
        if (when.getFullYear() !== now.getFullYear()) label += `, ${when.getFullYear()}`;
      }
      const last = groups[groups.length - 1];
      if (last && last.key === key) last.items.push(e);
      else groups.push({ key, label, items: [e] });
    }
    return groups;
  }

  // The API's [["equal"|"del"|"ins", "words"], ...] -> [{tag: "span"|"del"|"ins"|null, text}],
  // with plain-space entries (tag null) between chunks.
  function diffNodesModel(diff) {
    const tags = { equal: "span", del: "del", ins: "ins" };
    const out = [];
    for (const [kind, text] of diff || []) {
      if (!text || !tags[kind]) continue;
      if (out.length) out.push({ tag: null, text: " " });
      out.push({ tag: tags[kind], text });
    }
    return out;
  }

  const changed = (diff) => (diff || []).some(([kind]) => kind === "del" || kind === "ins");

  // How it ended: typed, pasted, copied, or nothing at all (a command or cleanup that failed).
  const outcome = (e) => (e.typed === false ? "nothing typed" : OUTPUT[e.output] || e.output || "");

  function metaLine(e) {
    const bits = [e.app || "", `${(Number(e.seconds) || 0).toFixed(1)} s`, outcome(e)];
    if (e.kind === "command") bits.unshift("command");
    return bits.filter(Boolean).join(" · ");
  }

  const helpers = { groupByDay, diffNodesModel, changed, metaLine };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // ---- the view --------------------------------------------------------------------------------
  const view = { current: null, seq: 0, q: "", gen: 0 };

  // Only the empty state needs it (for the real hotkey), so it is asked for then, and afresh.
  function getSetup(c) {
    return c.api("/api/setup").catch(() => (c.state && c.state.setup) || {});
  }

  function token(c) {
    if (c.state && c.state.token) return c.state.token;
    if (window.chttrLib && window.chttrLib.parseRoute) return window.chttrLib.parseRoute(location.hash).token || "";
    return location.hash.slice(1).split(/[/?]/)[0];
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

  async function copyCurrent(c) {
    if (!view.current) return;
    c.toast((await copyText(view.current.text)) ? "Copied." : "Couldn't copy; select the text instead.", false);
  }

  function emptyState(c, setup, q) {
    if (q) return c.el("div", { class: "empty" }, `No dictations match “${q}”.`);
    const key = (setup.hotkeys && setup.hotkeys.dictate) || "Insert";
    return c.el("div", { class: "empty" }, "Hold ", c.el("kbd", {}, key), " and talk. Dictations show up here.");
  }

  function row(c, e, route) {
    const on = route.id === e.id;
    // data-id lets app.js move the highlight (and aria-current) when another dictation opens
    return c.el("button", { class: "item dictation" + (on ? " active" : ""), type: "button", id: `dictation-${e.id}`,
      "data-id": e.id, "aria-current": on ? "true" : null, onclick: () => c.navigate({ view: "dictations", id: e.id, q: view.q }) },
      c.el("div", { class: "snip two-lines" }, e.text),
      c.el("div", { class: "meta" }, metaLine(e)));
  }

  async function fillList(c, box, route, q) {
    const seq = ++view.seq;
    view.q = q || "";
    const params = new URLSearchParams({ limit: String(LIMIT) });
    if (view.q) params.set("q", view.q);
    let entries;
    try {
      entries = await c.api(`/api/dictations?${params}`);
    } catch (e) {
      if (seq === view.seq) box.replaceChildren(c.el("div", { class: "section-label" }, "Couldn't load dictations"), c.el("p", { class: "meta pad" }, e.message));
      return;
    }
    if (seq !== view.seq) return;               // a newer search already answered
    if (!entries.length) {
      const setup = view.q ? {} : await getSetup(c);
      if (seq !== view.seq) return;
      box.replaceChildren(c.el("div", { class: "section-label" }, view.q ? "No matches" : "No dictations yet"),
        view.q ? c.el("p", { class: "meta pad" }, `Nothing you dictated contains “${view.q}”.`) : emptyState(c, setup, ""));
      return;
    }
    const more = entries.length >= LIMIT;
    const label = view.q ? `${entries.length}${more ? "+" : ""} matching “${view.q}”` : null;
    box.replaceChildren(...[label ? c.el("div", { class: "section-label" }, label) : null,
      ...groupByDay(entries).flatMap((g) => [c.el("div", { class: "section-label day" }, g.label), ...g.items.map((e) => row(c, e, route))]),
      more ? c.el("p", { class: "meta pad more" }, view.q ? `Showing the newest ${LIMIT}. Add a word to narrow it down.`
        : `Showing your newest ${LIMIT} dictations. Search to find older ones.`) : null]
      .filter(Boolean));
    const active = box.querySelector(".item.active");
    if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest" });
  }

  function diffBlock(c, d) {
    if (!d.raw || !d.raw.trim()) return null;
    if (d.typed === false) return null;            // the note under the text says so; there is nothing to compare
    // a voice command's raw is the instruction, not a draft of what got typed: no diff for it
    if (d.kind === "command") return [c.el("h2", {}, "You said"), c.el("p", { class: "heard" }, d.raw.trim())];
    const nodes = diffNodesModel(d.diff).map((n) => (n.tag ? c.el(n.tag, {}, n.text) : n.text));
    return [
      c.el("h2", {}, "What chttr heard"),
      changed(d.diff)
        ? c.el("p", { class: "heard diff" }, nodes)
        : c.el("p", { class: "meta" }, d.raw.trim() === d.text.trim() ? "Typed exactly as heard."
          : "Typed as heard, apart from capitals and punctuation."),
      changed(d.diff) ? c.el("p", { class: "meta legend" }, c.el("del", {}, "struck"), " = dropped by cleanup, ",
        c.el("ins", {}, "highlighted"), " = added or spelled differently") : null,
    ];
  }

  function audioBlock(c, d) {
    if (d.has_audio) {
      return c.el("audio", { controls: true, preload: "metadata",
        src: `/api/dictation/${encodeURIComponent(d.id)}/audio?token=${encodeURIComponent(token(c))}` });
    }
    const days = Number(d.keep_audio_days) || 0;
    return c.el("p", { class: "meta audio-hint" }, days
      ? `The audio of this dictation is gone (chttr keeps it ${days} day${days === 1 ? "" : "s"}).`
      : ["Set ", c.el("code", {}, "[history] keep_audio_days"), " to keep audio and replay dictations here."]);
  }

  function fixInLingo(c, d) {
    try { sessionStorage.setItem("chttrLingoPick", d.ts); } catch { /* private mode: Lingo just opens */ }
    c.navigate({ view: "lingo", id: d.id });
  }

  function renderDictation(c, d) {
    const when = d.ts && c.when ? c.when(d.ts) : d.ts;
    const meta = [when, d.app, d.mode && d.mode !== "default" ? `${d.mode} mode` : "", `${d.seconds.toFixed(1)} s`,
      `${d.words} word${d.words === 1 ? "" : "s"}`, outcome(d)].filter(Boolean).join(" · ");
    return c.el("div", { class: "dictation-view" },
      c.el("div", { class: "sub" }, d.kind === "command" ? c.el("span", { class: "chip note" }, "command") : null, c.el("span", {}, meta)),
      c.el("p", { class: "dict-text" }, d.text),
      d.typed === false ? c.el("p", { class: "meta not-typed" }, [
        d.kind === "command" ? "Nothing was typed for this one: the command didn't come back with an answer."
          : "Nothing was typed for this one.",
        d.text === d.raw ? " Above is what chttr heard." : ""].join("")) : null,
      c.el("div", { class: "dict-actions" },
        c.el("button", { class: "primary", type: "button", title: "Copy (c)", onclick: () => copyCurrent(c) }, "Copy"),
        d.kind !== "command" ? c.el("button", { class: "button", type: "button", onclick: () => fixInLingo(c, d) }, "Fix a word in Lingo") : null),
      audioBlock(c, d),
      ...[diffBlock(c, d)].flat().filter(Boolean));
  }

  async function detail(c, route) {
    let id = route.id;
    if (!id) {
      const params = new URLSearchParams({ limit: "1" });
      if (route.q) params.set("q", route.q);
      const [first] = await c.api(`/api/dictations?${params}`);
      if (!first) {
        view.current = null;
        return emptyState(c, await getSetup(c), route.q || "");
      }
      id = first.id;
    }
    let d;
    try {
      d = await c.api(`/api/dictation/${encodeURIComponent(id)}`);
    } catch (e) {
      view.current = null;
      return c.el("div", { class: "empty" }, /no such/i.test(e.message) ? "That dictation isn't in your history any more." : e.message);
    }
    view.current = d;
    return renderDictation(c, d);
  }

  function register() {
    const c = window.chttr;
    if (!c || typeof c.registerView !== "function" || register.done) return;
    register.done = true;
    c.registerView("dictations", {
      title: "Dictations",
      placeholder: "Search your dictations…",
      searchLabel: "Search dictations",
      // search() fills the list itself, so app.js only rebuilds it when the route asks for different
      // words than the list shows (Back after a search, a link); opening a dictation keeps it.
      listKey(route) {
        if (((route && route.q) || "") !== view.q) view.gen++;
        return String(view.gen);
      },
      list(route) {
        const box = c.el("div", { class: "dictation-list", "aria-busy": "true" }, c.el("div", { class: "section-label" }, "Loading…"));
        fillList(c, box, route || {}, (route && route.q) || "").finally(() => box.removeAttribute("aria-busy"));
        return box;
      },
      detail(route) { return detail(c, route || {}); },
      search(q) {
        const list = document.getElementById("list");
        const box = c.el("div", { class: "dictation-list" });
        if (list) list.replaceChildren(box);
        return fillList(c, box, { id: view.current && view.current.id }, q || "").then(() => box);
      },
      onKey(e) {
        if (e.key === "c" && !e.ctrlKey && !e.metaKey && !e.altKey && view.current) {
          copyCurrent(c);
          return true;
        }
        return false;
      },
    });
  }

  if (window.chttr) register();
  else window.addEventListener("chttr:ready", register, { once: true });
})();
