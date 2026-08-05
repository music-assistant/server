"""
Optional 1024-dim CLAP audio-embedding ANN index for sonic_similarity.

A usearch-backed on-disk index mapping a deterministic u64 label
(derived from provider+item_id) to a 1024-dim CLAP audio embedding.
A sibling JSON file holds the reverse label → (provider, item_id) map
so queries can return resolvable results.

This is the opt-in semantic-similarity engine that lives alongside the
mandatory 18-dim weighted-Euclidean index. It is enabled per-instance
via the enable_clap_index config entry; when disabled, no file is
created and no embeddings are loaded.

Storage lives under mass.storage_path as two sibling files:
  sonic_similarity_clap.usearch        — HNSW graph + f16 vectors
  sonic_similarity_clap_keys.json      — label → [provider, item_id]

The source of truth is SQLite (audio_analysis.extra_data["clap_embedding"]
populated by sonic_analysis); this index is a derived cache rebuilt
incrementally. Saves are debounced: at least SAVE_MIN_ADDS adds AND at
least SAVE_MIN_INTERVAL_SECONDS since the last save, plus an
unconditional flush on close.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .similarity import ScoredCandidate

if TYPE_CHECKING:
    import logging

    from music_assistant.mass import MusicAssistant


CLAP_EMBEDDING_DIM: int = 1024
SAVE_MIN_ADDS: int = 1000
SAVE_MIN_INTERVAL_SECONDS: float = 60.0


def derive_label(provider: str, item_id: str) -> int:
    """
    Deterministic 64-bit label for a (provider, item_id) pair.

    :param provider: Music provider domain (e.g. 'spotify').
    :param item_id: Provider-specific track identifier.

    Uses NUL as a separator so colons in item_ids can't create ambiguity
    with the provider string. First 8 bytes of SHA1 interpreted as an
    unsigned big-endian integer. Collision probability at 1M keys is
    ~3e-8; collisions are caught at insert time via the reverse map.
    """
    digest = hashlib.sha1(f"{provider}\x00{item_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


class ClapIndex:
    """CLAP-embedding search index with paired reverse-lookup map."""

    def __init__(
        self,
        mass: MusicAssistant,
        logger: logging.Logger,
        filename_stem: str = "sonic_similarity_clap",
    ) -> None:
        """Initialize; call load() before use."""
        self._mass = mass
        self._logger = logger
        self._filename_stem = filename_stem
        self._index: Any = None
        self._reverse: dict[int, tuple[str, str]] = {}
        self._dirty_adds: int = 0
        self._last_save: float = time.monotonic()
        # Serialises every worker-thread touch of _index/_reverse, teardown included.
        # Deliberately a threading.Lock: an asyncio.Lock is released the moment the
        # awaiting task is cancelled, which leaves the worker running unprotected.
        self._lock = threading.Lock()

    async def load(self) -> None:
        """Create a fresh index or restore from disk."""
        await asyncio.to_thread(self._load_sync)

    async def close(self) -> None:
        """Flush to disk and release the index."""
        await asyncio.to_thread(self._close_sync)

    def contains(self, provider: str, item_id: str) -> bool:
        """Check if a track is present in the index (cheap, in-memory)."""
        return derive_label(provider, item_id) in self._reverse

    async def add(self, provider: str, item_id: str, embedding: np.ndarray) -> None:
        """
        Insert or replace a track's CLAP embedding.

        :param provider: Music provider domain.
        :param item_id: Provider-specific track identifier.
        :param embedding: 1024-dim CLAP audio embedding.
        """
        if self._index is None:
            return
        if embedding.shape != (CLAP_EMBEDDING_DIM,):
            self._logger.warning("Ignoring embedding with unexpected shape %s", embedding.shape)
            return

        label = derive_label(provider, item_id)

        existing = self._reverse.get(label)
        if existing is not None and existing != (provider, item_id):
            self._logger.warning(
                "CLAP index hash collision for %s (conflicts with %s); skipping",
                (provider, item_id),
                existing,
            )
            return

        vec = np.asarray(embedding, dtype=np.float32)
        if not await asyncio.to_thread(self._add_sync, label, vec, provider, item_id):
            return
        self._dirty_adds += 1
        await self._maybe_flush()

    def get_embedding(self, provider: str, item_id: str) -> np.ndarray | None:
        """
        Return the stored embedding for a (provider, item_id), or None if absent.

        O(1) via the derived label; prefer this on hot paths where the provider
        is known. Use get_embedding_by_item_id only when it isn't.

        :param provider: Music provider domain.
        :param item_id: Provider-specific track identifier.
        """
        if self._index is None:
            return None
        label = derive_label(provider, item_id)
        if label not in self._reverse:
            return None
        return self._embedding_for_label(label)

    def get_embedding_by_item_id(self, item_id: str) -> tuple[str, np.ndarray] | None:
        """
        Return (provider, embedding) for a stored track, or None if absent.

        O(n) scan over the reverse map; used when only the item_id is known.

        :param item_id: Provider-specific track identifier to look up. The
            first label whose reverse-map entry matches is returned (an
            item_id is expected to be unique across providers in practice).
        """
        if self._index is None:
            return None
        for label, (prov, iid) in self._reverse.items():
            if iid != item_id:
                continue
            arr = self._embedding_for_label(label)
            return (prov, arr) if arr is not None else None
        return None

    async def search(self, embedding: np.ndarray, k: int) -> list[ScoredCandidate]:
        """
        Return the top-k nearest ScoredCandidate results.

        :param embedding: 1024-dim query embedding (from CLAP text encoder).
        :param k: Max number of neighbors to return.
        """
        if self._index is None or len(self._index) == 0:
            return []
        vec = np.asarray(embedding, dtype=np.float32).reshape(-1)
        matches = await asyncio.to_thread(self._search_sync, vec, k)
        results: list[ScoredCandidate] = []
        for label, distance in matches:
            entry = self._reverse.get(int(label))
            if entry is None:
                continue
            provider, item_id = entry
            results.append(
                ScoredCandidate(item_id=item_id, provider=provider, distance=float(distance))
            )
        return results

    async def save(self) -> None:
        """Force an unconditional flush."""
        await asyncio.to_thread(self._save_sync)
        self._dirty_adds = 0
        self._last_save = time.monotonic()

    def __len__(self) -> int:
        """Return the number of tracks currently in the index."""
        return len(self._reverse)

    @property
    def _index_path(self) -> Path:
        return Path(self._mass.storage_path) / f"{self._filename_stem}.usearch"

    @property
    def _keys_path(self) -> Path:
        return Path(self._mass.storage_path) / f"{self._filename_stem}_keys.json"

    def _load_sync(self) -> None:
        from usearch.index import (  # type: ignore[attr-defined]  # noqa: PLC0415
            Index,
            MetricKind,
            ScalarKind,
        )

        with self._lock:
            self._index = Index(
                ndim=CLAP_EMBEDDING_DIM,
                metric=MetricKind.Cos,
                dtype=ScalarKind.F16,
            )
            self._reverse = {}

            # Index + keys are paired: a populated reverse map without a backing
            # index makes contains() lie and permanently blocks rebuilds from
            # re-adding embeddings. Drop the keys file whenever the index is
            # missing or unreadable so the next rebuild starts consistent.
            index_loaded = False
            if self._index_path.exists():
                try:
                    self._index.load(str(self._index_path))
                    index_loaded = True
                    self._logger.debug(
                        "Loaded CLAP index from %s (%d vectors)",
                        self._index_path,
                        len(self._index),
                    )
                except Exception:
                    self._logger.exception("Failed to load CLAP index file, starting fresh")
                    self._index_path.unlink(missing_ok=True)
                    self._index = Index(
                        ndim=CLAP_EMBEDDING_DIM,
                        metric=MetricKind.Cos,
                        dtype=ScalarKind.F16,
                    )

            if not index_loaded:
                self._keys_path.unlink(missing_ok=True)
                return

            if self._keys_path.exists():
                try:
                    raw = json.loads(self._keys_path.read_text(encoding="utf-8"))
                    self._reverse = {int(k): (v[0], v[1]) for k, v in raw.items()}
                except Exception:
                    self._logger.exception("Failed to load CLAP keys file, starting fresh")
                    self._reverse = {}
                    self._keys_path.unlink(missing_ok=True)

    def _add_sync(self, label: int, vec: np.ndarray, provider: str, item_id: str) -> bool:
        """Insert the vector with its reverse entry, or return False if the index was released."""
        # both halves under one lock: a reverse entry without its vector makes
        # contains() lie and blocks a later rebuild from re-adding the embedding
        with self._lock:
            if self._index is None:
                return False
            if label in self._index:
                self._index.remove(label)
            self._index.add(label, vec)
            self._reverse[label] = (provider, item_id)
            return True

    def _embedding_for_label(self, label: int) -> np.ndarray | None:
        """Fetch and validate the stored 1024-dim vector for a label, or None."""
        try:
            vec = self._index.get(label)
        except Exception:
            return None
        if vec is None:
            return None
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        if arr.shape != (CLAP_EMBEDDING_DIM,):
            return None
        return arr

    def _search_sync(self, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        with self._lock:
            if self._index is None:
                return []
            res = self._index.search(vec, k)
            return list(zip(res.keys, res.distances, strict=False))

    def _save_sync(self) -> None:
        with self._lock:
            self._save_locked()

    def _close_sync(self) -> None:
        # Releasing under the same lock keeps a still-running save (its awaiting
        # task cancelled on shutdown) from writing out state torn down beneath it.
        with self._lock:
            self._save_locked()
            self._index = None
            # rebind rather than clear(): a reader iterating the map on the event
            # loop keeps the old one alive instead of failing mid-iteration
            self._reverse = {}

    def _save_locked(self) -> None:
        """Write index and keys to disk. Caller must hold self._lock."""
        if self._index is None:
            return
        # Keys first (atomic rename): a crash leaves phantom labels (silently
        # dropped) rather than orphan vectors (silently dropped from results).
        tmp_keys_path = self._keys_path.with_name(self._keys_path.name + ".tmp")
        tmp_keys_path.write_text(
            json.dumps({str(k): list(v) for k, v in self._reverse.items()}),
            encoding="utf-8",
        )
        tmp_keys_path.replace(self._keys_path)
        self._index.save(str(self._index_path))
        self._logger.debug(
            "Saved CLAP index to %s (%d vectors)",
            self._index_path,
            len(self._index),
        )

    async def _maybe_flush(self) -> None:
        """Flush if both add-count and time-since-last-save thresholds are met."""
        if self._dirty_adds < SAVE_MIN_ADDS:
            return
        if time.monotonic() - self._last_save < SAVE_MIN_INTERVAL_SECONDS:
            return
        await self.save()
