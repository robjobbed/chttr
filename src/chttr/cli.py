"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__
from . import config as C
from .client import DaemonNotRunning, read_state, send

ICONS = {"idle": "", "recording": "󰍬", "transcribing": "󰔟", "meeting": "󰑊"}


def _with_meeting(state: str) -> str:
    """Dictation idle while a meeting is being recorded shows as "meeting"."""
    return "meeting" if state == "idle" and C.MEETING_STATE_FILE.exists() else state


def _status_json(state: str) -> str:
    state = _with_meeting(state)
    tooltip = {"idle": "chttr: idle", "recording": "chttr: recording", "transcribing": "chttr: transcribing",
               "meeting": "chttr: recording a meeting"}[state]
    return json.dumps({"text": ICONS[state], "alt": state, "class": state, "tooltip": tooltip})


def cmd_daemon(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    C.write_default_config()
    try:
        cfg = C.load_config()
    except (ValueError, OSError) as exc:
        # Exiting would only have systemd restart us into the same error; say it once and keep working.
        import subprocess

        logging.error("config.toml: %s; running with the defaults", exc)
        try:
            subprocess.run(["notify-send", "-a", "chttr", "-u", "critical", "-t", "0", "chttr: config.toml has a problem",
                            f"{exc}. Running with the defaults until it is fixed."], check=False, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        cfg = C.Config(modes=C._default_modes())
    from .daemon import run

    run(cfg)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    try:
        reply = send(args.command)
    except DaemonNotRunning as exc:
        print(exc, file=sys.stderr)
        if args.command in ("start", "command", "toggle"):     # a key press: the binding discards stderr
            _offline_notice()
        return 2
    if args.json:
        print(json.dumps(reply))
    elif not reply.get("ok"):
        print(reply.get("error", "error"), file=sys.stderr)
        return 1
    elif reply.get("message"):
        print(reply["message"])
    return 0


def _offline_notice() -> None:
    """A dictation key was pressed with no daemon: start the service and say so."""
    import subprocess

    from . import install

    if not install.UNIT.exists():
        install.notify("chttr is not running", "Run: chttr setup", urgency="normal", timeout_ms=6000)
        return
    try:
        started = subprocess.run(["systemctl", "--user", "start", "chttr.service"], capture_output=True,
                                 check=False).returncode == 0
    except FileNotFoundError:
        started = False
    if started:
        install.notify("chttr is starting", "Hold the key again in a few seconds.", urgency="normal", timeout_ms=6000)
    else:
        install.notify("chttr couldn't start", "See: journalctl --user -u chttr, or run chttr doctor",
                       urgency="normal", timeout_ms=6000)


def cmd_keys(args: argparse.Namespace) -> int:
    """The shortcuts that really trigger chttr, and who catches them."""
    from . import hotkeys as H
    from .desktop import detect

    try:
        cfg = C.load_config()
    except ValueError as exc:
        print(f"config.toml: {exc}", file=sys.stderr)
        cfg = C.Config(modes=C._default_modes())
    desk = detect()
    backend = H.resolve(cfg.hotkeys.backend, desk.compositor)
    keys = H.effective_bindings(cfg, desk, backend=backend)
    width = max([len(H.DESCRIPTIONS[a]) for a in keys] + [0])
    for action, key in keys.items():
        print(f"  {H.DESCRIPTIONS[action]:<{width}}   {key}")
    _, source = H.compositor_bindings(desk.compositor) if backend == "compositor" else ({}, None)
    print()
    print({
        "compositor": f"Bound in {source}." if source else "Bound by your compositor (chttr setup adds the bindings).",
        "portal": "Registered with your desktop's shortcuts portal; change them in its keyboard settings.",
        "evdev": f"chttr reads the keyboard directly; change them in [hotkeys] of {C.CONFIG_FILE}.",
    }.get(backend, "Nothing catches the keys yet; see: chttr doctor"))
    return 0 if keys else 1


def cmd_status(args: argparse.Namespace) -> int:
    if not args.follow:
        state = read_state() if not C.SOCKET_PATH.exists() else send("status").get("state", "idle")
        print(_status_json(state) if args.json else state)
        return 0
    last = None
    try:
        while True:
            state = read_state()
            if state not in ICONS:
                state = "idle"
            state = _with_meeting(state) if args.json else state
            if state != last:
                print(_status_json(state) if args.json else state, flush=True)
                last = state
            time.sleep(0.15)
    except KeyboardInterrupt:
        return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    """Offline: run the pipeline on a WAV file without the daemon."""
    logging.basicConfig(level=logging.WARNING)
    cfg = C.load_config()
    if args.engine:
        cfg.engine.name = args.engine
    from . import tidy
    from .dictionary import Dictionary
    from .polish import backend, polish
    from .transcriber import Transcriber

    d = Dictionary.load()
    t0 = time.monotonic()
    raw = Transcriber(cfg).transcribe(Path(args.file), d.whisper_prompt())
    if args.verbose:
        print(f"[{cfg.engine.name} {time.monotonic() - t0:.2f}s incl. load]", file=sys.stderr)
    text = d.apply(raw)
    if raw and not args.raw:
        if backend(cfg.polish) == "none":
            text = tidy.tidy(text, cfg.polish.level)
        else:
            try:
                text = polish(text, cfg.polish, vocabulary=d.vocabulary, keep=list(d.snippets))
            except Exception as exc:  # noqa: BLE001 - local rules beat a traceback
                print(f"cleanup failed ({exc}); cleaned locally", file=sys.stderr)
                text = tidy.tidy(text, cfg.polish.level)
    print(d.expand_snippets(text))
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Time recognition (full pass vs. streaming), cleanup and accuracy on WAV files."""
    logging.basicConfig(level=logging.WARNING)
    from . import bench

    cfg = C.load_config()
    if args.engine:
        cfg.engine.name = args.engine
    files = [Path(f) for f in args.files]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        print(f"no such file: {', '.join(missing)}", file=sys.stderr)
        return 1
    load_ms, results = bench.run(files, cfg, runs=args.runs, cleanup=args.cleanup)
    out = {"json": bench.to_json, "markdown": bench.markdown}.get(args.format, bench.report)
    print(out(cfg.engine.name, load_ms, results))
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    """Connect (or list / remove) Claude or a destination for meeting notes."""
    import getpass
    import json as _json
    import os

    from . import apikey, vault
    from . import integrations as I

    try:
        cfg = C.load_config()
    except ValueError as exc:
        print(f"config.toml: {exc}; using the default key file", file=sys.stderr)
        cfg = C.Config(modes=C._default_modes())
    if args.service == "claude":
        if not args.remove:
            return 0 if apikey.connect_interactive(cfg) else 1
        if apikey.remove(cfg):
            print(f"Claude disconnected (removed {apikey.path(cfg)}).")
        else:
            print(f"no Claude key file at {apikey.path(cfg)}")
        if os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is still set in your environment; unset it to stop using Claude.")
        try:
            send("reload")
        except (DaemonNotRunning, OSError, ValueError):
            pass
        return 0
    if args.remove:
        print(f"disconnected {args.service}" if vault.remove(args.service) else f"{args.service} wasn't connected")
        return 0
    if args.service is None:
        print(f"  {'Claude':8} {apikey.status(cfg)}")
        for d in I.DESTINATIONS:
            print(f"  {I.label(d):8} {'connected' if vault.load(d) else '-'}")
        print("\nsecrets: " + ("desktop keyring (secret-tool)" if vault.keyring_available()
                                 else f"{vault._file()} (install libsecret's secret-tool to use the keyring)"))
        return 0

    def ask(prompt: str, secret: bool = True) -> str:
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if not value:
            raise SystemExit("nothing entered; not connected")
        return value

    s = args.service
    if s == "slack":
        print("Create an incoming webhook for the channel: https://api.slack.com/messaging/webhooks")
        value = ask("Webhook URL (https://hooks.slack.com/services/…): ")
        if not value.startswith("https://hooks.slack.com/"):
            raise SystemExit("that isn't a Slack incoming-webhook URL")
    elif s == "notion":
        print("Create an internal integration (https://www.notion.so/my-integrations), then share the page that")
        print("should hold your meeting notes with it (••• > Connections).")
        token = ask("Integration secret (ntn_… or secret_…): ")
        page = ask("Parent page link or id: ", secret=False)
        m = re.search(r"([0-9a-f]{32})", page.replace("-", ""))
        if not m:
            raise SystemExit("couldn't find a page id in that")
        value = _json.dumps({"token": token, "parent": m.group(1)})
    elif s == "hubspot":
        print("Create a private app (Settings > Integrations > Private apps) with the scopes")
        print("crm.objects.contacts.read and crm.objects.contacts.write.")
        value = ask("Access token (pat-…): ")
    else:
        url = ask("URL to POST each meeting to (https://…): ", secret=False)
        if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise SystemExit("use an https:// URL (or a local http:// one)")
        secret = getpass.getpass("Signing secret for X-Chttr-Signature (Enter for none): ").strip()
        value = _json.dumps({"url": url, "secret": secret})
    where = vault.store(s, value)
    print(f"{I.label(s)} connected (secret stored in {where}).")
    print("After each meeting the notes notification offers a \"Send to\" button; `chttr send` sends by hand.")
    return 0


def cmd_send_notes(args: argparse.Namespace) -> int:
    from . import integrations as I
    from . import library as L
    from .meeting import resolve

    cfg = C.load_config()
    directory = resolve(args.meeting)
    L.sync()
    meeting = L.meeting(directory.name)
    if not meeting:
        print(f"{directory.name} has no notes yet", file=sys.stderr)
        return 1
    targets = args.to or I.connected()
    if not targets:
        print("nothing connected; see: chttr connect", file=sys.stderr)
        return 1
    s = I.summary(meeting)
    print(f"{s['title']} ({s['when'][:16]}, {len(s['sections'])} sections, {len(s['actions'])} action items)")
    if not args.yes:
        try:
            if input(f"Send to {', '.join(I.label(t) for t in targets)}? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        except EOFError:
            return 1
    status = 0
    for t in targets:
        try:
            print(f"  {I.label(t)}: {I.send(t, meeting, hubspot_tasks=cfg.integrations.hubspot_tasks)}")
        except I.SendError as exc:
            print(f"  {I.label(t)}: FAILED: {exc}", file=sys.stderr)
            status = 1
    return status


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer a question from your meetings with Claude (or Ollama); without either, search them."""
    from . import ask as A
    from .notes import available
    from .polish import backend

    cfg = C.load_config()
    question = " ".join(args.question)

    def local(hint: str = "") -> int:
        lines = [A.citation(h, highlight=sys.stdout.isatty()) for h in A.search_hits(question)]
        for line in lines:
            print(line)
        if not lines:
            print("Nothing in your meetings or dictations matches that.", file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)
        return 0 if lines else 1

    if args.local or not available(cfg.polish):
        return local("" if args.local else
                     "Local search only; add a Claude key (chttr connect claude) or run Ollama for answers.")

    def show(name: str, kw: dict) -> None:
        if args.verbose:
            shown = ", ".join(f"{k}={v!r}" for k, v in kw.items() if v)
            print(f"  · {name}({shown})", file=sys.stderr, flush=True)

    if backend(cfg.polish) == "ollama":
        try:
            print(A.ask_ollama(question, cfg, on_search=show))
        except RuntimeError as exc:
            return local(f"{exc}; these are the search results.")
        return 0
    print(A.ask(question, cfg, on_search=show))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import run

    return run()


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui import serve

    serve(port=args.port, open_browser=not args.no_browser, cfg=C.load_config())
    return 0


def cmd_lingo(args: argparse.Namespace) -> int:
    """Lingo: fix how chttr spells your words, in the browser."""
    from .ui import serve

    serve(port=args.port, open_browser=not args.no_browser, cfg=C.load_config(), view="lingo",
          idle_timeout=args.idle_timeout)
    return 0


def cmd_polish(args: argparse.Namespace) -> int:
    """Read raw text on stdin, print cleaned text (usable as a post-processor for other tools)."""
    cfg = C.load_config()
    text = sys.stdin.read().strip()
    if not text:
        return 0
    from . import tidy
    from .polish import PolishUnavailable, polish

    if args.local:
        print(tidy.tidy(text, cfg.polish.level))
        return 0
    try:
        print(polish(text, cfg.polish, args.app or "", ""))
    except Exception as exc:  # noqa: BLE001 - local rules beat a traceback
        print(tidy.tidy(text, cfg.polish.level))
        none = isinstance(exc, PolishUnavailable) and str(exc).startswith("no cleanup backend")
        print("no cleanup backend; cleaned locally (add a key: chttr connect claude)" if none
              else f"cleanup failed ({exc}); cleaned locally", file=sys.stderr)
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    from .dictionary import DICTIONARY_FILE, Dictionary

    d = Dictionary.load()
    d.learn(args.word, args.heard)
    d.save()
    heard = ", ".join(f"{h!r}" for h in (args.heard or []))
    print(f"learned {args.word!r}" + (f" (heard as {heard})" if heard else "") + f" -> {DICTIONARY_FILE}")
    if args.notify:            # clicked "Learn it?" in a notification: nobody sees this terminal
        import subprocess

        try:
            subprocess.run(["notify-send", "-a", "chttr", "-t", "2500", f"Learned \u201c{args.word}\u201d",
                            "chttr spells it that way from now on."],
                           timeout=5, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    from .dictionary import Dictionary

    d = Dictionary.load()
    if d.forget(args.word):
        d.save()
        print(f"forgot {args.word!r}")
        return 0
    print(f"{args.word!r} is not in the dictionary", file=sys.stderr)
    return 1


def cmd_dictionary(args: argparse.Namespace) -> int:
    from .dictionary import DICTIONARY_FILE, Dictionary

    d = Dictionary.load()
    print(f"# {DICTIONARY_FILE}")
    print("vocabulary:", ", ".join(d.vocabulary) or "(none)")
    for heard, typed in sorted(d.replacements.items()):
        print(f"  {heard!r:20} -> {typed}")
    if d.snippets:
        print("snippets:")
        for phrase, expansion in sorted(d.snippets.items()):
            print(f"  {phrase!r:20} -> {expansion[:60]!r}")
    return 0


def cmd_snippet(args: argparse.Namespace) -> int:
    from .dictionary import Dictionary

    d = Dictionary.load()
    expansion = args.expansion.replace("\\n", "\n")
    d.add_snippet(args.phrase, expansion)
    d.save()
    print(f"say {args.phrase!r} (or \"insert {args.phrase}\") to type {expansion!r}")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    """Learn from the last dictation: pick what came out wrong, type what you meant."""
    from .dictionary import Dictionary
    from .history import load

    d = Dictionary.load()
    if args.wrong and args.right:
        d.learn(args.right, [args.wrong])
        d.save()
        print(f"learned: {args.wrong!r} -> {args.right!r}")
        return 0
    entries = load()
    if not entries:
        print("no dictations in history yet", file=sys.stderr)
        return 1
    last = entries[-1]
    print(f"heard:  {last.get('raw', '')}")
    print(f"typed:  {last.get('text', '')}\n")
    try:
        wrong = (args.wrong or input("Which word or phrase came out wrong? (as typed or heard) ")).strip()
        if not wrong:
            return 1
        right = input(f"What should {wrong!r} be? ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 130
    if not right:
        return 1
    d.learn(right, [wrong])
    d.save()
    print(f"\nlearned: {wrong!r} -> {right!r}. chttr will type {right!r} from now on.")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from .history import _local, load, pick

    entries = load()
    if not entries:
        print("no dictations in history yet", file=sys.stderr)
        return 1
    if args.print:
        for e in entries[-args.print:]:
            when = _local(e.get("ts", ""))
            print(f"{when.strftime('%m-%d %H:%M') if when else '?':11}  {e.get('app', '') or '-':12.12}  {e.get('text', '')}")
        return 0
    if args.play:
        with_audio = [e for e in entries if e.get("audio") and (C.HISTORY_AUDIO_DIR / e["audio"]).exists()]
        if not with_audio:
            print("no dictation audio kept (set [history] keep_audio_days in the config)", file=sys.stderr)
            return 1
        e = with_audio[-min(args.play, len(with_audio))]
        print(f"{e.get('ts', '')}  {e.get('text', '')}")
        import subprocess

        return subprocess.run(["pw-play", str(C.HISTORY_AUDIO_DIR / e["audio"])], check=False).returncode
    text = pick(entries)
    if text is None:
        return 130
    from .output import deliver

    cfg = C.load_config()
    time.sleep(0.25)            # let focus return to the window the menu covered
    method = deliver(text, cfg.output)
    if method == "clipboard":
        print("copied to clipboard")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from .history import load, stats

    print(stats(load(), days=args.days))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from . import doctor

    cfg = C.load_config()
    env = doctor.Env()
    checks = doctor.run_all(cfg, env)
    if args.fix:
        fixable = [c for c in checks if not c.ok and c.auto and c.fix]
        out = sys.stderr if args.json else sys.stdout
        if fixable:
            message = f"fixing {len(fixable)} problem{'' if len(fixable) == 1 else 's'}…"
        elif any(not c.ok for c in checks):
            message = "nothing --fix can do on its own; see the fix: lines below"
        else:
            message = "nothing to fix"
        print(message, file=out, flush=True)
        for line in doctor.apply_fixes(checks, env, cfg):
            print(line, file=out, flush=True)
        print(file=out)
        env.cache.clear()
        checks = doctor.run_all(cfg, env)
    ok = all(c.ok for c in checks)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
        return 0 if ok else 1
    print(f"config:      {C.CONFIG_FILE}")
    print(doctor.render(checks, hint=not args.fix))
    if args.verify:
        ok = _verify_models(cfg) and ok
    if args.mic:
        ok = _mic_check(cfg) and ok
    return 0 if ok else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    from .install import uninstall

    print("chttr uninstall", flush=True)
    uninstall(purge=args.purge, yes=args.yes, say=lambda line: print(f"  {line}", flush=True))
    return 0


def _mic_check(cfg, seconds: float = 4.0) -> bool:
    """Record a few seconds, show the levels and what the recognizer heard."""
    from . import levels
    from .recorder import Recorder
    from .transcriber import Transcriber

    print(f"mic:         say something for {seconds:.0f} seconds…", flush=True)
    rec = Recorder(cfg.audio.device)
    try:
        rec.start()
    except RuntimeError as exc:
        print(f"mic:         PROBLEM: {exc}")
        return False
    time.sleep(seconds)
    audio, _ = rec.stop()
    peak, chunks = rec.peak_db, list(rec.levels_db)
    configured = cfg.audio.threshold_db
    # The same numbers the daemon goes by: the room's floor, your speech (the chunks
    # clearly above it, so one short word counts) and the threshold that follows them.
    floor = levels.noise_floor(chunks)
    speech = levels.speech_level(chunks, floor)
    thr = levels.effective_threshold(chunks, configured) if cfg.audio.adaptive_threshold else configured
    text = Transcriber(cfg).transcribe(audio)     # the verdict never contradicts "heard:"
    if peak < -45:
        verdict = "PROBLEM: silent (muted, or the wrong input device?)"
    elif peak > -1:
        verdict = "clipping; lower the input volume"
    elif speech <= thr and not text:
        verdict = f"too quiet: speech is under the {thr:.0f} dB threshold; raise the input level or move closer"
    elif speech < configured:
        verdict = f"quiet but usable (threshold lowered to {thr:.0f} dB)" if thr < configured else "quiet but usable"
    else:
        verdict = "good"
    print(f"mic:         peak {peak:.0f} dB, speech {speech:.0f} dB, floor {floor:.0f} dB: {verdict}")
    print(f"heard:       {text or '(nothing)'}")
    return peak >= -45 and bool(text)


def _verify_models(cfg) -> bool:
    """Hash every model file against the pinned SHA-256."""
    from . import diarize
    from . import models as M
    from .transcriber import resolve_parakeet

    ok = True
    checks = [(spec, C.MODELS_DIR / rel) for rel, spec in diarize.MODELS.items()]
    try:
        parakeet = resolve_parakeet(cfg.engine.parakeet_dir)
        checks += [(spec, parakeet / spec.name) for spec in M.PARAKEET if spec.name != "config.json"]
    except FileNotFoundError:
        pass
    for spec, path in checks:
        if not path.exists():
            print(f"verify:      {spec.name}: not downloaded yet")
            continue
        good = M.verify(spec, path)
        ok = ok and good
        print(f"verify:      {spec.name}: {'ok' if good else 'PROBLEM: does not match the pinned checksum'}")
    return ok


def _send_or_exit(command: str) -> dict:
    try:
        return send(command)
    except DaemonNotRunning as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None


def cmd_meeting_control(args: argparse.Namespace) -> int:
    command = f"meeting {args.action}"
    # hidden flags, set by the daemon's "Record this call?" notification
    opts = {k: v for k in ("title", "source", "call") if (v := getattr(args, k, None))}
    if args.action == "start" and opts:
        command += " " + json.dumps(opts)
    reply = _send_or_exit(command)
    if args.json:
        print(json.dumps(reply))
        return 0 if reply.get("ok") else 1
    if not reply.get("ok"):
        print(reply.get("error", "error"), file=sys.stderr)
        return 1
    m, call = reply.get("meeting"), reply.get("call")
    if args.action == "status":
        if m:
            print(f"recording {m['title'] or m['code'] or 'a meeting'} ({m['seconds'] // 60} min) -> {m['dir']}")
        else:
            print("not recording" + (f"; in Meet call {call['code']}" if call else ""))
    else:
        print(reply.get("message", ""))
    return 0


def cmd_meeting_process(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    import signal

    from .meeting import notify_done, process, read_json, resolve, summary_line, update_meta

    directory = resolve(args.meeting)

    def interrupted(_signum, _frame):
        # stopped by a shutdown or `systemctl --user stop`: leave a status you can act on
        if (read_json(directory / "meta.json", {}) or {}).get("status") != "done":
            update_meta(directory, status="failed", error=f"interrupted; retry: chttr meeting process {directory.name}")
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        cfg = C.load_config()
    except (ValueError, OSError) as exc:
        # the daemon recorded this meeting on the defaults; transcribe it the same way rather than lose it
        logging.warning("config.toml: %s; processing with the defaults", exc)
        cfg = C.Config(modes=C._default_modes())
    try:
        md = process(directory, cfg, summary=False if args.no_summary else None)
    except Exception as exc:
        logging.exception("processing failed")
        update_meta(directory, status="failed", error=f"{type(exc).__name__}: {exc}")
        if args.notify:
            import subprocess

            subprocess.run(["notify-send", "-a", "chttr", "-u", "critical", "Meeting transcript failed",
                            f"{type(exc).__name__}: {str(exc)[:200]}\nRetry: chttr meeting process {directory.name}"],
                           check=False)
        return 1
    print(md)
    offer = _after_meeting(directory, cfg)
    if args.notify:
        notify_done(md, summary_line(md, directory), offer, on_send=lambda d: _send_and_tell(d, directory, cfg))
    return 0


def _after_meeting(directory: Path, cfg) -> list[str]:
    """Run the on_meeting_done hook and automatic sends; return the destinations to offer."""
    from . import integrations as I
    from . import library as L

    try:
        L.sync()
        meeting = L.meeting(directory.name)
    except Exception:
        logging.exception("library update failed")
        return []
    if not meeting:
        return []
    I.run_hook(cfg.integrations.on_meeting_done, meeting)
    auto = I.auto_destinations(cfg, meeting["title"])
    for d in auto:
        _send_and_tell(d, directory, cfg, meeting)
    return [d for d in I.connected() if d not in auto] if cfg.integrations.send in ("ask", "always") else []


def _send_and_tell(destination: str, directory: Path, cfg, meeting: dict | None = None) -> str:
    import subprocess

    from . import integrations as I
    from . import library as L

    meeting = meeting or L.meeting(directory.name) or {}
    try:
        result = I.send(destination, meeting, hubspot_tasks=cfg.integrations.hubspot_tasks)
        logging.info("%s: %s", destination, result)
        subprocess.run(["notify-send", "-a", "chttr", "-t", "4000", f"Meeting notes {result}", meeting.get("title", "")],
                       check=False)
        return result
    except I.SendError as exc:
        logging.warning("%s: %s", destination, exc)
        subprocess.run(["notify-send", "-a", "chttr", "-u", "critical", f"Couldn't send to {I.label(destination)}",
                        str(exc)[:300]], check=False)
        return f"failed: {exc}"


def cmd_meeting_list(args: argparse.Namespace) -> int:
    from datetime import datetime

    from .meeting import meeting_dirs, read_json

    dirs = meeting_dirs()
    if not dirs:
        print("no meetings recorded yet")
        return 0
    for d in dirs[-args.n:]:
        meta = read_json(d / "meta.json", {}) or {}
        when = datetime.fromtimestamp(float(meta.get("started", 0))).strftime("%a %b %d %H:%M")
        mins = round(float(meta.get("duration", 0)) / 60)
        what = Path(meta["markdown"]).name if meta.get("markdown") else (meta.get("title") or meta.get("code") or "")
        status = meta.get("status", "?") + (f": {meta['error']}" if meta.get("error") else "")
        print(f"{d.name:36} {when}  {mins:3} min  {status:10}  {what}")
    return 0


def cmd_meeting_rename(args: argparse.Namespace) -> int:
    from .meeting import rename, resolve

    md = rename(resolve(args.meeting), args.old, args.new, args.email or "", C.load_config())
    print(f"{args.old} -> {args.new} in {md}; voice remembered")
    return 0


def cmd_meeting_calendar(args: argparse.Namespace) -> int:
    from . import gcal

    cfg = C.load_config()
    if args.url:
        if "calendar.google.com" not in args.url or not args.url.endswith(".ics"):
            print("that doesn't look like a secret iCal address (https://calendar.google.com/calendar/ical/…/basic.ics)",
                  file=sys.stderr)
            return 1
        print(f"saved to {gcal.add_url(cfg.meeting.calendar_file, args.url)} (chmod 600)")
    urls = gcal.read_urls(cfg.meeting.calendar_file)
    if not urls:
        print("no calendar yet. In Google Calendar: Settings > (your calendar) > Integrate calendar >\n"
              "\"Secret address in iCal format\", then: chttr meeting calendar 'https://calendar.google.com/…/basic.ics'")
        return 1
    events = gcal.upcoming(urls, hours=48, me_email=cfg.meeting.email)
    print(f"{len(urls)} calendar(s); next 48 h:")
    for e in events[:15]:
        who = ", ".join(a.name or a.email for a in e.attendees if not a.is_me)
        print(f"  {e.start:%a %H:%M}  {e.title[:40]:40}  {'Meet ' + e.meet_code if e.meet_code else '':18}  {who[:60]}")
    if not events:
        print("  (nothing)")
    return 0


def cmd_meeting_voices(args: argparse.Namespace) -> int:
    from .diarize import load_voices, save_voices

    voices = load_voices()
    if args.forget:
        key = args.forget.lower()
        if key not in voices:
            print(f"no voice {args.forget!r}", file=sys.stderr)
            return 1
        del voices[key]
        save_voices(voices)
        print(f"forgot {args.forget}")
        return 0
    if not voices:
        print("no voices remembered yet")
    for v in voices.values():
        print(f"  {v.name or '-':24} {v.email or '':32} {v.count} meeting(s), last {v.updated[:10]}")
    return 0


def cmd_meeting_styles(args: argparse.Namespace) -> int:
    from .notes import USER_STYLES, load_styles

    cfg = C.load_config()
    for s in load_styles().values():
        mark = "*" if s.id == cfg.meeting.style else " "
        print(f"{mark} {s.id:17} {s.name:28} {s.description}" + ("" if s.builtin else "  (yours)"))
    print(f"\n* default ([meeting] style = {cfg.meeting.style!r}; \"auto\" lets Claude pick per meeting)")
    print(f"Add your own in {USER_STYLES}; see the built-in ones for the format.")
    print("Use: chttr meeting style <id>   or   chttr meeting style --prompt \"focus on engineering risks\"")
    return 0


def _recording_dir() -> Path | None:
    try:
        reply = send("meeting status")
    except DaemonNotRunning:
        return None
    m = reply.get("meeting")
    return Path(m["dir"]) if reply.get("recording") and m else None


def cmd_meeting_style(args: argparse.Namespace) -> int:
    from .meeting import notify_done, read_json, resolve, restyle, summary_line, update_meta
    from .notes import CUSTOM, load_styles

    styles = load_styles()
    style = args.style or (CUSTOM if args.prompt else "")
    if style and style != CUSTOM and style not in styles:
        print(f"no note style {style!r}; one of: {', '.join(styles)}", file=sys.stderr)
        return 1
    live = None if args.meeting else _recording_dir()
    directory = live or resolve(args.meeting)
    meta = read_json(directory / "meta.json", {}) or {}
    if not style:
        current = (read_json(directory / "result.json", {}) or {}).get("notes", {}).get("style") or meta.get("style")
        print(f"{directory.name}: {current or 'not chosen yet'}  (chttr meeting styles lists the options)")
        return 0
    name = styles[style].name if style in styles else "custom"
    if live or meta.get("status") in ("recording", "recorded"):
        update_meta(directory, style=style, style_extra=args.prompt or "")
        print(f"this meeting's notes will be written as {name}")
        return 0
    if meta.get("status") == "processing":
        print("this meeting is still being processed; run this again when its notes are saved", file=sys.stderr)
        return 1
    if args.notify:
        import subprocess

        subprocess.run(["notify-send", "-a", "chttr", "-t", "4000", f"Rewriting the notes as {name}…",
                        directory.name], check=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    try:
        md = restyle(directory, C.load_config(), style, args.prompt or "", fresh=args.fresh)
    except (SystemExit, Exception) as exc:
        if args.notify:
            import subprocess

            subprocess.run(["notify-send", "-a", "chttr", "-u", "critical", "Couldn't change the notes style",
                            str(exc)[:300]], check=False)
        raise
    print(md)
    if args.notify:
        notify_done(md, f"{name} notes · " + summary_line(md, directory))
    return 0


def cmd_meeting_install(args: argparse.Namespace) -> int:
    from .native_host import EXTENSION_DIR, extension_id, install

    for line in install():
        print(line)
    print(f"\nextension: {EXTENSION_DIR} (id {extension_id()})")
    print("Restart Chromium to load it. If you'd rather load it by hand: chrome://extensions >"
          " Developer mode > Load unpacked > the folder above.")
    return 0


def cmd_native_host(args: argparse.Namespace) -> int:
    from .native_host import run

    return run()


def cmd_setup(args: argparse.Namespace) -> int:
    from .install import run

    return run(do_models=not args.no_models, do_service=not args.no_service,
               do_bindings=not args.no_keybindings, do_meet=not args.no_meet)


def _ollama_script(polish) -> str:
    """Start Ollama so it outlives the terminal, then pull the cleanup model (a 404 otherwise)."""
    import shlex

    model = shlex.quote(polish.ollama_model)
    return ("setsid -f ollama serve >/dev/null 2>&1; "
            "for _ in $(seq 40); do ollama list >/dev/null 2>&1 && break; sleep 0.25; done; "
            f"ollama pull {model} && echo && "
            "echo 'Ollama is running: chttr cleans up your dictation offline now.' && "
            "echo 'It stops when you log out. To start it at boot: sudo systemctl enable --now ollama'")


def _terminal(command: str) -> None:
    from .desktop import open_terminal

    open_terminal(command)


def _pick_style(directory: Path) -> None:
    """Style picker for one meeting; the rewrite runs in the background with a notification."""
    import subprocess

    from . import desktop as DT
    from .meeting import read_json
    from .notes import load_styles

    styles = load_styles()
    current = (read_json(directory / "result.json", {}) or {}).get("notes", {}).get("style") \
        or (read_json(directory / "meta.json", {}) or {}).get("style", "")
    rows = [f"{'󰄬' if s.id == current else '󰈙'}\t{s.name}\t{s.description}" for s in styles.values()]
    rows.append("󰏫\tCustom instructions…\tDescribe the notes you want, in your own words")
    picked = DT.menu("Notes style", rows, width=760)
    choice = picked.split("\t")[1] if picked and "\t" in picked else ""
    if not choice:
        return
    cmd = [sys.argv[0], "meeting", "style", "--meeting", str(directory), "--notify"]
    if choice.startswith("Custom instructions"):
        prompt = DT.ask("What should the notes focus on?")
        if not prompt:
            return
        cmd += ["--prompt", prompt]
    else:
        style = next((s.id for s in styles.values() if s.name == choice), "")
        if not style:
            return
        cmd.append(style)
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_menu(args: argparse.Namespace) -> int:
    """The bar widget's menu (Omarchy's menu picker, or fuzzel / wofi / rofi elsewhere)."""
    import shutil
    import subprocess

    from . import desktop as DT
    from . import polish as P
    from .meeting import meeting_dirs, read_json
    try:
        status = send("meeting status")
    except DaemonNotRunning:
        status = None
    cfg = C.load_config()
    notes = [Path(m["markdown"]) for d in reversed(meeting_dirs())
             if (m := read_json(d / "meta.json", {}) or {}).get("markdown") and Path(m["markdown"]).exists()]
    items: list[tuple[str, Callable[[], object]]] = []
    no_key = cfg.polish.backend == "claude" and not P._claude_configured(cfg.polish)
    if cfg.polish.backend != "none" and cfg.polish.level != "none" and (P.backend(cfg.polish) == "none" or no_key):
        # cleanup is on but has nothing to run on: `chttr connect claude` is the guided key entry
        items.append(("󰚩  Turn on Claude cleanup…", lambda: _terminal("chttr connect claude")))
        if shutil.which("ollama") and cfg.polish.backend == "auto":
            items.append(("󰚩  Use Ollama for cleanup (offline)", lambda: _terminal(_ollama_script(cfg.polish))))
    if status is None:
        items.append(("󰑓  Start the chttr daemon", lambda: subprocess.run(["systemctl", "--user", "restart", "chttr"])))
    elif status.get("recording"):
        items.append(("󰓛  Stop recording the meeting", lambda: send("meeting stop")))
    else:
        items.append(("󰑊  Record a meeting", lambda: send("meeting start")))
    target = None
    if status and status.get("recording") and status.get("meeting"):
        target = Path(status["meeting"]["dir"])
        items.append(("󰏫  Notes style for this meeting…", lambda: _pick_style(target)))
    else:
        done = [d for d in reversed(meeting_dirs()) if (read_json(d / "meta.json", {}) or {}).get("status") == "done"]
        if done:
            target = done[0]
            items.append(("󰏫  Change the last meeting's notes style…", lambda: _pick_style(target)))
    if notes:
        items.append((f"󰈙  Open the last meeting notes ({notes[0].stem[:40]})",
                      lambda: subprocess.Popen(["xdg-open", str(notes[0])], start_new_session=True)))
    meetings_dir = Path(cfg.meeting.dir).expanduser()
    items += [
        ("󰉋  Meeting notes folder", lambda: (meetings_dir.mkdir(parents=True, exist_ok=True),
                                            subprocess.Popen(["xdg-open", str(meetings_dir)], start_new_session=True))),
        ("󰋚  Dictation history", lambda: subprocess.Popen([sys.argv[0], "history"], start_new_session=True)),
        ("󰗊  Lingo: fix how words are spelled", lambda: subprocess.Popen(
            [sys.argv[0], "lingo"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)),
        ("󰄨  Stats", lambda: _terminal("chttr stats")),
        ("󰒓  Edit settings", lambda: DT.open_editor(str(C.CONFIG_FILE))),
        ("󰓦  Check setup (doctor)", lambda: _terminal("chttr doctor")),
        ("󰜉  Restart the daemon", lambda: subprocess.run(["systemctl", "--user", "restart", "chttr"])),
    ]
    try:
        choice = DT.menu("chttr", [label for label, _ in items])
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    for label, action in items:
        if label == choice:
            action()
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    created = C.write_default_config()
    if args.path:
        print(C.CONFIG_FILE)
    else:
        print(("created " if created else "") + str(C.CONFIG_FILE))
        print(C.CONFIG_FILE.read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chttr", description="Wispr Flow-style voice dictation for Omarchy.")
    p.add_argument("--version", action="version", version=f"chttr {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="run the dictation daemon (loads the whisper model once)")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=cmd_daemon)

    for name, help_ in (
        ("toggle", "hands-free: start recording (stops by itself on silence), or stop now"),
        ("start", "start recording (push-to-talk press)"),
        ("command", "start a command-mode recording on the selected text (key press)"),
        ("release", "push-to-talk key release: stop and transcribe unless hands-free"),
        ("stop", "stop any recording and transcribe"),
        ("cancel", "discard the current recording"),
        ("reload", "reload config.toml in the running daemon"),
        ("quit", "stop the daemon"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--json", action="store_true", help="print the daemon reply as JSON")
        s.set_defaults(func=cmd_send, command=name)

    st = sub.add_parser("status", help="print the daemon state (idle/recording/transcribing)")
    st.add_argument("--follow", action="store_true", help="stream state changes until interrupted")
    st.add_argument("--json", action="store_true", help="bar-friendly JSON (text/alt/class/tooltip)")
    st.set_defaults(func=cmd_status)

    t = sub.add_parser("transcribe", help="transcribe a 16 kHz mono WAV file (no daemon needed)")
    t.add_argument("file")
    t.add_argument("--raw", action="store_true", help="skip the cleanup step")
    t.add_argument("--engine", choices=["parakeet", "whisper"], help="override [engine] name")
    t.add_argument("-v", "--verbose", action="store_true", help="print timing to stderr")
    t.set_defaults(func=cmd_transcribe)

    bn = sub.add_parser("bench", help="time recognition, streaming and cleanup on WAV files (no daemon needed)")
    bn.add_argument("files", nargs="+", metavar="FILE.wav", help="a FILE.txt next to it adds the word error rate")
    bn.add_argument("--runs", type=int, default=3)
    bn.add_argument("--cleanup", action="store_true", help="also time the cleanup round trip (uses your API key)")
    bn.add_argument("--engine", choices=["parakeet", "whisper"])
    bn.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    bn.set_defaults(func=cmd_bench)

    ak = sub.add_parser("ask", help='ask your meetings: chttr ask "what did Dana commit to last week?"')
    ak.add_argument("question", nargs="+")
    ak.add_argument("-v", "--verbose", action="store_true", help="show the searches Claude runs")
    ak.add_argument("--local", action="store_true", help="only search, with citations; nothing leaves your machine")
    ak.set_defaults(func=cmd_ask)

    cn = sub.add_parser("connect", help="connect Claude (an API key), or Slack, Notion, HubSpot or a webhook for "
                                        "meeting notes (no args: list)")
    cn.add_argument("service", nargs="?", choices=["claude", "slack", "notion", "hubspot", "webhook"])
    cn.add_argument("--remove", action="store_true", help="disconnect it")
    cn.set_defaults(func=cmd_connect)

    sd = sub.add_parser("send", help="send a meeting's notes to your connected destinations")
    sd.add_argument("meeting", nargs="?", help="meeting id (default: the latest)")
    sd.add_argument("--to", action="append", choices=["slack", "notion", "hubspot", "webhook"],
                    help="only this destination (repeatable)")
    sd.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sd.set_defaults(func=cmd_send_notes)

    mc = sub.add_parser("mcp", help="serve your meetings to Claude Code / Claude Desktop (MCP, stdio)")
    mc.set_defaults(func=cmd_mcp)

    ui = sub.add_parser("ui", help="the meeting library in your browser (search, transcripts, renames)")
    ui.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    ui.add_argument("--no-browser", action="store_true", help="only print the link")
    ui.set_defaults(func=cmd_ui)

    li = sub.add_parser("lingo", help="fix how chttr spells your words: pick them from past dictations, in your browser")
    li.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    li.add_argument("--no-browser", action="store_true", help="only print the link")
    li.add_argument("--idle-timeout", type=float, default=900, metavar="SECONDS",
                    help="stop once the page has been closed this long (default 900; 0 = run until Ctrl+C)")
    li.set_defaults(func=cmd_lingo)

    po = sub.add_parser("polish", help="clean up dictated text from stdin (Claude, Ollama or local rules)")
    po.add_argument("--app", help="name of the focused app, for tone/formatting")
    po.add_argument("--local", action="store_true", help="local rules only (no model)")
    po.set_defaults(func=cmd_polish)

    le = sub.add_parser("learn", help="teach chttr a word: chttr learn Sohrab --heard sorob --heard 'so rob'")
    le.add_argument("word", help="the correct spelling")
    le.add_argument("--heard", action="append", metavar="TEXT", help="what the recognizer produced instead (repeatable)")
    le.add_argument("--notify", action="store_true", help=argparse.SUPPRESS)
    le.set_defaults(func=cmd_learn)

    fo = sub.add_parser("forget", help="remove a word and its replacements from the dictionary")
    fo.add_argument("word")
    fo.set_defaults(func=cmd_forget)

    di = sub.add_parser("dictionary", help="show the personal dictionary")
    di.set_defaults(func=cmd_dictionary)

    sn = sub.add_parser("snippet", help="add a voice snippet: chttr snippet 'my email' rob@example.com")
    sn.add_argument("phrase", help="what you say")
    sn.add_argument("expansion", help="what gets typed (\\n for a new line)")
    sn.set_defaults(func=cmd_snippet)

    fx = sub.add_parser("fix", help="correct a word from the last dictation so it sticks")
    fx.add_argument("wrong", nargs="?", help="what came out (skip to be asked)")
    fx.add_argument("right", nargs="?", help="what it should be")
    fx.set_defaults(func=cmd_fix)

    hi = sub.add_parser("history", help="pick a past dictation and type it again")
    hi.add_argument("--print", type=int, nargs="?", const=20, metavar="N", help="print the last N instead")
    hi.add_argument("--play", type=int, nargs="?", const=1, metavar="N",
                    help="play the audio of the Nth most recent dictation (needs [history] keep_audio_days)")
    hi.set_defaults(func=cmd_history)

    sa = sub.add_parser("stats", help="words dictated, speaking speed, time saved")
    sa.add_argument("--days", type=int, default=7)
    sa.set_defaults(func=cmd_stats)

    dr = sub.add_parser("doctor", help="check the engine, cleanup backend and daemon")
    dr.add_argument("--verify", action="store_true", help="also hash every model file against its pinned SHA-256")
    dr.add_argument("--mic", action="store_true", help="also record 4 seconds and show the level and the transcript")
    dr.add_argument("--json", action="store_true", help="print the checks as JSON")
    dr.add_argument("--fix", action="store_true", help="run the safe fixes (no sudo), then check again")
    dr.set_defaults(func=cmd_doctor)

    me = sub.add_parser("meeting", help="record meetings: transcripts with speaker names, summary and action items")
    msub = me.add_subparsers(dest="meeting_cmd", required=True)
    for name, help_ in (
        ("start", "start recording a meeting (mic + speaker output)"),
        ("stop", "stop recording and write the notes"),
        ("toggle", "start or stop"),
        ("cancel", "stop and throw the recording away"),
        ("status", "is a meeting being recorded?"),
    ):
        s = msub.add_parser(name, help=help_)
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=cmd_meeting_control, action=name)
        if name == "start":                 # from the "Record this call?" notification
            s.add_argument("--title", help=argparse.SUPPRESS)
            s.add_argument("--source", choices=["manual", "app"], help=argparse.SUPPRESS)
            s.add_argument("--call", help=argparse.SUPPRESS)
    mp = msub.add_parser("process", help="(re)write the notes for a recorded meeting")
    mp.add_argument("meeting", nargs="?", help="meeting id or directory (default: the latest)")
    mp.add_argument("--no-summary", action="store_true", help="transcript only, no Claude notes")
    mp.add_argument("--notify", action="store_true", help=argparse.SUPPRESS)
    mp.set_defaults(func=cmd_meeting_process)
    ml = msub.add_parser("list", help="recorded meetings")
    ml.add_argument("-n", type=int, default=20)
    ml.set_defaults(func=cmd_meeting_list)
    mr = msub.add_parser("rename", help="fix a speaker's name (and remember their voice)")
    mr.add_argument("old", help='label in the transcript, e.g. "Speaker 2"')
    mr.add_argument("new", help="their name")
    mr.add_argument("--email")
    mr.add_argument("--meeting", help="meeting id (default: the latest)")
    mr.set_defaults(func=cmd_meeting_rename)
    mc = msub.add_parser("calendar", help="add a Google Calendar secret iCal address / show upcoming meetings")
    mc.add_argument("url", nargs="?")
    mc.set_defaults(func=cmd_meeting_calendar)
    mv = msub.add_parser("voices", help="list remembered voices")
    mv.add_argument("--forget", metavar="EMAIL_OR_NAME")
    mv.set_defaults(func=cmd_meeting_voices)
    mss = msub.add_parser("styles", help="list note styles (General, BANT, 1:1, standup, ...)")
    mss.set_defaults(func=cmd_meeting_styles)
    mst = msub.add_parser("style", help="set a meeting's notes style; after the call this rewrites the notes")
    mst.add_argument("style", nargs="?", help="style id (chttr meeting styles); omit to show the current one")
    mst.add_argument("--prompt", help="extra instructions, or with no style: fully custom notes")
    mst.add_argument("--meeting", help="meeting id (default: the one being recorded, else the latest)")
    mst.add_argument("--fresh", action="store_true", help="write again even if this style was written before")
    mst.add_argument("--notify", action="store_true", help=argparse.SUPPRESS)
    mst.set_defaults(func=cmd_meeting_style)
    mi = msub.add_parser("install", help="register the Google Meet extension and its native host")
    mi.set_defaults(func=cmd_meeting_install)

    se = sub.add_parser("setup", help="download models, install the service, keybindings and Meet extension")
    se.add_argument("--no-models", action="store_true")
    se.add_argument("--no-service", action="store_true", help="don't install the systemd user service")
    se.add_argument("--no-keybindings", action="store_true")
    se.add_argument("--no-meet", action="store_true", help="skip the Google Meet extension")
    se.set_defaults(func=cmd_setup)

    ky = sub.add_parser("keys", help="show the shortcuts that are bound right now")
    ky.set_defaults(func=cmd_keys)

    un = sub.add_parser("uninstall", help="remove the service, keybindings and Meet extension that setup added")
    un.add_argument("--purge", action="store_true", help="also delete history, meeting data, models and settings")
    un.add_argument("-y", "--yes", action="store_true", help="don't ask before --purge deletes anything")
    un.set_defaults(func=cmd_uninstall)

    mn = sub.add_parser("menu", help="the bar widget's menu")
    mn.set_defaults(func=cmd_menu)

    nh = sub.add_parser("native-host", help=argparse.SUPPRESS)
    nh.set_defaults(func=cmd_native_host)

    c = sub.add_parser("config", help="create (if missing) and show the config file")
    c.add_argument("--path", action="store_true", help="only print the config path")
    c.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    sys.exit(args.func(args))
