"""Tests for the source-image cache in the images helper."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from base64 import b64encode
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.client_exceptions import ClientError
from aiohttp.test_utils import TestServer
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderIconVariant
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from PIL import Image
from yarl import URL

from music_assistant.helpers import images
from music_assistant.helpers.images import (
    _SOURCE_CACHE_TTL,
    create_thumb_hash,
    detect_provider_icons,
    get_image_data,
    get_image_thumb,
    get_image_thumb_path,
    invalidate_cached_image,
    load_provider_icon,
)
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player_provider import PlayerProvider
from tests.common import collect_loop_errors

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from music_assistant.mass import MusicAssistant

RESOLVER = "music_assistant.helpers.security.resolve_hostname"


@pytest.fixture(autouse=True)
def _reset_image_caches() -> Iterator[None]:
    """Isolate the module-level image caches between tests."""
    images._thumb_memory_cache.clear()
    images._source_memory_cache.clear()
    images._failed_sources.clear()
    yield
    images._thumb_memory_cache.clear()
    images._source_memory_cache.clear()
    images._failed_sources.clear()


@pytest.fixture
def fetch_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Spy on origin fetches; returns the list of (provider, path) fetch calls."""
    calls: list[tuple[str, str]] = []
    real_fetch = images._fetch_source_image

    async def counting_fetch(
        mass: MusicAssistant, path_or_url: str, provider: str, depth: int
    ) -> tuple[bytes, bool]:
        calls.append((provider, path_or_url))
        return await real_fetch(mass, path_or_url, provider, depth)

    monkeypatch.setattr(images, "_fetch_source_image", counting_fetch)
    return calls


def _make_png_bytes(color: tuple[int, int, int] = (200, 30, 30), size: int = 400) -> bytes:
    """Create raw PNG bytes of a solid-color square."""
    img = Image.new("RGB", (size, size), color)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_png_file(tmp_path: Path, name: str = "art.png") -> str:
    """Create a PNG file on disk and return its absolute path."""
    filepath = tmp_path / name
    filepath.write_bytes(_make_png_bytes())
    return str(filepath)


async def test_multiple_thumb_sizes_fetch_source_once(
    mass_minimal: MusicAssistant, tmp_path: Path, fetch_calls: list[tuple[str, str]]
) -> None:
    """Generating several thumb variants of one image must fetch the source once."""
    image_path = _make_png_file(tmp_path)
    thumb_80 = await get_image_thumb(mass_minimal, image_path, 80, "builtin")
    thumb_256 = await get_image_thumb(mass_minimal, image_path, 256, "builtin")
    jpeg_flat = await get_image_thumb(
        mass_minimal, image_path, 256, "builtin", image_format="JPEG", flatten_transparency=True
    )
    assert thumb_80
    assert thumb_256
    assert jpeg_flat
    assert fetch_calls == [("builtin", image_path)]


async def test_thumb_path_reuses_existing_cache_file(
    mass_minimal: MusicAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated path lookup returns the same existing file without rewriting it."""
    image_path = _make_png_file(tmp_path)
    first_path = await get_image_thumb_path(mass_minimal, image_path, 256, "builtin")

    async def unexpected_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("existing thumbnail was rewritten")

    monkeypatch.setattr(images, "_write_thumb_to_disk", unexpected_write)
    second_path = await get_image_thumb_path(mass_minimal, image_path, 256, "builtin")

    assert second_path == first_path
    assert Path(second_path).is_file()


async def test_thumb_path_restores_missing_disk_file_from_memory(
    mass_minimal: MusicAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memory hit recreates its missing disk file without regenerating the thumbnail."""
    image_path = _make_png_file(tmp_path)
    thumb_data = await get_image_thumb(mass_minimal, image_path, 256, "builtin")
    thumb_hash = create_thumb_hash("builtin", image_path)
    cache_filename = images._thumb_cache_filename(thumb_hash, 256, "PNG")
    cache_path = Path(mass_minimal.cache_path, "thumbnails", cache_filename)
    cache_path.unlink()

    async def unexpected_generate(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("memory-cached thumbnail was regenerated")

    monkeypatch.setattr(images, "_generate_and_cache_thumb", unexpected_generate)
    restored_path = await get_image_thumb_path(mass_minimal, image_path, 256, "builtin")

    assert restored_path == str(cache_path.resolve())
    assert cache_path.read_bytes() == thumb_data


async def test_thumb_path_surfaces_cache_write_error(
    mass_minimal: MusicAssistant, tmp_path: Path
) -> None:
    """A path request raises when the thumbnail cache cannot be persisted."""
    invalid_cache_path = tmp_path / "not-a-directory"
    invalid_cache_path.write_text("file")
    mass_minimal.cache_path = str(invalid_cache_path)
    image_data = b64encode(_make_png_bytes()).decode()

    with pytest.raises(NotADirectoryError, match="not-a-directory"):
        await get_image_thumb_path(
            mass_minimal,
            f"data:image/png;base64,{image_data}",
            256,
            "builtin",
        )


async def test_concurrent_requests_share_one_fetch(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent get_image_data calls for the same source coalesce into one fetch."""
    gate = asyncio.Event()
    calls: list[str] = []

    async def gated_fetch(
        _mass: MusicAssistant, path_or_url: str, _provider: str, _depth: int
    ) -> tuple[bytes, bool]:
        calls.append(path_or_url)
        await gate.wait()
        return b"image-bytes", False

    monkeypatch.setattr(images, "_fetch_source_image", gated_fetch)
    tasks = [
        asyncio.create_task(get_image_data(mass_minimal, "/some/image.png", "builtin"))
        for _ in range(3)
    ]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert results == [b"image-bytes"] * 3
    assert calls == ["/some/image.png"]


async def test_cancelled_caller_of_a_failing_fetch_logs_no_loop_error(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source fetch failing after a caller gave up is not reported to the loop handler."""
    release = asyncio.Event()
    calls: list[str] = []

    async def failing_fetch(
        _mass: MusicAssistant, path_or_url: str, _provider: str, _depth: int
    ) -> tuple[bytes, bool]:
        calls.append(path_or_url)
        await release.wait()
        raise FileNotFoundError(f"Image not found: {path_or_url}")

    monkeypatch.setattr(images, "_fetch_source_image", failing_fetch)
    with collect_loop_errors() as reported:
        task_a = asyncio.create_task(get_image_data(mass_minimal, "/some/image.png", "builtin"))
        task_b = asyncio.create_task(get_image_data(mass_minimal, "/some/image.png", "builtin"))
        # let both callers await the (same) in-flight fetch, then cancel one
        await asyncio.sleep(0)
        task_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_a
        # release the fetch only once the cancellation is fully processed, so the
        # failure reliably lands after the giving-up caller is gone
        release.set()
        with pytest.raises(FileNotFoundError):
            await task_b

    assert calls == ["/some/image.png"]
    assert reported == []


async def test_cancelled_caller_of_a_failing_thumb_logs_no_loop_error(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thumbnail generation failing after its caller gave up is not reported either."""
    entered = asyncio.Event()
    release = asyncio.Event()
    generation: list[asyncio.Task[Any]] = []

    async def failing_source(_mass: MusicAssistant, path_or_url: str, _provider: str) -> bytes:
        current = asyncio.current_task()
        assert current is not None
        generation.append(current)
        entered.set()
        await release.wait()
        raise FileNotFoundError(f"Image not found: {path_or_url}")

    monkeypatch.setattr(images, "get_image_data", failing_source)
    with collect_loop_errors() as reported:
        caller = asyncio.create_task(
            get_image_thumb(mass_minimal, "/some/image.png", 256, "builtin")
        )
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # only fail the generation once the cancellation is fully processed
        release.set()
        await asyncio.wait(generation)

    assert isinstance(generation[0].exception(), FileNotFoundError)
    assert reported == []


async def test_data_uri_is_decoded_without_caching(
    mass_minimal: MusicAssistant, fetch_calls: list[tuple[str, str]]
) -> None:
    """Inline base64 data URIs are decoded directly and never enter the cache."""
    payload = b"\x89PNG\r\n\x1a\nfakepngdata"
    data_uri = f"data:image/png;base64,{b64encode(payload).decode()}"
    result = await get_image_data(mass_minimal, data_uri, "builtin")
    assert result == payload
    assert fetch_calls == []
    assert not images._source_memory_cache.entries


async def test_provider_bytes_use_disk_cache_across_restart(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fetch_calls: list[tuple[str, str]],
) -> None:
    """An expensive fetch is persisted on disk and reused after a (simulated) restart."""
    fake_provider = MagicMock(spec=MetadataProvider)
    fake_provider.resolve_image = AsyncMock(return_value=b"expensive-image-bytes")
    monkeypatch.setattr(mass_minimal, "get_provider", lambda _prov: fake_provider)

    data = await get_image_data(mass_minimal, "some/prov/path.jpg", "fake--1")
    assert data == b"expensive-image-bytes"
    assert len(fetch_calls) == 1
    cache_key = create_thumb_hash("fake--1", "some/prov/path.jpg")
    src_file = os.path.join(mass_minimal.cache_path, "thumbnails", f"{cache_key}_src")
    assert Path(src_file).is_file()

    # simulate a restart: memory tier gone, disk entry remains
    images._source_memory_cache.clear()
    data = await get_image_data(mass_minimal, "some/prov/path.jpg", "fake--1")
    assert data == b"expensive-image-bytes"
    assert len(fetch_calls) == 1


async def test_player_provider_can_resolve_image_bytes(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Player-provider images use the shared image retrieval pipeline."""
    fake_provider = MagicMock(spec=PlayerProvider)
    fake_provider.resolve_image = AsyncMock(return_value=b"player-image-bytes")
    monkeypatch.setattr(mass_minimal, "get_provider", lambda _prov: fake_provider)

    data = await get_image_data(mass_minimal, "player/artwork", "player--1")

    assert data == b"player-image-bytes"
    fake_provider.resolve_image.assert_awaited_once_with("player/artwork")


async def test_local_file_read_cached_on_disk(
    mass_minimal: MusicAssistant, tmp_path: Path, fetch_calls: list[tuple[str, str]]
) -> None:
    """A local file read lands in both cache tiers and is served from disk after restart."""
    image_path = _make_png_file(tmp_path)
    data = await get_image_data(mass_minimal, image_path, "builtin")
    assert len(fetch_calls) == 1
    cache_key = create_thumb_hash("builtin", image_path)
    assert cache_key in images._source_memory_cache.entries
    src_file = os.path.join(mass_minimal.cache_path, "thumbnails", f"{cache_key}_src")
    assert Path(src_file).is_file()

    # after a restart the disk entry serves the bytes without touching the origin
    # (which may live on a network mount) - local entries have no TTL
    images._source_memory_cache.clear()
    assert await get_image_data(mass_minimal, image_path, "builtin") == data
    assert len(fetch_calls) == 1


async def test_remote_disk_entry_expires_after_ttl(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fetch_calls: list[tuple[str, str]],
) -> None:
    """A stale on-disk entry for a remote url is refetched after the TTL."""
    mass_minimal.webserver = MagicMock(base_url="http://127.0.0.1:8095")
    mass_minimal.streams = MagicMock(base_url="http://127.0.0.1:8097")
    remote_url = "http://cdn.example.com/artwork.jpg"

    async def fake_remote_fetch(_mass: MusicAssistant, _url: str, **_kwargs: Any) -> bytes:
        return b"remote-image-bytes"

    monkeypatch.setattr(images, "_fetch_remote_image", fake_remote_fetch)
    await get_image_data(mass_minimal, remote_url, "builtin")
    assert len(fetch_calls) == 1
    cache_key = create_thumb_hash("builtin", remote_url)
    src_file = os.path.join(mass_minimal.cache_path, "thumbnails", f"{cache_key}_src")
    assert Path(src_file).is_file()

    # a fresh disk entry is used after a restart...
    images._source_memory_cache.clear()
    await get_image_data(mass_minimal, remote_url, "builtin")
    assert len(fetch_calls) == 1
    # ...but an expired one is not
    images._source_memory_cache.clear()
    expired = time.time() - _SOURCE_CACHE_TTL - 10
    os.utime(src_file, (expired, expired))
    await get_image_data(mass_minimal, remote_url, "builtin")
    assert len(fetch_calls) == 2


async def test_failing_source_fails_fast_with_single_warning(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fetch_calls: list[tuple[str, str]],
) -> None:
    """A persistently failing source is fetched once, then fails fast without new logs."""
    mass_minimal.webserver = MagicMock(base_url="http://127.0.0.1:8095")
    mass_minimal.streams = MagicMock(base_url="http://127.0.0.1:8097")
    remote_url = "http://sonos.example.com:1400/getaa?u=missing.flac"

    async def failing_remote_fetch(_mass: MusicAssistant, url: str, **_kwargs: Any) -> bytes:
        raise ClientError(f"404, message='Not Found', url='{url}'")

    monkeypatch.setattr(images, "_fetch_remote_image", failing_remote_fetch)
    caplog.set_level(logging.WARNING, logger="music_assistant.helpers.images")

    with pytest.raises(FileNotFoundError, match="404"):
        await get_image_data(mass_minimal, remote_url, "builtin")
    # follow-up requests (next metadata push, a thumbnail, a palette) fail
    # fast without a new origin fetch and without logging again
    with pytest.raises(FileNotFoundError, match="404"):
        await get_image_data(mass_minimal, remote_url, "builtin")
    with pytest.raises(FileNotFoundError, match="404"):
        await get_image_thumb(mass_minimal, remote_url, 256, "builtin")

    assert len(fetch_calls) == 1
    warnings = [rec for rec in caplog.records if rec.name == "music_assistant.helpers.images"]
    assert len(warnings) == 1
    assert "not retrying" in warnings[0].getMessage()


async def test_provider_reported_missing_image_fails_fast_with_single_warning(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fetch_calls: list[tuple[str, str]],
) -> None:
    """A provider reporting a missing image is asked once, then fails fast without new logs."""
    fake_provider = MagicMock(spec=MusicProvider)
    fake_provider.resolve_image = AsyncMock(
        side_effect=MediaNotFoundError("Image path is a directory: Some Artist")
    )
    monkeypatch.setattr(mass_minimal, "get_provider", lambda _prov: fake_provider)
    caplog.set_level(logging.WARNING, logger="music_assistant.helpers.images")

    with pytest.raises(MediaNotFoundError, match="Some Artist"):
        await get_image_data(mass_minimal, "Some Artist", "filesystem_local--1")
    # follow-up requests (a thumbnail, a palette) fail fast from the negative cache,
    # without asking the provider again and without logging again
    with pytest.raises(FileNotFoundError, match="Some Artist"):
        await get_image_data(mass_minimal, "Some Artist", "filesystem_local--1")

    assert len(fetch_calls) == 1
    assert fake_provider.resolve_image.await_count == 1
    warnings = [rec for rec in caplog.records if rec.name == "music_assistant.helpers.images"]
    assert len(warnings) == 1
    assert "not retrying" in warnings[0].getMessage()


async def test_provider_without_an_image_is_a_quiet_miss(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fetch_calls: list[tuple[str, str]],
) -> None:
    """A provider resolving a path to no image is asked once; the miss is remembered, not warned."""
    fake_provider = MagicMock(spec=MetadataProvider)
    fake_provider.resolve_image = AsyncMock(return_value=None)
    monkeypatch.setattr(mass_minimal, "get_provider", lambda _prov: fake_provider)
    caplog.set_level(logging.DEBUG, logger="music_assistant.helpers.images")

    with pytest.raises(MediaNotFoundError, match="no image"):
        await get_image_data(mass_minimal, "rg-in-rainbows", "coverartarchive")
    # follow-up requests fail fast from the negative cache, without asking the provider again
    with pytest.raises(FileNotFoundError, match="no image"):
        await get_image_data(mass_minimal, "rg-in-rainbows", "coverartarchive")

    assert len(fetch_calls) == 1
    assert fake_provider.resolve_image.await_count == 1
    records = [rec for rec in caplog.records if rec.name == "music_assistant.helpers.images"]
    assert [rec.levelno for rec in records] == [logging.DEBUG]
    assert "not retrying" in records[0].getMessage()


async def test_configured_provider_not_loaded_yet_is_not_remembered_as_failed(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image of a configured provider that loads late is served once the provider is loaded."""
    instance_id = "filesystem_local--late"
    mass_minimal.config.set(
        f"providers/{instance_id}", {"domain": "filesystem_local", "instance_id": instance_id}
    )
    loaded_provider: MagicMock | None = None
    monkeypatch.setattr(
        mass_minimal, "get_provider", lambda _prov, *_args, **_kwargs: loaded_provider
    )

    with pytest.raises(ProviderUnavailableError):
        await get_image_data(mass_minimal, "Some Artist/folder.jpg", instance_id)

    loaded_provider = MagicMock(spec=MusicProvider)
    loaded_provider.resolve_image = AsyncMock(return_value=b"late-provider-image-bytes")
    image = await get_image_data(mass_minimal, "Some Artist/folder.jpg", instance_id)
    assert image == b"late-provider-image-bytes"


async def test_failed_source_retried_after_ttl_or_invalidation(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fetch_calls: list[tuple[str, str]],
) -> None:
    """A failed source is retried after the negative-cache TTL or invalidation."""
    mass_minimal.webserver = MagicMock(base_url="http://127.0.0.1:8095")
    mass_minimal.streams = MagicMock(base_url="http://127.0.0.1:8097")
    remote_url = "http://cdn.example.com/broken.jpg"
    cache_key = create_thumb_hash("builtin", remote_url)

    async def failing_remote_fetch(_mass: MusicAssistant, _url: str, **_kwargs: Any) -> bytes:
        raise ClientError("503, message='Service Unavailable'")

    monkeypatch.setattr(images, "_fetch_remote_image", failing_remote_fetch)
    with pytest.raises(FileNotFoundError):
        await get_image_data(mass_minimal, remote_url, "builtin")
    assert len(fetch_calls) == 1

    # once the TTL has passed, the origin is tried again
    _expires_at, message = images._failed_sources[cache_key]
    images._failed_sources[cache_key] = (time.monotonic() - 1, message)
    with pytest.raises(FileNotFoundError):
        await get_image_data(mass_minimal, remote_url, "builtin")
    assert len(fetch_calls) == 2

    # invalidation drops the entry immediately; a recovered origin then serves
    await invalidate_cached_image(mass_minimal, "builtin", remote_url)
    assert cache_key not in images._failed_sources

    async def ok_remote_fetch(_mass: MusicAssistant, _url: str, **_kwargs: Any) -> bytes:
        return b"remote-image-bytes"

    monkeypatch.setattr(images, "_fetch_remote_image", ok_remote_fetch)
    assert await get_image_data(mass_minimal, remote_url, "builtin") == b"remote-image-bytes"
    assert len(fetch_calls) == 3


async def test_remote_http_404_yields_file_not_found(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real HTTP 404 response converts into FileNotFoundError with one origin hit."""
    # the provider is configured with the loopback test server, so its URL there is fetched
    fake_provider = MagicMock(spec=MusicProvider, domain="sonos", instance_id="sonos--1")
    fake_provider.resolve_image = AsyncMock(side_effect=lambda path: path)
    monkeypatch.setattr(mass_minimal, "get_provider", lambda *_args, **_kwargs: fake_provider)
    hits = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal hits
        hits += 1
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/getaa", handler)
    server = TestServer(app)
    await server.start_server()
    session = ClientSession()
    try:
        mass_minimal.webserver = MagicMock(base_url="http://127.0.0.1:8095")
        mass_minimal.streams = MagicMock(base_url="http://127.0.0.1:8097")
        mass_minimal._http_session_no_ssl = session
        fake_provider.config = _provider_config({"url": str(server.make_url("/"))})
        url = str(server.make_url("/getaa")) + "?u=track.flac"
        with pytest.raises(FileNotFoundError, match="404"):
            await get_image_data(mass_minimal, url, "sonos--1")
        # the negative cache prevents a second hit on the origin
        with pytest.raises(FileNotFoundError, match="404"):
            await get_image_data(mass_minimal, url, "sonos--1")
        assert hits == 1
    finally:
        await session.close()
        await server.close()


async def test_guarded_fetch_refuses_redirect_to_loopback(mass_minimal: MusicAssistant) -> None:
    """A guarded fetch does not follow a redirect from a public host to a loopback address."""
    requested = _fake_http_session(
        mass_minimal, {"http://cdn.example.com/a.jpg": (302, "http://127.0.0.1/secret", b"")}
    )
    with (
        patch(RESOLVER, AsyncMock(return_value=["93.184.215.14"])),
        pytest.raises(InvalidDataError, match="blocked address"),
    ):
        await images._fetch_remote_image(mass_minimal, "http://cdn.example.com/a.jpg", guard=True)
    assert requested == ["http://cdn.example.com/a.jpg"]


async def test_guarded_fetch_follows_public_redirect(mass_minimal: MusicAssistant) -> None:
    """A guarded fetch follows a redirect between public hosts."""
    requested = _fake_http_session(
        mass_minimal,
        {
            "http://cdn.example.com/a.jpg": (301, "https://img.example.org/b.jpg", b""),
            "https://img.example.org/b.jpg": (200, None, b"image-bytes"),
        },
    )
    with patch(RESOLVER, AsyncMock(return_value=["93.184.215.14"])):
        data = await images._fetch_remote_image(
            mass_minimal, "http://cdn.example.com/a.jpg", guard=True
        )
    assert data == b"image-bytes"
    assert requested == ["http://cdn.example.com/a.jpg", "https://img.example.org/b.jpg"]


async def test_guarded_fetch_gives_up_after_three_redirects(mass_minimal: MusicAssistant) -> None:
    """A guarded fetch stops following redirects after three hops."""
    requested = _fake_http_session(
        mass_minimal,
        {f"http://cdn.example.com/{hop}": (302, f"/{hop + 1}", b"") for hop in range(10)},
    )
    with (
        patch(RESOLVER, AsyncMock(return_value=["93.184.215.14"])),
        pytest.raises(FileNotFoundError, match="redirects"),
    ):
        await images._fetch_remote_image(mass_minimal, "http://cdn.example.com/0", guard=True)
    assert len(requested) == 4


async def test_unknown_provider_image_on_loopback_is_refused(mass_minimal: MusicAssistant) -> None:
    """An image of an unknown provider pointing at a loopback host is not fetched."""
    mass_minimal.webserver = MagicMock(base_url="http://192.168.1.2:8095")
    mass_minimal.streams = MagicMock(base_url="http://192.168.1.2:8097")
    requested = _fake_http_session(
        mass_minimal, {"http://localhost/art.jpg": (200, None, b"internal")}
    )
    with (
        patch(RESOLVER, AsyncMock(return_value=["127.0.0.1"])),
        pytest.raises(FileNotFoundError, match="blocked address"),
    ):
        await images._fetch_source_image(mass_minimal, "http://localhost/art.jpg", "gone--1", 0)
    assert requested == []


async def test_loaded_provider_image_is_not_guarded(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image URL on a server the named provider is configured with is fetched as is."""
    mass_minimal.webserver = MagicMock(base_url="http://192.168.1.2:8095")
    mass_minimal.streams = MagicMock(base_url="http://192.168.1.2:8097")
    fake_provider = MagicMock(spec=MusicProvider, domain="sonos", instance_id="sonos--1")
    fake_provider.config = _provider_config({"url": "http://localhost"})
    fake_provider.resolve_image = AsyncMock(side_effect=lambda path: path)
    monkeypatch.setattr(mass_minimal, "get_provider", lambda *_args, **_kwargs: fake_provider)
    _fake_http_session(mass_minimal, {"http://localhost/art.jpg": (200, None, b"provider-art")})
    resolver = AsyncMock(return_value=["127.0.0.1"])
    with patch(RESOLVER, resolver):
        data, _ = await images._fetch_source_image(
            mass_minimal, "http://localhost/art.jpg", "sonos--1", 0
        )
    assert data == b"provider-art"
    resolver.assert_not_called()


async def test_untrusted_url_is_refused_before_the_provider_fetches_it(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that fetches http paths itself never sees a URL on a blocked address."""
    podcast = MagicMock(spec=MusicProvider, domain="podcastfeed", instance_id="podcastfeed--1")
    podcast.config = _provider_config({"feed_url": "https://feeds.example.com/show.xml"})
    podcast.resolve_image = AsyncMock(return_value=b"fetched-by-provider")
    monkeypatch.setattr(mass_minimal, "get_provider", lambda *_args, **_kwargs: podcast)
    with pytest.raises(FileNotFoundError, match="blocked address"):
        await images._fetch_source_image(
            mass_minimal, "http://127.0.0.1:8095/x", "podcastfeed--1", 0
        )
    podcast.resolve_image.assert_not_awaited()


@pytest.mark.parametrize(
    "configured_url",
    ["http://192.168.1.10:8096", "http://127.0.0.1:8096"],
    ids=["other-host", "other-port"],
)
async def test_provider_image_off_configured_endpoint_is_guarded(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch, configured_url: str
) -> None:
    """An image of the named provider on a host or port it is not configured with is guarded."""
    mass_minimal.webserver = MagicMock(base_url="http://192.168.1.2:8095")
    mass_minimal.streams = MagicMock(base_url="http://192.168.1.2:8097")
    jellyfin = MagicMock(spec=MusicProvider, domain="jellyfin", instance_id="jellyfin--1")
    jellyfin.config = _provider_config({"url": configured_url})
    jellyfin.resolve_image = AsyncMock(side_effect=lambda path: path)
    monkeypatch.setattr(mass_minimal, "get_provider", lambda *_args, **_kwargs: jellyfin)
    requested = _fake_http_session(
        mass_minimal, {"http://127.0.0.1:8095/x": (200, None, b"internal")}
    )
    with pytest.raises(FileNotFoundError, match="blocked address"):
        await images._fetch_source_image(mass_minimal, "http://127.0.0.1:8095/x", "jellyfin--1", 0)
    assert requested == []


@pytest.mark.parametrize(
    ("setup", "endpoint"),
    [
        ({"url": "http://localhost:8096"}, "http://localhost:8096"),
        ({"ip_address": "127.0.0.1", "port": 4533}, "http://127.0.0.1:4533"),
        (
            {"local_server_ip": "127.0.0.1", "local_server_port": "32400"},
            "http://127.0.0.1:32400",
        ),
    ],
    ids=["url", "ip-and-port", "local-server-ip-and-port"],
)
async def test_provider_image_on_setup_endpoint_is_not_guarded(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    setup: dict[str, Any],
    endpoint: str,
) -> None:
    """An image on the server from the provider's setup data is fetched as is, others not."""
    mass_minimal.webserver = MagicMock(base_url="http://192.168.1.2:8095")
    mass_minimal.streams = MagicMock(base_url="http://192.168.1.2:8097")
    stored = {key: f"_encrypted_{key}" for key in setup}
    fake_provider = MagicMock(spec=MusicProvider, domain="jellyfin", instance_id="jellyfin--1")
    fake_provider.config = _provider_config({}, setup_data=stored)
    fake_provider.get_setup_value = MagicMock(side_effect=setup.get)
    fake_provider.resolve_image = AsyncMock(side_effect=lambda path: path)
    monkeypatch.setattr(mass_minimal, "get_provider", lambda *_args, **_kwargs: fake_provider)
    other_port = endpoint.rsplit(":", 1)[0] + ":8095"
    requested = _fake_http_session(
        mass_minimal,
        {
            f"{endpoint}/art.jpg": (200, None, b"provider-art"),
            f"{other_port}/art.jpg": (200, None, b""),
        },
    )
    with patch(RESOLVER, AsyncMock(return_value=["127.0.0.1"])):
        data, _ = await images._fetch_source_image(
            mass_minimal, f"{endpoint}/art.jpg", "jellyfin--1", 0
        )
        with pytest.raises(FileNotFoundError, match="blocked address"):
            await images._fetch_source_image(
                mass_minimal, f"{other_port}/art.jpg", "jellyfin--1", 0
            )
    assert data == b"provider-art"
    assert requested == [f"{endpoint}/art.jpg"]


@pytest.mark.parametrize(
    ("url", "refused"),
    [("rtsp://127.0.0.1:8554/cam", True), ("rtsp://cam.example.com/stream", False)],
    ids=["loopback", "public"],
)
async def test_builtin_stream_scheme_image_is_guarded(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch, url: str, refused: bool
) -> None:
    """A builtin rtsp image reaches ffmpeg only when its host passes the address guard."""
    builtin = MagicMock(spec=MusicProvider, domain="builtin", instance_id="builtin")
    builtin.resolve_image = AsyncMock(side_effect=lambda path: path)
    monkeypatch.setattr(mass_minimal, "get_provider", lambda *_args, **_kwargs: builtin)
    embedded = AsyncMock(return_value=b"frame")
    monkeypatch.setattr(images, "get_embedded_image", embedded)
    if refused:
        with pytest.raises(FileNotFoundError, match="blocked address"):
            await images._fetch_source_image(mass_minimal, url, "builtin", 0)
        embedded.assert_not_called()
    else:
        data, _ = await images._fetch_source_image(mass_minimal, url, "builtin", 0)
        assert data == b"frame"
        embedded.assert_called_once_with(url)


async def test_sibling_provider_image_is_guarded(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image of an unavailable instance served by a sibling instance stays client-supplied."""
    mass_minimal.webserver = MagicMock(base_url="http://192.168.1.2:8095")
    mass_minimal.streams = MagicMock(base_url="http://192.168.1.2:8097")
    sibling = MagicMock(spec=MusicProvider, domain="spotify", instance_id="spotify--b")
    sibling.resolve_image = AsyncMock(side_effect=lambda path: path)
    monkeypatch.setattr(mass_minimal, "get_provider", lambda *_args, **_kwargs: sibling)
    requested = _fake_http_session(
        mass_minimal, {"http://localhost/art.jpg": (200, None, b"internal")}
    )
    with (
        patch(RESOLVER, AsyncMock(return_value=["127.0.0.1"])),
        pytest.raises(FileNotFoundError, match="blocked address"),
    ):
        await images._fetch_source_image(mass_minimal, "http://localhost/art.jpg", "spotify--a", 0)
    assert requested == []


async def test_own_imageproxy_url_cached_under_resolved_key_only(
    mass_minimal: MusicAssistant, tmp_path: Path, fetch_calls: list[tuple[str, str]]
) -> None:
    """Imageproxy URLs to our own server never create an alias cache entry."""
    image_path = _make_png_file(tmp_path)
    mass_minimal.webserver = MagicMock(base_url="http://127.0.0.1:8095")
    mass_minimal.streams = MagicMock(base_url="http://127.0.0.1:8097")
    image_id = create_thumb_hash("builtin", image_path)
    mass_minimal.metadata = MagicMock(
        resolve_image_id=AsyncMock(return_value=("builtin", image_path))
    )
    proxy_url = f"http://127.0.0.1:8095/imageproxy/{image_id}?size=256"

    await get_image_data(mass_minimal, proxy_url, "builtin")
    assert fetch_calls == [("builtin", image_path)]
    assert create_thumb_hash("builtin", image_path) in images._source_memory_cache.entries
    assert create_thumb_hash("builtin", proxy_url) not in images._source_memory_cache.entries
    # a repeated request through the proxy url form hits the resolved cache entry
    await get_image_data(mass_minimal, proxy_url, "builtin")
    assert len(fetch_calls) == 1


async def test_invalidate_cached_image_clears_all_tiers(
    mass_minimal: MusicAssistant, tmp_path: Path, fetch_calls: list[tuple[str, str]]
) -> None:
    """Invalidation removes every thumb variant plus source bytes, then refetches."""
    image_path = _make_png_file(tmp_path, "target.png")
    other_path = _make_png_file(tmp_path, "other.png")
    for path in (image_path, other_path):
        await get_image_thumb(mass_minimal, path, 80, "builtin")
        await get_image_thumb(mass_minimal, path, 256, "builtin")
    assert len(fetch_calls) == 2

    thumb_dir = Path(mass_minimal.cache_path, "thumbnails")
    target_hash = create_thumb_hash("builtin", image_path)
    other_hash = create_thumb_hash("builtin", other_path)
    # two thumb variants plus the source entry per image
    assert len([f for f in thumb_dir.iterdir() if f.name.startswith(target_hash)]) == 3

    await invalidate_cached_image(mass_minimal, "builtin", image_path)

    # all artifacts of the target image are gone, the other image is untouched
    assert not [f for f in thumb_dir.iterdir() if f.name.startswith(target_hash)]
    assert len([f for f in thumb_dir.iterdir() if f.name.startswith(other_hash)]) == 3
    assert target_hash not in images._source_memory_cache.entries
    assert not any(key.startswith(f"{target_hash}_") for key in images._thumb_memory_cache)
    assert any(key.startswith(f"{other_hash}_") for key in images._thumb_memory_cache)

    # the next request must hit the origin again
    await get_image_thumb(mass_minimal, image_path, 80, "builtin")
    assert len(fetch_calls) == 3


async def test_source_memory_cache_respects_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory tier evicts oldest entries once the byte budget is exceeded."""
    monkeypatch.setattr(images, "_SOURCE_MEMORY_MAX_BYTES", 1000)
    monkeypatch.setattr(images, "_SOURCE_MEMORY_ENTRY_MAX_BYTES", 600)
    cache = images._source_memory_cache
    cache.put("aa", b"x" * 400)
    cache.put("bb", b"y" * 400)
    assert cache.get("aa") is not None
    # third entry pushes total over budget: oldest entry is evicted
    cache.put("cc", b"z" * 400)
    assert cache.get("bb") is None
    assert cache.get("aa") is not None  # refreshed by the get above
    assert cache.get("cc") is not None
    # an entry larger than the per-entry cap is not stored at all
    cache.put("dd", b"w" * 601)
    assert cache.get("dd") is None
    assert cache.total_bytes == 800


async def test_source_memory_cache_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory entries older than the TTL are treated as a miss."""
    cache = images._source_memory_cache
    cache.put("aa", b"payload")
    assert cache.get("aa") == b"payload"
    # shrink the TTL so the stored entry is immediately considered expired
    monkeypatch.setattr(images, "_SOURCE_CACHE_TTL", -1)
    assert cache.get("aa") is None
    assert cache.total_bytes == 0


def _create_mp3_with_cover(track_path: str, cover_path: str) -> None:
    """Create a short mp3 file with the given image embedded as cover art."""
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-t",
            "0.1",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-i",
            cover_path,
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-c:a",
            "libmp3lame",
            "-c:v",
            "mjpeg",
            "-id3v2_version",
            "3",
            track_path,
        ],
        check=True,
        capture_output=True,
    )


async def test_embedded_art_retag_flow(
    mass_minimal: MusicAssistant, tmp_path: Path, fetch_calls: list[tuple[str, str]]
) -> None:
    """
    The full retag scenario against a real audio file.

    Embedded art (an expensive ffmpeg extraction) is cached in memory and on
    disk; replacing the artwork in the file goes unnoticed until the cache is
    invalidated, after which the new artwork is extracted and served.
    """
    red_cover = str(tmp_path / "red.png")
    blue_cover = str(tmp_path / "blue.png")
    Image.new("RGB", (64, 64), (255, 0, 0)).save(red_cover, "PNG")
    Image.new("RGB", (64, 64), (0, 0, 255)).save(blue_cover, "PNG")
    track_path = str(tmp_path / "track.mp3")
    _create_mp3_with_cover(track_path, red_cover)

    original_art = await get_image_data(mass_minimal, track_path, "builtin")
    assert original_art.startswith(b"\xff\xd8")  # extracted as JPEG
    assert len(fetch_calls) == 1
    cache_key = create_thumb_hash("builtin", track_path)
    src_file = os.path.join(mass_minimal.cache_path, "thumbnails", f"{cache_key}_src")
    assert Path(src_file).is_file()  # ffmpeg extraction is disk-cache worthy

    # a restart later, the disk entry avoids re-running ffmpeg
    images._source_memory_cache.clear()
    assert await get_image_data(mass_minimal, track_path, "builtin") == original_art
    assert len(fetch_calls) == 1

    # "retag" the file with new artwork: the cache still serves the old art...
    _create_mp3_with_cover(track_path, blue_cover)
    assert await get_image_data(mass_minimal, track_path, "builtin") == original_art
    assert len(fetch_calls) == 1
    # ...until it is invalidated, after which the new art is extracted
    await invalidate_cached_image(mass_minimal, "builtin", track_path)
    assert not Path(src_file).exists()
    new_art = await get_image_data(mass_minimal, track_path, "builtin")
    assert len(fetch_calls) == 2
    assert new_art != original_art


# Provider icon helpers tests
async def test_load_provider_icon_svg(tmp_path: Path) -> None:
    """Test loading an SVG icon file returns minified UTF-8 bytes."""
    icon = tmp_path / "icon.svg"
    icon.write_text("<svg>\n  <path/>\n</svg>\n")
    mime, data = await load_provider_icon(str(icon))
    assert mime == "image/svg+xml"
    assert data == b"<svg>  <path/></svg>"


async def test_load_provider_icon_png(tmp_path: Path) -> None:
    """Test loading a PNG icon file returns raw bytes."""
    icon = tmp_path / "icon.png"
    raw = b"\x89PNG\r\n\x1a\n\x00\x00"
    icon.write_bytes(raw)
    mime, data = await load_provider_icon(str(icon))
    assert mime == "image/png"
    assert data == raw


async def test_load_provider_icon_bad_ext(tmp_path: Path) -> None:
    """Test loading an unsupported file format raises ValueError."""
    icon = tmp_path / "icon.gif"
    icon.write_bytes(b"gif")
    with pytest.raises(ValueError, match="Unsupported"):
        await load_provider_icon(str(icon))


async def test_detect_provider_icons_svg_preferred(tmp_path: Path) -> None:
    """Test that SVG is preferred over PNG when both exist."""
    # both svg and png present for default -> svg wins
    (tmp_path / "icon.svg").write_text("<svg/>")
    (tmp_path / "icon.png").write_bytes(b"PNGDATA")
    icons = await detect_provider_icons(str(tmp_path))
    assert icons[ProviderIconVariant.DEFAULT] == ("image/svg+xml", b"<svg/>")
    assert set(icons) == {ProviderIconVariant.DEFAULT}


async def test_detect_provider_icons_png_and_variants(tmp_path: Path) -> None:
    """Test detecting multiple icon variants."""
    (tmp_path / "icon.png").write_bytes(b"PNGDATA")
    (tmp_path / "icon_dark.svg").write_text("<svg/>")
    (tmp_path / "icon_monochrome.png").write_bytes(b"MONO")
    icons = await detect_provider_icons(str(tmp_path))
    assert icons[ProviderIconVariant.DEFAULT] == ("image/png", b"PNGDATA")
    assert icons[ProviderIconVariant.DARK] == ("image/svg+xml", b"<svg/>")
    assert icons[ProviderIconVariant.MONOCHROME] == ("image/png", b"MONO")


async def test_detect_provider_icons_none(tmp_path: Path) -> None:
    """Test detecting icons in an empty directory returns empty dict."""
    assert await detect_provider_icons(str(tmp_path)) == {}


def _fake_http_session(
    mass: MusicAssistant, responses: dict[str, tuple[int, str | None, bytes]]
) -> list[str]:
    """Serve (status, location, body) per URL from the no-ssl session; return requested URLs."""
    requested: list[str] = []

    @asynccontextmanager
    async def _get(url: str, **_kwargs: Any) -> AsyncIterator[MagicMock]:
        requested.append(str(url))
        status, location, body = responses[str(url)]
        resp = MagicMock(status=status, headers={"Location": location} if location else {})
        resp.url = URL(str(url))
        resp.read = AsyncMock(return_value=body)
        yield resp

    mass._http_session_no_ssl = MagicMock(get=_get)
    return requested


def _provider_config(
    values: dict[str, str], setup_data: dict[str, Any] | None = None
) -> ProviderConfig:
    """Return a provider config holding the given string values and stored setup data."""
    entries = [ConfigEntry(key=key, type=ConfigEntryType.STRING, label=key) for key in values]
    raw = {
        "type": "music",
        "domain": "fake",
        "instance_id": "fake--1",
        "values": values,
        "setup_data": setup_data or {},
    }
    return cast("ProviderConfig", ProviderConfig.parse(entries, raw))
