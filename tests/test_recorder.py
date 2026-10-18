"""Recorder against a fake pw-record: a microphone that goes away, a process that
won't quit. No PipeWire needed."""

from __future__ import annotations

import io
import subprocess
import threading
import time
from typing import ClassVar

import pytest

from chttr import recorder as R


class FakePwRecord:
    """Writes `audio` bytes to stdout, then either keeps running until signalled or
    exits with `code` and `stderr` (the mic went away)."""

    audio = b"\x00\x10" * R.CHUNK_FRAMES * 10       # 10 chunks: 0.2 s
    stderr_text = b""
    code: int | None = None                          # None: run until signalled
    exit_after = 0.0                                 # seconds before exiting on its own
    stubborn = 0                                     # wait() calls that time out
    instances: ClassVar[list[FakePwRecord]] = []

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode: int | None = None
        self.signals: list[str] = []
        self.stubborn_left = type(self).stubborn
        self._done = threading.Event()
        self._out_r, self._out_w = _pipe()
        self.stdout = self._out_r
        self.stderr = io.BytesIO(self.stderr_text)
        type(self).instances.append(self)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self._out_w.write(self.audio)
        self._out_w.flush()
        if self.code is not None:
            time.sleep(self.exit_after)
            self._exit(self.code)

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self._out_w.close()
            self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.stubborn_left > 0:
            self.stubborn_left -= 1
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append("int")
        if not self.stubborn_left:
            self._exit(0)

    def terminate(self):
        self.signals.append("term")
        if not self.stubborn_left:
            self._exit(-15)

    def kill(self):
        self.signals.append("kill")
        self.stubborn_left = 0
        self._exit(-9)


def _pipe():
    import os

    r, w = os.pipe()
    return os.fdopen(r, "rb"), os.fdopen(w, "wb")


@pytest.fixture
def fake_pw(monkeypatch):
    class Fake(FakePwRecord):
        instances: ClassVar[list[FakePwRecord]] = []

    monkeypatch.setattr(R.subprocess, "Popen", Fake)
    return Fake


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_a_normal_recording_stops_cleanly(fake_pw):
    rec = R.Recorder()
    rec.start()
    assert rec.active
    assert wait_for(lambda: rec.chunk_count == 10)
    audio, secs = rec.stop()
    assert len(audio) == 10 * R.CHUNK_FRAMES and secs > 0
    assert rec.error == "" and not rec.exited_early
    assert fake_pw.instances[0].signals == ["int"]


def test_a_microphone_that_goes_away_is_reported_not_raised(fake_pw):
    fake_pw.code, fake_pw.exit_after, fake_pw.stderr_text = 1, 0.15, b"stream error: connection lost\n"
    rec = R.Recorder()
    rec.start()
    assert wait_for(lambda: not rec.active)
    audio, secs = rec.stop()
    assert len(audio) == 10 * R.CHUNK_FRAMES                 # what it heard before it went away
    assert secs == pytest.approx(0.2)
    assert rec.exited_early and "connection lost" in rec.error
    assert fake_pw.instances[0].signals == []               # nothing left to stop


def test_an_exit_without_a_word_still_explains_itself(fake_pw):
    fake_pw.code, fake_pw.exit_after = 3, 0.15
    rec = R.Recorder()
    rec.start()
    assert wait_for(lambda: rec.exited_early)
    assert rec.error == "pw-record exited with 3"
    rec.stop()


def test_stop_escalates_on_a_pw_record_that_will_not_quit(fake_pw):
    fake_pw.stubborn = 2                                     # deaf to SIGINT and SIGTERM
    rec = R.Recorder()
    rec.start()
    rec.stop()                                               # returns, never raises
    assert fake_pw.instances[0].signals == ["int", "term", "kill"]
    assert not rec.exited_early and rec.error == ""
    assert not rec.active


def test_start_fails_fast_with_pw_records_reason(fake_pw):
    fake_pw.code, fake_pw.audio, fake_pw.stderr_text = 1, b"", b"error: no such target 'usb-mic'\n"
    rec = R.Recorder(device="usb-mic")
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="no such target"):
        rec.start()
    assert time.monotonic() - t0 < 1
    assert not rec.active
    assert "--target" in fake_pw.instances[0].cmd


def test_start_without_pw_record_says_what_is_missing(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("pw-record")

    monkeypatch.setattr(R.subprocess, "Popen", missing)
    with pytest.raises(RuntimeError, match="pw-record not found"):
        R.Recorder().start()


def test_a_new_recording_forgets_the_last_error(fake_pw):
    fake_pw.code, fake_pw.exit_after, fake_pw.stderr_text = 1, 0.1, b"connection lost\n"
    rec = R.Recorder()
    rec.start()
    assert wait_for(lambda: rec.exited_early)
    rec.stop()
    fake_pw.code, fake_pw.stderr_text = None, b""
    rec.start()
    assert rec.error == "" and not rec.exited_early
    rec.stop()
    assert rec.error == ""
