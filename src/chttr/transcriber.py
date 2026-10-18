"""Local speech-to-text: NVIDIA Parakeet (onnx-asr) or whisper.cpp (pywhispercpp)."""

from __future__ import annotations

import logging
import os
import re
import threading
import wave
from pathlib import Path

import numpy as np

from . import levels
from .config import MODEL_SEARCH_DIRS, PARAKEET_SEARCH_DIRS, Config

log = logging.getLogger("chttr.asr")

SAMPLE_RATE = 16000

# Whisper emits these on silence / noise; never type them.
_NON_SPEECH = re.compile(r"^[\s\[\(\*].*[\]\)\*]\s*$")
_HALLUCINATIONS = {
    "thank you.", "thanks for watching.", "thank you for watching.", "you", "you.",
    "bye.", "thanks.", "subtitles by the amara.org community",
}
_PARAKEET_FILES = ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt")


def resolve_model(name: str) -> str:
    """Return a path to a ggml model if one is already on disk, else the bare name
    (pywhispercpp will download it to its own models dir)."""
    p = Path(name).expanduser()
    if p.suffix == ".bin" and p.exists():
        return str(p)
    for d in MODEL_SEARCH_DIRS:
        candidate = d / f"ggml-{name}.bin"
        if candidate.exists():
            return str(candidate)
    return name


def resolve_parakeet(configured: str) -> Path:
    dirs = [Path(configured).expanduser()] if configured else PARAKEET_SEARCH_DIRS
    for d in dirs:
        if all((d / f).exists() for f in _PARAKEET_FILES):
            return d
    raise FileNotFoundError(
        "Parakeet model not found in " + ", ".join(str(d) for d in dirs)
        + " (download it with voxtype, or set [engine] name = \"whisper\")")


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        assert wf.getsampwidth() == 2, "expected 16-bit PCM"
        frames = wf.readframes(wf.getnframes())
        channels = wf.getnchannels()
        rate = wf.getframerate()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio), n, endpoint=False), np.arange(len(audio)), audio).astype(np.float32)
    return audio


def frame_levels(audio: np.ndarray) -> np.ndarray:
    """The RMS level (dBFS) of every whole 20 ms frame."""
    frame = SAMPLE_RATE // 50
    n = len(audio) // frame
    frames = audio[: n * frame].reshape(n, frame)
    return 10 * np.log10(np.maximum(np.mean(frames * frames, axis=1), 1e-12))


def trim_silence(audio: np.ndarray, threshold_db: float, pad: float = 0.3,
                 levels_db: np.ndarray | None = None) -> np.ndarray:
    """Cut leading and trailing silence (20 ms frames below threshold_db RMS), keeping
    `pad` seconds either side. Shorter input is faster and gives whisper less room to
    hallucinate. Returns an empty array if no frame reaches the threshold. Pass
    `levels_db` (from frame_levels) when you already have them."""
    frame = SAMPLE_RATE // 50
    n = len(audio) // frame
    if n == 0:
        return audio
    rms_db = frame_levels(audio) if levels_db is None else levels_db
    loud = np.flatnonzero(rms_db > threshold_db)
    if len(loud) == 0:
        return audio[:0]
    p = int(pad * 50)
    start = max(0, loud[0] - p) * frame
    end = min(n, loud[-1] + 1 + p) * frame
    return audio[start:end]


def boost_gain(audio: np.ndarray, whisper: bool = False, target_peak: float = 0.5) -> float:
    """How much boost_quiet raises this audio (1.0: not at all)."""
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    if peak <= 0 or peak >= (target_peak if whisper else 0.1):
        return 1.0
    return min(target_peak / peak, 30.0 if whisper else 6.0)


def boost_quiet(audio: np.ndarray, whisper: bool = False, target_peak: float = 0.5) -> np.ndarray:
    """Raise a quiet recording toward `target_peak`: the recognizer does better on
    whispered or far-from-the-mic speech at a normal level. Normally only clearly
    quiet audio (peak under 0.1) is raised, by at most 6x; in whisper mode anything
    under the target, by up to 30x."""
    gain = boost_gain(audio, whisper, target_peak)
    return audio if gain == 1.0 else (audio * gain).astype(np.float32)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text or _NON_SPEECH.match(text) or text.lower() in _HALLUCINATIONS:
        return ""
    return text


def _threads(n: int) -> int:
    return n or max(1, (os.cpu_count() or 4) // 2)


class WhisperTranscriber:
    name = "whisper"

    def __init__(self, cfg: Config) -> None:
        try:
            from pywhispercpp.model import Model
        except ImportError as exc:
            raise RuntimeError("the whisper engine needs pywhispercpp: re-run bin/setup, or "
                               "uv tool install --editable <chttr checkout> --with pywhispercpp") from exc

        w = cfg.whisper
        threads = _threads(cfg.engine.threads or w.threads)
        model = resolve_model(w.model)
        log.info("loading whisper model %s (%d threads)", model, threads)
        self.model = Model(
            model,
            redirect_whispercpp_logs_to=None,
            n_threads=threads,
            print_progress=False,
            print_realtime=False,
            language=w.language,
            no_context=True,
            suppress_blank=True,
            suppress_nst=True,
        )
        self.key = ("whisper", w.model, w.language, threads)

    def recognize(self, audio: np.ndarray, vocabulary_prompt: str | None) -> str:
        # initial_prompt biases decoding toward these spellings (names, jargon).
        segments = self.model.transcribe(audio, initial_prompt=vocabulary_prompt or "")
        return " ".join(s.text.strip() for s in segments if s.text and s.text.strip())


class ParakeetTranscriber:
    name = "parakeet"

    def __init__(self, cfg: Config) -> None:
        import onnx_asr
        import onnxruntime as ort

        path = resolve_parakeet(cfg.engine.parakeet_dir)
        threads = _threads(cfg.engine.threads)
        log.info("loading parakeet from %s (%d threads)", path, threads)
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", str(path), quantization="int8", sess_options=so)
        self.key = ("parakeet", str(path), threads)

    def recognize(self, audio: np.ndarray, vocabulary_prompt: str | None) -> str:
        # Parakeet takes no prompt; the dictionary's replacements cover names instead.
        return str(self.model.recognize(audio))


def engine_key(cfg: Config) -> tuple:
    if cfg.engine.name == "whisper":
        return ("whisper", cfg.whisper.model, cfg.whisper.language, _threads(cfg.engine.threads or cfg.whisper.threads))
    return ("parakeet", str(resolve_parakeet(cfg.engine.parakeet_dir)), _threads(cfg.engine.threads))


class Transcriber:
    """The configured engine behind a lock, so live captions and the final pass
    never run the model at the same time."""

    def __init__(self, cfg: Config) -> None:
        self.lock = threading.Lock()
        self.engine = ParakeetTranscriber(cfg) if cfg.engine.name == "parakeet" else WhisperTranscriber(cfg)
        self.threshold_db = cfg.audio.threshold_db
        self.boost = cfg.audio.whisper_mode
        self.adaptive = cfg.audio.adaptive_threshold
        log.info("%s ready", self.engine.name)

    @property
    def key(self) -> tuple:
        return self.engine.key

    def transcribe(self, source: Path | np.ndarray, vocabulary_prompt: str | None = None, trim: bool = True) -> str:
        audio = source if isinstance(source, np.ndarray) else load_wav(source)
        # Boost first, then trim. Thresholds stay in the mic's own levels (shifted by
        # the boost), so -38 dB still means -38 dB at the mic.
        gain = boost_gain(audio, whisper=self.boost)
        audio = boost_quiet(audio, whisper=self.boost)
        if trim:
            audio = self._trim(audio, 20 * np.log10(gain))
        if len(audio) < SAMPLE_RATE * 0.3:  # < 0.3 s
            return ""
        with self.lock:
            return _clean(self.engine.recognize(audio, vocabulary_prompt))

    def _trim(self, audio: np.ndarray, gain_db: float) -> np.ndarray:
        """Cut the silence either side. With adaptive levels the threshold follows this
        recording's noise floor: a quiet mic's speech never reaches a fixed -38 dB."""
        frames = frame_levels(audio)
        thr = self.threshold_db
        if self.adaptive and len(frames):
            thr = levels.effective_threshold(frames - gain_db, self.threshold_db)
            if thr != self.threshold_db:
                log.debug("trim threshold %.0f dB (configured %.0f)", thr, self.threshold_db)
        return trim_silence(audio, thr + gain_db, levels_db=frames)
