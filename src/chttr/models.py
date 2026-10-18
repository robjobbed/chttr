"""Model files chttr downloads, pinned to a repository commit and a SHA-256.

Every download is fetched from a fixed Hugging Face revision (never `main`) and
checked against its published hash before it is moved into place, so a changed
or tampered file is rejected instead of being loaded into the process.

Standard library only: bin/setup runs this file directly with the system Python
(`python3 -I models.py <parakeet dir> <models dir>`), so the 650 MB download
overlaps with `uv tool install`.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import inspect
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("chttr.models")

HF = "https://huggingface.co"


@dataclass(frozen=True)
class ModelFile:
    repo: str
    revision: str
    name: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return f"{HF}/{self.repo}/resolve/{self.revision}/{self.name}"


_PARAKEET = ("istupakov/parakeet-tdt-0.6b-v3-onnx", "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce")
PARAKEET = [
    ModelFile(*_PARAKEET, "encoder-model.int8.onnx",
              "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09", 652183999),
    ModelFile(*_PARAKEET, "decoder_joint-model.int8.onnx",
              "eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70", 18202004),
    ModelFile(*_PARAKEET, "vocab.txt",
              "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d", 93939),
    ModelFile(*_PARAKEET, "config.json",
              "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466", 97),
]
SILERO_VAD = ModelFile("istupakov/silero-vad-onnx", "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6", "silero_vad.onnx",
                       "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3", 2327524)
WESPEAKER = ModelFile("Wespeaker/wespeaker-voxceleb-resnet34-LM", "f0c48c298fd835726c27956a5d617bad7115627e",
                      "voxceleb_resnet34_LM.onnx",
                      "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068", 26530309)
# Where the meeting models live under the models directory (diarize.MODELS uses the same paths)
MEETING_MODELS = {
    "silero-vad/silero_vad.onnx": SILERO_VAD,
    "wespeaker-resnet34/voxceleb_resnet34_LM.onnx": WESPEAKER,
}

CHUNK = 1 << 20
TIMEOUT = 30                     # seconds without data before a read gives up
RETRY_DELAYS = (1, 2, 4, 8, 16)  # seconds to wait before each retry
TRANSIENT = (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead, OSError)
_LOCAL_ERRORS = {errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EPERM, errno.EISDIR, errno.ENOTDIR}
_sleep = time.sleep              # tests replace it


class ChecksumError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _takes_rate(fn: Callable) -> bool:
    """Progress callbacks are progress(done, total, bytes_per_sec); older ones take two arguments."""
    try:
        inspect.signature(fn).bind(0, 0, 0.0)
    except TypeError:
        return False
    except ValueError:              # no signature (some builtins): assume the new form
        return True
    return True


class _Rate:
    """Bytes per second over the last few seconds, so the speed follows the connection."""

    def __init__(self, window: float = 5.0) -> None:
        self.window = window
        self.samples: list[tuple[float, int]] = []

    def __call__(self, done: int) -> float:
        now = time.monotonic()
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[1][0] > self.window:
            self.samples.pop(0)
        t0, d0 = self.samples[0]
        return (done - d0) / (now - t0) if now - t0 > 0.05 else 0.0


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, ChecksumError):
        return False
    if isinstance(exc, urllib.error.HTTPError):     # 404/403 won't change by asking again
        return exc.code >= 500 or exc.code in (408, 416, 429)
    if isinstance(exc, OSError) and exc.errno in _LOCAL_ERRORS:   # the disk, not the network
        return False
    return isinstance(exc, TRANSIENT)


def _check_space(spec: ModelFile, dest: Path, have: int) -> None:
    free = shutil.disk_usage(dest.parent).free
    need = (spec.size - have) * 1.1
    if free < need:
        raise OSError(f"needs {need / 2**20:.0f} MB free in {dest.parent}, {free / 2**20:.0f} MB available")


def download(spec: ModelFile, dest: Path, progress: Callable[..., None] | None = None) -> Path:
    """Fetch `spec` to `dest`, verifying size and SHA-256 on the way. A file already at
    `dest` is trusted if its size matches (hashing 650 MB on every setup run is slow);
    `chttr doctor --verify` hashes everything.

    The bytes go to `dest.part` first. A `.part` left by an interrupted run is picked
    up where it stopped (HTTP Range), and a dropped connection is retried with backoff
    (1, 2, 4, 8, 16 s; the backoff starts over whenever data arrived). Raises OSError
    when the network or the disk gives up, ChecksumError when the bytes are wrong."""
    if dest.exists() and dest.stat().st_size == spec.size:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    if tmp.exists():
        if tmp.stat().st_size <= spec.size:
            with tmp.open("rb") as fh:
                while chunk := fh.read(CHUNK):
                    h.update(chunk)
                    done += len(chunk)
        else:
            tmp.unlink()
    _check_space(spec, dest, done)
    report = None
    if progress:
        three = _takes_rate(progress)
        report = (lambda d, t, bps: progress(d, t, bps)) if three else (lambda d, t, bps: progress(d, t))
    rate = _Rate()
    if report:
        report(done, spec.size, 0.0)
    retries = 0
    while done < spec.size:
        before = done
        try:
            headers = {"User-Agent": "chttr"}
            if done:
                headers["Range"] = f"bytes={done}-"
            log.info("downloading %s%s", spec.url, f" from byte {done}" if done else "")
            req = urllib.request.Request(spec.url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if done and getattr(r, "status", 200) != 206:   # the server ignored Range: start over
                    h, done = hashlib.sha256(), 0
                    rate = _Rate()
                with tmp.open("r+b" if done else "wb") as fh:
                    fh.seek(done)
                    fh.truncate()               # drop anything written after the last hashed byte
                    while chunk := r.read(CHUNK):
                        fh.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if done > spec.size:
                            break
                        if report:
                            report(done, spec.size, rate(done))
            if done < spec.size:
                raise ConnectionError(f"the connection closed after {done} of {spec.size} bytes")
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 416:   # our .part is no use to the server
                tmp.unlink(missing_ok=True)
                h, done = hashlib.sha256(), 0
            if not _retryable(exc):
                raise
            if done > before:
                retries = 0                     # it was moving: a fresh set of retries
            if retries >= len(RETRY_DELAYS):
                raise OSError(f"{spec.name}: download failed after {retries + 1} tries ({_reason(exc)}); "
                              "run it again to continue where it stopped") from exc
            delay = RETRY_DELAYS[retries]
            retries += 1
            log.warning("%s: %s; retrying in %d s", spec.name, _reason(exc), delay)
            _sleep(delay)
    if done != spec.size or h.hexdigest() != spec.sha256:
        tmp.unlink(missing_ok=True)
        raise ChecksumError(f"{spec.name}: download does not match the pinned checksum "
                            f"(got {done} bytes, sha256 {h.hexdigest()[:16]}…); refusing to use it")
    os.replace(tmp, dest)
    return dest


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or type(exc).__name__


def verify(spec: ModelFile, path: Path) -> bool:
    return path.exists() and path.stat().st_size == spec.size and sha256_of(path) == spec.sha256


# ---- progress -----------------------------------------------------------------------

BAR_CELLS = 24


def _amounts(done: int, total: int) -> str:
    if total >= 10 << 20:
        return f"{done >> 20}/{total >> 20} MB"
    if total >= 1 << 20:
        return f"{done / 2**20:.1f}/{total / 2**20:.1f} MB"
    return f"{-(-done // 1024)}/{-(-total // 1024)} KB"


def _clock(seconds: float) -> str:
    s = int(seconds + 0.5)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def progress_line(done: int, total: int, bytes_per_sec: float = 0.0) -> str:
    """[████████████░░░░░░░░░░░░] 52%  341/652 MB  24 MB/s  eta 0:13"""
    frac = min(1.0, done / total) if total else 1.0
    cells = int(frac * BAR_CELLS)
    line = f"[{'█' * cells}{'░' * (BAR_CELLS - cells)}] {int(frac * 100):3d}%  {_amounts(done, total)}"
    if bytes_per_sec > 0:
        mbps = bytes_per_sec / 2**20
        line += f"  {mbps:.0f} MB/s" if mbps >= 10 else f"  {mbps:.1f} MB/s"
        if done < total:
            line += f"  eta {_clock((total - done) / bytes_per_sec)}"
    return line


def console_progress(name: str, stream=None) -> Callable[[int, int, float], None]:
    """A progress bar on one terminal line, redrawn at most twice a second; a newline when
    done. When the output is not a terminal only the finished line is printed."""
    out = stream or sys.stdout
    try:
        tty = out.isatty()
    except (AttributeError, ValueError):
        tty = False
    last = [0.0, -1]

    def show(done: int, total: int, bytes_per_sec: float = 0.0) -> None:
        finished = done >= total
        now = time.monotonic()
        if finished:
            if last[1] == total:
                return
        elif not tty or now - last[0] < 0.5:
            return
        last[0], last[1] = now, done
        line = f"    {name}  {progress_line(done, total, bytes_per_sec)}"
        out.write(("\r" + line + "\033[K" if tty else line) + ("\n" if finished else ""))
        out.flush()

    return show


# ---- python3 -I models.py <parakeet dir> <models dir> ---------------------------------

def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 -I models.py <parakeet dir> <models dir>", file=sys.stderr)
        return 2
    parakeet, models_dir = Path(argv[0]).expanduser(), Path(argv[1]).expanduser()
    jobs = [(spec, parakeet / spec.name) for spec in PARAKEET]
    jobs += [(spec, models_dir / rel) for rel, spec in MEETING_MODELS.items()]
    todo = [(spec, dest) for spec, dest in jobs if not (dest.exists() and dest.stat().st_size == spec.size)]
    if not todo:
        return 0
    mb = sum(spec.size for spec, _ in todo) / 2**20
    print(f"  speech model: downloading Parakeet TDT 0.6B v3 and the meeting models (~{mb:.0f} MB, "
          f"checksum-verified) to {models_dir}", flush=True)
    try:
        for spec, dest in todo:
            download(spec, dest, console_progress(spec.name))
    except (OSError, ChecksumError) as exc:
        print(f"\nchttr: model download failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nchttr: model download stopped; run setup again to continue where it stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
