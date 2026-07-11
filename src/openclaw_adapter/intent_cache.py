"""Semantic intent cache for low-latency voice assistant routing.

Stores embeddings of text → intent mappings in SQLite with:
- Exact hash lookup (no embedding call)
- Semantic similarity lookup with cosine distance
- Digit guard to prevent cross-parameter confusion (e.g. "volume 50" ≠ "volume 70")
- TTL-based expiry and max-entry eviction
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

logger = logging.getLogger(__name__)


class OllamaEmbedClient:
    """POST client for Ollama /api/embed endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: int = 30,
        keep_alive: str = "30m",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.keep_alive = keep_alive

    def _url(self) -> str:
        """Normalize endpoint to /api/embed (mimic OllamaTextClient._url pattern)."""
        path = self.endpoint
        if path.endswith("/api/embed"):
            return path
        if path.endswith("/api/generate"):
            return path.replace("/api/generate", "/api/embed")
        if path.endswith("/api"):
            return f"{path}/embed"
        return f"{path}/api/embed"

    def embed(self, text: str) -> list[float]:
        """Embed text; raise RuntimeError on HTTP error or malformed response."""
        try:
            payload = {
                "model": self.model,
                "input": [text],
                "keep_alive": self.keep_alive,
            }
            request = Request(
                self._url(),
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            if not isinstance(parsed.get("embeddings"), list):
                raise RuntimeError(
                    f"Ollama embed response missing embeddings array: {type(parsed.get('embeddings'))}"
                )
            embeddings_list = parsed["embeddings"]
            if not embeddings_list or not isinstance(embeddings_list[0], list):
                raise RuntimeError(
                    f"Ollama embed response embeddings[0] type: {type(embeddings_list[0] if embeddings_list else None)}"
                )
            return embeddings_list[0]
        except HTTPError as exc:
            raise RuntimeError(f"Ollama embed HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"Ollama embed request failed: {exc.reason}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Ollama embed response parse failed: {exc}") from exc


class SemanticIntentCache:
    """SQLite-backed semantic intent cache with cosine similarity lookup."""

    def __init__(
        self,
        db_path: str,
        *,
        embed_fn: Callable[[str], list[float]],
        similarity_threshold: float = 0.93,
        ttl_seconds: int = 604800,
        max_entries: int = 500,
    ) -> None:
        self.db_path = db_path
        self.embed_fn = embed_fn
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create table if not exists."""
        try:
            with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intent_cache (
                      namespace TEXT NOT NULL,
                      text_hash TEXT NOT NULL,
                      text TEXT NOT NULL,
                      embedding BLOB NOT NULL,
                      intent_json TEXT NOT NULL,
                      created_at REAL NOT NULL,
                      last_hit REAL NOT NULL,
                      PRIMARY KEY (namespace, text_hash)
                    )
                    """
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("intent_cache: failed to init DB: %s", exc)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """NFKC normalize and strip."""
        return unicodedata.normalize("NFKC", text).strip()

    @staticmethod
    def _text_hash(text: str) -> str:
        """SHA256 of normalized text."""
        normalized = SemanticIntentCache._normalize_text(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_digits(text: str) -> list[str]:
        """Extract all digit sequences for parameter-guard comparison."""
        return re.findall(r"\d+", text)

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity between two float vectors."""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a < 1e-9 or norm_b < 1e-9:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def lookup(self, namespace: str, text: str) -> tuple[dict, str] | None:
        """Lookup (intent_dict, "exact"|"semantic") or None."""
        try:
            normalized = self._normalize_text(text)
            text_hash = self._text_hash(text)
            query_digits = self._extract_digits(normalized)

            with self._lock:
                with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                    now = time.time()

                    # Exact hit: same namespace + text_hash. An expired exact row
                    # falls through to the semantic scan (other rows may be valid).
                    cursor = conn.execute(
                        """
                        SELECT intent_json, last_hit, created_at
                        FROM intent_cache
                        WHERE namespace = ? AND text_hash = ?
                        """,
                        (namespace, text_hash),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        intent_json, last_hit, created_at = row
                        if created_at >= now - self.ttl_seconds:
                            try:
                                intent_dict = json.loads(intent_json)
                                conn.execute(
                                    "UPDATE intent_cache SET last_hit = ? WHERE namespace = ? AND text_hash = ?",
                                    (now, namespace, text_hash),
                                )
                                conn.commit()
                                return (intent_dict, "exact")
                            except (json.JSONDecodeError, TypeError):
                                pass

            # Embed outside the lock: this is an HTTP call (up to timeout_seconds)
            # and must not block concurrent exact-hit lookups.
            try:
                query_embedding = np.array(self.embed_fn(normalized), dtype=np.float32)
            except Exception as exc:
                logger.warning("intent_cache: embed_fn failed: %s", exc)
                return None

            with self._lock:
                with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                    now = time.time()
                    cursor = conn.execute(
                        """
                        SELECT text_hash, text, embedding, intent_json, last_hit, created_at
                        FROM intent_cache
                        WHERE namespace = ?
                        ORDER BY last_hit DESC
                        """,
                        (namespace,),
                    )
                    best_row = None
                    best_score = -1.0

                    for row in cursor:
                        hash_val, cached_text, embedding_buf, intent_json, last_hit, created_at = row
                        if created_at < now - self.ttl_seconds:
                            continue

                        # Digit guard: numbers must match exactly
                        cached_digits = self._extract_digits(cached_text)
                        if query_digits != cached_digits:
                            continue

                        # Containment guard: when one text strictly contains the
                        # other, the longer one may carry extra steps (e.g.
                        # "開燈然後放歌" contains cached "開燈") — embedding
                        # similarity stays deceptively high for such pairs, so
                        # refuse the semantic hit and let the LLM decide.
                        if normalized != cached_text and (
                            normalized in cached_text or cached_text in normalized
                        ):
                            continue

                        try:
                            cached_embedding = np.frombuffer(
                                embedding_buf, dtype=np.float32
                            ).copy()
                            score = self._cosine_similarity(query_embedding, cached_embedding)
                            if score > best_score:
                                best_score = score
                                best_row = (hash_val, intent_json, last_hit)
                        except Exception:
                            continue

                    if best_score >= self.similarity_threshold and best_row is not None:
                        hash_val, intent_json, last_hit = best_row
                        try:
                            intent_dict = json.loads(intent_json)
                            conn.execute(
                                "UPDATE intent_cache SET last_hit = ? WHERE namespace = ? AND text_hash = ?",
                                (now, namespace, hash_val),
                            )
                            conn.commit()
                            return (intent_dict, "semantic")
                        except (json.JSONDecodeError, TypeError):
                            pass

                    return None
        except Exception as exc:
            logger.warning("intent_cache.lookup failed: %s", exc, exc_info=True)
            return None

    def store(self, namespace: str, text: str, intent: dict) -> None:
        """Store embedding + intent for this text."""
        try:
            normalized = self._normalize_text(text)
            text_hash = self._text_hash(text)

            try:
                embedding = np.array(self.embed_fn(normalized), dtype=np.float32)
            except Exception as exc:
                logger.warning("intent_cache.store: embed_fn failed: %s", exc)
                return

            with self._lock:
                with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
                    now = time.time()
                    intent_json = json.dumps(intent)
                    embedding_blob = embedding.tobytes()

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO intent_cache
                        (namespace, text_hash, text, embedding, intent_json, created_at, last_hit)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            namespace,
                            text_hash,
                            normalized,
                            embedding_blob,
                            intent_json,
                            now,
                            now,
                        ),
                    )

                    # Evict oldest by last_hit if over max_entries
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM intent_cache WHERE namespace = ?", (namespace,)
                    )
                    count = cursor.fetchone()[0]
                    if count > self.max_entries:
                        conn.execute(
                            """
                            DELETE FROM intent_cache
                            WHERE namespace = ? AND text_hash IN (
                              SELECT text_hash FROM intent_cache
                              WHERE namespace = ?
                              ORDER BY last_hit ASC
                              LIMIT ?
                            )
                            """,
                            (namespace, namespace, count - self.max_entries),
                        )

                    conn.commit()
        except Exception as exc:
            logger.warning("intent_cache.store failed: %s", exc, exc_info=True)


def build_intent_cache_from_settings(settings) -> SemanticIntentCache | None:
    """Build cache if enabled and local endpoint available."""
    if not getattr(settings, "openclaw_intent_cache_enabled", True):
        return None
    endpoint = getattr(settings, "openclaw_local_text_endpoint", None)
    if not endpoint or not endpoint.strip():
        return None

    try:
        client = OllamaEmbedClient(
            endpoint=endpoint,
            model=getattr(settings, "openclaw_local_embed_model", "bge-m3"),
        )
        return SemanticIntentCache(
            getattr(settings, "openclaw_intent_cache_path", "data/intent_cache.sqlite3"),
            embed_fn=client.embed,
            similarity_threshold=getattr(
                settings, "openclaw_intent_cache_threshold", 0.93
            ),
            ttl_seconds=getattr(settings, "openclaw_intent_cache_ttl_seconds", 604800),
        )
    except Exception as exc:
        logger.warning("intent_cache: failed to build from settings: %s", exc)
        return None
