from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import wave
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from chttr import config as C
from chttr import ui as U
from chttr import ui_api as A
from tests.test_library import lib  # noqa: F401 - the fixture


def tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 160)
    return buf.getvalue()


@pytest.fixture
def dictated(lib, monkeypatch):  # noqa: F811
    now = datetime.now(UTC)
    audio_dir = lib / "history-audio"
    audio_dir.mkdir()
    (audio_dir / "20261018-120000-000000.wav").write_bytes(tiny_wav())
    (lib / "secret.wav").write_bytes(b"not yours")
    rows = [
        {"ts": (now - timedelta(days=1)).isoformat(timespec="seconds"), "seconds": 4.0, "app": "Slack", "kind": "dictate",
         "mode": "chat", "raw": "um ping the vendor about the contract", "text": "Ping the vendor about the contract.",
         "output": "type", "audio": "20261018-120000-000000.wav"},
        {"ts": now.isoformat(timespec="seconds"), "seconds": 2.5, "app": "kitty", "kind": "dictate", "mode": "",
         "raw": "git status", "text": "git status", "output": "type"},
        {"ts": now.isoformat(timespec="seconds"), "seconds": 1.0, "app": "foot", "kind": "command",
         "raw": "make it shorter", "text": "Short.", "output": "paste", "audio": "../secret.wav"},
        {"ts": now.isoformat(timespec="seconds"), "seconds": 3.0, "app": "Slack", "kind": "dictate",
         "raw": "send the deck to Dana", "text": "Send the deck to Dana.", "output": "clipboard",
         "audio": "gone.wav"},
    ]
    (lib / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(C, "HISTORY_AUDIO_DIR", audio_dir)
    return lib


def get(path: str, **q):
    r = A.route(path, {k: str(v) for k, v in q.items()}, C.Config())
    assert r is not None, path
    status, body, ctype = r
    return status, (json.loads(body) if ctype.startswith("application/json") else body), ctype


def test_unknown_paths_are_not_ours():
    for path in ("/api/meetings", "/api/dictation", "/api/dictations/x", "/api/dictation/1/audio/x", "/"):
        assert A.route(path, {}, None) is None


def test_dashboard_works_without_a_daemon(dictated):
    status, data, _ = get("/api/dashboard")
    assert status == 200
    h = data["history"]
    assert h["dictations"] == 4 and h["streak_days"] == 2 and len(h["per_day"]) == 30
    assert h["top_apps"][0] == {"app": "Slack", "words": 11}
    assert data["typing_wpm"] == 40
    m = data["meetings"]
    assert (m["meetings_total"], m["minutes_total"], m["open_actions"]) == (2, 60, 1)
    assert m["speakers_named"] == 3                                       # Dana, Ali, Rob
    assert [r["title"] for r in m["recent"]] == ["Launch sync", "Pricing review"]


def test_dashboard_counts_unnamed_speakers_out(dictated, monkeypatch):
    monkeypatch.setattr(A.L, "meetings", lambda limit: [
        {"id": "a", "title": "t", "when": datetime.now().astimezone().isoformat(), "minutes": 5,
         "speakers": ["Speaker 1", "In the room 2", "Dana"]},
        {"id": "b", "title": "t", "when": "2020-01-01T10:00+00:00", "minutes": 3, "speakers": []}])
    m = A.dashboard()["meetings"]
    assert (m["speakers_named"], m["meetings_last_7d"], m["meetings_total"]) == (1, 1, 2)


def test_dashboard_survives_a_broken_library(dictated, monkeypatch):
    def boom(limit):
        raise OSError("disk full")

    monkeypatch.setattr(A.L, "meetings", boom)
    data = A.dashboard()
    assert data["history"]["dictations"] == 4 and data["meetings"]["meetings_total"] == 0


def test_dictations_newest_first_and_filtered(dictated):
    status, rows, _ = get("/api/dictations")
    assert status == 200
    assert [r["id"] for r in rows] == ["3", "2", "1", "0"]
    first = rows[-1]
    assert first == {"id": "0", "ts": first["ts"], "app": "Slack", "mode": "chat", "kind": "dictate", "seconds": 4.0,
                     "output": "type", "raw": "um ping the vendor about the contract",
                     "text": "Ping the vendor about the contract.", "has_audio": True}
    assert [r["has_audio"] for r in rows] == [False, False, False, True]     # gone.wav, ../secret.wav, none
    assert [r["id"] for r in get("/api/dictations", q="VENDOR")[1]] == ["0"]
    assert [r["id"] for r in get("/api/dictations", q="um ping")[1]] == ["0"]    # what chttr heard counts too
    assert [r["id"] for r in get("/api/dictations", q="the", app="slack")[1]] == ["3", "0"]
    assert get("/api/dictations", q="nothing like this")[1] == []
    assert [r["id"] for r in get("/api/dictations", limit=2)[1]] == ["3", "2"]
    assert get("/api/dictations", limit="lots")[0] == 400


def test_one_dictation_has_the_diff(dictated):
    status, d, _ = get("/api/dictation/0")
    assert status == 200 and d["text"] == "Ping the vendor about the contract." and d["words"] == 6
    assert d["diff"] == [["del", "um"], ["equal", "Ping the vendor about the contract."]]
    assert d["keep_audio_days"] == 0
    assert get("/api/dictation/99")[0] == 404


def test_audio_is_served_when_kept(dictated):
    status, body, ctype = get("/api/dictation/0/audio")
    assert status == 200 and ctype == "audio/wav" and body[:4] == b"RIFF"
    assert get("/api/dictation/1/audio")[0] == 404             # never kept
    assert get("/api/dictation/3/audio")[0] == 404             # pruned since
    assert get("/api/dictation/99/audio")[0] == 404


def test_audio_never_leaves_the_audio_folder(dictated, monkeypatch):
    assert get("/api/dictation/2/audio")[0] == 404             # history says "../secret.wav"
    for bad in ("..", "..%2F..%2Fsecret.wav", "-1", "0x1", "1.wav", "%2e%2e"):
        status, data, _ = get(f"/api/dictation/{bad}/audio")
        assert status == 404 and data == {"error": "no such dictation"}, bad
    # a symlink planted in the folder can't point outside it either
    (C.HISTORY_AUDIO_DIR / "link.wav").symlink_to(dictated / "secret.wav")
    lines = (dictated / "history.jsonl").read_text().splitlines()
    lines[1] = json.dumps({**json.loads(lines[1]), "audio": "link.wav"})
    (dictated / "history.jsonl").write_text("\n".join(lines) + "\n")
    assert get("/api/dictation/1/audio")[0] == 404


@pytest.fixture
def server(dictated):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
    port = srv.server_address[1]
    U.Handler.token, U.Handler.host, U.Handler.cfg = "secret", f"127.0.0.1:{port}", C.Config()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()
    U.Handler.cfg = None


def fetch(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_the_ui_server_dispatches_here_behind_the_token(server):
    assert fetch(server + "/api/dashboard")[0] == 403
    status, body, _ = fetch(server + "/api/dashboard", {"X-Chttr-Token": "secret"})
    assert status == 200 and json.loads(body)["history"]["dictations"] == 4
    status, body, headers = fetch(server + "/api/dictation/0/audio?token=secret")   # what <audio src> sends
    assert status == 200 and headers["Content-Type"] == "audio/wav" and body[:4] == b"RIFF"
    assert fetch(server + "/api/dictation/../../secret.wav?token=secret")[0] == 404
    assert fetch(server + "/api/meetings", {"X-Chttr-Token": "secret"})[0] == 200    # the old routes still answer


def test_index_has_the_home_and_dictations_tabs(server):
    status, body, _ = fetch(server + "/")
    html = body.decode()
    assert status == 200
    assert html.index('data-view="home"') < html.index('data-view="meetings"') < html.index('data-view="dictations"')
    assert html.index("static/app.js") < html.index("static/home.js") < html.index("static/dictations.js")
    assert "static/home.css" in html
    for asset in ("home.js", "dictations.js", "home.css"):
        assert fetch(server + "/static/" + asset)[0] == 200
