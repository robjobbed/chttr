"""Microphone capture through PipeWire (pw-record), streamed so levels can be metered."""

from __future__ import annotations

import collections
import math
import signal
import subprocess
import threading
import time
from collections.abc import Callable

import numpy as np

SAMPLE_RATE = 16000
CHUNK_FRAMES = 320          # 20 ms
CHUNK_BYTES = CHUNK_FRAMES * 2
START_CHECK = 0.05          # pw-record that dies this fast (bad device, no PipeWire) fails start()

LevelCallback = Callable[[float, float], None]   # (rms 0..1, peak_db)


class Recorder:
    def __init__(self, device: str = "", on_level: LevelCallback | None = None) -> None:
        self.device = device
        self.on_level = on_level
        self._proc: subprocess.Popen | None = None
        self._chunks: list[bytes] = []
        self.levels_db: list[float] = []     # RMS level of every 20 ms chunk, for pause detection
        self._reader: threading.Thread | None = None
        self._started = 0.0
        self._ended = 0.0
        self.peak_db = -90.0
        # Why pw-record stopped on its own (a headset unplugged, PipeWire restarted):
        # its last stderr lines, or its exit status.
        self.error = ""
        self.exited_early = False
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._stderr_reader: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def elapsed(self) -> float:
        """Seconds recorded: up to now, or up to the moment pw-record quit by itself."""
        if not self._proc:
            return 0.0
        return (self._ended or time.monotonic()) - self._started

    def start(self) -> None:
        if self.active:
            raise RuntimeError("already recording")
        cmd = [
            "pw-record", "--raw",
            "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16",
            "--media-role", "Communication", "--latency", "20ms",
        ]
        if self.device:
            cmd += ["--target", self.device]
        cmd.append("-")
        self._chunks = []
        self.levels_db = []
        self.peak_db = -90.0
        self.error = ""
        self.exited_early = False
        self._ended = 0.0
        self._stderr = collections.deque(maxlen=20)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise RuntimeError("pw-record not found: install PipeWire's command-line tools") from exc
        self._proc = proc
        self._started = time.monotonic()
        # Read both pipes from the first moment: audio is metered at once, and a
        # pw-record that complains on stderr can never block on a full pipe.
        self._stderr_reader = threading.Thread(target=self._drain_stderr, args=(proc, self._stderr), daemon=True)
        self._stderr_reader.start()
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
        self._reader.start()
        deadline = self._started + START_CHECK
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        if proc.poll() is not None:
            self._reader.join(timeout=1)
            err = self.error or "\n".join(self._stderr)
            self._proc = None
            raise RuntimeError(f"pw-record failed: {err or 'exited immediately'}")

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen, lines: collections.deque[str]) -> None:
        if proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    lines.append(line)
        except (OSError, ValueError):
            pass

    def _read_loop(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                break
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
            # level before the chunk itself, so a reader never sees audio without its level
            self.levels_db.append(20 * math.log10(max(rms, 1e-6)))
            self._chunks.append(data)
            peak = float(np.abs(samples).max()) if len(samples) else 0.0
            peak_db = 20 * math.log10(max(peak, 1e-6))
            self.peak_db = max(self.peak_db, peak_db)
            if self.on_level:
                try:
                    self.on_level(rms, peak_db)
                except Exception:  # noqa: BLE001 - metering must never break recording
                    pass
        if proc is self._proc:
            self._note_exit(proc)       # pw-record quit by itself while we were still listening

    def _note_exit(self, proc: subprocess.Popen) -> None:
        if self.exited_early:
            return
        self._ended = self._ended or time.monotonic()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if self._stderr_reader:
            self._stderr_reader.join(timeout=0.5)
        code = proc.returncode
        said = "\n".join(self._stderr)
        if code not in (0, None, -2, 130):
            self.error = said or f"pw-record exited with {code}"
        else:
            self.error = said or "pw-record stopped by itself"
        self.exited_early = True

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def audio_range(self, start: int, end: int) -> np.ndarray:
        """Chunks [start, end) of the recording so far, as float32."""
        return np.frombuffer(b"".join(self._chunks[start:end]), dtype=np.int16).astype(np.float32) / 32768.0

    def snapshot(self, max_seconds: float = 0) -> np.ndarray:
        """The audio recorded so far (the last max_seconds of it, if given), without stopping."""
        chunks = list(self._chunks)
        if max_seconds:
            chunks = chunks[-int(max_seconds * SAMPLE_RATE / CHUNK_FRAMES):]
        return np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0

    def stop(self) -> tuple[np.ndarray, float]:
        """Stop recording; returns (float32 mono audio at 16 kHz, seconds recorded).
        Never hangs or raises on a pw-record that won't quit: SIGINT, then SIGTERM, then SIGKILL."""
        if self._proc is None:
            raise RuntimeError("not recording")
        proc, secs = self._proc, self.elapsed
        self._proc = None
        quit_by_itself = proc.poll() is not None
        if not quit_by_itself:
            for ask, wait in ((lambda: proc.send_signal(signal.SIGINT), 3), (proc.terminate, 2), (proc.kill, 1)):
                try:
                    ask()
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except OSError:             # already gone
                    break
        if self._reader:
            self._reader.join(timeout=2)
        raw = b"".join(self._chunks)
        self._chunks = []
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if quit_by_itself:
            self._note_exit(proc)
            secs = len(audio) / SAMPLE_RATE         # what it got before it went away
        return audio, secs

    def cancel(self) -> None:
        if self._proc is None:
            return
        self.stop()
