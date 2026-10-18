from __future__ import annotations

import numpy as np

from chttr import diarize as D


def voices(n_speakers: int, per: int, dim: int = 64, noise: float = 0.04, seed: int = 0) -> tuple[np.ndarray, list[int]]:
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(n_speakers, dim))
    rows, truth = [], []
    for s in range(n_speakers):
        for _ in range(per):
            v = centers[s] + noise * rng.normal(size=dim) * np.linalg.norm(centers[s])
            rows.append(v / np.linalg.norm(v))
            truth.append(s)
    order = rng.permutation(len(rows))
    return np.stack(rows)[order], [truth[i] for i in order]


def same_partition(a, b) -> bool:
    return len(set(zip(a, b))) == len(set(a)) == len(set(b))


def test_cluster_recovers_speakers():
    emb, truth = voices(3, 12)
    labels = D.cluster(emb)
    assert same_partition(list(labels), truth)


def test_cluster_numbers_by_first_appearance():
    emb, _ = voices(2, 5)
    labels = D.cluster(emb)
    assert labels[0] == 0
    first_seen = [labels[i] for i in range(len(labels)) if labels[i] not in labels[:i]]
    assert first_seen == sorted(first_seen)


def test_cluster_edge_cases():
    assert len(D.cluster(np.zeros((0, 8)))) == 0
    single = np.ones((1, 8)) / np.sqrt(8)
    assert list(D.cluster(single)) == [0]


def test_centroid_is_unit_length_and_weighted():
    a, b = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    c = D.centroid(np.stack([a, b]), np.array([3.0, 1.0]))
    assert np.isclose(np.linalg.norm(c), 1.0)
    assert c[0] > c[1]


def test_voice_memory_round_trip_and_matching(tmp_path):
    path = tmp_path / "voices.json"
    rng = np.random.default_rng(1)
    dana = rng.normal(size=256)
    dana /= np.linalg.norm(dana)
    store: dict[str, D.Voice] = {}
    D.remember(store, "Dana", "dana@example.com", dana.astype(np.float32), seconds=300)
    D.save_voices(store, path)
    loaded = D.load_voices(path)
    assert set(loaded) == {"dana@example.com"}

    near = dana + 0.3 * rng.normal(size=256) / 16
    near /= np.linalg.norm(near)
    match, score = D.match_voice(loaded, near, invited={"dana@example.com"})
    assert match is not None and match.name == "Dana" and score > 0.9

    stranger = rng.normal(size=256)
    stranger /= np.linalg.norm(stranger)
    assert D.match_voice(loaded, stranger, invited=set())[0] is None


def test_one_short_cameo_cannot_overwrite_a_known_voice():
    rng = np.random.default_rng(2)
    known = rng.normal(size=32)
    known /= np.linalg.norm(known)
    store: dict[str, D.Voice] = {}
    for _ in range(10):
        D.remember(store, "Ali", "ali@example.com", known.astype(np.float32), 600)
    other = rng.normal(size=32)
    other /= np.linalg.norm(other)
    D.remember(store, "Ali", "ali@example.com", other.astype(np.float32), 15)
    assert float(store["ali@example.com"].embedding @ known) > 0.95


def test_remember_needs_a_name_or_email():
    store: dict[str, D.Voice] = {}
    D.remember(store, "", "", np.ones(4, dtype=np.float32), 60)
    assert store == {}


def test_match_voice_prefers_the_most_recently_heard_voice_on_a_tie():
    v = np.ones(8, dtype=np.float32) / np.sqrt(8)
    voices = {"old@x.com": D.Voice("old@x.com", "Old", "old@x.com", v, 3, "2026-01-01T10:00:00+00:00"),
              "new@x.com": D.Voice("new@x.com", "New", "new@x.com", v, 1, "2026-09-01T10:00:00+00:00"),
              "mid@x.com": D.Voice("mid@x.com", "Mid", "mid@x.com", v, 2, "2026-05-01T10:00:00+00:00")}
    match, score = D.match_voice(voices, v, invited=set())
    assert match is not None and match.name == "New" and score > 0.99
