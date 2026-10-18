"""Who spoke when, on the "them" channel: voice activity, speaker embeddings, clustering, voice memory.

Models (downloaded once to ~/.local/share/chttr/models, ~29 MB):
- Silero VAD, to cut the audio into utterances.
- WeSpeaker ResNet34 (VoxCeleb), 256-dim speaker embeddings.

Both run through onnx-asr, which chttr already uses for Parakeet.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from . import models as M
from .config import MODELS_DIR, VOICES_FILE

log = logging.getLogger("chttr.diarize")

SAMPLE_RATE = 16000
MODELS = {
    "silero-vad/silero_vad.onnx": M.SILERO_VAD,
    "wespeaker-resnet34/voxceleb_resnet34_LM.onnx": M.WESPEAKER,
}
VAD_CHUNK_SECONDS = 600          # run the VAD over 10-minute pieces to bound memory
MIN_EMBED_SECONDS = 1.2          # shorter utterances don't make reliable embeddings
MAX_EMBED_SECONDS = 10.0
CLUSTER_THRESHOLD = 0.40         # average-linkage cosine similarity to keep merging
VOICE_MATCH_ATTENDEE = 0.50      # remembered voice vs. cluster, when the person is invited
VOICE_MATCH_ANYONE = 0.62        # ... when they are not on the invite


def ensure_models() -> None:
    """Download the VAD and speaker-embedding models (pinned and checksum-verified) if missing."""
    for rel, spec in MODELS.items():
        M.download(spec, MODELS_DIR / rel)


def _session_options(threads: int):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = threads or max(1, (os.cpu_count() or 4) // 2)
    return so


# ---- voice activity -----------------------------------------------------

def as_float(samples: np.ndarray) -> np.ndarray:
    """int16 PCM (e.g. a slice of a memmap) to float32 in [-1, 1]."""
    if samples.dtype == np.int16:
        return samples.astype(np.float32) / 32768.0
    return np.asarray(samples, dtype=np.float32)


def speech_segments(audio: np.ndarray, threads: int = 0) -> list[tuple[float, float]]:
    """(start, end) seconds of speech. `audio` may be an int16 memmap; it is
    converted one chunk at a time."""
    from onnx_asr.models.silero import SileroVad

    vad = SileroVad({"model": MODELS_DIR / "silero-vad/silero_vad.onnx"},
                    {"sess_options": _session_options(threads), "providers": ["CPUExecutionProvider"]})
    opts = dict(threshold=0.5, min_speech_duration_ms=300, min_silence_duration_ms=300,
                max_speech_duration_s=20, speech_pad_ms=100)
    out: list[tuple[float, float]] = []
    step = VAD_CHUNK_SECONDS * SAMPLE_RATE
    for offset in range(0, len(audio), step):
        chunk = as_float(audio[offset:offset + step])
        if len(chunk) < SAMPLE_RATE // 2:
            continue
        lens = np.array([len(chunk)], dtype=np.int64)
        for segs in vad.segment_batch(chunk[None, :], lens, SAMPLE_RATE, **opts):
            for s, e in segs:
                start, end = (offset + s) / SAMPLE_RATE, (offset + e) / SAMPLE_RATE
                # a piece cut at the chunk boundary continues in the next chunk
                if out and start - out[-1][1] < 0.05 and end - out[-1][0] <= 20:
                    out[-1] = (out[-1][0], end)
                else:
                    out.append((start, end))
    return out


# ---- embeddings ---------------------------------------------------------

class Embedder:
    def __init__(self, threads: int = 0) -> None:
        from onnx_asr.loader import Manager

        self.se = Manager().create_se(None, MODELS_DIR / "wespeaker-resnet34",
                                      config={"sess_options": _session_options(threads)})

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        """Unit-length embedding of one utterance (float32 at 16 kHz). One at a
        time on purpose: onnx-asr pads batches, and the padding skews the
        embedding (the model takes no lengths)."""
        limit = int(MAX_EMBED_SECONDS * SAMPLE_RATE)
        if len(audio) > limit:
            mid = (len(audio) - limit) // 2
            audio = audio[mid:mid + limit]
        e = np.asarray(self.se.embedding(audio.astype(np.float32)), dtype=np.float32).reshape(-1)
        return e / (np.linalg.norm(e) + 1e-9)


# ---- clustering ---------------------------------------------------------

def cluster(embeddings: np.ndarray, threshold: float = CLUSTER_THRESHOLD) -> np.ndarray:
    """Average-linkage agglomerative clustering on cosine similarity. Returns a
    label per row, numbered by first appearance."""
    n = len(embeddings)
    if n == 0:
        return np.zeros(0, dtype=int)
    sim = embeddings @ embeddings.T
    np.fill_diagonal(sim, -np.inf)
    sizes = np.ones(n)
    members = {i: [i] for i in range(n)}
    active = np.ones(n, dtype=bool)
    while active.sum() > 1:
        masked = np.where(active[:, None] & active[None, :], sim, -np.inf)
        i, j = (int(x) for x in np.unravel_index(np.argmax(masked), masked.shape))
        if masked[i, j] < threshold:
            break
        # Lance-Williams update for average linkage
        merged = (sizes[i] * sim[i] + sizes[j] * sim[j]) / (sizes[i] + sizes[j])
        sim[i], sim[:, i] = merged, merged
        sim[i, i] = -np.inf
        sizes[i] += sizes[j]
        members[i] += members.pop(j)
        active[j] = False
        sim[j], sim[:, j] = -np.inf, -np.inf
    labels = np.empty(n, dtype=int)
    for k, (_root, rows) in enumerate(sorted(members.items(), key=lambda kv: min(kv[1]))):
        labels[rows] = k
    return labels


def centroid(vectors: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    c = np.average(vectors, axis=0, weights=weights)
    return c / (np.linalg.norm(c) + 1e-9)


# ---- voice memory -------------------------------------------------------

@dataclass
class Voice:
    key: str            # email, or the name when there is no email
    name: str
    email: str
    embedding: np.ndarray
    count: int = 1
    updated: str = ""


def load_voices(path: Path = VOICES_FILE) -> dict[str, Voice]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: Voice(k, v.get("name", ""), v.get("email", ""), np.array(v["embedding"], dtype=np.float32),
                     int(v.get("count", 1)), v.get("updated", ""))
            for k, v in data.items()}


def save_voices(voices: dict[str, Voice], path: Path = VOICES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: {"name": v.name, "email": v.email, "embedding": [round(float(x), 5) for x in v.embedding],
                "count": v.count, "updated": v.updated} for k, v in sorted(voices.items())}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def remember(voices: dict[str, Voice], name: str, email: str, embedding: np.ndarray, seconds: float) -> None:
    """Fold one meeting's centroid into the stored voice. Weighted by meetings
    seen (capped), so one noisy call can't overwrite a well-known voice."""
    key = email.lower() if email else name.strip().lower()
    if not key:
        return
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    weight = min(1.0, seconds / 60)       # a 20-second cameo counts for a third of a meeting
    v = voices.get(key)
    if v is None:
        voices[key] = Voice(key, name, email, embedding.astype(np.float32), 1, stamp)
        return
    old = min(v.count, 20)
    mixed = v.embedding * old + embedding * weight
    v.embedding = (mixed / (np.linalg.norm(mixed) + 1e-9)).astype(np.float32)
    v.count += 1
    v.updated = stamp
    v.name = name or v.name
    v.email = email or v.email


def match_voice(voices: dict[str, Voice], embedding: np.ndarray, invited: set[str]) -> tuple[Voice | None, float]:
    """Best remembered voice for a cluster centroid, or (None, best score). On a tie the
    voice heard most recently wins."""
    best, best_rank = None, (-1.0, "")
    for v in voices.values():
        score = float(v.embedding @ embedding)
        bar = VOICE_MATCH_ATTENDEE if v.key in invited else VOICE_MATCH_ANYONE
        if score >= bar and (score, v.updated) > best_rank:
            best, best_rank = v, (score, v.updated)
    return best, best_rank[0]


def timed(label: str):
    """Tiny context manager for timing log lines."""
    class _T:
        def __enter__(self):
            self.t = time.monotonic()
            return self

        def __exit__(self, *exc):
            log.info("%s: %.1fs", label, time.monotonic() - self.t)
    return _T()
