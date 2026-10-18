"""The post-call pipeline: voice activity, transcription, echo removal, diarization,
speaker naming and notes. Runs in its own low-priority process (`chttr meeting process`)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .. import diarize as D
from ..config import Config
from . import lite
from .analysis import SELF_CAPTION_NAMES, _intervals, _is_echo, _overlap
from .naming import ROOM_OFFSET, assign_speakers, room_clusters
from .render import render
from .store import SAMPLE_RATE, Utterance, update_meta, write_json
from .styles import choose_style, write_notes

log = logging.getLogger("chttr.meeting")


def _load_channel(directory: Path, name: str) -> np.ndarray:
    path = directory / f"{name}.raw"
    if not path.exists() or path.stat().st_size < SAMPLE_RATE:
        return np.zeros(0, dtype=np.int16)
    return np.memmap(path, dtype=np.int16, mode="r")



def _transcribe(utts: list[Utterance], audio: dict[str, np.ndarray], cfg: Config, names: list[str]) -> None:
    from ..dictionary import Dictionary
    from ..transcriber import Transcriber, _clean

    engine = Transcriber(cfg).engine
    dictionary = Dictionary.load()
    prompt = dictionary.whisper_prompt()
    t0 = time.monotonic()
    for i, u in enumerate(utts):
        seg = D.as_float(audio[u.channel][int(u.start * SAMPLE_RATE):int(u.end * SAMPLE_RATE)])
        text = re.sub(r"\s+", " ", engine.recognize(seg, prompt)).strip()
        if engine.name == "whisper":
            text = _clean(text)
        u.text = dictionary.apply(text, names) if text else ""
        if i and i % 50 == 0:
            log.info("transcribed %d/%d utterances (%.0fs)", i, len(utts), time.monotonic() - t0)


def _diarize(them: list[Utterance], audio: np.ndarray, threads: int) -> dict[int, np.ndarray]:
    """Sets .cluster on every them-utterance; returns {cluster: centroid}."""
    if not them:
        return {}
    embed = D.Embedder(threads)
    vecs: dict[int, np.ndarray] = {}
    for i, u in enumerate(them):
        if u.end - u.start >= D.MIN_EMBED_SECONDS:
            vecs[i] = embed(D.as_float(audio[int(u.start * SAMPLE_RATE):int(u.end * SAMPLE_RATE)]))
    if not vecs:
        for u in them:
            u.cluster = 0
        return {}
    idx = list(vecs)
    labels = D.cluster(np.stack([vecs[i] for i in idx]))
    for i, label in zip(idx, labels, strict=True):
        them[i].cluster = int(label)
    centroids = {}
    for c in set(int(x) for x in labels):
        rows = [vecs[i] for i, label in zip(idx, labels, strict=True) if label == c]
        weights = [them[i].end - them[i].start for i, label in zip(idx, labels, strict=True) if label == c]
        centroids[c] = D.centroid(np.stack(rows), np.array(weights))
    # Short utterances: nearest voice if it is close, else whoever spoke just before.
    for i, u in enumerate(them):
        if u.cluster >= 0:
            continue
        seg = D.as_float(audio[int(u.start * SAMPLE_RATE):int(u.end * SAMPLE_RATE)])
        best, score = -1, -1.0
        if len(seg) >= SAMPLE_RATE // 2:
            v = embed(seg)
            best, score = max(((c, float(v @ m)) for c, m in centroids.items()), key=lambda x: x[1])
        prev = next((them[j] for j in range(i - 1, -1, -1) if them[j].cluster >= 0), None)
        if score >= 0.3 or prev is None or u.start - prev.end > 3:
            u.cluster = best if best >= 0 else (prev.cluster if prev else 0)
        else:
            u.cluster = prev.cluster
    return centroids



def process(directory: Path, cfg: Config, summary: bool | None = None) -> Path:
    """Turn a recorded meeting into notes. Returns the Markdown file."""
    from .. import notes as N

    meta = update_meta(directory, status="processing", error="")
    started = float(meta["started"])
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()] \
        if (directory / "events.jsonl").exists() else []
    threads = cfg.engine.threads
    D.ensure_models()

    audio = {"mic": _load_channel(directory, "mic"), "them": _load_channel(directory, "them")}
    utts: list[Utterance] = []
    with D.timed("voice activity"):
        excluded = _intervals(events, started)
        for channel, data in audio.items():
            for s, e in D.speech_segments(data, threads):
                if channel == "mic" and sum(_overlap(s, e, a, b) for a, b in excluded) > 0.5 * (e - s):
                    continue
                utts.append(Utterance(s, e, channel))
    log.info("%d utterances (%d mic, %d them)", len(utts), sum(u.channel == "mic" for u in utts),
             sum(u.channel == "them" for u in utts))

    # Names from the invite and the captions: misheard versions get corrected like dictionary words.
    from .. import gcal

    urls = gcal.read_urls(cfg.meeting.calendar_file)
    event = gcal.find_event(urls, meta.get("code", ""), datetime.fromtimestamp(started).astimezone(),
                            me_email=cfg.meeting.email) if urls else None
    names = sorted({part for full in [*(a.name for a in (event.attendees if event else [])),
                                      *(str(e.get("name", "")) for e in events if e.get("type") == "speaking")]
                    for part in full.split() if len(part) >= 3 and part.lower() not in SELF_CAPTION_NAMES})
    with D.timed("transcription"):
        _transcribe(utts, audio, cfg, names)
    utts = [u for u in utts if u.text]
    them = sorted((u for u in utts if u.channel == "them"), key=lambda u: u.start)
    mine = [u for u in utts if u.channel == "mic" and not _is_echo(u, them)]
    log.info("dropped %d mic utterances as echo", sum(u.channel == "mic" for u in utts) - len(mine))

    with D.timed("speaker embeddings"):
        centroids = _diarize(them, audio["them"], threads)
        mic_centroids = _diarize(mine, audio["mic"], threads) if cfg.meeting.room_speakers and len(mine) >= 6 else {}

    # ---- who is who --------------------------------------------------------
    attendees = [a.to_dict() for a in event.attendees] if event else []
    voices = D.load_voices()
    me_att = next((a for a in attendees if a["is_me"]), None)
    me_key = ((me_att or {}).get("email") or cfg.meeting.me or "me").lower()
    own = voices.get(me_key)
    me_cluster, room = room_clusters(mine, mic_centroids, own.embedding if own else None)
    if room:
        log.info("%d other voice(s) in the room on your microphone", len(room))
    speakers, me_name, cluster_seconds = assign_speakers(mine, them, events, started, attendees, cfg.meeting.me,
                                                         centroids, voices, room)
    centroids.update({ROOM_OFFSET + c: v for c, v in mic_centroids.items()})
    all_utts = sorted(mine + them, key=lambda u: u.start)

    duration = max((len(a) for a in audio.values()), default=0) / SAMPLE_RATE
    meta = update_meta(directory, me=me_name)
    result = {
        "meta": meta, "duration": duration,
        "event": event.to_dict() if event else None,
        "speakers": {k: asdict(v) for k, v in speakers.items()},
        "utterances": [asdict(u) for u in all_utts],
        "notes": {}, "notes_by_style": {},
    }

    # ---- notes -------------------------------------------------------------
    want_summary = cfg.meeting.summary if summary is None else summary
    if want_summary and all_utts and not N.available(cfg.polish):
        log.info("notes: on-device (no Claude key or Ollama)")
        result["notes"] = lite.notes_lite(result)
    elif want_summary and all_utts:
        style_id = ""
        try:
            style_id = choose_style(result, cfg)
            with D.timed("notes"):
                write_notes(result, cfg, style_id, meta.get("style_extra", ""))
        except Exception as exc:  # noqa: BLE001 - the transcript is still worth saving
            log.warning("notes failed: %s; writing on-device notes instead", exc)
            result["notes"] = {**lite.notes_lite(result), "error": f"{type(exc).__name__}: {exc}", "wanted": style_id}

    # ---- remember voices we are sure of ------------------------------------
    for sp in speakers.values():
        vecs = [centroids[c] for c in sp.clusters if c in centroids]
        secs = sum(cluster_seconds[c] for c in sp.clusters)
        if sp.source in ("captions", "calendar", "voice") and vecs and secs >= 10:
            D.remember(voices, sp.name, sp.email, D.centroid(np.stack(vecs)), secs)
    if me_cluster is not None and me_cluster in mic_centroids:
        mine_secs = sum(u.end - u.start for u in mine if u.cluster == me_cluster)
        if mine_secs >= 30:
            D.remember(voices, me_name, me_key if "@" in me_key else "", mic_centroids[me_cluster], mine_secs)
    D.save_voices(voices)
    np.savez(directory / "speakers.npz", **{str(c): v for c, v in centroids.items()})  # pyright: ignore[reportArgumentType]

    md = render(directory, result, cfg)
    result["markdown"] = str(md)
    write_json(directory / "result.json", result)

    audio.clear()                   # release the memory-mapped recordings before deleting them
    _finish_audio(directory, md, cfg)
    update_meta(directory, status="done", markdown=str(md))
    return md


def _finish_audio(directory: Path, md: Path, cfg: Config) -> None:
    raws = [directory / "mic.raw", directory / "them.raw"]
    if cfg.meeting.keep_audio and all(p.exists() for p in raws) and shutil.which("ffmpeg"):
        out = md.with_suffix(".opus")
        cmd = ["ffmpeg", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", str(raws[0]),
               "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", str(raws[1]),
               "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]", "-map", "[a]",
               "-c:a", "libopus", "-b:a", "48k", str(out)]
        if subprocess.run(cmd, check=False).returncode != 0:
            log.warning("could not encode the audio; keeping the raw files in %s", directory)
            return
    for p in raws:
        p.unlink(missing_ok=True)

