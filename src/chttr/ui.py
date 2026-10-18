"""`chttr ui`: the meeting library in your browser.

A small local web app on 127.0.0.1 (a random port; every request needs the
secret token in the URL chttr opens, and the Host header must match, so other
local users and web pages can't read it): search across meetings, read the
notes and the transcript with clickable timestamps (synced to the audio when
`[meeting] keep_audio` kept it), rename speakers, switch note styles, and see
every action item in one place. The page follows the active Omarchy theme
(theme.py), keeps its place in the URL (#TOKEN/meeting/<id>?t=152), and can be
opened at a `chttr ask` citation with `chttr ui --open "Title, 2026-10-01, 00:02:32"`.

Lingo (`chttr lingo`, the Lingo tab) edits the personal dictionary: pick words
from a past dictation and say how they should be spelled, rename a spelling,
drop a mis-hearing, and try a sentence to see what chttr would type.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import mimetypes
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import client
from . import config as C
from . import library as L
from . import theme as T
from .dictionary import Dictionary

log = logging.getLogger("chttr.ui")
STATIC = Path(__file__).with_name("ui_static")
_ID = re.compile(r"^(?!\.+$)[\w.-]+$")     # a meeting id; never "." or ".."
LINGO_RECENT = 40               # dictations shown in Lingo
LINGO_MAX = 120                 # longest spelling or heard phrase Lingo accepts
TRY_MAX = 4000                  # longest sentence the "try it" preview takes (a long dictation)
AUDIO_CHUNK = 256 * 1024
_run = subprocess.run           # tests replace this: POST /api/daemon/start runs systemctl


def _lingo_text(value: object, limit: int = LINGO_MAX) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise ValueError(f"keep it under {limit} characters")
    return text


def lingo_state() -> dict:
    from .history import load

    recent = [{"ts": e.get("ts", ""), "app": e.get("app", ""), "heard": str(e.get("raw") or e.get("text") or ""),
               "typed": str(e.get("text") or "")}
              for e in reversed(load()[-LINGO_RECENT:]) if e.get("kind", "dictate") == "dictate"]
    return {"words": Dictionary.load().words(), "recent": recent}


def lingo_action(action: str, body: dict) -> dict:
    """One Lingo edit; saves the dictionary (the daemon re-reads it on every dictation)."""
    d = Dictionary.load()
    if action == "learn":
        word = _lingo_text(body.get("word"))
        heard = [h for h in (_lingo_text(x) for x in body.get("heard") or []) if h]
        if not word:
            raise ValueError("type how it should be spelled")
        d.learn(word, heard)
    elif action == "rename":
        old, new = str(body.get("old", "")), _lingo_text(body.get("new"))
        if not d.rename(old, new):
            raise ValueError(f"{old!r} is not in Lingo")
    elif action == "unhear":
        if not d.unhear(str(body.get("heard", ""))):
            raise ValueError("that isn't in Lingo")
    elif action == "forget":
        if not d.forget(str(body.get("word", ""))):
            raise ValueError("that isn't in Lingo")
    else:
        raise LookupError(action)
    d.save()
    return lingo_state()


def _audio_for(meeting_id: str) -> Path | None:
    m = L.meeting(meeting_id)
    if not m or not m.get("markdown"):
        return None
    opus = Path(m["markdown"]).with_suffix(".opus")
    return opus if opus.exists() else None


# ---- setup and daemon state, for the page's banners and empty states --------------------------

_backend_cache: tuple[float, str] = (0.0, "")


def _cleanup_backend(cfg: C.Config) -> str:
    """polish.backend() probes Ollama over HTTP; ask at most once a minute."""
    global _backend_cache
    from .polish import backend

    at, value = _backend_cache
    if not value or time.monotonic() - at > 60:
        try:
            value = backend(cfg.polish)
        except Exception:  # noqa: BLE001 - a status line must not break the page
            value = "none"
        _backend_cache = (time.monotonic(), value)
    return value


def _daemon_status(timeout: float = 0.5) -> dict | None:
    """The daemon's `meeting status` reply, or None when it isn't running (or doesn't answer)."""
    try:
        reply = client.send("meeting status", timeout=timeout)
    except (client.DaemonNotRunning, OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


def _extension_installed() -> bool:
    from .native_host import BROWSERS, HOST_NAME

    return any((config_dir / "NativeMessagingHosts" / f"{HOST_NAME}.json").exists() for _, config_dir, _ in BROWSERS)


def setup_info(cfg: C.Config) -> dict:
    hk = cfg.hotkeys
    return {"daemon": _daemon_status() is not None, "cleanup_backend": _cleanup_backend(cfg),
            "notes_available": _notes_available(cfg),
            "hotkeys": {"dictate": hk.dictate, "hands_free": hk.hands_free, "meeting": hk.meeting,
                        "history": hk.history, "command": hk.command},
            "detect_calls": cfg.meeting.detect_calls, "extension_installed": _extension_installed(),
            "meetings_dir": str(Path(cfg.meeting.dir).expanduser()), "typer": cfg.output.typer}


def _notes_available(cfg: C.Config) -> bool:
    from . import notes as N

    try:
        return bool(N.available(cfg.polish))
    except Exception:  # noqa: BLE001 - a status line must not break the page
        return False


def status_info() -> dict:
    """What the daemon is doing; never raises (the page polls this)."""
    reply = _daemon_status()
    if reply is None:
        return {"state": client.read_state(), "meeting": None, "daemon": False}
    meeting = reply.get("meeting") if isinstance(reply.get("meeting"), dict) else None
    return {"state": str(reply.get("state") or client.read_state()), "meeting": meeting, "daemon": True}


# ---- export -----------------------------------------------------------------------------------

def _filename(title: str, when: str, ext: str) -> str:
    stem = " ".join(re.sub(r"[^\w .,()&+-]", " ", f"{when[:10]} {title}").split()) or "meeting"
    return f"{stem[:80]}.{ext}"


def export(meeting_id: str, fmt: str) -> tuple[bytes, str, str] | None:
    """(body, content type, file name) of a meeting as Markdown, JSON or CSV; None when there is
    nothing to export (no such meeting, or no Markdown file)."""
    m = L.meeting(meeting_id)
    if m is None:
        return None
    if fmt == "md":
        md = Path(m["markdown"]) if m.get("markdown") else None
        if md is None or not md.is_file():
            return None
        return md.read_bytes(), "text/markdown; charset=utf-8", md.name
    if fmt == "json":
        data = {k: m[k] for k in ("id", "title", "when", "minutes", "speakers", "talk_seconds", "notes", "transcript")
                if k in m}
        body = json.dumps(data, ensure_ascii=False, indent=1).encode()
        return body, "application/json; charset=utf-8", _filename(m["title"], m["when"], "json")
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["start", "end", "speaker", "text"])
        for u in m["transcript"]:
            w.writerow([f"{float(u['start']):.2f}", f"{float(u.get('end') or u['start']):.2f}", u["speaker"], u["text"]])
        return out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", _filename(m["title"], m["when"], "csv")
    raise LookupError(fmt)


def _disposition(name: str, inline: bool = False) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    return f'{"inline" if inline else "attachment"}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'


# ---- `chttr ui --open "<citation>"` -----------------------------------------------------------

_CITATION = re.compile(r"^\s*\[?\s*(?P<title>.+?)\s*,\s*(?P<date>\d{4}-\d{2}-\d{2})\s*"
                       r"(?:,\s*(?P<time>\d{1,2}(?::\d{1,2}){1,2}))?\s*\]?\s*$")


def route_for_citation(text: str, meetings: list[dict]) -> str | None:
    """A `chttr ask` citation ("Pricing review, 2026-10-01, 00:02:32", brackets optional) ->
    the page route "meeting/<id>?t=152"; None when no meeting has that title on that day."""
    m = _CITATION.match(text or "")
    if not m:
        return None
    title, day = m.group("title").strip().casefold(), m.group("date")
    same_day = [x for x in meetings if str(x.get("when", ""))[:10] == day]
    found = (next((x for x in same_day if str(x.get("title", "")).casefold() == title), None)
             or next((x for x in same_day if title in str(x.get("title", "")).casefold()
                      or str(x.get("title", "")).casefold() in title), None)
             or (same_day[0] if len(same_day) == 1 else None))
    if found is None:
        return None
    route = f"meeting/{quote(str(found['id']), safe='')}"
    if m.group("time"):
        parts = [int(x) for x in m.group("time").split(":")]
        h, mi, sec = ([0, 0, *parts])[-3:]
        route += f"?t={h * 3600 + mi * 60 + sec}"
    return route


class Handler(BaseHTTPRequestHandler):
    token = ""
    host = ""
    cfg: C.Config | None = None
    last_request = 0.0          # monotonic time of the last authorized request, for the idle timeout

    # ---- plumbing ----------------------------------------------------------------
    def log_message(self, fmt, *args) -> None:
        log.debug("ui: " + fmt, *args)

    def _authorized(self) -> bool:
        if self.headers.get("Host", "") != self.host:
            return False                     # DNS rebinding: a page on another name pointing at 127.0.0.1
        query = parse_qs(urlparse(self.path).query)
        given = self.headers.get("X-Chttr-Token") or (query.get("token") or [""])[0]
        ok = secrets.compare_digest(given, self.token)
        if ok:
            Handler.last_request = time.monotonic()
        return ok

    def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; media-src 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---- routes ---------------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html") or path.startswith("/static/"):
            return self._static(path)
        if not self._authorized():
            return self._error(HTTPStatus.FORBIDDEN, "forbidden")
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            if path == "/api/ping":
                return self._json({"ok": True})
            if path == "/api/lingo":
                return self._json(lingo_state())
            if path == "/api/meetings":
                return self._json(L.meetings(limit=500))
            if path == "/api/search":
                hits, facets = L.search_with_facets(
                    q.get("q", ""), limit=60, speaker=q.get("speaker", ""), after=q.get("after", ""),
                    before=q.get("before", ""), prefix_last=q.get("prefix_last") == "1",
                    meeting_id=q.get("meeting", ""), facets_too=q.get("facets") == "1")
                if q.get("facets") == "1":
                    return self._json({"hits": [h.to_dict() for h in hits], "facets": facets})
                return self._json([h.to_dict() for h in hits])
            if path == "/api/theme":
                return self._json(T.load_theme() or {"mode": "auto", "vars": {}})
            if path == "/api/setup":
                return self._json(setup_info(self.cfg or C.load_config()))
            if path == "/api/status":
                return self._json(status_info())
            if path == "/api/actions":
                return self._json(L.action_items(owner=q.get("owner", "")))
            if path == "/api/styles":
                from .notes import load_styles

                return self._json([{"id": s.id, "name": s.name, "description": s.description}
                                   for s in load_styles().values()])
            # other packages: extra API routes dispatch here
            m = re.match(r"^/api/meeting/([^/]+)(/audio|/export)?$", path)
            if m and _ID.match(unquote(m.group(1))):
                mid = unquote(m.group(1))
                if m.group(2) == "/audio":
                    return self._audio(mid)
                if m.group(2) == "/export":
                    return self._export(mid, q.get("format", "md"), inline=q.get("inline") == "1")
                data = L.meeting(mid)
                if data is None:
                    return self._error(HTTPStatus.NOT_FOUND, "no such meeting")
                data["has_audio"] = _audio_for(mid) is not None
                data["notes_available"] = _notes_available(self.cfg or C.load_config())
                return self._json(data)
        except Exception as exc:
            log.exception("ui request failed")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        if not self._authorized() or self.headers.get("X-Chttr-Token") is None:
            return self._error(HTTPStatus.FORBIDDEN, "forbidden")      # POSTs need the header: no form CSRF
        path = urlparse(self.path).path
        if path.startswith("/api/lingo/"):
            return self._lingo(path.removeprefix("/api/lingo/"), self._body())
        if path == "/api/daemon/start":
            return self._start_daemon()
        if path == "/api/daemon/meeting-start":
            return self._meeting_start()
        m = re.match(r"^/api/meeting/([^/]+)/(rename|style)$", path)
        if not m or not _ID.match(unquote(m.group(1))):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        from .meeting import rename, resolve, restyle

        body = self._body()
        cfg = self.cfg or C.load_config()
        try:
            directory = resolve(unquote(m.group(1)))
            if m.group(2) == "rename":
                old, new = str(body.get("old", "")).strip(), str(body.get("new", "")).strip()
                if not old or not new:
                    return self._error(HTTPStatus.BAD_REQUEST, "old and new names are required")
                rename(directory, old, new, str(body.get("email", "")).strip(), cfg)
            else:
                restyle(directory, cfg, str(body.get("style", "")), str(body.get("prompt", "")), fresh=False)
            L.sync()
            return self._json(L.meeting(directory.name))
        except SystemExit as exc:            # the meeting helpers report user errors this way
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            log.exception("ui action failed")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _lingo(self, action: str, body: dict) -> None:
        try:
            if action == "try":
                return self._json({"typed": Dictionary.load().apply(_lingo_text(body.get("text"), TRY_MAX))})
            return self._json(lingo_action(action, body))
        except LookupError:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            log.exception("lingo action failed")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _start_daemon(self) -> None:
        try:
            out = _run(["systemctl", "--user", "start", "chttr.service"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"couldn't run systemctl: {exc}")
        if out.returncode != 0:
            detail = (out.stderr or out.stdout or "").strip().splitlines()
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR,
                               "chttr didn't start" + (f": {detail[-1]}" if detail else "") + " (see: chttr doctor)")
        return self._json({"ok": True, "message": "chttr is starting"})

    def _meeting_start(self) -> None:
        try:
            reply = client.send("meeting start")
        except client.DaemonNotRunning:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "chttr isn't running")
        except (OSError, ValueError) as exc:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, f"chttr didn't answer: {exc}")
        if not reply.get("ok", True):
            return self._error(HTTPStatus.CONFLICT, str(reply.get("error") or "couldn't start recording"))
        return self._json(reply)

    def _export(self, meeting_id: str, fmt: str, inline: bool = False) -> None:
        try:
            out = export(meeting_id, fmt)
        except LookupError:
            return self._error(HTTPStatus.BAD_REQUEST, "format must be md, json or csv")
        if out is None:
            return self._error(HTTPStatus.NOT_FOUND, "no Markdown notes for this meeting" if fmt == "md"
                               else "no such meeting")
        body, ctype, name = out
        if inline and fmt == "md":
            ctype = "text/plain; charset=utf-8"           # shown in the tab, not downloaded
        self._send(200, body, ctype, {"Content-Disposition": _disposition(name, inline and fmt == "md")})

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        f = (STATIC / name).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        body = f.read_bytes()
        if name == "index.html":
            body = _themed(body)
        self._send(200, body, ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

    def _audio(self, meeting_id: str) -> None:
        f = _audio_for(meeting_id)
        if f is None:
            return self._error(HTTPStatus.NOT_FOUND, "no audio kept for this meeting")
        size = f.stat().st_size
        start, end, status = 0, size - 1, 200
        extra = {"Accept-Ranges": "bytes"}
        rng = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", self.headers.get("Range", ""))
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
            else:                                          # bytes=-500: the last 500 bytes
                start = max(0, size - int(rng.group(2)))
            if start >= size or end < start:
                return self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", "audio/ogg",
                                  {"Content-Range": f"bytes */{size}", **extra})
            status = 206
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        self._headers(status, "audio/ogg", length, extra)
        if self.command == "HEAD":
            return
        with f.open("rb") as fh:                           # stream the range; never the whole file at once
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(AUDIO_CHUNK, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return                                 # the player seeked elsewhere and hung up
                left -= len(chunk)


def _themed(page: bytes) -> bytes:
    """index.html with the Omarchy theme applied up front (no flash of the default palette).
    The page re-reads /api/theme when it comes back into view, for theme switches."""
    t = T.load_theme()
    if not t:
        return page
    style = ";".join(f"{k}:{v}" for k, v in t["vars"].items() if re.fullmatch(r"--[\w-]+", k)
                     and re.fullmatch(r"[#\w(),. ]+", v))
    attrs = f' data-theme="{t["mode"]}"' + (f' style="{style}"' if style else "")
    return page.replace(b'<html lang="en">', f'<html lang="en"{attrs}>'.encode(), 1)


def serve(port: int = 0, open_browser: bool = True, cfg: C.Config | None = None, view: str = "",
          idle_timeout: float = 0) -> None:
    """Serve until Ctrl+C, or until no page has asked for anything for `idle_timeout` seconds
    (an open page pings every minute), so a copy started from a menu doesn't linger."""
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    host = f"127.0.0.1:{server.server_address[1]}"
    Handler.token, Handler.host, Handler.cfg = token, host, cfg
    Handler.last_request = time.monotonic()
    url = f"http://{host}/#{token}" + (f"/{view}" if view else "")
    L.sync()
    name = re.split(r"[/?]", view)[0] if view else "library"
    print(f"chttr {name}: {url}\n(Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Thread(target=_open, args=(url,), daemon=True).start()
    if idle_timeout > 0:
        threading.Thread(target=_stop_when_idle, args=(server, idle_timeout), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _stop_when_idle(server: ThreadingHTTPServer, idle_timeout: float) -> None:
    while time.monotonic() - Handler.last_request < idle_timeout:
        time.sleep(min(30.0, idle_timeout / 4))
    log.info("ui: no requests for %.0f s, stopping", idle_timeout)
    server.shutdown()


def _open(url: str) -> None:
    try:
        subprocess.Popen(["xdg-open", url], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        webbrowser.open(url)
