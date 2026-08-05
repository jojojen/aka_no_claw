# Web Artifact Delivery Implementation Plan

Status: Current
Owner area: command-bridge / conversation-runtime
Last reviewed: 2026-08-05

## 1. Read This First

This document owns the output-artifact contract for the Web command bridge.
Read it before you add a task that returns a file.

Correctness means general correctness. Do not add a response field for one task or one filename. A producer must publish through `ArtifactStore`. A consumer must render the common `ArtifactRef` contract.

If the correct general design is unclear, inspect current primary documentation and proven implementations before you change code. Do not add a task-specific exception as a substitute for a contract.

## 2. Goal

An ordinary task can return text and zero or more files. The first supported file classes are:

- UTF-8 Markdown
- UTF-8 CSV
- PDF
- PNG
- JPEG

The Web UI displays image previews. It gives download links for documents. The result remains available after a page reload, a network interruption, or a bridge restart while the session and artifact are retained.

The seller reputation snapshot is the first production flow that uses the contract. It returns its existing PDF and PNG output. The artifact layer does not contain seller-specific rules.

## 3. Contract

`ArtifactRef` is metadata only. Do not put file bytes or data URLs in a JSON response or event.

```json
{
  "artifact_id": "opaque identifier",
  "filename": "report.pdf",
  "content_type": "application/pdf",
  "kind": "document",
  "size_bytes": 12345,
  "sha256": "64 lowercase hexadecimal characters",
  "download_url": "/api/command/artifacts/...?..."
}
```

The bridge can include `artifacts` in these payloads:

- blocking `WebCommandResponse`
- NDJSON `done` event
- background job terminal snapshot
- durable `assistant.message` event
- projected session message

The field is optional. An absent field and an empty list have the same meaning for older consumers.

## 4. Data Flow

1. A tool writes a result to a temporary file or provides bytes.
2. The producer calls `CommandBridge.publish_artifact`.
3. `ArtifactStore` validates the name, MIME type, extension, signature, encoding, and size.
4. The store copies the file into a session-scoped directory and writes metadata atomically.
5. The command result carries `ArtifactRef`. Durable jobs and session events persist this reference.
6. The Web client validates the reference before it stores or renders the value.
7. The browser downloads the file from the session-bound artifact route.

The source temporary file remains owned by the producer. The producer must delete it after publication when it is temporary.

## 5. Storage and Security Rules

- The default root is `.openclaw_tmp/web_artifacts/`.
- The default maximum size is 20 MiB per file.
- A file belongs to one validated session identifier.
- An artifact identifier is opaque.
- A filename cannot contain a path component or control character.
- The MIME type must match the filename extension.
- PDF, PNG, and JPEG files must have the expected file signature.
- Markdown and CSV files must be valid UTF-8.
- Retrieval checks the stored size and SHA-256 digest.
- A download requires both the artifact identifier and its session identifier.
- The route sends `X-Content-Type-Options: nosniff` and `Cache-Control: private, no-store`.
- Session clearing removes the session artifact directory.

The initial closed MIME set is intentional. Extend the MIME table and its tests when a new file class has a real producer. Do not accept arbitrary MIME values.

## 6. Producer API

Use one of these operations:

```python
ref = bridge.publish_artifact(
    session_id=request.session_id,
    path=output_path,
    content_type="application/pdf",
)
```

```python
ref = bridge.artifact_store.publish_bytes(
    request.session_id,
    markdown.encode("utf-8"),
    filename="report.md",
    content_type="text/markdown",
)
```

Return the reference through `WebCommandResponse.artifacts` or `_JobResult.artifacts`. Do not add a new task-specific download route.

## 7. Consumer Rules

The Web consumer must:

- reject malformed references at runtime;
- render images with a preview and an open link;
- render documents with a download link;
- keep artifact references in session restoration and event replay;
- copy terminal job artifacts into the final assistant message;
- treat unknown future MIME types as unsupported data and omit them safely.

The Web consumer must not infer a file type from a user-controlled filename alone.

## 8. Failure Semantics

Publication is fail-closed. An invalid or oversized artifact makes the producer fail. The bridge does not return a reference to a file that failed validation.

Retrieval returns `404` for a missing file, a wrong session, a wrong filename, invalid metadata, or an integrity mismatch. These cases do not reveal whether an artifact exists in another session.

A text answer can still exist without artifacts. A producer decides whether missing file output makes its whole result fail or become a text-only partial result. Record that choice in the producer's own contract.

## 9. Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `OPENCLAW_WEB_ARTIFACT_DIR` | `.openclaw_tmp/web_artifacts` | Artifact storage root. |
| `OPENCLAW_WEB_ARTIFACT_MAX_BYTES` | `20971520` | Maximum bytes per file. |

## 10. Verification

Backend tests must cover:

- all allowed MIME types;
- invalid names, MIME mismatches, signatures, encodings, and sizes;
- session isolation and digest verification;
- response, stream, job, event, and session reference propagation;
- safe HTTP headers and filename handling;
- producer temporary-file cleanup.

Frontend tests must cover:

- runtime reference validation;
- document download links;
- image previews;
- blocking, streaming, polling, and restored-session propagation;
- type checking and production build.

Run the repository verification matrix after focused tests pass. Then restart the stack through the supported restart control and perform a live Web check.

## 11. Rollout Status

- [x] Add the bounded session artifact store.
- [x] Add `ArtifactRef` to response and event contracts.
- [x] Persist references in jobs and session events.
- [x] Add the session-bound download route.
- [x] Add Web reference validation and artifact cards.
- [x] Use durable background execution for seller reputation output.
- [x] Publish the existing seller reputation PDF and PNG.
- [ ] Add Markdown and CSV producers when a task needs those outputs.
- [ ] Add a retention policy that is independent from explicit session clearing.
- [ ] Add an object-store adapter if the bridge moves off one host.

## 12. Cross-Repository Files

Backend owner files:

- `src/openclaw_adapter/artifact_store.py`
- `src/openclaw_adapter/command_bridge_models.py`
- `src/openclaw_adapter/command_bridge.py`
- `src/openclaw_adapter/command_bridge_server.py`
- `src/openclaw_adapter/run_recorder.py`
- `src/openclaw_adapter/session_projection.py`

Web consumer files:

- `frontend/src/types/command.ts`
- `frontend/src/artifacts.ts`
- `frontend/src/components/ArtifactList.tsx`
- `frontend/src/components/MessageBubble.tsx`
- `frontend/src/App.tsx`

