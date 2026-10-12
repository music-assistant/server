"""Test Deezer catalogue resolution and GraphQL item retrieval."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientConnectionError, ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer
from deezer_python_gql import GraphQLClientError
from music_assistant_models.enums import ExternalID, ProviderFeature, ProviderType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    RetriesExhausted,
)
from music_assistant_models.media_items import Album, Track

from music_assistant.controllers.music import MusicController
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.deezer import rest_client
from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.rest_client import DeezerRESTClient

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.deezer.provider import DeezerProvider

ISRC = "USWB11506516"
UPC = "602547852748"
TRACK_ID = "101742167"
ALBUM_ID = "12761204"


@pytest.fixture
def responses() -> list[tuple[int, Any, dict[str, str]]]:
    """Supply catalogue responses in order, repeating the last response."""
    return [(200, {"id": int(TRACK_ID), "isrc": ISRC}, {})]


@pytest.fixture
def requests() -> list[dict[str, Any]]:
    """Collect requests received by the catalogue server."""
    return []


@pytest.fixture
async def client(
    mass_minimal: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, Any, dict[str, str]]],
    requests: list[dict[str, Any]],
) -> AsyncGenerator[DeezerRESTClient]:
    """Use a real HTTP session and MA cache against a local catalogue."""

    async def handle(request: web.Request) -> web.Response:
        requests.append(
            {"path": request.path, "cookies": dict(request.cookies), "headers": request.headers}
        )
        status, body, headers = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(body, str):
            return web.Response(text=body, status=status, content_type="application/json")
        return web.json_response(body, status=status, headers=headers)

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    await mass_minimal.cache._setup_database()
    async with (
        TestServer(app) as server,
        ClientSession(cookie_jar=CookieJar(unsafe=True)) as session,
    ):
        monkeypatch.setattr(rest_client, "REST_API_URL", str(server.make_url("")).rstrip("/"))
        monkeypatch.setattr(mass_minimal, "_http_session", session)
        session.cookie_jar.update_cookies(
            {"arl": "another-account", "sid": "another-session"}, response_url=server.make_url("/")
        )
        result = DeezerRESTClient(mass_minimal)
        result.throttler = ThrottlerManager(rate_limit=100, period=1, retry_attempts=2)
        yield result
        await _wait_for_cache(mass_minimal)


@pytest.fixture
def lookup_provider(
    provider: DeezerProvider, client: DeezerRESTClient, gql_client: Mock
) -> DeezerProvider:
    """Wire the provider to the real REST resolver and synthetic GraphQL items."""
    provider.mass = client.mass
    provider.rest_client = client
    provider.media_manager = DeezerMediaManager(provider)
    provider.gql_client = gql_client
    return provider


@pytest.fixture
def gql_client() -> Mock:
    """Return synthetic GraphQL responses for catalogue lookup tests."""
    return Mock(
        get_track=AsyncMock(
            return_value=SimpleNamespace(
                id=TRACK_ID,
                title="7 Years",
                duration=240,
                isrc=ISRC,
                contributors=SimpleNamespace(edges=[]),
                album=None,
                media=None,
                is_explicit=False,
            )
        ),
        get_album=AsyncMock(
            return_value=SimpleNamespace(
                id=ALBUM_ID,
                display_title="Lukas Graham",
                url=SimpleNamespace(web_url=f"https://www.deezer.com/album/{ALBUM_ID}"),
                contributors=SimpleNamespace(edges=[]),
                cover=None,
                type_=None,
                release_date=None,
            )
        ),
    )


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Use a real library database and provider fallback loop."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    await controller._setup_database()
    yield controller
    if controller._database:
        await controller._database.close()


async def test_track_lookup_normalizes_and_caches(
    gql_client: Mock, lookup_provider: DeezerProvider, requests: list[dict[str, Any]]
) -> None:
    """Reuse equivalent ISRC lookups and preserve GraphQL availability and account mapping."""
    first = await lookup_provider.get_track_by_external_id("us-wb1-15-06516", ExternalID.ISRC)
    await _wait_for_cache(lookup_provider.mass)
    second = await lookup_provider.get_track_by_external_id(ISRC, ExternalID.ISRC)

    assert first is not None
    assert second is not None
    assert first.item_id == second.item_id == TRACK_ID
    assert first.provider == lookup_provider.instance_id
    assert (ExternalID.ISRC, ISRC) in first.external_ids
    assert not first.available
    assert len(requests) == 1
    assert requests[0]["path"] == f"/track/isrc:{ISRC}"
    assert not any(requests[0]["cookies"].values())
    assert "Authorization" not in requests[0]["headers"]
    gql_client.get_track.assert_awaited_once_with(track_id=TRACK_ID)


@pytest.mark.parametrize("gql_isrc", [None, "", "us-wb1-15-06516"])
async def test_track_lookup_accepts_missing_or_formatted_isrc(
    gql_client: Mock, lookup_provider: DeezerProvider, gql_isrc: str | None
) -> None:
    """Fill absent ISRC metadata from REST without changing the cached GraphQL item."""
    gql_client.get_track.return_value.isrc = gql_isrc

    track = await lookup_provider.get_track_by_external_id(ISRC, ExternalID.ISRC)

    assert track is not None
    assert track.item_id == TRACK_ID
    assert not track.available
    expected_isrc = gql_isrc or ISRC
    assert (ExternalID.ISRC, expected_isrc) in track.external_ids
    await _wait_for_cache(lookup_provider.mass)
    cached_track = await lookup_provider.get_track(TRACK_ID)
    assert cached_track.external_ids == ({(ExternalID.ISRC, gql_isrc)} if gql_isrc else set())
    gql_client.get_track.assert_awaited_once_with(track_id=TRACK_ID)


@pytest.mark.parametrize("external_id_type", [ExternalID.ISRC, ExternalID.BARCODE])
@pytest.mark.parametrize(
    "failure", ["rest_outage", "rest_invalid", "graphql", "connection", "timeout"]
)
async def test_lookup_failure_allows_provider_fallback(
    lookup_provider: DeezerProvider,
    gql_client: Mock,
    responses: list[tuple[int, Any, dict[str, str]]],
    music: MusicController,
    caplog: pytest.LogCaptureFixture,
    external_id_type: ExternalID,
    failure: str,
) -> None:
    """Keep REST and GraphQL failures diagnostic while another provider answers the lookup."""
    is_track = external_id_type == ExternalID.ISRC
    external_id = ISRC if is_track else UPC
    item_id = TRACK_ID if is_track else ALBUM_ID
    field = "isrc" if is_track else "upc"
    lookup = (
        lookup_provider.get_track_by_external_id
        if is_track
        else lookup_provider.get_album_by_external_id
    )
    gql_get = gql_client.get_track if is_track else gql_client.get_album
    responses[:] = [(200, {"id": item_id, field: external_id}, {})]
    expected_error: type[Exception]
    if failure == "rest_outage":
        responses[:] = [(503, {}, {})]
        expected_error = RetriesExhausted
    elif failure == "rest_invalid":
        responses[:] = [(200, {}, {})]
        expected_error = InvalidDataError
    else:
        expected_error = {
            "graphql": GraphQLClientError,
            "connection": ClientConnectionError,
            "timeout": TimeoutError,
        }[failure]
        gql_get.side_effect = expected_error("catalogue unavailable")

    remote_item = (Track if is_track else Album)(
        item_id="remote", provider="other--test", name="Remote match", provider_mappings=set()
    )
    remote = Mock(
        type=ProviderType.MUSIC,
        available=True,
        domain="other",
        instance_id="other--test",
        supported_features={
            ProviderFeature.TRACK_BY_EXTERNAL_ID,
            ProviderFeature.ALBUM_BY_EXTERNAL_ID,
        },
        get_track_by_external_id=AsyncMock(return_value=remote_item),
        get_album_by_external_id=AsyncMock(return_value=remote_item),
    )
    lookup_provider.available = True
    lookup_provider.manifest.type = ProviderType.MUSIC
    music.mass._providers = {
        lookup_provider.instance_id: lookup_provider,
        remote.instance_id: remote,
    }
    controller = music.tracks if is_track else music.albums

    with patch("music_assistant.helpers.throttle_retry.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ProviderUnavailableError) as exc:
            await lookup(external_id, external_id_type)
        assert isinstance(exc.value.__cause__, expected_error)

        result = await controller.get_item_by_external_id(external_id, external_id_type)

    assert result is remote_item
    remote_get = remote.get_track_by_external_id if is_track else remote.get_album_by_external_id
    remote_get.assert_awaited_once_with(external_id, external_id_type)
    assert lookup_provider.instance_id in caplog.text
    if failure.startswith("rest_"):
        gql_get.assert_not_awaited()


@pytest.mark.parametrize(
    ("barcode", "upc", "canonical"),
    [
        (UPC, UPC, "00602547852748"),
        ("0602547852748", UPC, "00602547852748"),
        ("00602547852748", UPC, "00602547852748"),
        ("4006381333931", "4006381333931", "04006381333931"),
    ],
)
async def test_album_lookup_verifies_barcode(
    gql_client: Mock,
    lookup_provider: DeezerProvider,
    responses: list[tuple[int, Any, dict[str, str]]],
    requests: list[dict[str, Any]],
    barcode: str,
    upc: str,
    canonical: str,
) -> None:
    """Resolve equivalent barcode forms and attach the verified UPC to the GraphQL album."""
    responses[:] = [(200, {"id": ALBUM_ID, "upc": upc}, {})]
    album = await lookup_provider.get_album_by_external_id(barcode, ExternalID.BARCODE)
    await _wait_for_cache(lookup_provider.mass)
    await lookup_provider.get_album_by_external_id(upc, ExternalID.BARCODE)

    assert album is not None
    assert album.item_id == ALBUM_ID
    assert album.provider == lookup_provider.instance_id
    assert (ExternalID.BARCODE, canonical) in album.external_ids
    assert [request["path"] for request in requests] == [f"/album/upc:{upc}"]
    gql_client.get_album.assert_awaited_once_with(album_id=ALBUM_ID)
    original = await lookup_provider.get_album(ALBUM_ID)
    assert not original.external_ids


@pytest.mark.parametrize(
    ("value", "kind"),
    [("bad", ExternalID.ISRC), ("../../me", ExternalID.BARCODE), (ISRC, ExternalID.MB_ARTIST)],
)
async def test_invalid_input_does_not_request(
    gql_client: Mock,
    lookup_provider: DeezerProvider,
    requests: list[dict[str, Any]],
    value: str,
    kind: ExternalID,
) -> None:
    """Reject invalid and unsupported identifiers before any API request."""
    assert await lookup_provider.get_track_by_external_id(value, kind) is None
    assert await lookup_provider.get_album_by_external_id(value, kind) is None
    assert not requests
    gql_client.get_track.assert_not_awaited()
    gql_client.get_album.assert_not_awaited()


@pytest.mark.parametrize("status", [200, 404])
async def test_missing_item_is_cached(
    gql_client: Mock,
    lookup_provider: DeezerProvider,
    responses: list[tuple[int, Any, dict[str, str]]],
    requests: list[dict[str, Any]],
    status: int,
) -> None:
    """Treat Deezer's JSON no-data response as a cached miss without calling GraphQL."""
    responses[:] = [(status, {"error": {"code": 800, "message": "no data"}}, {})]
    assert await lookup_provider.get_track_by_external_id(ISRC, ExternalID.ISRC) is None
    await _wait_for_cache(lookup_provider.mass)
    assert await lookup_provider.get_track_by_external_id(ISRC, ExternalID.ISRC) is None
    assert len(requests) == 1
    gql_client.get_track.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        "{invalid json",
        [],
        {},
        {"error": "broken"},
        {"id": TRACK_ID},
        {"id": True, "isrc": ISRC},
        {"id": -1, "isrc": ISRC},
        {"id": "../me", "isrc": ISRC},
        {"id": TRACK_ID, "isrc": "USWB11506517"},
    ],
)
async def test_invalid_response_is_not_cached_as_missing(
    client: DeezerRESTClient,
    responses: list[tuple[int, Any, dict[str, str]]],
    requests: list[dict[str, Any]],
    body: Any,
) -> None:
    """Surface malformed or mismatched responses and allow the next call to recover."""
    responses[:] = [(200, body, {}), (200, {"id": TRACK_ID, "isrc": ISRC}, {})]
    with pytest.raises(InvalidDataError):
        await client.get_item_id(ISRC, ExternalID.ISRC)
    assert await client.get_item_id(ISRC, ExternalID.ISRC) == TRACK_ID
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "body", "headers", "minimum_delay"),
    [
        (429, {}, {"Retry-After": "12"}, 12),
        (200, {"error": {"code": 4}}, {}, 5),
        (503, {}, {}, 0),
    ],
)
async def test_transient_failure_backs_off_and_recovers(
    client: DeezerRESTClient,
    responses: list[tuple[int, Any, dict[str, str]]],
    requests: list[dict[str, Any]],
    status: int,
    body: Any,
    headers: dict[str, str],
    minimum_delay: int,
) -> None:
    """Retry rate limits and server failures without caching them as catalogue misses."""
    responses[:] = [(status, body, headers), (200, {"id": TRACK_ID, "isrc": ISRC}, {})]
    with patch(
        "music_assistant.helpers.throttle_retry.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        assert await client.get_item_id(ISRC, ExternalID.ISRC) == TRACK_ID
    assert len(requests) == 2
    sleep.assert_awaited_once()
    assert sleep.call_args.args[0] >= minimum_delay


async def test_retries_exhausted(
    client: DeezerRESTClient, responses: list[tuple[int, Any, dict[str, str]]]
) -> None:
    """Keep an exhausted outage distinguishable from an absent recording."""
    responses[:] = [(503, {}, {})]
    with (
        patch("music_assistant.helpers.throttle_retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RetriesExhausted),
    ):
        await client.get_item_id(ISRC, ExternalID.ISRC)


@pytest.mark.parametrize(
    "response",
    [
        (403, {}, {}),
        (200, {"error": {"code": 100}}, {}),
        (302, {}, {"Location": "/redirected"}),
    ],
)
async def test_permanent_failure(
    client: DeezerRESTClient,
    responses: list[tuple[int, Any, dict[str, str]]],
    requests: list[dict[str, Any]],
    response: tuple[int, Any, dict[str, str]],
) -> None:
    """Report unexpected API errors instead of treating them as no match."""
    responses[:] = [response]
    with pytest.raises(ProviderUnavailableError):
        await client.get_item_id(ISRC, ExternalID.ISRC)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ("id", f"Deezer returned track different instead of {TRACK_ID}"),
        ("isrc", f"Deezer track {TRACK_ID} does not match ISRC {ISRC}"),
        ("missing", f"Track {TRACK_ID} not found on Deezer"),
    ],
)
async def test_graphql_track_must_still_match(
    gql_client: Mock, lookup_provider: DeezerProvider, changed: str, message: str
) -> None:
    """Reject a GraphQL miss, relink or different recording after REST resolution."""
    if changed == "missing":
        gql_client.get_track.return_value = None
    else:
        setattr(gql_client.get_track.return_value, changed, "different")
    with pytest.raises(MediaNotFoundError, match=message):
        await lookup_provider.get_track_by_external_id(ISRC, ExternalID.ISRC)


async def test_graphql_album_must_still_match(
    gql_client: Mock,
    lookup_provider: DeezerProvider,
    responses: list[tuple[int, Any, dict[str, str]]],
) -> None:
    """Do not assign the resolved UPC to a replacement album."""
    responses[:] = [(200, {"id": ALBUM_ID, "upc": UPC}, {})]
    gql_client.get_album.return_value.id = "different"
    with pytest.raises(MediaNotFoundError, match=f"returned album different instead of {ALBUM_ID}"):
        await lookup_provider.get_album_by_external_id(UPC, ExternalID.BARCODE)


@pytest.mark.parametrize("error", [TimeoutError, ClientConnectionError])
async def test_network_failure_is_retried(client: DeezerRESTClient, error: type[Exception]) -> None:
    """Keep connection errors and timeouts distinguishable from missing catalogue items."""
    with (
        patch.object(client.mass.http_session, "get", side_effect=error) as get,
        patch("music_assistant.helpers.throttle_retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RetriesExhausted),
    ):
        await client.get_item_id(ISRC, ExternalID.ISRC)
    assert get.call_count == 2


async def test_rest_album_barcode_must_match(
    gql_client: Mock,
    lookup_provider: DeezerProvider,
    responses: list[tuple[int, Any, dict[str, str]]],
) -> None:
    """Reject another release's barcode before retrieving or annotating a GraphQL album."""
    responses[:] = [(200, {"id": ALBUM_ID, "upc": "4006381333931"}, {})]
    with pytest.raises(ProviderUnavailableError) as exc:
        await lookup_provider.get_album_by_external_id(UPC, ExternalID.BARCODE)
    assert isinstance(exc.value.__cause__, InvalidDataError)
    gql_client.get_album.assert_not_awaited()


def test_lookup_features(lookup_provider: DeezerProvider) -> None:
    """Advertise only the implemented external-ID media types."""
    assert ProviderFeature.TRACK_BY_EXTERNAL_ID in lookup_provider.supported_features
    assert ProviderFeature.ALBUM_BY_EXTERNAL_ID in lookup_provider.supported_features
    assert ProviderFeature.ARTIST_BY_EXTERNAL_ID not in lookup_provider.supported_features


async def _wait_for_cache(mass: MusicAssistant) -> None:
    """Wait for background cache writes before checking a subsequent lookup."""
    await asyncio.gather(*tuple(mass._tracked_tasks.values()))
