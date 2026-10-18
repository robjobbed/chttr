"""`chttr ui`: the meeting library in your browser.

A small local web app on 127.0.0.1 (a random port; every request needs the
secret token in the URL chttr opens, and the Host header must match, so other
local users and web pages can't read it): search across meetings, read the
notes and the transcript with clickable timestamps (synced to the audio when
`[meeting] keep_audio` kept it), rename speakers, switch note styles, and see
every action item in one place.

Lingo (`chttr lingo`, the Lingo tab) edits the personal dictionary: pick words
from a past dictation and say how they should be spelled, rename a spelling,
drop a mis-hearing, and try a sentence to see what chttr would type.
"""

from __future__ import annotations

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
from urllib.parse import parse_qs, unquote, urlparse

from . import config as C
from . import library as L
from . import ui_api
from .dictionary import Dictionary

log = logging.getLogger("chttr.ui")
STATIC = Path(__file__).with_name("ui_static")
_ID = re.compile(r"^[\w.-]+$")
LINGO_RECENT = 40               # dictations shown in Lingo
LINGO_MAX = 120                 # longest spelling or heard phrase Lingo accepts


def _lingo_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > LINGO_MAX:
        raise ValueError(f"keep it under {LINGO_MAX} characters")
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

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; media-src 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
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
                hits = L.search(q.get("q", ""), limit=60, speaker=q.get("speaker", ""), after=q.get("after", ""),
                                before=q.get("before", ""))
                return self._json([h.to_dict() for h in hits])
            if path == "/api/actions":
                return self._json(L.action_items(owner=q.get("owner", "")))
            if path == "/api/styles":
                from .notes import load_styles

                return self._json([{"id": s.id, "name": s.name, "description": s.description}
                                   for s in load_styles().values()])
            # other packages: extra API routes dispatch here
            r = ui_api.route(path, q, self.cfg)
            if r is not None:
                return self._send(r[0], r[1], r[2])
            m = re.match(r"^/api/meeting/([^/]+)(/audio)?$", path)
            if m and _ID.match(unquote(m.group(1))):
                mid = unquote(m.group(1))
                if m.group(2):
                    return self._audio(mid)
                data = L.meeting(mid)
                if data is None:
                    return self._error(HTTPStatus.NOT_FOUND, "no such meeting")
                data["has_audio"] = _audio_for(mid) is not None
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
                return self._json({"typed": Dictionary.load().apply(_lingo_text(body.get("text")))})
            return self._json(lingo_action(action, body))
        except LookupError:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            log.exception("lingo action failed")
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        f = (STATIC / name).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

    def _audio(self, meeting_id: str) -> None:
        f = _audio_for(meeting_id)
        if f is None:
            return self._error(HTTPStatus.NOT_FOUND, "no audio kept for this meeting")
        data = f.read_bytes()
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and (rng.group(1) or rng.group(2)):
            start = int(rng.group(1) or 0)
            end = min(int(rng.group(2)) if rng.group(2) else len(data) - 1, len(data) - 1)
            return self._send(206, data[start:end + 1], "audio/ogg",
                              {"Content-Range": f"bytes {start}-{end}/{len(data)}", "Accept-Ranges": "bytes"})
        self._send(200, data, "audio/ogg", {"Accept-Ranges": "bytes"})


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
    print(f"chttr {view or 'library'}: {url}\n(Ctrl+C to stop)", flush=True)
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
