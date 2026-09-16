"""
Packed on-disk codec for AudioAnalysisData.

A record is stored as a small JSON header (every scalar field, ``extra_data`` and an
index of the arrays) plus one binary payload holding the arrays back to back, so a
fully analysed track costs tens of kilobytes instead of hundreds and a read is one
page fetch plus zero-copy slicing. Only this module knows the layout.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Final

import numpy as np

from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.audio_analysis import AudioAnalysisData

LOGGER = logging.getLogger(__name__)

# list[float] fields of the model, in declaration order; the payload is packed in this order
ARRAY_FIELDS: Final[tuple[str, ...]] = tuple(
    field.name
    for field in dataclasses.fields(AudioAnalysisData)
    if field.type == "list[float] | None"
)
# envelopes (0..1 or Hz, 1800 bins) and the unit-norm embedding tolerate float16; beat
# timestamps need float32 (float16 resolves only 0.125 s at five minutes)
F16_FIELDS: Final[frozenset[str]] = frozenset(ARRAY_FIELDS) - {"beats", "downbeats"}
_DTYPES: Final[dict[str, np.dtype[Any]]] = {"f16": np.dtype("<f2"), "f32": np.dtype("<f4")}
_ARRAY_FIELD_SET: Final[frozenset[str]] = frozenset(ARRAY_FIELDS)


def encode(analysis: AudioAnalysisData) -> tuple[str, bytes]:
    """
    Pack an analysis record into its stored form.

    :param analysis: The record to store.
    :returns: JSON header (scalars, extra_data and the array index) and the binary payload.
    """
    doc = {key: value for key, value in analysis.to_dict().items() if value is not None}
    index: list[list[Any]] = []
    parts: list[bytes] = []
    offset = 0
    for name in ARRAY_FIELDS:
        values = doc.pop(name, None)
        if values is None:
            continue
        tag = "f16" if name in F16_FIELDS else "f32"
        raw = np.asarray(values, dtype=_DTYPES[tag]).tobytes()
        index.append([name, tag, offset, len(raw)])
        parts.append(raw)
        offset += len(raw)
    doc["arrays"] = index
    return json_dumps(doc), b"".join(parts)


def decode(header: str | bytes, payload: bytes) -> AudioAnalysisData:
    """
    Unpack a stored record.

    :param header: JSON header as written by :func:`encode`.
    :param payload: Binary payload as written by :func:`encode`.
    :raises ValueError: When an array slice in the header is truncated in the payload.
    """
    doc = json_loads(header)
    view = memoryview(payload)
    for name, tag, offset, nbytes in doc.pop("arrays", []):
        if name not in _ARRAY_FIELD_SET:
            LOGGER.warning("Ignoring unknown packed analysis array %s", name)
            continue
        chunk = view[offset : offset + nbytes]
        if len(chunk) != nbytes:
            raise ValueError(f"packed analysis array {name} is truncated")
        arr = np.frombuffer(chunk, dtype=_DTYPES[tag])
        doc[name] = (arr.astype(np.float32) if tag == "f16" else arr).tolist()
    return AudioAnalysisData.from_dict(doc)
