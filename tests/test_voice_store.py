"""#82 PR2/PR3 — embedding, SQLite voice store, benchmark, learning tokens."""

from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from openclaw_adapter.voice.benchmark import load_manifest, run_benchmark
from openclaw_adapter.voice.learning import (
    LEARNING_COMMITTED,
    LEARNING_SKIPPED_NO_EMBEDDING,
    commit_prototype,
    issue_learning_token,
    redeem_learning_token,
)
from openclaw_adapter.voice.embedding import (
    SyntheticEmbeddingBackend,
    WhisperEncoderEmbeddingBackend,
    cosine_similarity,
    resolve_embedding_backend,
)
from openclaw_adapter.voice.prototype_store import (
    PROTOTYPE_STATUS_ACTIVE,
    PROTOTYPE_STATUS_DISABLED,
    PROTOTYPE_STATUS_ORPHANED,
    VoiceStore,
    VoiceStoreCorruptError,
    VoiceStoreVersionError,
    open_voice_store,
)


# --- embedding backend -------------------------------------------------------
def test_synthetic_backend_is_deterministic_and_normalized():
    backend = SyntheticEmbeddingBackend(dim=32)
    a = backend.embed(b"audio-bytes-1")
    assert a == backend.embed(b"audio-bytes-1")
    assert len(a) == 32
    assert abs(sum(v * v for v in a) - 1.0) < 1e-6
    b = backend.embed(b"different-bytes")
    assert cosine_similarity(a, b) < 0.9
    assert cosine_similarity(a, a) == pytest.approx(1.0)


def test_synthetic_backend_rejects_empty_audio():
    with pytest.raises(ValueError):
        SyntheticEmbeddingBackend().embed(b"")


def test_resolve_embedding_backend_settings():
    assert resolve_embedding_backend(
        SimpleNamespace(openclaw_voice_embedding_backend="")
    ) is None
    assert resolve_embedding_backend(
        SimpleNamespace(openclaw_voice_embedding_backend="no-such-model")
    ) is None
    backend = resolve_embedding_backend(
        SimpleNamespace(openclaw_voice_embedding_backend="synthetic")
    )
    assert backend is not None
    assert backend.model_version.startswith("synthetic-v1")


def test_resolve_embedding_backend_whisper_encoder():
    backend = resolve_embedding_backend(
        SimpleNamespace(
            openclaw_voice_embedding_backend="whisper_encoder",
            openclaw_stt_model="base",
        )
    )
    assert backend is not None
    assert backend.model_version == "whisper-encoder-v2:base"


def test_whisper_backend_rejects_empty_audio():
    with pytest.raises(ValueError):
        WhisperEncoderEmbeddingBackend(
            model_name="base",
            device="auto",
            compute_type="default",
            download_root=".openclaw_tmp/whisper",
        ).embed(b"")


@pytest.mark.skipif(
    os.getenv("OPENCLAW_RUN_LIVE_VOICE_EMBEDDING") != "1",
    reason="Set OPENCLAW_RUN_LIVE_VOICE_EMBEDDING=1 to run the live acoustic discrimination check (macOS `say` + Whisper model).",
)
def test_whisper_backend_discriminates_real_speech(tmp_path):
    """Same phrase re-rendered must score above threshold; different phrases below.

    Guards the v1→v2 padding regression: pooling the whole 30s padded window
    collapsed all short commands to ~0.95+ mutual similarity."""
    import subprocess

    from openclaw_adapter.voice.policy import DIRECT_SIMILARITY_THRESHOLD

    renditions = {
        "fan_off_a": ("關電扇", "150"),
        "fan_off_b": ("關電扇", "210"),
        "weather": ("今天天氣如何", "180"),
    }
    audio: dict[str, bytes] = {}
    for name, (phrase, rate) in renditions.items():
        path = tmp_path / f"{name}.wav"
        subprocess.run(
            ["say", "-v", "Meijia", "-r", rate, "--data-format=LEI16@16000",
             "-o", str(path), phrase],
            check=True,
        )
        audio[name] = path.read_bytes()

    backend = WhisperEncoderEmbeddingBackend(
        model_name="base",
        device="auto",
        compute_type="default",
        download_root=".openclaw_tmp/whisper",
    )
    vecs = {name: backend.embed(data) for name, data in audio.items()}
    same = cosine_similarity(vecs["fan_off_a"], vecs["fan_off_b"])
    diff = cosine_similarity(vecs["fan_off_a"], vecs["weather"])
    assert same >= DIRECT_SIMILARITY_THRESHOLD
    assert diff < DIRECT_SIMILARITY_THRESHOLD
    assert same > diff


# --- voice store -------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    clock = {"now": 1000.0}
    s = VoiceStore(str(tmp_path / "voice.sqlite3"), now=lambda: clock["now"])
    s._clock = clock  # test hook: advance time
    return s


def test_utterance_roundtrip_and_ttl(store):
    store.save_utterance(
        utterance_id="u1",
        transcript="關鍵善",
        duration_ms=1450,
        ttl_seconds=60,
        language="zh",
        language_probability=0.98,
        embedding=[0.1, 0.2, 0.3],
        embedding_model_version="synthetic-v1-d3",
    )
    rec = store.get_utterance("u1")
    assert rec is not None
    assert rec.transcript == "關鍵善"
    assert rec.embedding == pytest.approx((0.1, 0.2, 0.3))
    assert rec.embedding_model_version == "synthetic-v1-d3"

    store._clock["now"] = 1061.0  # past expires_at
    assert store.get_utterance("u1") is None
    assert store.gc_expired() == 1
    assert store.get_utterance("u1") is None


def test_gc_removes_consumed_utterances(store):
    store.save_utterance(
        utterance_id="u1", transcript="hi", duration_ms=500, ttl_seconds=600
    )
    store.mark_utterance_consumed("u1")
    assert store.gc_expired() == 1


def test_prototype_crud_and_model_version_isolation(store):
    store.add_prototype(
        prototype_id="p1", action_id="ir.fan.power",
        embedding=[1.0, 0.0], embedding_model_version="m1",
    )
    store.add_prototype(
        prototype_id="p2", action_id="ir.fan.power",
        embedding=[0.0, 1.0, 0.0], embedding_model_version="m2",
    )
    m1 = store.list_prototypes(embedding_model_version="m1")
    assert [p.prototype_id for p in m1] == ["p1"]
    assert m1[0].embedding == (1.0, 0.0)
    # Cross-version vectors never meet (design §12.3).
    m2 = store.list_prototypes(embedding_model_version="m2")
    assert [p.prototype_id for p in m2] == ["p2"]
    assert len(store.list_prototypes()) == 2

    store.record_confirmation("p1")
    p1 = store.list_prototypes(embedding_model_version="m1")[0]
    assert p1.confirmed_count == 2
    assert p1.status == PROTOTYPE_STATUS_ACTIVE


def test_rejections_disable_prototype(store):
    store.add_prototype(
        prototype_id="p1", action_id="ir.fan.power",
        embedding=[1.0], embedding_model_version="m1",
    )
    store.record_rejection("p1", disable_after=2)
    assert store.list_prototypes(status=None)[0].status == PROTOTYPE_STATUS_ACTIVE
    store.record_rejection("p1", disable_after=2)
    only = store.list_prototypes(status=None)[0]
    assert only.status == PROTOTYPE_STATUS_DISABLED
    assert only.rejected_count == 2
    # Disabled prototypes drop out of the default (active) listing.
    assert store.list_prototypes() == ()


def test_orphaned_action_prototypes_never_listed_active(store):
    store.add_prototype(
        prototype_id="p1", action_id="ir.old.gone",
        embedding=[1.0], embedding_model_version="m1",
    )
    assert store.mark_action_orphaned("ir.old.gone") == 1
    assert store.list_prototypes() == ()
    assert (
        store.list_prototypes(status=PROTOTYPE_STATUS_ORPHANED)[0].action_id
        == "ir.old.gone"
    )


def test_action_stats_and_reset(store):
    store.record_action_outcome("music.playpause", success=True)
    store.record_action_outcome("music.playpause", success=False)
    store.save_utterance(
        utterance_id="u1", transcript="hi", duration_ms=500, ttl_seconds=600
    )
    store.add_prototype(
        prototype_id="p1", action_id="music.playpause",
        embedding=[1.0], embedding_model_version="m1",
    )
    store.reset_profile()
    assert store.list_prototypes(status=None) == ()
    assert store.get_utterance("u1") is None


def test_corrupt_db_raises_explicitly(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all........")
    with pytest.raises(VoiceStoreCorruptError):
        VoiceStore(str(path))


def test_newer_schema_version_refuses(tmp_path):
    path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(VoiceStoreVersionError):
        VoiceStore(str(path))


def test_open_voice_store_disabled_and_corrupt(tmp_path):
    assert open_voice_store(SimpleNamespace(openclaw_voice_store_path="")) is None
    good = open_voice_store(
        SimpleNamespace(openclaw_voice_store_path=str(tmp_path / "ok.sqlite3"))
    )
    assert good is not None
    bad_path = tmp_path / "bad.sqlite3"
    bad_path.write_bytes(b"garbage-not-sqlite-garbage-not-sqlite-garbage")
    with pytest.raises(VoiceStoreCorruptError):
        open_voice_store(SimpleNamespace(openclaw_voice_store_path=str(bad_path)))


# --- learning tokens (PR3, §5.4/§13.2) ---------------------------------------
def _save_embedded_utterance(store, utterance_id="u1"):
    store.save_utterance(
        utterance_id=utterance_id,
        transcript="關鍵善",
        duration_ms=1450,
        ttl_seconds=600,
        embedding=[0.6, 0.8],
        embedding_model_version="synthetic-v1-d2",
    )


def test_learning_token_roundtrip_and_single_use(store):
    _save_embedded_utterance(store)
    token = issue_learning_token(
        store, utterance_id="u1",
        candidate_action_ids=("ir.fan.power", "music.playpause"),
    )
    assert token is not None
    # Raw token never stored — only its hash (design §13.2).
    conn = sqlite3.connect(store._path)
    rows = conn.execute(
        "SELECT token_hash FROM voice_learning_tokens"
    ).fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0][0] != token

    redeemed = redeem_learning_token(store, token)
    assert redeemed is not None
    assert redeemed.utterance_id == "u1"
    assert redeemed.candidate_action_ids == ("ir.fan.power", "music.playpause")
    # Replay must fail (§7.4).
    assert redeem_learning_token(store, token) is None


def test_learning_token_expiry(store):
    _save_embedded_utterance(store)
    token = issue_learning_token(
        store, utterance_id="u1",
        candidate_action_ids=("ir.fan.power",), ttl_seconds=60,
    )
    store._clock["now"] = 1061.0
    assert redeem_learning_token(store, token) is None


def test_unknown_token_redeems_nothing(store):
    assert redeem_learning_token(store, "never-issued-token") is None


def test_commit_prototype_success_and_consumed_gc(store):
    _save_embedded_utterance(store)
    assert commit_prototype(
        store, utterance_id="u1", action_id="ir.fan.power"
    ) == LEARNING_COMMITTED
    protos = store.list_prototypes(embedding_model_version="synthetic-v1-d2")
    assert len(protos) == 1
    assert protos[0].action_id == "ir.fan.power"
    assert protos[0].embedding == pytest.approx((0.6, 0.8))
    # Consumed utterance is GC'd promptly (§12.2).
    assert store.gc_expired() == 1


def test_commit_prototype_reinforces_similar_prototype(store):
    """Repeated confirmations of the same phrase must mature ONE prototype
    (§7.5 merge) — count=1 siblings would keep the direct path unreachable."""
    for uid in ("u1", "u2", "u3"):
        store.save_utterance(
            utterance_id=uid,
            transcript="關鍵善",
            duration_ms=1450,
            ttl_seconds=600,
            embedding=[0.6, 0.8],
            embedding_model_version="synthetic-v1-d2",
        )
        assert commit_prototype(
            store, utterance_id=uid, action_id="ir.fan.power"
        ) == LEARNING_COMMITTED
    protos = store.list_prototypes(embedding_model_version="synthetic-v1-d2")
    assert len(protos) == 1
    assert protos[0].confirmed_count == 3


def test_commit_prototype_keeps_distinct_phrases_separate(store):
    for uid, emb in (("u1", [0.6, 0.8]), ("u2", [1.0, 0.0])):
        store.save_utterance(
            utterance_id=uid,
            transcript="關鍵善",
            duration_ms=1450,
            ttl_seconds=600,
            embedding=emb,
            embedding_model_version="synthetic-v1-d2",
        )
        assert commit_prototype(
            store, utterance_id=uid, action_id="ir.fan.power"
        ) == LEARNING_COMMITTED
    protos = store.list_prototypes(embedding_model_version="synthetic-v1-d2")
    assert len(protos) == 2
    assert all(p.confirmed_count == 1 for p in protos)


def test_commit_prototype_requires_embedding(store):
    store.save_utterance(
        utterance_id="u1", transcript="hi", duration_ms=500, ttl_seconds=600
    )
    assert commit_prototype(
        store, utterance_id="u1", action_id="ir.fan.power"
    ) == LEARNING_SKIPPED_NO_EMBEDDING
    assert commit_prototype(
        store, utterance_id="missing", action_id="ir.fan.power"
    ) == LEARNING_SKIPPED_NO_EMBEDDING
    assert store.list_prototypes(status=None) == ()


# --- benchmark harness --------------------------------------------------------
def test_benchmark_leave_one_out_with_synthetic_backend(tmp_path):
    # Two identical recordings of the fan action (synthetic backend maps
    # identical bytes to identical vectors), one unknown short phrase.
    (tmp_path / "fan1.bin").write_bytes(b"fan-off-recording")
    (tmp_path / "fan2.bin").write_bytes(b"fan-off-recording")
    (tmp_path / "unknown.bin").write_bytes(b"totally-unrelated-speech")
    manifest = [
        {
            "sample_id": "fan-1",
            "audio_path": "fan1.bin",
            "expected": {"kind": "clarify", "selected_action_id": "ir.fan.power"},
            "session": "s1",
        },
        {
            "sample_id": "fan-2",
            "audio_path": "fan2.bin",
            "expected": {"kind": "clarify", "selected_action_id": "ir.fan.power"},
            "session": "s2",
        },
        {
            "sample_id": "unknown-1",
            "audio_path": "unknown.bin",
            "expected": {"kind": "fallback"},
        },
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    samples = load_manifest(manifest_path)
    assert len(samples) == 3
    assert samples[2].expected_action_id is None

    report = run_benchmark(
        samples,
        SyntheticEmbeddingBackend(dim=32),
        base_dir=tmp_path,
        accept_threshold=0.8,
    )
    assert report.known_total == 2
    assert report.top1_accuracy == 1.0
    assert report.unknown_total == 1
    assert report.false_accept_rate == 0.0
    assert report.to_dict()["model_version"].startswith("synthetic-v1")
