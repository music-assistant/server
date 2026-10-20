"""Shared on-disk writing for the plugin's usearch indexes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def save_index(index: Any, path: Path) -> None:
    """
    Write a usearch index to disk. Must be called from a worker thread.

    :param index: The usearch index to serialize.
    :param path: Destination file, overwritten in place.
    """
    # usearch's own save(path) holds the GIL for the entire write, so handing it to
    # asyncio.to_thread still freezes the event loop for the duration. Passing no
    # path returns the serialized bytes instead of writing them, which costs a
    # fraction of that; the bytes then go out through plain file I/O, which does
    # release the GIL.
    path.write_bytes(index.save())
