from __future__ import annotations

import hashlib
import io

import pytest

from chttr import models as M


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def spec_for(data: bytes) -> M.ModelFile:
    return M.ModelFile("org/repo", "0" * 40, "model.onnx", hashlib.sha256(data).hexdigest(), len(data))


def test_every_pinned_model_uses_a_commit_and_a_full_hash():
    for spec in [*M.PARAKEET, M.SILERO_VAD, M.WESPEAKER]:
        assert len(spec.revision) == 40 and "/resolve/main/" not in spec.url
        assert len(spec.sha256) == 64 and spec.size > 0


def test_download_verifies_and_moves_into_place(tmp_path, monkeypatch):
    data = b"onnx" * 1000
    monkeypatch.setattr(M.urllib.request, "urlopen", lambda req, timeout: FakeResponse(data))
    dest = tmp_path / "m" / "model.onnx"
    progress = []
    M.download(spec_for(data), dest, lambda done, total: progress.append(done))
    assert dest.read_bytes() == data
    assert progress[-1] == len(data)
    assert M.verify(spec_for(data), dest)


def test_tampered_download_is_rejected_and_removed(tmp_path, monkeypatch):
    good = b"good model"
    monkeypatch.setattr(M.urllib.request, "urlopen", lambda req, timeout: FakeResponse(b"evil model"))
    dest = tmp_path / "model.onnx"
    with pytest.raises(M.ChecksumError):
        M.download(spec_for(good), dest)
    assert not dest.exists()
    assert not (tmp_path / "model.onnx.part").exists()


def test_existing_file_of_the_right_size_is_kept(tmp_path, monkeypatch):
    data = b"x" * 10
    dest = tmp_path / "model.onnx"
    dest.write_bytes(data)

    def boom(*a, **k):
        raise AssertionError("should not download")

    monkeypatch.setattr(M.urllib.request, "urlopen", boom)
    assert M.download(spec_for(data), dest) == dest


# ---- resume, retry, disk space, progress ------------------------------------------------

class RangeServer:
    """urlopen() stand-in that honours Range and can drop the connection mid-file."""

    def __init__(self, data: bytes, chunk: int = 4, failures: int = 0, honour_range: bool = True):
        self.data, self.chunk, self.failures, self.honour_range = data, chunk, failures, honour_range
        self.ranges: list[str | None] = []

    def __call__(self, req, timeout):
        rng = req.get_header("Range")
        self.ranges.append(rng)
        start = int(rng.removeprefix("bytes=").rstrip("-")) if rng and self.honour_range else 0
        drop = self.failures > 0
        self.failures -= drop
        return Reader(self.data[start:], self.chunk, 206 if start else 200, drop)


class Reader:
    def __init__(self, body: bytes, chunk: int, status: int, drop: bool):
        self.body, self.chunk, self.status, self.drop, self.reads = body, chunk, status, drop, 0

    def read(self, n):
        self.reads += 1
        if self.drop and self.reads == 2:
            raise ConnectionResetError(104, "Connection reset by peer")
        out, self.body = self.body[:self.chunk], self.body[self.chunk:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def slept(monkeypatch):
    calls = []
    monkeypatch.setattr(M, "_sleep", calls.append)
    return calls


def test_download_resumes_an_existing_part_file_with_range(tmp_path, monkeypatch, slept):
    data = bytes(range(256)) * 4
    dest = tmp_path / "model.onnx"
    (tmp_path / "model.onnx.part").write_bytes(data[:300])
    server = RangeServer(data, chunk=100)
    monkeypatch.setattr(M.urllib.request, "urlopen", server)
    seen = []
    M.download(spec_for(data), dest, lambda done, total, bps: seen.append(done))
    assert dest.read_bytes() == data
    assert server.ranges == ["bytes=300-"]
    assert seen[0] == 300 and seen[-1] == len(data)        # the bar starts where the last run stopped
    assert not (tmp_path / "model.onnx.part").exists()
    assert slept == []


def test_download_starts_over_when_the_server_ignores_range(tmp_path, monkeypatch, slept):
    data = b"abcdefghij" * 50
    dest = tmp_path / "model.onnx"
    (tmp_path / "model.onnx.part").write_bytes(b"stale bytes")
    monkeypatch.setattr(M.urllib.request, "urlopen", RangeServer(data, chunk=64, honour_range=False))
    M.download(spec_for(data), dest)
    assert dest.read_bytes() == data


def test_dropped_connections_are_retried_from_where_they_stopped(tmp_path, monkeypatch, slept):
    data = b"0123456789" * 3
    server = RangeServer(data, chunk=4, failures=2)
    monkeypatch.setattr(M.urllib.request, "urlopen", server)
    dest = tmp_path / "model.onnx"
    M.download(spec_for(data), dest, lambda done, total: None)      # old two-argument callback
    assert dest.read_bytes() == data
    assert server.ranges == [None, "bytes=4-", "bytes=8-"]
    assert slept == [1, 1]          # each try got a chunk through, so the backoff starts over


def test_download_gives_up_after_the_retries_with_a_plain_error(tmp_path, monkeypatch, slept):
    import urllib.error

    def offline(req, timeout):
        raise urllib.error.URLError(OSError(-3, "Temporary failure in name resolution"))

    monkeypatch.setattr(M.urllib.request, "urlopen", offline)
    with pytest.raises(OSError, match=r"failed after 6 tries.*name resolution.*continue where it stopped"):
        M.download(spec_for(b"x" * 10), tmp_path / "model.onnx")
    assert slept == [1, 2, 4, 8, 16]


def test_http_404_is_not_retried(tmp_path, monkeypatch, slept):
    import urllib.error

    def missing(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(M.urllib.request, "urlopen", missing)
    with pytest.raises(OSError):
        M.download(spec_for(b"x" * 10), tmp_path / "model.onnx")
    assert slept == []


def test_checksum_errors_are_not_retried(tmp_path, monkeypatch, slept):
    monkeypatch.setattr(M.urllib.request, "urlopen", RangeServer(b"evil model"))
    with pytest.raises(M.ChecksumError):
        M.download(spec_for(b"good model"), tmp_path / "model.onnx")
    assert slept == []


def test_download_refuses_when_the_disk_is_short(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(M.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100 << 20, 99 << 20, 1 << 20))
    monkeypatch.setattr(M.urllib.request, "urlopen", lambda req, timeout: pytest.fail("should not download"))
    spec = M.ModelFile("org/repo", "0" * 40, "big.onnx", "0" * 64, 600 << 20)
    with pytest.raises(OSError, match=rf"needs 660 MB free in {tmp_path}, 1 MB available"):
        M.download(spec, tmp_path / "big.onnx")


def test_progress_callbacks_with_two_or_three_arguments():
    assert M._takes_rate(lambda done, total, bps: None)
    assert M._takes_rate(lambda *a: None)
    assert not M._takes_rate(lambda done, total: None)


def test_progress_line_shows_bar_amounts_speed_and_eta():
    line = M.progress_line(398 << 20, 652 << 20, 24 << 20)
    assert line == "[██████████████░░░░░░░░░░]  61%  398/652 MB  24 MB/s  eta 0:11"
    assert M.progress_line(1 << 20, 18 << 20, 0) == "[█░░░░░░░░░░░░░░░░░░░░░░░]   5%  1/18 MB"
    assert M.progress_line(93939, 93939, 1.5 * 2**20).endswith("100%  92/92 KB  1.5 MB/s")
    assert "eta 1:05:00" in M.progress_line(0, 3900 << 20, 1 << 20)


def test_console_progress_redraws_in_place_and_ends_the_line():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    out = Tty()
    show = M.console_progress("vocab.txt", out)
    show(0, 100, 0.0)
    show(50, 100, 10.0)              # within 0.5 s of the last redraw: skipped
    show(100, 100, 10.0)
    show(100, 100, 10.0)             # already finished
    text = out.getvalue()
    assert text.count("\r") == 2 and text.endswith("\n") and text.count("\n") == 1
    assert "vocab.txt  [" in text and "100%" in text


def test_console_progress_off_a_terminal_prints_only_the_finished_line():
    out = io.StringIO()
    show = M.console_progress("vocab.txt", out)
    for done in (0, 50, 100):
        show(done, 100)
    assert out.getvalue() == "    vocab.txt  [" + "█" * 24 + "] 100%  1/1 KB\n"


def test_meeting_model_paths_match_diarize():
    from chttr import diarize

    assert diarize.MODELS == M.MEETING_MODELS


def test_models_main_downloads_parakeet_and_the_meeting_models(tmp_path, monkeypatch, capsys):
    blobs = {name: f"fake {name}".encode() * 50 for name in ("encoder.onnx", "vocab.txt", "vad.onnx", "spk.onnx")}

    def fake(name):
        return M.ModelFile("org/repo", "0" * 40, name, hashlib.sha256(blobs[name]).hexdigest(), len(blobs[name]))

    monkeypatch.setattr(M, "PARAKEET", [fake("encoder.onnx"), fake("vocab.txt")])
    monkeypatch.setattr(M, "MEETING_MODELS", {"silero-vad/vad.onnx": fake("vad.onnx"),
                                              "wespeaker-resnet34/spk.onnx": fake("spk.onnx")})
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url.rsplit("/", 1)[1])
        return FakeResponse(blobs[calls[-1]])

    monkeypatch.setattr(M.urllib.request, "urlopen", urlopen)
    assert M.main([str(tmp_path / "parakeet"), str(tmp_path / "models")]) == 0
    assert (tmp_path / "parakeet/encoder.onnx").read_bytes() == blobs["encoder.onnx"]
    assert (tmp_path / "models/silero-vad/vad.onnx").exists()
    assert (tmp_path / "models/wespeaker-resnet34/spk.onnx").exists()
    assert calls == ["encoder.onnx", "vocab.txt", "vad.onnx", "spk.onnx"]
    assert "downloading Parakeet" in capsys.readouterr().out
    assert M.main([str(tmp_path / "parakeet"), str(tmp_path / "models")]) == 0     # all there: nothing fetched
    assert len(calls) == 4


def _run_models_py(tmp_path, monkeypatch, urlopen) -> int:
    import runpy

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("sys.argv", ["models.py", str(tmp_path / "parakeet"), str(tmp_path / "models")])
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(M.__file__, run_name="__main__")
    return stop.value.code


def test_running_models_py_skips_files_that_are_there(tmp_path, monkeypatch, capsys):
    for spec, dest in [*((s, tmp_path / "parakeet" / s.name) for s in M.PARAKEET),
                       *((s, tmp_path / "models" / rel) for rel, s in M.MEETING_MODELS.items())]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            fh.truncate(spec.size)          # sparse: the right size without the disk space
    assert _run_models_py(tmp_path, monkeypatch, lambda req, timeout: pytest.fail("nothing to fetch")) == 0
    assert capsys.readouterr().out == ""


def test_running_models_py_reports_a_failure_in_one_line(tmp_path, monkeypatch, capsys):
    import urllib.error

    def forbidden(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    assert _run_models_py(tmp_path, monkeypatch, forbidden) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("chttr: model download failed:") and "\n" not in err


def test_a_retry_is_reported_on_its_own_line_under_the_bar(tmp_path, monkeypatch, slept):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    data = b"0123456789" * 3
    monkeypatch.setattr(M.urllib.request, "urlopen", RangeServer(data, chunk=4, failures=1))
    out = Tty()
    M.download(spec_for(data), tmp_path / "model.onnx", M.console_progress("model.onnx", out))
    assert "\n    model.onnx: Connection reset by peer; retrying in 1 s\n" in out.getvalue()
    assert out.getvalue().endswith("100%  1/1 KB\x1b[K\n")
