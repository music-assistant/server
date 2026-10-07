"""Tests for the Snapcast Unix-socket server payload serialization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage
from music_assistant_models.media_items.metadata import IMAGE_PROXY_ID_RESOLVER

from music_assistant.providers.snapcast.socket_server import SnapcastSocketServer


def _fake_compute_image_id(provider: str, path: str) -> str:
    """Return a deterministic marker id so assertions can pin the (provider, path)."""
    return f"id::{provider}::{path}"


def _make_server(socket_path: Path) -> SnapcastSocketServer:
    """Build a socket server whose mass resolves image ids via the fake above."""
    mass = MagicMock()
    mass.metadata.compute_image_id = _fake_compute_image_id
    return SnapcastSocketServer(
        mass=mass,
        queue_id="queue-1",
        socket_path=str(socket_path),
        streamserver_ip="127.0.0.1",
        streamserver_port=8097,
    )


def test_serialize_injects_proxy_id_on_images(tmp_path: Path) -> None:
    """A serialized MediaItemImage carries the proxy_id the control script builds its URL from."""
    server = _make_server(tmp_path / "control.sock")
    image = MediaItemImage(type=ImageType.THUMB, path="/covers/a.jpg", provider="filesystem")

    result = server._serialize(image)

    assert result["proxy_id"] == "id::filesystem::/covers/a.jpg"


def test_serialize_passes_through_non_models(tmp_path: Path) -> None:
    """Plain data without a to_dict is returned unchanged."""
    server = _make_server(tmp_path / "control.sock")
    assert server._serialize("ok") == "ok"
    assert server._serialize({"a": 1}) == {"a": 1}


def test_serialize_resets_resolver(tmp_path: Path) -> None:
    """The imageproxy resolver must not leak onto the context after serialization."""
    server = _make_server(tmp_path / "control.sock")
    server._serialize(MediaItemImage(type=ImageType.THUMB, path="/x.jpg", provider="filesystem"))
    assert IMAGE_PROXY_ID_RESOLVER.get() is None
