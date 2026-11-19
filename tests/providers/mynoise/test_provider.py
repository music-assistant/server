"""Tests for the myNoise provider."""

from pathlib import Path
from typing import TYPE_CHECKING, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, BrowseFolder
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.mynoise import provider as provider_module
from music_assistant.providers.mynoise.catalog import (
    Catalog,
    Category,
    Machine,
    MachineLayer,
)
from music_assistant.providers.mynoise.constants import (
    CONF_PATRON_EMAIL,
    CONF_REFRESH_CATALOG,
)
from music_assistant.providers.mynoise.provider import BROWSE_ROOT, MyNoiseProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _FakeResponse:
    """Minimal aiohttp download response stand-in (async context manager)."""

    def __init__(self, body: bytes = b"", error: Exception | None = None) -> None:
        self._body = body
        self._error = error

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._body

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error


class _DownloadSession:
    """Stand-in for mass.http_session serving fixed bodies per url."""

    def __init__(self, files: dict[str, bytes], error_urls: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self._files = files
        self._error_urls = error_urls

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append(url)
        if url in self._error_urls:
            return _FakeResponse(error=aiohttp.ClientError("offline"))
        return _FakeResponse(body=self._files[url])


def _create_provider(tmp_path: Path) -> MyNoiseProvider:
    """Create a MyNoiseProvider with mocked dependencies."""
    with patch.object(MyNoiseProvider, "__init__", lambda *_a, **_kw: None):
        provider = MyNoiseProvider.__new__(MyNoiseProvider)
    provider.mass = MagicMock()
    provider.mass.cache_path = str(tmp_path)
    provider.mass.runtime_path = str(tmp_path)
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.config = MagicMock()
    provider.config.instance_id = "mynoise"
    # no declared config values: get_config_value's fallback resolves to the default
    provider.config.values = {}
    provider.config.get_value = lambda _key, default=None: default
    provider.manifest = MagicMock()
    provider.manifest.domain = "mynoise"
    provider.logger = MagicMock()
    return provider


def _catalog() -> Catalog:
    machines = {
        "JAPANGARDEN": Machine(
            machine_id="JAPANGARDEN",
            name="Japanese Garden",
            category="Landscapes",
            generator_path="/NoiseMachines/japaneseGardenNoiseGenerator.php",
            image_url="https://mynoise.net/Data/JAPANGARDEN/fb.jpg",
        ),
        "THUNDER": Machine(machine_id="THUNDER", name="Thunder", category="Nature"),
    }
    categories = [
        Category(id="landscapes", name="Landscapes", machine_ids=["JAPANGARDEN"]),
        Category(id="nature", name="Nature", machine_ids=["THUNDER"]),
    ]
    return Catalog(categories, machines)


def _patch_catalog(monkeypatch: pytest.MonkeyPatch, catalog: Catalog) -> None:
    monkeypatch.setattr(provider_module, "fetch_catalog", AsyncMock(return_value=catalog))


# ------------------------------------------------------------------ radios


def test_radio_is_endless_not_dynamic(tmp_path: Path) -> None:
    """A machine radio is an endless stream, not a dynamic tracklist station."""
    provider = _create_provider(tmp_path)
    machine = next(iter(_catalog().machines.values()))
    radio = provider._radio_from_machine(machine)
    assert radio.media_type == MediaType.RADIO
    assert radio.is_endless
    assert not radio.is_dynamic
    assert radio.provider == "mynoise"
    assert radio.metadata.description
    assert radio.provider_mappings
    mapping = radio.provider_mappings.pop()
    assert mapping.url is not None
    assert mapping.url.endswith("/NoiseMachines/japaneseGardenNoiseGenerator.php")


def test_radio_images(tmp_path: Path) -> None:
    """Landscape and thumb images are attached as a UniqueList when known."""
    provider = _create_provider(tmp_path)
    garden, thunder = _catalog().machines.values()
    radio = provider._radio_from_machine(garden)
    assert isinstance(radio.metadata.images, UniqueList)
    assert len(radio.metadata.images) == 2
    assert not provider._radio_from_machine(thunder).metadata.images


async def test_search_by_name_category_and_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search matches on machine name, category and machine id."""
    _patch_catalog(monkeypatch, _catalog())
    provider = _create_provider(tmp_path)
    results = await provider.search("garden", [MediaType.RADIO])
    assert [item.item_id for item in results.radio] == ["JAPANGARDEN"]
    results = await provider.search("nature", [MediaType.RADIO])
    assert [item.item_id for item in results.radio] == ["THUNDER"]
    results = await provider.search("japangarden", [MediaType.RADIO])
    assert [item.item_id for item in results.radio] == ["JAPANGARDEN"]


async def test_search_other_media_types_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-radio searches return nothing; results respect the limit."""
    _patch_catalog(monkeypatch, _catalog())
    provider = _create_provider(tmp_path)
    assert not (await provider.search("a", [MediaType.TRACK])).radio
    results = await provider.search("a", [MediaType.RADIO], limit=1)
    assert len(results.radio) == 1


async def test_get_radio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A known machine resolves; an unknown id raises MediaNotFoundError."""
    _patch_catalog(monkeypatch, _catalog())
    provider = _create_provider(tmp_path)
    assert (await provider.get_radio("JAPANGARDEN")).name == "Japanese Garden"
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("NOPE")


# ------------------------------------------------------------------ browse


async def test_browse_root_lists_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root browse path yields one folder per category."""
    _patch_catalog(monkeypatch, _catalog())
    provider = _create_provider(tmp_path)
    items = await provider.browse(BROWSE_ROOT)
    assert [item.item_id for item in items] == ["landscapes", "nature"]
    folders = [item for item in items if isinstance(item, BrowseFolder)]
    assert [folder.path for folder in folders] == [
        f"{BROWSE_ROOT}/landscapes",
        f"{BROWSE_ROOT}/nature",
    ]


async def test_browse_category_and_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A category path lists its radios; a machine path yields that machine."""
    _patch_catalog(monkeypatch, _catalog())
    provider = _create_provider(tmp_path)
    items = await provider.browse(f"{BROWSE_ROOT}/landscapes")
    assert [item.item_id for item in items] == ["JAPANGARDEN"]
    items = await provider.browse(f"{BROWSE_ROOT}/landscapes/JAPANGARDEN")
    assert [item.item_id for item in items] == ["JAPANGARDEN"]
    items = await provider.browse("mynoise://THUNDER")
    assert [item.item_id for item in items] == ["THUNDER"]


async def test_browse_unknown_path_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown browse path raises MediaNotFoundError."""
    _patch_catalog(monkeypatch, _catalog())
    provider = _create_provider(tmp_path)
    with pytest.raises(MediaNotFoundError):
        await provider.browse(f"{BROWSE_ROOT}/nonexistent")


# --------------------------------------------------------------- streaming


def _machine_with_layers() -> Machine:
    return Machine(
        machine_id="JAPANGARDEN",
        name="Japanese Garden",
        layers=[
            MachineLayer("https://cdn/x1", "https://cdn/x1b", 0.5),
            MachineLayer("https://cdn/x2", "https://cdn/x2b", 0.0),
        ],
    )


async def test_get_stream_details_defers_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamdetails carry both layer variants and trigger no download."""
    machine = _machine_with_layers()
    _patch_catalog(monkeypatch, Catalog([], {"JAPANGARDEN": machine}))
    monkeypatch.setattr(provider_module, "fetch_machine_details", AsyncMock(return_value=machine))
    provider = _create_provider(tmp_path)
    session = _DownloadSession({})
    monkeypatch.setattr(provider.mass.http_session, "get", session.get)

    details = await provider.get_stream_details("JAPANGARDEN")
    assert details.stream_type == StreamType.CUSTOM
    assert details.media_type == MediaType.RADIO
    assert details.duration is None
    assert details.is_realtime
    assert details.data["gains"] == [1.0, 0.0]
    assert details.data["layers"] == [
        {"url_a": "https://cdn/x1", "url_b": "https://cdn/x1b"},
        {"url_a": "https://cdn/x2", "url_b": "https://cdn/x2b"},
    ]
    assert session.calls == []


def _stream_details(**overrides: object) -> StreamDetails:
    data: dict[str, object] = {
        "machine_id": "JAPANGARDEN",
        "machine_name": "Japanese Garden",
        "layers": [
            {"url_a": "https://cdn/x1", "url_b": "https://cdn/x1b"},
            {"url_a": "https://cdn/x2", "url_b": "https://cdn/x2b"},
        ],
        "gains": [1.0, 0.0],
        "master_gain_db": 0.0,
        "use_variant_b": False,
    }
    data.update(overrides)
    return StreamDetails(
        provider="mynoise",
        item_id="JAPANGARDEN",
        audio_format=AudioFormat(content_type=ContentType.PCM_S16LE),
        data=data,
    )


class _FakeStream:
    """Stand-in for stream_machine yielding fixed chunks."""

    def __init__(self) -> None:
        self.machines: list[Machine] = []
        # the layer sources as they were during playback (staged files are
        # removed again once the stream ends, so snapshot their content here)
        self.sources: list[list[bytes | str]] = []

    async def __call__(
        self, machine: Machine, gains: list[float], **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        self.machines.append(machine)
        snapshot: list[bytes | str] = []
        for layer in machine.layers:
            path = Path(layer.url_a)
            snapshot.append(path.read_bytes() if path.is_file() else layer.url_a)
        self.sources.append(snapshot)
        yield b"chunk"


async def test_get_audio_stream_stages_layers_for_playback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Active layers are downloaded to a temp dir that is removed when playback ends."""
    provider = _create_provider(tmp_path)
    session = _DownloadSession({"https://cdn/x1": b"LAYER1"})
    monkeypatch.setattr(provider.mass.http_session, "get", session.get)
    fake_stream = _FakeStream()
    monkeypatch.setattr(provider_module, "stream_machine", fake_stream)

    chunks = [chunk async for chunk in provider.get_audio_stream(_stream_details())]
    assert chunks == [b"chunk"]

    # the active layer was downloaded once; the muted one kept its remote url
    assert session.calls == ["https://cdn/x1"]
    assert fake_stream.sources[0][0] == b"LAYER1"
    assert Path(fake_stream.machines[0].layers[0].url_a).is_relative_to(tmp_path)
    assert fake_stream.sources[0][1] == "https://cdn/x2"
    # playback ended: the staged audio is gone, no copy survives the stream
    assert list(tmp_path.iterdir()) == []


async def test_get_audio_stream_stages_variant_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the B-variant option, the alternate take is the one downloaded."""
    provider = _create_provider(tmp_path)
    session = _DownloadSession({"https://cdn/x1b": b"LAYER1B"})
    monkeypatch.setattr(provider.mass.http_session, "get", session.get)
    fake_stream = _FakeStream()
    monkeypatch.setattr(provider_module, "stream_machine", fake_stream)

    details = _stream_details(use_variant_b=True)
    assert [chunk async for chunk in provider.get_audio_stream(details)] == [b"chunk"]
    assert session.calls == ["https://cdn/x1b"]
    assert fake_stream.sources[0][0] == b"LAYER1B"


async def test_get_audio_stream_download_failure_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed layer download keeps the remote url so playback still works."""
    provider = _create_provider(tmp_path)
    session = _DownloadSession({}, error_urls=("https://cdn/x1",))
    monkeypatch.setattr(provider.mass.http_session, "get", session.get)
    fake_stream = _FakeStream()
    monkeypatch.setattr(provider_module, "stream_machine", fake_stream)

    assert [chunk async for chunk in provider.get_audio_stream(_stream_details())] == [b"chunk"]
    assert fake_stream.sources[0][0] == "https://cdn/x1"
    cast("MagicMock", provider.logger.warning).assert_called()
    assert list(tmp_path.iterdir()) == []


async def test_get_audio_stream_cleans_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staging dir is removed even when the mix fails mid-stream."""
    provider = _create_provider(tmp_path)
    session = _DownloadSession({"https://cdn/x1": b"LAYER1"})
    monkeypatch.setattr(provider.mass.http_session, "get", session.get)

    async def _boom(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        yield b"chunk"
        raise AudioError("ffmpeg died")

    monkeypatch.setattr(provider_module, "stream_machine", _boom)
    with pytest.raises(AudioError):
        [chunk async for chunk in provider.get_audio_stream(_stream_details())]
    assert list(tmp_path.iterdir()) == []


async def test_get_audio_stream_invalid_data(tmp_path: Path) -> None:
    """Stream data without layers or gains raises MediaNotFoundError."""
    provider = _create_provider(tmp_path)
    with pytest.raises(MediaNotFoundError):
        [chunk async for chunk in provider.get_audio_stream(_stream_details(layers=[]))]
    with pytest.raises(MediaNotFoundError):
        [chunk async for chunk in provider.get_audio_stream(_stream_details(gains=[]))]


async def test_get_stream_details_unknown_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamdetails for an unknown machine raise MediaNotFoundError."""
    _patch_catalog(monkeypatch, _catalog())
    provider = _create_provider(tmp_path)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("NOPE")


async def test_get_stream_details_machine_without_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine whose definition has no audio layers raises MediaNotFoundError."""
    machine = Machine(machine_id="JAPANGARDEN", name="Japanese Garden")
    _patch_catalog(monkeypatch, Catalog([], {"JAPANGARDEN": machine}))
    monkeypatch.setattr(provider_module, "fetch_machine_details", AsyncMock(return_value=machine))
    provider = _create_provider(tmp_path)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("JAPANGARDEN")


# ------------------------------------------------------------- config/misc


async def test_handle_config_action_refreshes_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh action forces a catalog re-download."""
    fetch_mock = AsyncMock()
    monkeypatch.setattr(provider_module, "fetch_catalog", fetch_mock)
    provider = _create_provider(tmp_path)
    await provider.handle_config_action(CONF_REFRESH_CATALOG)
    fetch_mock.assert_awaited_once_with(provider.mass, force_refresh=True, patron=False)


def _provider_with_patron_email(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MyNoiseProvider:
    """Create a provider configured with a patron email."""
    provider = _create_provider(tmp_path)
    monkeypatch.setattr(
        provider,
        "get_config_value",
        lambda key, default=None: "me@example.com" if key == CONF_PATRON_EMAIL else default,
    )
    return provider


async def test_patron_email_unlocks_patron_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured patron email logs in once and requests the patron catalog."""
    provider = _provider_with_patron_email(tmp_path, monkeypatch)
    login = AsyncMock(return_value=True)
    monkeypatch.setattr(provider_module, "patron_login", login)
    fetch_mock = AsyncMock()
    monkeypatch.setattr(provider_module, "fetch_catalog", fetch_mock)

    await provider.handle_config_action(CONF_REFRESH_CATALOG)
    login.assert_awaited_once_with(provider.mass, "me@example.com")
    fetch_mock.assert_awaited_once_with(provider.mass, force_refresh=True, patron=True)

    # the login outcome is cached: browsing does not re-authenticate
    await provider._patron_enabled()
    login.assert_awaited_once()


async def test_patron_login_failure_stays_anonymous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected patron email warns and keeps the anonymous catalog."""
    provider = _provider_with_patron_email(tmp_path, monkeypatch)
    monkeypatch.setattr(provider_module, "patron_login", AsyncMock(return_value=False))
    fetch_mock = AsyncMock()
    monkeypatch.setattr(provider_module, "fetch_catalog", fetch_mock)

    await provider.handle_config_action(CONF_REFRESH_CATALOG)
    fetch_mock.assert_awaited_once_with(provider.mass, force_refresh=True, patron=False)
    cast("MagicMock", provider.logger.warning).assert_called()


async def test_patron_transient_error_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network hiccup during login is not latched: the next call retries."""
    provider = _provider_with_patron_email(tmp_path, monkeypatch)
    login = AsyncMock(side_effect=[aiohttp.ClientError("offline"), True])
    monkeypatch.setattr(provider_module, "patron_login", login)

    # the hiccup degrades to anonymous for this call only
    assert not await provider._patron_enabled()
    # the outcome was not cached: the next call authenticates again
    assert await provider._patron_enabled()
    assert login.await_count == 2


async def test_get_config_entries_include_patron_email(tmp_path: Path) -> None:
    """The options expose a patron email field next to the other settings."""
    provider = _create_provider(tmp_path)
    entries = await provider.get_config_entries()
    assert CONF_PATRON_EMAIL in [entry.key for entry in entries]


def test_supported_features() -> None:
    """The provider advertises browse and search, no library features."""
    assert ProviderFeature.BROWSE in provider_module.SUPPORTED_FEATURES
    assert ProviderFeature.SEARCH in provider_module.SUPPORTED_FEATURES
    assert not any(
        feature.value.startswith("library_") for feature in provider_module.SUPPORTED_FEATURES
    )
