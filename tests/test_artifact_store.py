from __future__ import annotations

from pathlib import Path
import json

import pytest

from openclaw_adapter.artifact_store import (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
)


def test_publish_and_open_markdown_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.publish_bytes(
        "session-1",
        "# 報告\n".encode(),
        filename="report.md",
        content_type="text/markdown",
    )

    stored = store.open("session-1", ref.artifact_id)

    assert stored.path.read_text(encoding="utf-8") == "# 報告\n"
    assert stored.ref == ref
    assert ref.kind == "document"
    assert ref.download_url.endswith(
        f"/{ref.artifact_id}/report.md?session_id=session-1"
    )
    assert ArtifactRef.from_dict(ref.to_dict()) == ref


@pytest.mark.parametrize(
    ("filename", "content_type", "data", "kind"),
    [
        ("report.markdown", "text/markdown", b"# report\n", "document"),
        ("data.csv", "text/csv", b"name,value\na,1\n", "document"),
        ("report.pdf", "application/pdf", b"%PDF-1.7\n", "document"),
        ("preview.png", "image/png", b"\x89PNG\r\n\x1a\n", "image"),
        ("preview.jpg", "image/jpeg", b"\xff\xd8\xff\xe0", "image"),
    ],
)
def test_publish_accepts_each_supported_content_type(
    tmp_path: Path,
    filename: str,
    content_type: str,
    data: bytes,
    kind: str,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    ref = store.publish_bytes(
        "session-1", data, filename=filename, content_type=content_type
    )

    assert ref.kind == kind
    assert store.open("session-1", ref.artifact_id).path.read_bytes() == data


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("../report.md", "text/markdown", b"text"),
        ("report.pdf", "text/markdown", b"text"),
        ("report.exe", "application/octet-stream", b"text"),
        ("report.pdf", "application/pdf", b"not a pdf"),
        ("image.png", "image/png", b"not a png"),
    ],
)
def test_publish_rejects_unsafe_or_mismatched_artifact(
    tmp_path: Path, filename: str, content_type: str, data: bytes
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactError):
        store.publish_bytes(
            "session-1", data, filename=filename, content_type=content_type
        )


def test_open_is_bound_to_session_and_checks_integrity(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.publish_bytes(
        "session-1", b"a,b\n1,2\n", filename="data.csv", content_type="text/csv"
    )

    with pytest.raises(ArtifactNotFoundError):
        store.open("session-2", ref.artifact_id)

    stored = store.open("session-1", ref.artifact_id)
    stored.path.write_bytes(b"changed")
    with pytest.raises(ArtifactNotFoundError):
        store.open("session-1", ref.artifact_id)


def test_open_rejects_metadata_that_redirects_the_reference(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.publish_bytes(
        "session-1", b"# report\n", filename="report.md", content_type="text/markdown"
    )
    metadata_path = (
        tmp_path / "artifacts" / "session-1" / ref.artifact_id / "metadata.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["download_url"] = (
        f"/api/command/artifacts/{ref.artifact_id}/report.md?session_id=session-2"
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError):
        store.open("session-1", ref.artifact_id)


def test_publish_enforces_size_limit_and_clear_removes_session(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", max_file_bytes=4)
    with pytest.raises(ArtifactError):
        store.publish_bytes(
            "session-1", b"12345", filename="data.csv", content_type="text/csv"
        )

    ref = store.publish_bytes(
        "session-1", b"1234", filename="data.csv", content_type="text/csv"
    )
    store.clear_session("session-1")
    with pytest.raises(ArtifactNotFoundError):
        store.open("session-1", ref.artifact_id)


def test_discard_removes_one_artifact_without_removing_others(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.publish_bytes(
        "session-1", b"first", filename="first.md", content_type="text/markdown"
    )
    second = store.publish_bytes(
        "session-1", b"second", filename="second.md", content_type="text/markdown"
    )

    store.discard("session-1", first.artifact_id)

    with pytest.raises(ArtifactNotFoundError):
        store.open("session-1", first.artifact_id)
    assert store.open("session-1", second.artifact_id).path.read_bytes() == b"second"
