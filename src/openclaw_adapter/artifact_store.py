"""Bounded local storage for user-visible task artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unicodedata
from urllib.parse import quote
from uuid import uuid4

from .session_events import validate_identifier


DEFAULT_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_MIME_RULES = {
    "text/markdown": ("document", frozenset({".md", ".markdown"})),
    "text/html": ("document", frozenset({".html", ".htm"})),
    "text/csv": ("document", frozenset({".csv"})),
    "application/pdf": ("document", frozenset({".pdf"})),
    "image/png": ("image", frozenset({".png"})),
    "image/jpeg": ("image", frozenset({".jpg", ".jpeg"})),
}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class ArtifactError(RuntimeError):
    """Base error for artifact publication and retrieval."""


class ArtifactNotFoundError(ArtifactError):
    """The requested artifact does not exist in the specified session."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    filename: str
    content_type: str
    kind: str
    size_bytes: int
    sha256: str
    download_url: str

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "download_url": self.download_url,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ArtifactRef":
        if not isinstance(value, dict):
            raise ArtifactError("artifact reference must be an object")
        artifact_id = validate_identifier(value.get("artifact_id"), "artifact_id")
        filename = _validate_filename(value.get("filename"))
        content_type, kind = _validate_content_type(value.get("content_type"), filename)
        if value.get("kind") != kind:
            raise ArtifactError("artifact kind does not match its content type")
        size_bytes = value.get("size_bytes")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ArtifactError("artifact size must be a non-negative integer")
        sha256 = value.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ArtifactError("artifact sha256 is invalid")
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise ArtifactError("artifact sha256 is invalid") from exc
        download_url = value.get("download_url")
        if not isinstance(download_url, str) or not download_url.startswith("/api/command/artifacts/"):
            raise ArtifactError("artifact download URL is invalid")
        return cls(
            artifact_id=artifact_id,
            filename=filename,
            content_type=content_type,
            kind=kind,
            size_bytes=size_bytes,
            sha256=sha256,
            download_url=download_url,
        )


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    ref: ArtifactRef
    path: Path


class ArtifactStore:
    """Copy approved files into session-scoped storage and return opaque references."""

    def __init__(self, root_dir: str | Path, *, max_file_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES) -> None:
        if max_file_bytes <= 0:
            raise ValueError("artifact size limit must be positive")
        self.root = Path(root_dir).expanduser().resolve()
        self.max_file_bytes = max_file_bytes
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.root), threading.RLock())

    def publish_file(
        self,
        session_id: str,
        path: str | Path,
        *,
        filename: str | None = None,
        content_type: str,
    ) -> ArtifactRef:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ArtifactError("artifact source must be a regular file")
        resolved_name = _validate_filename(filename or source.name)
        mime, kind = _validate_content_type(content_type, resolved_name)
        size = source.stat().st_size
        if size > self.max_file_bytes:
            raise ArtifactError(f"artifact exceeds the {self.max_file_bytes}-byte limit")
        _validate_file_signature(source, mime)

        sid = validate_identifier(session_id, "session_id")
        artifact_id = uuid4().hex
        destination_dir = self.root / sid / artifact_id
        destination = destination_dir / resolved_name
        with self._lock:
            destination_dir.mkdir(parents=True, exist_ok=False)
            try:
                with source.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                digest = _sha256(destination)
                ref = ArtifactRef(
                    artifact_id=artifact_id,
                    filename=resolved_name,
                    content_type=mime,
                    kind=kind,
                    size_bytes=size,
                    sha256=digest,
                    download_url=_download_url(sid, artifact_id, resolved_name),
                )
                _write_metadata(destination_dir, ref)
                return ref
            except Exception:
                shutil.rmtree(destination_dir, ignore_errors=True)
                raise

    def publish_bytes(
        self,
        session_id: str,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise ArtifactError("artifact data must be bytes")
        if len(data) > self.max_file_bytes:
            raise ArtifactError(f"artifact exceeds the {self.max_file_bytes}-byte limit")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".artifact-", dir=self.root)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return self.publish_file(
                session_id,
                temp_path,
                filename=filename,
                content_type=content_type,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def open(self, session_id: str, artifact_id: str) -> StoredArtifact:
        sid = validate_identifier(session_id, "session_id")
        aid = validate_identifier(artifact_id, "artifact_id")
        artifact_dir = self.root / sid / aid
        with self._lock:
            try:
                metadata = json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))
                ref = ArtifactRef.from_dict(metadata)
                path = artifact_dir / ref.filename
                stat = path.stat()
            except (FileNotFoundError, OSError, ValueError, ArtifactError) as exc:
                raise ArtifactNotFoundError("artifact not found") from exc
            if (
                ref.artifact_id != aid
                or ref.download_url != _download_url(sid, aid, ref.filename)
                or not path.is_file()
                or stat.st_size != ref.size_bytes
                or _sha256(path) != ref.sha256
            ):
                raise ArtifactNotFoundError("artifact failed integrity validation")
            return StoredArtifact(ref=ref, path=path)

    def clear_session(self, session_id: str) -> None:
        sid = validate_identifier(session_id, "session_id")
        with self._lock:
            shutil.rmtree(self.root / sid, ignore_errors=True)

    def discard(self, session_id: str, artifact_id: str) -> None:
        """Remove one unpublished or rolled-back artifact."""
        sid = validate_identifier(session_id, "session_id")
        aid = validate_identifier(artifact_id, "artifact_id")
        with self._lock:
            shutil.rmtree(self.root / sid / aid, ignore_errors=True)


def _validate_filename(value: object) -> str:
    if not isinstance(value, str):
        raise ArtifactError("artifact filename must be a string")
    filename = unicodedata.normalize("NFC", value).strip()
    if (
        not filename
        or filename in {".", ".."}
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 for char in filename)
        or len(filename.encode("utf-8")) > 180
    ):
        raise ArtifactError("artifact filename is invalid")
    return filename


def _validate_content_type(value: object, filename: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ArtifactError("artifact content type must be a string")
    mime = value.split(";", 1)[0].strip().lower()
    rule = _MIME_RULES.get(mime)
    if rule is None:
        raise ArtifactError(f"unsupported artifact content type: {mime or '(empty)'}")
    kind, suffixes = rule
    if Path(filename).suffix.lower() not in suffixes:
        raise ArtifactError("artifact filename extension does not match its content type")
    return mime, kind


def _validate_file_signature(path: Path, content_type: str) -> None:
    with path.open("rb") as handle:
        head = handle.read(8)
    if content_type == "application/pdf" and not head.startswith(b"%PDF-"):
        raise ArtifactError("artifact is not a valid PDF payload")
    if content_type == "image/png" and head != b"\x89PNG\r\n\x1a\n":
        raise ArtifactError("artifact is not a valid PNG payload")
    if content_type == "image/jpeg" and not head.startswith(b"\xff\xd8\xff"):
        raise ArtifactError("artifact is not a valid JPEG payload")
    if content_type.startswith("text/"):
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactError("text artifacts must use UTF-8") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_url(session_id: str, artifact_id: str, filename: str) -> str:
    return (
        f"/api/command/artifacts/{quote(artifact_id, safe='')}/{quote(filename, safe='')}"
        f"?session_id={quote(session_id, safe='')}"
    )


def _write_metadata(directory: Path, ref: ArtifactRef) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp") as tmp:
        json.dump(ref.to_dict(), tmp, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_path = Path(tmp.name)
    os.replace(temp_path, directory / "metadata.json")
