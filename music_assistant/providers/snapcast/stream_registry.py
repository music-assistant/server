"""Central registry for Music Assistant managed Snapcast streams."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ma_stream import SnapcastMAStream


class SnapcastStreamRegistry:
    """Central lookup for Snapcast MA streams by internal and external references."""

    def __init__(self, streams_by_name: dict[str, SnapcastMAStream] | None = None) -> None:
        """Initialize the registry."""
        self._streams_by_name = streams_by_name if streams_by_name is not None else {}

    @property
    def streams_by_name(self) -> dict[str, SnapcastMAStream]:
        """Return the underlying stream mapping keyed by internal stream name."""
        return self._streams_by_name

    def register(self, stream: SnapcastMAStream) -> None:
        """Register or replace a stream by its internal stream name."""
        self._streams_by_name[stream.stream_name] = stream

    def unregister(self, stream_name: str) -> SnapcastMAStream | None:
        """Remove a stream from the registry by its internal stream name."""
        return self._streams_by_name.pop(stream_name, None)

    def clear(self) -> None:
        """Remove all tracked streams from the registry."""
        self._streams_by_name.clear()

    def get_by_stream_name(self, stream_name: str) -> SnapcastMAStream | None:
        """Return a tracked stream by its internal stream name."""
        return self._streams_by_name.get(stream_name)

    def resolve(self, ref: str | None) -> SnapcastMAStream | None:
        """Resolve any known stream reference to a tracked stream."""
        matches = self.resolve_all(ref)
        return matches[0] if matches else None

    def resolve_all(self, ref: str | None) -> tuple[SnapcastMAStream, ...]:
        """Resolve all tracked streams matching the given reference."""
        if ref is None:
            return ()

        matches: list[SnapcastMAStream] = []

        if stream := self._streams_by_name.get(ref):
            matches.append(stream)

        for stream in self._streams_by_name.values():
            if stream in matches:
                continue
            if getattr(stream, "stream_id", None) == ref:
                matches.append(stream)
                continue
            if getattr(stream, "stream_display_name", None) == ref:
                matches.append(stream)
                continue
            if getattr(stream, "source_id", None) == ref:
                matches.append(stream)
                continue
            if getattr(stream, "queue_id", None) == ref:
                matches.append(stream)
        return tuple(matches)

    def all(self) -> tuple[SnapcastMAStream, ...]:
        """Return all tracked streams."""
        return tuple(self._streams_by_name.values())

    def names(self) -> tuple[str, ...]:
        """Return the internal names of all tracked streams."""
        return tuple(self._streams_by_name.keys())
