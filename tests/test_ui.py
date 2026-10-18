from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from chttr import ui as U
from tests.test_library import lib  # noqa: F401 - the fixture


@pytest.fixture(autouse=True)
def _no_desktop_theme(monkeypatch, tmp_path):
    """The served page reads the Omarchy theme; never the one on this machine."""
    from chttr import theme

    monkeypatch.setattr(theme, "OMARCHY_COLORS", tmp_path / "no-theme" / "colors.toml")


@pytest.fixture
def server(lib):  # noqa: F811
    srv = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
    port = srv.server_address[1]
    U.Handler.token, U.Handler.host = "secret", f"127.0.0.1:{port}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


TOKEN = {"X-Chttr-Token": "secret"}


def test_page_and_assets_are_served_without_the_token(server):
    status, body, headers = get(server + "/")
    assert status == 200 and b"static/app.js" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert get(server + "/static/app.js")[0] == 200
    assert get(server + "/static/../ui.py")[0] == 404


def test_api_needs_the_token(server):
    assert get(server + "/api/meetings")[0] == 403
    assert get(server + "/api/meetings", {"X-Chttr-Token": "wrong"})[0] == 403
    status, body, _ = get(server + "/api/meetings", TOKEN)
    assert status == 200 and [m["title"] for m in json.loads(body)] == ["Launch sync", "Pricing review"]


def test_api_rejects_a_foreign_host_header(server):
    # a page on evil.example resolving to 127.0.0.1 (DNS rebinding) sends its own Host
    assert get(server + "/api/meetings", {**TOKEN, "Host": "evil.example"})[0] == 403


def test_search_meeting_and_actions(server):
    hits = json.loads(get(server + "/api/search?q=budget", TOKEN)[1])
    assert any(h["title"] == "Pricing review" for h in hits)
    m = json.loads(get(server + "/api/meeting/2026-10-01-140000", TOKEN)[1])
    assert m["title"] == "Pricing review" and m["has_audio"] is False
    assert get(server + "/api/meeting/does-not-exist", TOKEN)[0] == 404
    assert json.loads(get(server + "/api/actions?owner=ali", TOKEN)[1])[0]["due"] == "Friday"


def test_posts_need_the_header_not_just_the_query_token(server, monkeypatch):
    status, _ = post(server + "/api/meeting/2026-10-01-140000/rename?token=secret", {"old": "Ali", "new": "Ali Khan"})
    assert status == 403                                     # a cross-site form can't set headers
    import chttr.meeting as M

    renamed = {}
    monkeypatch.setattr(M, "rename", lambda d, old, new, email, cfg: renamed.update(old=old, new=new))
    status, _ = post(server + "/api/meeting/2026-10-01-140000/rename", {"old": "Ali", "new": "Ali Khan"}, TOKEN)
    assert status == 200 and renamed == {"old": "Ali", "new": "Ali Khan"}
    status, _ = post(server + "/api/meeting/2026-10-01-140000/rename", {"old": "", "new": "x"}, TOKEN)
    assert status == 400


@pytest.fixture
def lingo_files(monkeypatch, tmp_path):
    from chttr import dictionary, history

    log = tmp_path / "history.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in [
        {"ts": "2026-10-13T13:52:37+00:00", "kind": "dictate", "app": "foot",
         "raw": "So my name is Sora Beth Ani.", "text": "So my name is Sora Beth Ani."},
        {"ts": "2026-10-13T13:53:00+00:00", "kind": "command", "raw": "make it shorter", "text": "Short."},
    ]) + "\n")
    monkeypatch.setattr(history.C, "HISTORY_FILE", log)
    dictionary.DICTIONARY_FILE.unlink(missing_ok=True)
    yield
    dictionary.DICTIONARY_FILE.unlink(missing_ok=True)


def test_lingo_needs_the_token_and_the_header(server, lingo_files):
    assert get(server + "/api/lingo")[0] == 403
    assert post(server + "/api/lingo/learn?token=secret", {"word": "Sohrab"})[0] == 403


def test_lingo_learns_from_a_dictation_and_applies_it(server, lingo_files):
    state = json.loads(get(server + "/api/lingo", TOKEN)[1])
    assert state["words"] == []
    assert [r["heard"] for r in state["recent"]] == ["So my name is Sora Beth Ani."]   # commands are left out

    status, state = post(server + "/api/lingo/learn", {"word": "Sohrab Behbahani", "heard": ["Sora Beth Ani"]}, TOKEN)
    assert status == 200
    assert state["words"] == [{"word": "Sohrab Behbahani", "heard": ["sora beth ani"], "vocabulary": True}]
    status, out = post(server + "/api/lingo/try", {"text": "So my name is Sora, Beth Ani."}, TOKEN)
    assert status == 200 and out["typed"] == "So my name is Sohrab Behbahani."

    from chttr.dictionary import Dictionary

    assert Dictionary.load().replacements == {"sora beth ani": "Sohrab Behbahani"}   # saved for the daemon


def test_lingo_rename_unhear_and_forget(server, lingo_files):
    post(server + "/api/lingo/learn", {"word": "Sorab", "heard": ["so rob", "sorob"]}, TOKEN)
    status, state = post(server + "/api/lingo/rename", {"old": "Sorab", "new": "Sohrab"}, TOKEN)
    assert status == 200 and state["words"][0] == {"word": "Sohrab", "heard": ["so rob", "sorob"], "vocabulary": True}
    status, state = post(server + "/api/lingo/unhear", {"heard": "sorob"}, TOKEN)
    assert status == 200 and state["words"][0]["heard"] == ["so rob"]
    status, state = post(server + "/api/lingo/forget", {"word": "Sohrab"}, TOKEN)
    assert status == 200 and state["words"] == []


def test_lingo_rejects_bad_input(server, lingo_files):
    assert post(server + "/api/lingo/learn", {"word": "  ", "heard": ["x"]}, TOKEN)[0] == 400
    assert post(server + "/api/lingo/learn", {"word": "x" * 500}, TOKEN)[0] == 400
    assert post(server + "/api/lingo/rename", {"old": "Nobody", "new": "Somebody"}, TOKEN)[0] == 400
    assert post(server + "/api/lingo/forget", {"word": "Nobody"}, TOKEN)[0] == 400
    assert post(server + "/api/lingo/drop-tables", {}, TOKEN)[0] == 404


def test_idle_server_stops_itself(monkeypatch):
    stopped = threading.Event()

    class Fake:
        def shutdown(self):
            stopped.set()

    U.Handler.last_request = 0.0
    monkeypatch.setattr(U.time, "monotonic", lambda: 1000.0)
    t = threading.Thread(target=U._stop_when_idle, args=(Fake(), 60), daemon=True)
    t.start()
    assert stopped.wait(2)


def request(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_lingo_try_takes_a_whole_long_dictation(server, lingo_files):
    sentence = ("so my name is Sora Beth Ani and " * 10).strip()
    assert 300 <= len(sentence) < 4000
    status, out = post(server + "/api/lingo/try", {"text": sentence}, TOKEN)
    assert status == 200 and out["typed"]
    assert post(server + "/api/lingo/try", {"text": "x" * 4001}, TOKEN)[0] == 400
    assert post(server + "/api/lingo/learn", {"word": "x" * 121}, TOKEN)[0] == 400   # spellings stay short


def test_theme_api_and_the_themed_page(server, monkeypatch, fixtures, tmp_path):
    from chttr import theme

    monkeypatch.setattr(theme, "OMARCHY_COLORS", tmp_path / "none.toml")
    assert json.loads(get(server + "/api/theme", TOKEN)[1]) == {"mode": "auto", "vars": {}}
    assert b"data-theme" not in get(server + "/")[1]
    monkeypatch.setattr(theme, "OMARCHY_COLORS", fixtures / "colors.toml")
    t = json.loads(get(server + "/api/theme", TOKEN)[1])
    assert t["mode"] == "light" and t["vars"]["--accent"] == "#5b2bb5"
    page = get(server + "/")[1].decode()
    assert '<html lang="en" data-theme="light" style="--bg:#f3eefa;' in page and "--accent:#5b2bb5" in page
    assert get(server + "/api/theme")[0] == 403


def test_served_page_semantics(server):
    page = get(server + "/")[1].decode()
    detail = re.search(r'<section id="detail"[^>]*>', page).group(0)
    assert "aria-live" not in detail
    assert '<meta name="color-scheme" content="light dark">' in page
    assert re.search(r'<div id="status" role="status" aria-live="polite" class="sr-only">', page)
    assert page.index("static/lib.js") < page.index("static/app.js")
    assert '<dialog id="help"' in page and 'role="tablist"' in page
    assert get(server + "/static/lib.js")[0] == 200


@pytest.fixture
def no_daemon(monkeypatch, tmp_path):
    from chttr import native_host, polish

    monkeypatch.setattr(native_host, "BROWSERS", [(("chromium",), tmp_path / "chromium", tmp_path / "flags.conf")])
    monkeypatch.setattr(polish, "backend", lambda cfg: "none")
    monkeypatch.setattr(U, "_backend_cache", (0.0, ""))
    return tmp_path


def test_setup_reports_what_the_page_needs(server, no_daemon):
    status, body, _ = get(server + "/api/setup", TOKEN)
    s = json.loads(body)
    assert status == 200 and s["daemon"] is False and s["extension_installed"] is False
    assert s["cleanup_backend"] == "none" and s["notes_available"] is False
    assert s["hotkeys"] == {"dictate": "Insert", "hands_free": "Super+Insert", "meeting": "Super+Alt+Insert",
                            "history": "Super+Shift+Insert", "command": "Alt+Insert"}
    assert s["detect_calls"] is True and s["typer"] == "auto" and s["meetings_dir"].endswith("Meetings")
    hosts = no_daemon / "chromium" / "NativeMessagingHosts"
    hosts.mkdir(parents=True)
    (hosts / "com.chttr.meet.json").write_text("{}")
    assert json.loads(get(server + "/api/setup", TOKEN)[1])["extension_installed"] is True


def test_status_never_raises(server, monkeypatch):
    s = json.loads(get(server + "/api/status", TOKEN)[1])
    assert s == {"state": "idle", "meeting": None, "daemon": False}

    def hangs(cmd, timeout=5.0):
        raise TimeoutError("timed out")

    monkeypatch.setattr(U.client, "send", hangs)
    assert get(server + "/api/status", TOKEN)[0] == 200
    monkeypatch.setattr(U.client, "send", lambda cmd, timeout=5.0: {
        "ok": True, "state": "idle", "recording": True,
        "meeting": {"title": "Standup", "code": "", "seconds": 42, "dir": "/x"}})
    s = json.loads(get(server + "/api/status", TOKEN)[1])
    assert s["daemon"] is True and s["meeting"]["seconds"] == 42


def test_meeting_has_turns_and_notes_availability(server, no_daemon):
    m = json.loads(get(server + "/api/meeting/2026-10-01-140000", TOKEN)[1])
    assert [t["speaker"] for t in m["turns"]] == ["Dana", "Ali"] and m["turns"][0]["lines"][0]["start"] == 10
    assert m["notes_available"] is False and m["talk_seconds"]["Dana"] == 60


def _with_markdown(lib, mid="2026-10-01-140000"):  # noqa: F811
    f = lib / "meetings" / mid / "result.json"
    result = json.loads(f.read_text())
    md = lib / "notes" / "2026-10-01 14.00 Pricing review.md"
    md.parent.mkdir(exist_ok=True)
    md.write_text("# Pricing review\n\nBudget set at $50k.\n", encoding="utf-8")
    result["markdown"] = str(md)
    f.write_text(json.dumps(result))
    return md


def test_export_markdown_json_and_csv(server, lib):  # noqa: F811
    base = server + "/api/meeting/2026-10-01-140000/export"
    assert get(base + "?format=md", TOKEN)[0] == 404                   # no notes file yet
    _with_markdown(lib)
    status, body, headers = get(base + "?format=md", TOKEN)
    assert status == 200 and body.startswith(b"# Pricing review")
    assert headers["Content-Type"].startswith("text/markdown")
    assert headers["Content-Disposition"].startswith('attachment; filename="2026-10-01 14.00 Pricing review.md"')
    status, body, headers = get(base + "?format=md&inline=1", TOKEN)
    assert headers["Content-Disposition"].startswith("inline") and headers["Content-Type"].startswith("text/plain")

    status, body, headers = get(base + "?format=json", TOKEN)
    data = json.loads(body)
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert data["title"] == "Pricing review" and len(data["transcript"]) == 2 and "markdown" not in data
    assert "2026-10-01 Pricing review.json" in headers["Content-Disposition"]

    status, body, headers = get(base + "?format=csv", TOKEN)
    import csv
    import io

    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert status == 200 and headers["Content-Type"].startswith("text/csv")
    assert rows[0] == ["start", "end", "speaker", "text"] and len(rows) == 3
    assert rows[2] == ["40.00", "43.00", "Ali", "I will send the revised contract by Friday."]

    assert get(base + "?format=pdf", TOKEN)[0] == 400
    assert get(server + "/api/meeting/nope/export?format=json", TOKEN)[0] == 404
    assert get(base + "?format=csv")[0] == 403
    assert get(base + "?format=csv&token=secret")[0] == 200            # a plain link (download) carries the token


def test_search_facets(server):
    hits = json.loads(get(server + "/api/search?q=budget", TOKEN)[1])
    assert isinstance(hits, list)                                      # the plain list stays the default
    out = json.loads(get(server + "/api/search?q=budg&prefix_last=1&facets=1", TOKEN)[1])
    assert {f["meeting_id"] for f in out["facets"]} == {"2026-10-01-140000", "2026-10-08-140000"}
    assert all(set(f) == {"meeting_id", "title", "when", "count"} for f in out["facets"])
    assert any(h["kind"] == "line" for h in out["hits"])
    out = json.loads(get(server + "/api/search?q=budget&facets=1&meeting=2026-10-08-140000", TOKEN)[1])
    assert {h["meeting_id"] for h in out["hits"]} == {"2026-10-08-140000"}
    out = json.loads(get(server + "/api/search?q=speaker%3Arob%20budget&facets=1", TOKEN)[1])
    assert {h["speaker"] for h in out["hits"]} == {"Rob"}


def test_audio_ranges_stream_from_disk(server, lib):  # noqa: F811
    md = _with_markdown(lib)
    audio = bytes(range(256)) * 20                                     # 5120 bytes of "Opus"
    md.with_suffix(".opus").write_bytes(audio)
    url = server + "/api/meeting/2026-10-01-140000/audio"
    status, body, headers = request(url, headers={**TOKEN, "Range": "bytes=100-199"})
    assert status == 206 and body == audio[100:200]
    assert headers["Content-Range"] == "bytes 100-199/5120" and headers["Content-Length"] == "100"
    status, body, headers = request(url, headers={**TOKEN, "Range": "bytes=5000-"})
    assert status == 206 and body == audio[5000:] and headers["Content-Range"] == "bytes 5000-5119/5120"
    status, body, headers = request(url, headers={**TOKEN, "Range": "bytes=-20"})
    assert status == 206 and body == audio[-20:]
    status, body, headers = request(url, headers={**TOKEN, "Range": "bytes=5000-99999"})
    assert status == 206 and body == audio[5000:]
    status, _, headers = request(url, headers={**TOKEN, "Range": "bytes=6000-"})
    assert status == 416 and headers["Content-Range"] == "bytes */5120"
    status, body, headers = request(url, headers=TOKEN)
    assert status == 200 and body == audio and headers["Accept-Ranges"] == "bytes"
    status, body, headers = request(url, "HEAD", headers=TOKEN)
    assert status == 200 and body == b"" and headers["Content-Length"] == "5120"
    assert request(url + "?token=secret", headers={"Range": "bytes=0-9"})[1] == audio[:10]   # <audio src> carries it
    assert json.loads(get(server + "/api/meeting/2026-10-01-140000", TOKEN)[1])["has_audio"] is True


def test_daemon_posts_need_the_header_and_reach_the_fakes(server, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    ran, sent = [], []
    monkeypatch.setattr(U, "_run", lambda argv, **kw: ran.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(U.client, "send", lambda cmd, timeout=5.0: sent.append(cmd) or {"ok": True, "recording": True})
    assert request(server + "/api/daemon/start?token=secret", "POST", body={})[0] == 403
    assert request(server + "/api/daemon/meeting-start?token=secret", "POST", body={})[0] == 403
    assert ran == [] and sent == []

    assert post(server + "/api/daemon/start", {}, TOKEN) == (200, {"ok": True, "message": "chttr is starting"})
    assert ran == [["systemctl", "--user", "start", "chttr.service"]]
    assert post(server + "/api/daemon/meeting-start", {}, TOKEN)[0] == 200 and sent == ["meeting start"]

    monkeypatch.setattr(U, "_run", lambda argv, **kw: SimpleNamespace(returncode=5, stdout="", stderr="Unit chttr.service not found."))
    status, out = post(server + "/api/daemon/start", {}, TOKEN)
    assert status == 500 and "Unit chttr.service not found." in out["error"]

    def missing(argv, **kw):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(U, "_run", missing)
    assert post(server + "/api/daemon/start", {}, TOKEN)[0] == 500
    monkeypatch.setattr(U, "_run", lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 20)))
    assert post(server + "/api/daemon/start", {}, TOKEN)[0] == 500

    def down(cmd, timeout=5.0):
        raise U.client.DaemonNotRunning("not running")

    monkeypatch.setattr(U.client, "send", down)
    status, out = post(server + "/api/daemon/meeting-start", {}, TOKEN)
    assert status == 503 and out["error"] == "chttr isn't running"


def test_dot_ids_are_not_meetings(server):
    assert get(server + "/api/meeting/..", TOKEN)[0] == 404
    assert get(server + "/api/meeting/%2E%2E/export?format=json", TOKEN)[0] == 404


def test_route_for_citation():
    meetings = [{"id": "2026-10-01-140000", "title": "Pricing review", "when": "2026-10-01T14:00+02:00"},
                {"id": "2026-10-01-160000", "title": "Pricing review: follow-up", "when": "2026-10-01T16:00+02:00"},
                {"id": "2026-10-08-140000", "title": "Launch sync", "when": "2026-10-08T14:00+02:00"}]
    assert U.route_for_citation("Pricing review, 2026-10-01, 00:02:32", meetings) == "meeting/2026-10-01-140000?t=152"
    assert U.route_for_citation("[launch sync, 2026-10-08, 12:40]", meetings) == "meeting/2026-10-08-140000?t=760"
    assert U.route_for_citation("Launch, 2026-10-08", meetings) == "meeting/2026-10-08-140000"
    assert U.route_for_citation("Pricing review: follow-up, 2026-10-01, 1:00:00", meetings) == \
        "meeting/2026-10-01-160000?t=3600"
    assert U.route_for_citation("Board, with commas, 2026-10-08, 00:00:05", meetings) == "meeting/2026-10-08-140000?t=5"
    assert U.route_for_citation("Pricing review, 2026-10-02, 00:00:01", meetings) is None
    assert U.route_for_citation("just some words", meetings) is None
