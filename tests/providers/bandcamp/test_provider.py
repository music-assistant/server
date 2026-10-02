"""Test Bandcamp Provider integration."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from aiohttp import ClientConnectionError, ClientPayloadError, ConnectionTimeoutError
from bandcamp_async_api import (
    BandcampAPIClient,
    BandcampAPIError,
    BandcampMustBeLoggedInError,
    BandcampNotFoundError,
    BandcampRateLimitError,
    BandcampUnexpectedResponseError,
    SearchResultAlbum,
    SearchResultArtist,
    SearchResultTrack,
)
from bandcamp_async_api.models import (
    BCArtist,
    BCTrack,
    CollectionItem,
    CollectionType,
    FollowingItem,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
    UnplayableMediaError,
)
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import Album, Artist, BrowseFolder, ProviderMapping, Track
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.bandcamp import BandcampProvider, setup, split_id
from music_assistant.providers.bandcamp._ids import make_artist_id
from music_assistant.providers.bandcamp.constants import (
    BANDCAMP_TIMEOUT,
    CACHE_CHANGING_LISTING,
    CACHE_EMPTY_RESULTS,
    CACHE_METADATA,
    CACHE_USER_LISTS,
    CONF_GET_LYRICS,
    DEFAULT_TOP_TRACKS_LIMIT,
    PARSED_ITEM_CACHE_CHECKSUM,
    SUPPORTED_FEATURES,
)
from tests.common import use_real_create_task


def _fan_mock(
    fan_id: int,
    name: str | None,
    image_url: str | None = None,
    url: str | None = None,
) -> Mock:
    """Create a mock FanItem with real string attributes for BrowseFolder compatibility."""
    fan = Mock(spec=["fan_id", "name", "image_url", "url"])
    fan.fan_id = fan_id
    fan.name = name
    fan.image_url = image_url
    fan.url = url
    return fan


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()
    # setup_data is unset in these unit tests, so get_setup_value falls through to
    # the provider config's get_value (which the config mock stubs)
    mass.config.get = Mock(return_value=None)
    mass.config.get_raw_provider_config_value = Mock(return_value=None)
    use_real_create_task(mass)
    return mass


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Bandcamp Test"
    config.instance_id = "bandcamp_test"
    config.enabled = True
    config.values = {}
    config.get_value.side_effect = lambda key, default=None: {
        "identity": "mock_identity_token",
        "search_limit": 10,
        "top_tracks_limit": 50,
        "log_level": "INFO",
    }.get(
        key,
        default
        if default is not None
        else (10 if key == "search_limit" else (50 if key == "top_tracks_limit" else "INFO")),
    )
    return config


@pytest.fixture
async def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> BandcampProvider:
    """Return a BandcampProvider instance."""
    provider = BandcampProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)
    provider.throttler = ThrottlerManager(
        rate_limit=provider.throttler.throttler.rate_limit,
        period=provider.throttler.throttler.period,
        retry_attempts=provider.throttler.retry_attempts,
        initial_backoff=provider.throttler.initial_backoff,
    )

    # Initialize the provider
    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        await provider.handle_async_init()

    return provider


async def test_provider_initialization(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test provider initialization."""
    provider = BandcampProvider(mass_mock, manifest_mock, config_mock)

    assert provider.domain == "bandcamp"
    assert provider.instance_id == "bandcamp_test"

    # Test that initialization sets the correct values
    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        await provider.handle_async_init()

        assert provider.top_tracks_limit == DEFAULT_TOP_TRACKS_LIMIT


async def test_setup_declares_lyrics_feature_only_when_enabled(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() adds ProviderFeature.LYRICS only while the get_lyrics setting is on."""
    provider = await setup(mass_mock, manifest_mock, config_mock)
    assert ProviderFeature.LYRICS not in provider.supported_features

    original_side_effect = config_mock.get_value.side_effect
    config_mock.get_value.side_effect = lambda key, default=None: (
        True if key == CONF_GET_LYRICS else original_side_effect(key, default)
    )
    provider = await setup(mass_mock, manifest_mock, config_mock)
    assert ProviderFeature.LYRICS in provider.supported_features
    # the module-level feature set must stay unchanged
    assert ProviderFeature.LYRICS not in SUPPORTED_FEATURES


async def test_get_config_entries_includes_lyrics_toggle(provider: BandcampProvider) -> None:
    """The lyrics toggle is a plain boolean setting, off by default, not advanced."""
    entries = await provider.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_GET_LYRICS)
    assert entry.type == ConfigEntryType.BOOLEAN
    assert entry.default_value is False
    assert entry.advanced is False


async def test_handle_async_init_with_identity(provider: BandcampProvider) -> None:
    """Test successful async initialization with identity token."""
    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        await provider.handle_async_init()

        mock_client_class.assert_called_once_with(
            session=provider.mass.http_session,
            identity_token="mock_identity_token",
            default_retry_after=3,
            timeout=BANDCAMP_TIMEOUT,
        )
        assert provider._client == mock_client
        assert provider._converters is not None


async def test_handle_async_init_without_identity(mass_mock: Mock, manifest_mock: Mock) -> None:
    """Test async initialization without identity token."""
    config = Mock()
    config.values = {}
    config.get_value.side_effect = lambda key, default=None: (
        default if default is not None else ("INFO" if key == "log_level" else None)
    )
    provider = BandcampProvider(mass_mock, manifest_mock, config)

    with patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        await provider.handle_async_init()

        mock_client_class.assert_called_once_with(
            session=provider.mass.http_session,
            identity_token=None,
            default_retry_after=3,
            timeout=BANDCAMP_TIMEOUT,
        )


@pytest.mark.parametrize(
    "error",
    [BandcampAPIError("boom"), ClientConnectionError("down"), TimeoutError()],
)
async def test_handle_async_init_survives_a_transient_error(
    provider: BandcampProvider, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed login check caused by the network only logs a warning, with the error type."""
    with (
        patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class,
        caplog.at_level(logging.WARNING),
    ):
        mock_client_class.return_value.get_collection_summary = AsyncMock(side_effect=error)
        await provider.handle_async_init()

    assert f"Could not validate Bandcamp login: {error!r}" in caplog.text


async def test_is_streaming_provider(provider: BandcampProvider) -> None:
    """Test that Bandcamp is a streaming provider."""
    assert provider.is_streaming_provider is True


def _search_track_mock(
    *,
    artist_id: int = 123,
    artist_name: str = "Test Artist",
    album_id: int | None = 456,
    track_id: int = 789,
) -> Mock:
    """Construct a SearchResultTrack mock with concrete attribute values."""
    item = Mock(spec=SearchResultTrack)
    item.id = track_id
    item.name = "Track"
    item.artist_id = artist_id
    item.artist_name = artist_name
    item.album_id = album_id
    item.album_name = "Album"
    item.url = ""
    item.image_url = None
    return item


def _search_album_mock(
    *,
    artist_id: int = 123,
    artist_name: str = "Test Artist",
    album_id: int = 456,
    artist_url: str = "",
) -> Mock:
    """Construct a SearchResultAlbum mock with concrete attribute values."""
    item = Mock(spec=SearchResultAlbum)
    item.id = album_id
    item.name = "Album"
    item.artist_id = artist_id
    item.artist_name = artist_name
    item.artist_url = artist_url
    item.url = ""
    item.image_url = None
    return item


def _search_artist_mock(
    *,
    artist_id: int = 123,
    name: str = "Test Artist",
    is_label: bool = False,
    url: str = "",
) -> Mock:
    """Construct a SearchResultArtist mock with concrete attribute values."""
    item = Mock(spec=SearchResultArtist)
    item.id = artist_id
    item.name = name
    item.url = url
    item.image_url = None
    item.tags = None
    item.is_label = is_label
    return item


async def test_search_with_identity(provider: BandcampProvider) -> None:
    """Test search functionality with identity token."""
    mock_search_results = [
        _search_track_mock(),
        _search_album_mock(),
        _search_artist_mock(),
    ]

    with (
        patch.object(provider._client, "search", new_callable=AsyncMock) as mock_search,
        patch.object(provider._converters, "track_from_search") as mock_track_converter,
        patch.object(provider._converters, "album_from_search") as mock_album_converter,
        patch.object(provider._converters, "artist_from_search") as mock_artist_converter,
    ):
        mock_search.return_value = mock_search_results

        mock_track_converter.return_value = Mock()
        mock_album_converter.return_value = Mock()
        mock_artist_converter.return_value = Mock()

        results = await provider.search(
            "test query", [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST], limit=5
        )

        mock_search.assert_called_once_with("test query")
        mock_track_converter.assert_called_once()
        mock_album_converter.assert_called_once()
        mock_artist_converter.assert_called_once()
        assert len(results.tracks) == 1
        assert len(results.albums) == 1
        # The album/track results match the artist `b` result so no
        # synthetic artists are emitted; the count stays at 1.
        assert len(results.artists) == 1


async def test_search_synthesizes_artists_for_label_releases(
    provider: BandcampProvider,
) -> None:
    """
    Surface a synthetic artist when an album's performer != the page owner.

    A label-released album whose credit doesn't appear as a `b` result
    becomes its own artist entry under MediaType.ARTIST search.
    """
    label_id = 441379041
    # `b` result for the label, plus two `a` results — one by the label
    # itself and one by Mortaja (a performer with no band page).
    label_band = _search_artist_mock(
        artist_id=label_id, name="audiophob", url="https://audiophob.bandcamp.com"
    )
    label_album = _search_album_mock(
        artist_id=label_id, artist_name="audiophob", album_id=1198969224
    )
    mortaja_album = _search_album_mock(
        artist_id=label_id,
        artist_name="Mortaja",
        album_id=1938115920,
        artist_url="https://audiophob.bandcamp.com",
    )
    another_album = _search_album_mock(
        artist_id=label_id,
        artist_name="Another Performer",
        album_id=1938115921,
        artist_url="https://audiophob.bandcamp.com",
    )

    with patch.object(provider._client, "search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [label_band, label_album, mortaja_album, another_album]

        results = await provider.search("Mortaja", [MediaType.ALBUM, MediaType.ARTIST], limit=10)

        artist_ids = {a.item_id for a in results.artists}
        # Real label artist + both synthetic performers from audiophob.
        assert artist_ids == {
            str(label_id),
            f"{label_id}:mortaja",
            f"{label_id}:another-performer",
        }
        synthetic = [cast("Artist", artist) for artist in results.artists if ":" in artist.item_id]
        assert len(synthetic) == 2
        assert len({artist.uri for artist in synthetic}) == 2
        assert "https://audiophob.bandcamp.com" not in {artist.uri for artist in synthetic}
        assert {next(iter(artist.provider_mappings)).url for artist in synthetic} == {
            "https://audiophob.bandcamp.com"
        }

        # The album by the label itself should link to the real ID; the
        # performer albums should link to their respective synthetic IDs.
        album_artist_ids = {next(iter(cast("Album", a).artists)).item_id for a in results.albums}
        assert album_artist_ids == {
            str(label_id),
            f"{label_id}:mortaja",
            f"{label_id}:another-performer",
        }


async def test_search_does_not_duplicate_existing_artists(
    provider: BandcampProvider,
) -> None:
    """
    Don't duplicate an artist as a synthetic when a real `b` result matches.

    When an album's performer slug equals the band's own slug, the artist
    link reuses the real `{band_id}` and no synthetic entry is emitted.
    """
    band_id = 3658985110
    real = _search_artist_mock(artist_id=band_id, name="Apollo Brown")
    own_album = _search_album_mock(artist_id=band_id, artist_name="Apollo Brown", album_id=1)

    with patch.object(provider._client, "search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [real, own_album]

        results = await provider.search("Apollo Brown", [MediaType.ARTIST], limit=10)

        assert [a.item_id for a in results.artists] == [str(band_id)]


async def test_search_unifies_label_release_to_real_performer_band(
    provider: BandcampProvider,
) -> None:
    """
    A label-released album whose performer has their own band page links to the real band.

    Regression test for music-assistant/support#5389 / Apollo Brown on
    Hip Dozer. When a user searches by album name (e.g. "Night Moves"),
    Bandcamp's autocomplete returns the album row with ``band_id`` =
    Hip Dozer (the label) and ``artist_name`` = "Apollo Brown" — but no
    ``b`` row for Apollo Brown's own page in the same response. The
    secondary autocomplete lookup for "Apollo Brown" finds the real
    band, and the album's artist link uses that real ``band_id``
    instead of a synthetic ``{label_id}:apollo-brown``. This unifies the
    artist with what direct-search-by-artist would find.
    """
    label_id = 4119123456
    apollo_band_id = 3658985110
    hipdozer_band = _search_artist_mock(artist_id=label_id, name="Hip Dozer", is_label=True)
    night_moves = _search_album_mock(artist_id=label_id, artist_name="Apollo Brown", album_id=1)

    # Primary autocomplete for "Night Moves" returns the album + label.
    # Secondary lookup for "Apollo Brown" returns the real artist `b` row.
    primary_response = [hipdozer_band, night_moves]
    secondary_response = [_search_artist_mock(artist_id=apollo_band_id, name="Apollo Brown")]

    apollo_real_artist = Mock(spec=Artist)
    apollo_real_artist.item_id = str(apollo_band_id)

    async def fake_search(query: str) -> list[Mock]:
        if query == "Apollo Brown":
            return secondary_response
        return primary_response

    with (
        patch.object(provider._client, "search", side_effect=fake_search) as mock_search,
        patch.object(
            provider, "get_artist", new_callable=AsyncMock, return_value=apollo_real_artist
        ) as mock_get_artist,
    ):
        results = await provider.search(
            "Night Moves", [MediaType.ALBUM, MediaType.ARTIST], limit=10
        )

        # The album's artist link points to Apollo Brown's real band_id,
        # NOT a synthetic `{label_id}:apollo-brown`.
        album_artist_ids = {next(iter(cast("Album", a).artists)).item_id for a in results.albums}
        assert album_artist_ids == {str(apollo_band_id)}

        # The artist results include Apollo Brown materialized via get_artist.
        artist_ids = {a.item_id for a in results.artists}
        assert str(apollo_band_id) in artist_ids
        # No synthetic artist for a performer who has their own band page.
        assert not any(":" in a.item_id for a in results.artists)
        # Hip Dozer (the label) is still surfaced — it was a `b` row in
        # the primary response and matches MediaType.ARTIST directly.
        assert str(label_id) in artist_ids

        # Two autocomplete calls: the primary search + one secondary
        # lookup for "Apollo Brown". (Hip Dozer didn't need a lookup —
        # it was already the page owner.)
        assert mock_search.call_count == 2
        mock_get_artist.assert_awaited_once_with(str(apollo_band_id))


async def test_lookup_performer_band_id_caches_negative_result(
    provider: BandcampProvider, mass_mock: Mock
) -> None:
    """
    A performer with no own band page caches the 'not found' outcome.

    The cache layer treats ``None`` as a miss, so we store integer 0 as
    the negative-cache sentinel. A repeated lookup for the same slug
    must NOT trigger a second autocomplete call.
    """
    cache_data: dict[str, int] = {}

    async def fake_get(key: str, **_: object) -> int | None:
        return cache_data.get(key)

    async def fake_set(key: str, value: int, **_: object) -> None:
        cache_data[key] = value

    mass_mock.cache.get.side_effect = fake_get
    mass_mock.cache.set.side_effect = fake_set

    # Secondary search returns no matching b-row for "Mortaja" — the
    # response contains an unrelated label.
    unrelated = _search_artist_mock(artist_id=441379041, name="audiophob", is_label=True)
    with patch.object(
        provider._client, "search", new_callable=AsyncMock, return_value=[unrelated]
    ) as mock_search:
        first = await provider._lookup_performer_band_id("Mortaja")
        second = await provider._lookup_performer_band_id("Mortaja")

        assert first is None
        assert second is None
        # The negative result is cached; the upstream search runs once.
        assert mock_search.call_count == 1
        # Sentinel 0 was written for the slug.
        assert cache_data["performer_band_id.mortaja"] == 0


async def test_lookup_performer_band_id_does_not_cache_api_errors(
    provider: BandcampProvider, mass_mock: Mock
) -> None:
    """Transient autocomplete failures must not become negative cache entries."""
    with (
        patch.object(
            provider._client,
            "search",
            new_callable=AsyncMock,
            side_effect=BandcampAPIError("temporary failure"),
        ),
        pytest.raises(BandcampAPIError, match="temporary failure"),
    ):
        await provider._lookup_performer_band_id("Mortaja")

    mass_mock.cache.set.assert_not_awaited()


async def test_lookup_performer_band_id_skips_label_results(
    provider: BandcampProvider,
) -> None:
    """
    A label that shares a performer's exact name must not be returned as a band.

    Without the ``is_label`` filter, a label called "Apollo Brown"
    (rare but possible) would shadow the actual artist's band page.
    """
    label_with_same_name = _search_artist_mock(artist_id=999, name="Apollo Brown", is_label=True)
    real_artist = _search_artist_mock(artist_id=3658985110, name="Apollo Brown")

    # Order matters: even when the label appears first, we skip it and
    # return the non-label match.
    with patch.object(
        provider._client,
        "search",
        new_callable=AsyncMock,
        return_value=[label_with_same_name, real_artist],
    ):
        result = await provider._lookup_performer_band_id("Apollo Brown")
        assert result == 3658985110


async def test_search_resilient_to_lookup_exception_in_one_slug(
    provider: BandcampProvider,
) -> None:
    """
    One slug's lookup raising must not kill the whole batch.

    ``_resolve_search_artist_ids`` runs lookups in parallel via
    ``asyncio.gather(..., return_exceptions=True)``; an unexpected
    exception in any single lookup must degrade that slug to "no own
    band page" (synthetic fallback) rather than abort the entire search.
    """
    label_a_id = 1111111
    label_b_id = 2222222
    real_b_band_id = 3658985110
    label_a = _search_artist_mock(artist_id=label_a_id, name="Label A", is_label=True)
    label_b = _search_artist_mock(artist_id=label_b_id, name="Label B", is_label=True)
    album_a = _search_album_mock(artist_id=label_a_id, artist_name="Performer A", album_id=10)
    album_b = _search_album_mock(artist_id=label_b_id, artist_name="Performer B", album_id=20)

    async def fake_lookup(name: str) -> int | None:
        if name == "Performer A":
            raise RuntimeError("boom")
        if name == "Performer B":
            return real_b_band_id
        return None

    with (
        patch.object(
            provider._client,
            "search",
            new_callable=AsyncMock,
            return_value=[label_a, label_b, album_a, album_b],
        ),
        patch.object(provider, "_lookup_performer_band_id", side_effect=fake_lookup),
        patch.object(
            provider, "get_artist", new_callable=AsyncMock, return_value=Mock(spec=Artist)
        ),
    ):
        results = await provider.search("test", [MediaType.ALBUM], limit=20)

    # Performer A's lookup blew up → falls through to synthetic.
    # Performer B's lookup succeeded → uses the real band_id.
    album_artist_ids = {next(iter(cast("Album", a).artists)).item_id for a in results.albums}
    assert f"{label_a_id}:performer-a" in album_artist_ids
    assert str(real_b_band_id) in album_artist_ids


async def test_lookup_performer_band_id_corrupt_cache_falls_through(
    provider: BandcampProvider, mass_mock: Mock
) -> None:
    """
    A non-integer cache payload is discarded and a fresh fetch runs.

    Defensive guard: if the cache returns a value that can't be coerced
    to int (schema change, manual edit, …), the lookup must NOT raise
    ``ValueError`` into the parallel ``asyncio.gather`` — it must log,
    discard the entry, and re-fetch from the upstream API.
    """
    mass_mock.cache.get.return_value = "not-an-int"

    real_band_id = 3658985110
    real_artist = _search_artist_mock(artist_id=real_band_id, name="Apollo Brown")
    with patch.object(
        provider._client, "search", new_callable=AsyncMock, return_value=[real_artist]
    ) as mock_search:
        result = await provider._lookup_performer_band_id("Apollo Brown")

    assert result == real_band_id
    mock_search.assert_awaited_once_with("Apollo Brown")


async def test_search_suppresses_retries_exhausted_from_get_artist(
    provider: BandcampProvider,
) -> None:
    """
    ``RetriesExhausted`` from ``get_artist`` must not crash an in-progress search.

    The materialized-artist emission is best-effort — its failure should
    leave album/track results intact rather than propagate up.
    """
    label_id = 4119123456
    apollo_band_id = 3658985110
    label_band = _search_artist_mock(artist_id=label_id, name="Hip Dozer", is_label=True)
    night_moves = _search_album_mock(artist_id=label_id, artist_name="Apollo Brown", album_id=1)
    secondary_response = [_search_artist_mock(artist_id=apollo_band_id, name="Apollo Brown")]

    async def fake_search(query: str) -> list[Mock]:
        if query == "Apollo Brown":
            return secondary_response
        return [label_band, night_moves]

    with (
        patch.object(provider._client, "search", side_effect=fake_search),
        patch.object(
            provider,
            "get_artist",
            new_callable=AsyncMock,
            side_effect=RetriesExhausted("throttle exhausted"),
        ),
    ):
        results = await provider.search("Night Moves", [MediaType.ALBUM, MediaType.ARTIST])

    # Album result still produced despite the artist-materialization failure.
    assert len(results.albums) == 1
    # The artist-materialization branch was suppressed → no Apollo Brown
    # in artists, but Hip Dozer (the in-batch `b` row) is still surfaced.
    artist_ids = {a.item_id for a in results.artists}
    assert str(label_id) in artist_ids
    assert str(apollo_band_id) not in artist_ids


async def test_get_album_unifies_label_release_to_real_performer_band(
    provider: BandcampProvider,
) -> None:
    """
    Fetching a label-released album embeds the performer's real band_id.

    Regression test paralleling
    :func:`test_search_unifies_label_release_to_real_performer_band` but
    on the album-fetch path. When MA opens or syncs a label-released
    album, the artist link must point to the performer's own band page
    rather than a synthetic ID — otherwise list view and detail view
    diverge.
    """
    label_id = 4119123456
    apollo_band_id = 3658985110

    api_album = Mock()
    api_album.artist.id = label_id
    api_album.artist.name = "Hip Dozer"
    api_album.tralbum_artist = "Apollo Brown"

    secondary_response = [_search_artist_mock(artist_id=apollo_band_id, name="Apollo Brown")]

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock, return_value=api_album),
        patch.object(
            provider._client, "search", new_callable=AsyncMock, return_value=secondary_response
        ) as mock_search,
        patch.object(provider._converters, "album_from_api") as mock_converter,
    ):
        mock_converter.return_value = Mock()
        await provider.get_album(f"{label_id}-456")

        mock_converter.assert_called_once_with(api_album, artist_item_id=str(apollo_band_id))
        mock_search.assert_awaited_once_with("Apollo Brown")


LOOKUP_ERRORS = [
    pytest.param(BandcampUnexpectedResponseError("not usable JSON"), id="unusable_answer"),
    pytest.param(BandcampAPIError("API Error"), id="api_error"),
    pytest.param(TimeoutError(), id="timeout"),
]


@pytest.mark.parametrize("error", LOOKUP_ERRORS)
async def test_resolve_artist_item_id_falls_back_when_the_lookup_fails(
    provider: BandcampProvider, error: Exception
) -> None:
    """A failed performer lookup gives the synthetic artist ID and caches nothing."""
    with patch.object(provider._client, "search", new_callable=AsyncMock, side_effect=error):
        item_id = await provider._resolve_artist_item_id(
            band_id=4119123456, performer="Apollo Brown", band_name="Hip Dozer"
        )

    assert item_id == make_artist_id(4119123456, "Apollo Brown")
    mock_cache_set = cast("AsyncMock", provider.mass.cache.set)
    cached_keys = [cache_call.args[0] for cache_call in mock_cache_set.await_args_list]
    assert not [key for key in cached_keys if key.startswith("performer_band_id.")]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RetriesExhausted("throttle exhausted"), id="retries_exhausted"),
        pytest.param(ClientConnectionError("down"), id="dropped_connection"),
    ],
)
async def test_resolve_artist_item_id_falls_back_after_a_fetch_error(
    provider: BandcampProvider, error: Exception
) -> None:
    """A lookup that ends in a fetch error of the core gives the synthetic artist ID."""
    with patch.object(
        provider, "_lookup_performer_band_id", new_callable=AsyncMock, side_effect=error
    ):
        item_id = await provider._resolve_artist_item_id(
            band_id=4119123456, performer="Apollo Brown", band_name="Hip Dozer"
        )

    assert item_id == make_artist_id(4119123456, "Apollo Brown")


async def test_resolve_artist_item_id_lets_a_programming_error_out(
    provider: BandcampProvider,
) -> None:
    """A bug in the performer lookup is not hidden behind the synthetic artist ID."""
    with (
        patch.object(
            provider,
            "_lookup_performer_band_id",
            new_callable=AsyncMock,
            side_effect=AttributeError("bug"),
        ),
        pytest.raises(AttributeError, match="bug"),
    ):
        await provider._resolve_artist_item_id(
            band_id=4119123456, performer="Apollo Brown", band_name="Hip Dozer"
        )


@pytest.mark.parametrize("error", LOOKUP_ERRORS)
async def test_get_album_keeps_a_label_release_when_the_lookup_fails(
    provider: BandcampProvider, error: Exception
) -> None:
    """A label-released album still loads when the search for its performer fails."""
    label_id = 4119123456
    api_album = Mock()
    api_album.artist.id = label_id
    api_album.artist.name = "Hip Dozer"
    api_album.tralbum_artist = "Apollo Brown"

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock, return_value=api_album),
        patch.object(provider._client, "search", new_callable=AsyncMock, side_effect=error),
        patch.object(provider._converters, "album_from_api") as mock_converter,
    ):
        mock_converter.return_value = Mock()
        await provider.get_album(f"{label_id}-456")

    mock_converter.assert_called_once_with(
        api_album, artist_item_id=make_artist_id(label_id, "Apollo Brown")
    )


@pytest.mark.parametrize("error", LOOKUP_ERRORS)
async def test_search_keeps_its_results_when_an_artist_fetch_fails(
    provider: BandcampProvider, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed fetch of a performer's own page drops only that artist, and logs why."""
    caplog.set_level(logging.DEBUG, logger=provider.logger.name)
    label_id = 4119123456
    apollo_band_id = 3658985110
    label_band = _search_artist_mock(artist_id=label_id, name="Hip Dozer", is_label=True)
    night_moves = _search_album_mock(artist_id=label_id, artist_name="Apollo Brown", album_id=1)
    secondary_response = [_search_artist_mock(artist_id=apollo_band_id, name="Apollo Brown")]

    async def fake_search(query: str) -> list[Mock]:
        if query == "Apollo Brown":
            return secondary_response
        return [label_band, night_moves]

    with (
        patch.object(provider._client, "search", side_effect=fake_search),
        patch.object(
            provider._client, "get_artist", new_callable=AsyncMock, side_effect=error
        ) as mock_get_artist,
    ):
        results = await provider.search("Night Moves", [MediaType.ALBUM, MediaType.ARTIST])

    mock_get_artist.assert_awaited_once_with(apollo_band_id)
    assert len(results.albums) == 1
    artist_ids = {artist.item_id for artist in results.artists}
    assert str(label_id) in artist_ids
    assert str(apollo_band_id) not in artist_ids
    assert any(
        f"Skipping artist {apollo_band_id} of the search" in record.getMessage()
        for record in caplog.records
    )


async def test_search_without_identity(provider: BandcampProvider) -> None:
    """Test search returns empty results without identity token."""
    provider._client.identity = None

    results = await provider.search("test query", [MediaType.TRACK])

    assert len(results.tracks) == 0
    assert len(results.albums) == 0
    assert len(results.artists) == 0


@pytest.mark.parametrize("browse", [False, True])
async def test_unexpected_response_error(provider: BandcampProvider, browse: bool) -> None:
    """Preserve the library's response error and guidance in Music Assistant errors."""
    error = BandcampUnexpectedResponseError(
        "The Bandcamp API returned a response that is not usable JSON (HTTP 200). Try again later."
    )
    if browse:
        with pytest.raises(InvalidDataError) as exc:
            async with provider._map_api_errors("Bandcamp browse failed"):
                raise error
    else:
        with (
            patch.object(provider._client, "search", side_effect=error),
            pytest.raises(InvalidDataError) as exc,
        ):
            await provider.search("private query", [MediaType.TRACK])
    context = "Bandcamp browse failed" if browse else "Bandcamp search failed"
    assert str(exc.value) == f"{context}: {error}"
    assert exc.value.__cause__ is error


async def test_search_api_error(provider: BandcampProvider) -> None:
    """Test search handles API errors gracefully."""
    with (
        patch.object(provider._client, "search", side_effect=BandcampAPIError("API Error")),
        pytest.raises(InvalidDataError, match="Bandcamp search failed: API Error"),
    ):
        await provider.search("test query", [MediaType.TRACK])


async def _drain(generator: AsyncGenerator[object]) -> list[object]:
    """Collect every item of an async generator."""
    return [item async for item in generator]


@pytest.mark.parametrize(
    ("client_method", "call_provider"),
    [
        pytest.param("search", lambda p: p.search("query", [MediaType.TRACK]), id="search"),
        pytest.param("get_artist", lambda p: p.get_artist("123"), id="artist"),
        pytest.param("get_artist", lambda p: p.get_artist("123:performer"), id="synthetic_artist"),
        pytest.param("get_album", lambda p: p.get_album("123-456"), id="album"),
        pytest.param("get_album", lambda p: p.get_album_tracks("123-456"), id="album_tracks"),
        pytest.param("get_album", lambda p: p._fetch_api_track("123-456-789"), id="album_track"),
        pytest.param("get_track", lambda p: p._fetch_api_track("123-0-789"), id="single_track"),
        pytest.param(
            "get_artist_discography", lambda p: p.get_artist_albums("123"), id="artist_albums"
        ),
        pytest.param(
            "get_collection_items", lambda p: _drain(p.get_library_albums()), id="library_albums"
        ),
        pytest.param(
            "get_collection_items", lambda p: _drain(p.get_library_artists()), id="library_artists"
        ),
    ],
)
async def test_unusable_answer_gives_the_translated_error(
    provider: BandcampProvider,
    client_method: str,
    call_provider: Callable[[BandcampProvider], Awaitable[object]],
) -> None:
    """Every Bandcamp call turns an answer that is not usable JSON into the translated error."""
    error = BandcampUnexpectedResponseError("not usable JSON (HTTP 200)")
    with (
        patch.object(provider._client, client_method, side_effect=error),
        pytest.raises(InvalidDataError) as exc,
    ):
        await call_provider(provider)
    assert exc.value.translation_key == "unusable_answer"
    assert exc.value.translation_owner == "provider.bandcamp"
    assert exc.value.__cause__ is error


@pytest.mark.parametrize("status", [500, 503])
async def test_a_server_error_page_is_temporary(provider: BandcampProvider, status: int) -> None:
    """An error page of the server can go away, so it gives a temporary error."""
    error = BandcampUnexpectedResponseError("not usable JSON. Try again later.", status=status)
    with pytest.raises(ResourceTemporarilyUnavailable) as exc:
        async with provider._map_api_errors("Failed to get album 456"):
            raise error
    assert str(exc.value) == f"Failed to get album 456: {error}"
    assert exc.value.__cause__ is error


@pytest.mark.parametrize(
    ("status", "outcome", "attempts"),
    [
        pytest.param(503, RetriesExhausted, 5, id="server_error"),
        pytest.param(None, InvalidDataError, 1, id="robot_check"),
        pytest.param(404, InvalidDataError, 1, id="client_error"),
    ],
)
async def test_get_album_retries_only_a_server_error_page(
    provider: BandcampProvider, status: int | None, outcome: type[Exception], attempts: int
) -> None:
    """A server error page gets the retries, and a page that repeats on each attempt does not."""
    error = BandcampUnexpectedResponseError("not usable JSON", status=status)
    with (
        patch.object(provider._client, "get_album", side_effect=error) as mock_get_album,
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(outcome),
    ):
        await provider.get_album("123-456")

    assert mock_get_album.call_count == attempts


async def test_get_artist_success(provider: BandcampProvider) -> None:
    """Test successful artist retrieval."""
    mock_artist = Mock()

    with (
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist,
        patch.object(provider._converters, "artist_from_api") as mock_converter,
    ):
        mock_get_artist.return_value = mock_artist
        mock_converter.return_value = Mock()

        result = await provider.get_artist("123")

        # The composite-ID parser converts the string to an int before
        # forwarding to the underlying client.
        mock_get_artist.assert_called_once_with(123)
        mock_converter.assert_called_once_with(mock_artist)
        assert result is not None


async def test_get_artist_synthetic_builds_from_discography(
    provider: BandcampProvider,
) -> None:
    """
    Genuine label-style synthetic resolves via the band's discography.

    The slug names a per-page performer distinct from the band's own
    name, so the band-name short-circuit doesn't apply and we look up
    the credit in the discography.
    """
    label_id = 441379041
    label_artist = Mock(id=label_id, url="https://audiophob.bandcamp.com")
    label_artist.name = "audiophob"  # the band's own (page-owner) name

    discography = [
        {
            "item_type": "album",
            "band_id": label_id,
            "item_id": 1938115920,
            "title": "Combined Minds",
            "artist_name": "Mortaja",
            "band_name": "audiophob",
            "art_id": 2825942492,
            "release_date": "18 Oct 2018 00:00:00 GMT",
        },
    ]

    with (
        patch.object(
            provider._client, "get_artist_discography", new_callable=AsyncMock
        ) as mock_disco,
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_artist.return_value = label_artist
        mock_disco.return_value = discography

        result = await provider.get_artist(f"{label_id}:mortaja")

        assert result.item_id == f"{label_id}:mortaja"
        assert result.name == "Mortaja"
        # The hosting URL is mapping metadata, not synthetic artist identity.
        assert result.uri == f"bandcamp_test://artist/{label_id}:mortaja"
        mapping = next(iter(result.provider_mappings))
        assert mapping.url == "https://audiophob.bandcamp.com"
        # Both API calls happen: band-name lookup didn't match the slug,
        # so we proceeded to look in the discography.
        mock_get_artist.assert_called_once_with(label_id)
        mock_disco.assert_called_once_with(label_id)


async def test_get_artist_synthetic_band_own_slug_collapses_to_real(
    provider: BandcampProvider,
) -> None:
    """
    Synthetic slug equal to the band's own slug resolves to the real band.

    This is the drift-collapse path. Bandcamp's autocomplete sometimes
    returns ``a``/``t`` rows for a band-by-itself album without the
    matching ``b`` row in the same response; the search-time path mints
    a synthetic ``{band_id}:slug-of-band-own-name`` because it can't
    disambiguate without the ``b`` row. When the user navigates to that
    synthetic, we collapse it back to the real band before MA can
    persist a duplicate library entry.

    Critically, the discography has matching items (band-by-itself
    entries with ``artist_name=null`` fall through to
    ``band_name='Apollo Brown'`` whose slug matches), but we must NOT
    build a shadow synthetic from them.
    """
    band_id = 3658985110
    api_artist = Mock(id=band_id, url="https://apollobrown360.bandcamp.com")
    api_artist.name = "Apollo Brown"

    with (
        patch.object(
            provider._client, "get_artist_discography", new_callable=AsyncMock
        ) as mock_disco,
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_artist.return_value = api_artist
        # Discography includes items that WOULD match the slug if we
        # consulted it (artist_name=null → falls through to band_name
        # which equals "Apollo Brown" → slug "apollo-brown"). The fix
        # depends on never reaching this lookup once the band-name
        # short-circuit fires.
        mock_disco.return_value = [
            {
                "item_type": "album",
                "band_id": band_id,
                "item_id": 999,
                "title": "Skilled Trade",
                "artist_name": None,
                "band_name": "Apollo Brown",
            },
        ]

        result = await provider.get_artist(f"{band_id}:apollo-brown")

        assert result.item_id == str(band_id)
        assert result.name == "Apollo Brown"
        mock_get_artist.assert_called_once_with(band_id)
        # Short-circuit: no discography fetch when the band's own name
        # already matches the synthetic slug.
        mock_disco.assert_not_called()


async def test_get_artist_albums_owner_slug_collapses_to_real_discography(
    provider: BandcampProvider,
) -> None:
    """A legacy owner-slug ID returns the real band's unfiltered discography."""
    band_id = 3658985110
    api_artist = Mock(id=band_id)
    api_artist.name = "Apollo Brown"
    discography = [
        {
            "item_type": "album",
            "band_id": band_id,
            "item_id": 1,
            "title": "Own Album",
            "artist_name": None,
            "band_name": "Apollo Brown",
        },
        {
            "item_type": "album",
            "band_id": band_id,
            "item_id": 2,
            "title": "Another Own Album",
            "artist_name": "Apollo Brown",
            "band_name": "Apollo Brown",
        },
    ]

    with (
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist,
        patch.object(
            provider._client, "get_artist_discography", new_callable=AsyncMock
        ) as mock_get_discography,
        patch.object(provider._client, "search", new_callable=AsyncMock) as mock_search,
    ):
        mock_get_artist.return_value = api_artist
        mock_get_discography.return_value = discography

        result = await provider.get_artist_albums(f"{band_id}:apollo-brown")

    assert {album.name for album in result} == {"Own Album", "Another Own Album"}
    assert all(next(iter(album.artists)).item_id == str(band_id) for album in result)
    mock_get_artist.assert_awaited_once_with(band_id)
    mock_get_discography.assert_awaited_once_with(band_id)
    mock_search.assert_not_awaited()


async def test_lookup_performer_band_ids_propagates_cancellation(
    provider: BandcampProvider,
) -> None:
    """Cancellation must not be converted into a failed lookup result."""
    with (
        patch.object(
            provider,
            "_lookup_performer_band_id",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider._lookup_performer_band_ids_parallel({"mortaja": "Mortaja"})


async def test_lookup_performer_band_ids_reraises_other_base_exception(
    provider: BandcampProvider,
) -> None:
    """Non-Exception BaseExceptions must not be treated as band IDs."""

    class TestBaseException(BaseException):
        pass

    with (
        patch.object(
            provider,
            "_lookup_performer_band_id",
            new_callable=AsyncMock,
            side_effect=TestBaseException,
        ),
        pytest.raises(TestBaseException),
    ):
        await provider._lookup_performer_band_ids_parallel({"mortaja": "Mortaja"})


async def test_get_synthetic_artist_rate_limit_at_band_lookup(
    provider: BandcampProvider,
) -> None:
    """
    Rate-limit on the initial band lookup converts to RateLimited.

    Tests ``_get_synthetic_artist`` directly to bypass the public method's
    ``@throttle_with_retries`` decorator. The decorator's job is to retry
    on this exception; our job is to make sure we *raise* it with the
    backoff hint preserved so the decorator (and any other caller) can
    use it.
    """
    band_id = 441379041
    with patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist:
        mock_get_artist.side_effect = BandcampRateLimitError("Rate limited", retry_after=42)
        with pytest.raises(RateLimited) as exc_info:
            await provider._get_synthetic_artist(f"{band_id}:mortaja", band_id, "mortaja")
        assert exc_info.value.backoff_time == 42


async def test_get_synthetic_artist_rate_limit_at_discography(
    provider: BandcampProvider,
) -> None:
    """
    Rate-limit on the discography fetch surfaces with backoff hint.

    Regression guard: an earlier version of this method caught
    ``BandcampAPIError`` (the parent of ``BandcampRateLimitError``) in a
    single ``except`` clause for the secondary lookup, swallowing the
    backoff hint. Both API call sites must now handle rate-limits
    explicitly.
    """
    band_id = 441379041
    label_artist = Mock(id=band_id)
    label_artist.name = "audiophob"

    with (
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist,
        patch.object(
            provider._client, "get_artist_discography", new_callable=AsyncMock
        ) as mock_disco,
    ):
        mock_get_artist.return_value = label_artist
        mock_disco.side_effect = BandcampRateLimitError("Rate limited", retry_after=99)
        with pytest.raises(RateLimited) as exc_info:
            await provider._get_synthetic_artist(f"{band_id}:mortaja", band_id, "mortaja")
        assert exc_info.value.backoff_time == 99


async def test_get_artist_synthetic_no_matching_performer_raises(
    provider: BandcampProvider,
) -> None:
    """
    Unresolvable synthetic IDs raise MediaNotFoundError.

    A synthetic slug that matches neither the band's own name nor any
    per-item performer credit in the discography is genuinely unknown —
    we surface MediaNotFoundError rather than fabricate a phantom.
    """
    band_id = 441379041
    label_artist = Mock(id=band_id)
    label_artist.name = "audiophob"

    with (
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist,
        patch.object(
            provider._client, "get_artist_discography", new_callable=AsyncMock
        ) as mock_disco,
    ):
        mock_get_artist.return_value = label_artist
        mock_disco.return_value = [
            {
                "item_type": "album",
                "band_id": band_id,
                "item_id": 1,
                "title": "Some Other Album",
                "artist_name": "Some Other Artist",
                "band_name": "audiophob",
            },
        ]
        with pytest.raises(MediaNotFoundError):
            await provider.get_artist(f"{band_id}:nonexistent-performer")


async def test_get_artist_malformed_id_raises(provider: BandcampProvider) -> None:
    """Non-numeric band_id portions surface as InvalidDataError."""
    with pytest.raises(InvalidDataError, match=r"Malformed Bandcamp artist ID"):
        await provider.get_artist("not-a-number")


async def test_get_artist_not_found(provider: BandcampProvider) -> None:
    """Test artist retrieval when not found."""
    with (
        patch.object(
            provider._client, "get_artist", side_effect=BandcampNotFoundError("Not found")
        ),
        pytest.raises(MediaNotFoundError, match=r"Artist 123 not found on Bandcamp"),
    ):
        await provider.get_artist("123")


async def test_get_album_success(provider: BandcampProvider) -> None:
    """Test successful album retrieval."""
    mock_album = Mock()
    mock_album.artist.id = 123
    mock_album.artist.name = "Test Band"
    mock_album.tralbum_artist = None

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "album_from_api") as mock_converter,
    ):
        mock_get_album.return_value = mock_album
        mock_converter.return_value = Mock()

        result = await provider.get_album("123-456")

        mock_get_album.assert_called_once_with(123, 456)
        # Plain band_id passes through when there's no performer credit.
        mock_converter.assert_called_once_with(mock_album, artist_item_id="123")
        assert result is not None


async def test_get_track_success(provider: BandcampProvider) -> None:
    """Test successful track retrieval, end to end through the album-listing path."""
    mock_album = Mock()
    mock_track = Mock()
    mock_album.tracks = [mock_track]
    mock_album.artist.id = 123
    mock_album.artist.name = "Test Band"
    mock_album.tralbum_artist = None
    mock_track.id = 789

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_album.return_value = mock_album
        mock_converter.return_value = _mapped_track("123-456-789")

        result = await provider.get_track("123-456-789")

        mock_get_album.assert_called_once_with(123, 456)
        assert result is not None


async def test_get_track_standalone(provider: BandcampProvider) -> None:
    """A track asked for without its album takes the album and the cover from get_track."""
    mock_api_track = Mock()
    # get_track never sets `album`, it names the album in album_id and album_title
    mock_api_track.album = None
    mock_api_track.album_id = 456
    mock_api_track.album_title = "Standalone Album"
    mock_api_track.art_url = "http://example.com/art.jpg"
    # Standalone single tracks carry their own performer credit; the
    # provider forwards it to the converter so synthetic IDs are emitted
    # for label-released singles. Performer matches band name here so the
    # secondary lookup short-circuits to the plain band_id.
    mock_api_track.tralbum_artist = "Test Artist"
    mock_api_track.artist.id = 123
    mock_api_track.artist.name = "Test Artist"

    with (
        patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_track.return_value = mock_api_track
        mock_converter.return_value = _mapped_track("123-456-789")

        result = await provider.get_track("123-0-789")

        mock_get_track.assert_called_once_with(123, 789)
        mock_converter.assert_called_once_with(
            track=mock_api_track,
            album_id=456,
            album_name="Standalone Album",
            album_image_url="http://example.com/art.jpg",
            tralbum_artist="Test Artist",
            artist_item_id="123",
        )
        assert result is not None


async def test_get_track_standalone_no_album(provider: BandcampProvider) -> None:
    """A single has no album, and it keeps its own cover."""
    mock_api_track = Mock()
    mock_api_track.album = None
    mock_api_track.album_id = None
    mock_api_track.album_title = None
    mock_api_track.art_url = "http://example.com/single.jpg"
    mock_api_track.tralbum_artist = None
    mock_api_track.artist.id = 123
    mock_api_track.artist.name = "Test Band"

    with (
        patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_track.return_value = mock_api_track
        mock_converter.return_value = _mapped_track("123-0-789")

        result = await provider.get_track("123-0-789")

        mock_get_track.assert_called_once_with(123, 789)
        mock_converter.assert_called_once_with(
            track=mock_api_track,
            album_id=None,
            album_name="",
            album_image_url="http://example.com/single.jpg",
            tralbum_artist=None,
            artist_item_id="123",
        )
        assert result is not None


def _api_track(art_url: str | None, album_id: int | None = None) -> BCTrack:
    """Build a streamable library track of band 123 with the ID 789."""
    return BCTrack(
        id=789,
        title="Track",
        artist=BCArtist(id=123, name="Test Band"),
        duration=200.0,
        streaming_url={"mp3-128": "http://example.com/track.mp3"},
        album_id=album_id,
        album_title="Album" if album_id else None,
        art_url=art_url,
    )


def _image_paths(track: Track) -> list[str]:
    """Return the image paths of a converted track."""
    return [image.path for image in track.metadata.images or []]


async def test_get_track_without_album_part_gets_the_album_listing_id(
    provider: BandcampProvider,
) -> None:
    """A track of an album asked for as 123-0-789 gets the ID that its album listing gives."""
    api_track = _api_track("http://example.com/own.jpg", album_id=456)
    with patch.object(
        provider._client, "get_track", new_callable=AsyncMock, return_value=api_track
    ):
        result = await provider.get_track("123-0-789")

    assert result.item_id == "123-456-789"
    assert result.album is not None
    assert (result.album.item_id, result.album.name) == ("123-456", "Album")
    assert _image_paths(result) == ["http://example.com/own.jpg"]


async def test_get_album_tracks_prefers_the_track_cover(provider: BandcampProvider) -> None:
    """A track with a cover of its own shows that cover, the others show the album cover."""
    own_cover = _api_track("http://example.com/own.jpg")
    no_cover = _api_track(None)
    no_cover.id = 790
    api_album = Mock()
    api_album.tracks = [own_cover, no_cover]
    api_album.title = "Album"
    api_album.art_url = "http://example.com/album.jpg"
    api_album.artist.id = 123
    api_album.artist.name = "Test Band"
    api_album.tralbum_artist = None

    with patch.object(
        provider._client, "get_album", new_callable=AsyncMock, return_value=api_album
    ):
        result = await provider.get_album_tracks("123-456")

    assert [_image_paths(track) for track in result] == [
        ["http://example.com/own.jpg"],
        ["http://example.com/album.jpg"],
    ]


def _album_with_a_hidden_track() -> Mock:
    """Build an album of band 123 with a streamable track 789 and a hidden track 790."""
    hidden = _api_track(None)
    hidden.id = 790
    hidden.streaming_url = None
    api_album = Mock()
    api_album.tracks = [_api_track(None), hidden]
    api_album.id = 456
    api_album.title = "Album"
    api_album.art_url = "http://example.com/album.jpg"
    api_album.artist.id = 123
    api_album.artist.name = "Test Band"
    api_album.tralbum_artist = None
    return api_album


async def test_get_album_tracks_keeps_a_track_without_a_stream(provider: BandcampProvider) -> None:
    """A hidden track stays in the album listing, marked unavailable, like the references do."""
    with patch.object(
        provider._client,
        "get_album",
        new_callable=AsyncMock,
        return_value=_album_with_a_hidden_track(),
    ):
        result = await provider.get_album_tracks("123-456")

    assert [(track.item_id, track.available) for track in result] == [
        ("123-456-789", True),
        ("123-456-790", False),
    ]


async def test_get_track_finds_a_hidden_track_in_the_album_listing(
    provider: BandcampProvider,
) -> None:
    """A hidden track comes from the cached album listing, with no request of its own."""
    with (
        patch.object(
            provider._client,
            "get_album",
            new_callable=AsyncMock,
            return_value=_album_with_a_hidden_track(),
        ),
        patch.object(provider, "_fetch_api_track", new_callable=AsyncMock) as mock_fetch,
    ):
        result = await provider.get_track("123-456-790")

    assert (result.item_id, result.available) == ("123-456-790", False)
    mock_fetch.assert_not_awaited()


async def test_get_track_album_fallback_prefers_the_track_cover(provider: BandcampProvider) -> None:
    """A track that the album listing lacks keeps its own cover on the fresh path."""
    api_album = Mock()
    api_album.tracks = [_api_track("http://example.com/own.jpg")]
    api_album.id = 456
    api_album.title = "Album"
    api_album.art_url = "http://example.com/album.jpg"
    api_album.artist.id = 123
    api_album.artist.name = "Test Band"
    api_album.tralbum_artist = None

    with (
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock, return_value=[]),
        patch.object(provider._client, "get_album", new_callable=AsyncMock, return_value=api_album),
    ):
        result = await provider.get_track("123-456-789")

    assert _image_paths(result) == ["http://example.com/own.jpg"]


class _CacheLookup(Exception):
    """Stops a cached call at its cache lookup."""


@pytest.mark.parametrize(
    ("call_provider", "checksum"),
    [
        pytest.param(lambda p: p.get_artist("123"), PARSED_ITEM_CACHE_CHECKSUM, id="artist"),
        pytest.param(lambda p: p.get_album("123-456"), PARSED_ITEM_CACHE_CHECKSUM, id="album"),
        pytest.param(
            lambda p: p._get_fetched_track_monthly("123-0-789"),
            PARSED_ITEM_CACHE_CHECKSUM,
            id="fetched_track_monthly",
        ),
        pytest.param(
            lambda p: p._get_fetched_track_daily("123-0-789"),
            PARSED_ITEM_CACHE_CHECKSUM,
            id="fetched_track_daily",
        ),
        pytest.param(
            lambda p: p.get_album_tracks("123-456"), PARSED_ITEM_CACHE_CHECKSUM, id="album_tracks"
        ),
        pytest.param(
            lambda p: p._get_album_tracks_daily("123-456"),
            PARSED_ITEM_CACHE_CHECKSUM,
            id="album_tracks_daily",
        ),
        pytest.param(
            lambda p: p.get_artist_albums("123"), PARSED_ITEM_CACHE_CHECKSUM, id="artist_albums"
        ),
        pytest.param(
            lambda p: p.get_artist_toptracks("123"), PARSED_ITEM_CACHE_CHECKSUM, id="top_tracks"
        ),
        pytest.param(lambda p: p._fetch_discography(123), None, id="raw_discography"),
        pytest.param(lambda p: p._get_tralbum_lyrics(456, True), None, id="raw_lyrics"),
    ],
)
async def test_cache_checksum_of_each_cached_call(
    provider: BandcampProvider,
    call_provider: Callable[[BandcampProvider], Awaitable[object]],
    checksum: str | None,
) -> None:
    """A cached converted item carries the checksum, so a bump drops the old cache rows."""
    lookup = cast("AsyncMock", provider.mass.cache.get_with_freshness)
    lookup.side_effect = _CacheLookup

    with pytest.raises(_CacheLookup):
        await call_provider(provider)

    assert lookup.await_args is not None
    assert lookup.await_args.kwargs["checksum"] == checksum


async def test_get_album_tracks_serves_an_expired_listing_and_refreshes_it(
    provider: BandcampProvider, mass_mock: Mock
) -> None:
    """An expired album listing comes back at once, and a background task fetches it again."""
    old_track = Track(
        item_id="123-456-789",
        provider="bandcamp_test",
        name="Old name",
        provider_mappings={
            ProviderMapping(
                item_id="123-456-789", provider_domain="bandcamp", provider_instance="bandcamp_test"
            )
        },
    )
    mass_mock.cache.get_with_freshness.return_value = ([old_track.to_dict()], False, True)

    with patch.object(
        provider._client,
        "get_album",
        new_callable=AsyncMock,
        side_effect=_album_over_the_network(_album_with_a_hidden_track()),
    ) as mock_get_album:
        result = await provider.get_album_tracks("123-456")
        assert [track.name for track in result] == ["Old name"]
        await _let_background_tasks_finish(mass_mock)

    lookup = mass_mock.cache.get_with_freshness.await_args
    assert lookup.kwargs["include_expired"] is True
    mock_get_album.assert_awaited_once()
    stored = mass_mock.cache.set.await_args
    assert stored.kwargs["allow_expired_cache"] is True
    assert [track.item_id for track in stored.kwargs["data"]] == ["123-456-789", "123-456-790"]


@pytest.mark.parametrize(
    ("second_track_streams", "from_daily_listing"),
    [
        pytest.param(True, False, id="every_track_streams"),
        pytest.param(False, True, id="a_track_without_a_stream"),
    ],
)
async def test_get_album_tracks_takes_a_changing_album_from_the_daily_listing(
    provider: BandcampProvider, second_track_streams: bool, from_daily_listing: bool
) -> None:
    """An album with a track without a stream comes from the listing of one day."""
    monthly = [
        _mapped_track("123-456-789"),
        _mapped_track("123-456-790", available=second_track_streams),
    ]
    daily = [_mapped_track("123-456-789"), _mapped_track("123-456-790")]
    # the choice reads the Bandcamp mapping, so a provider reload does not change it
    await set_global_cache_values({"available_providers": {"other_instance"}})

    with (
        patch.object(
            provider, "_get_album_tracks_monthly", new_callable=AsyncMock, return_value=monthly
        ),
        patch.object(
            provider, "_get_album_tracks_daily", new_callable=AsyncMock, return_value=daily
        ) as mock_daily,
    ):
        result = await provider.get_album_tracks("123-456")

    assert result is (daily if from_daily_listing else monthly)
    assert mock_daily.await_count == int(from_daily_listing)


@pytest.mark.parametrize(
    ("listing", "expiration"),
    [
        pytest.param("_get_album_tracks_monthly", CACHE_METADATA, id="monthly"),
        pytest.param("_get_album_tracks_daily", CACHE_CHANGING_LISTING, id="daily"),
    ],
)
async def test_album_track_listings_keep_their_cache_time(
    provider: BandcampProvider, mass_mock: Mock, listing: str, expiration: int
) -> None:
    """Each album track listing stores its own cache time, and both serve an expired row."""
    with patch.object(
        provider._client,
        "get_album",
        new_callable=AsyncMock,
        return_value=_album_with_a_hidden_track(),
    ):
        await getattr(provider, listing)("123-456")
        await _let_background_tasks_finish(mass_mock)

    stored = mass_mock.cache.set.await_args
    assert stored.kwargs["expiration"] == expiration
    assert stored.kwargs["allow_expired_cache"] is True


class _ClockCache:
    """A dict-backed stand-in for the cache of use_cache, with a clock that the test moves."""

    def __init__(self) -> None:
        self.now = 0.0
        self.rows: dict[str, tuple[Any, float, str | None]] = {}

    async def get_with_freshness(
        self, key: str, *, checksum: str | None = None, include_expired: bool = False, **_: Any
    ) -> tuple[Any, bool, bool]:
        row = self.rows.get(key)
        if row is None or row[2] != checksum:
            return None, False, False
        data, expires, _checksum = row
        if expires > self.now:
            return data, True, True
        if include_expired:
            return data, False, True
        return None, False, False

    async def set(
        self, key: str, data: Any, *, expiration: int, checksum: str | None = None, **_: Any
    ) -> None:
        stored = [item.to_dict() for item in data] if isinstance(data, list) else data.to_dict()
        self.rows[key] = (stored, self.now + expiration, checksum)


async def _let_background_tasks_finish(mass: Mock) -> None:
    """Wait until the tasks that use_cache started in the background have finished."""
    async with asyncio.timeout(5):
        while pending := [task for task in mass._tracked_tasks.values() if not task.done()]:
            await asyncio.gather(*pending)


def _album_over_the_network(api_album: Mock) -> Callable[..., Awaitable[Mock]]:
    """Return a stand-in for get_album that suspends, as a real request does."""

    async def get_album(*_: object) -> Mock:
        # the suspension lets the background cache work outlive the call that started it
        await asyncio.sleep(0.01)
        return api_album

    return get_album


async def test_get_track_follows_its_album_listing_when_a_track_opens(
    provider: BandcampProvider, mass_mock: Mock
) -> None:
    """A track page follows its album listing when the track opens."""
    cache = _ClockCache()
    mass_mock.cache.get_with_freshness = cache.get_with_freshness
    mass_mock.cache.set = cache.set
    api_album = _album_with_a_hidden_track()

    with patch.object(
        provider._client, "get_album", side_effect=_album_over_the_network(api_album)
    ):
        before = await provider.get_track("123-456-790")
        await _let_background_tasks_finish(mass_mock)
        api_album.tracks[1].streaming_url = {"mp3-128": "http://example.com/opened.mp3"}
        cache.now += CACHE_CHANGING_LISTING + 60
        # the first visit after a day serves the expired listing and refreshes it
        await provider.get_track("123-456-790")
        await _let_background_tasks_finish(mass_mock)
        after = await provider.get_track("123-456-790")

    assert before.available is False
    assert after.available is True
    assert not [key for key in cache.rows if key.startswith(("_get_track_base", "_get_fetched"))]


@pytest.mark.parametrize(
    ("track_streams", "from_daily_cache"),
    [
        pytest.param(True, False, id="the_track_streams"),
        pytest.param(False, True, id="the_track_has_no_stream"),
    ],
)
async def test_get_track_takes_a_fetched_track_without_a_stream_from_the_daily_cache(
    provider: BandcampProvider, track_streams: bool, from_daily_cache: bool
) -> None:
    """A single without a stream comes from the one-day cache, also while the provider reloads."""
    monthly = _mapped_track("123-0-789", available=track_streams)
    daily = _mapped_track("123-0-789")
    await set_global_cache_values({"available_providers": {"other_instance"}})

    with (
        patch.object(
            provider, "_get_fetched_track_monthly", new_callable=AsyncMock, return_value=monthly
        ),
        patch.object(
            provider, "_get_fetched_track_daily", new_callable=AsyncMock, return_value=daily
        ) as mock_daily,
    ):
        result = await provider._get_track_base("123-0-789")

    assert result is (daily if from_daily_cache else monthly)
    assert mock_daily.await_count == int(from_daily_cache)


@pytest.mark.parametrize(
    ("cached_call", "expiration"),
    [
        pytest.param("_get_fetched_track_monthly", CACHE_METADATA, id="monthly"),
        pytest.param("_get_fetched_track_daily", CACHE_CHANGING_LISTING, id="daily"),
    ],
)
async def test_fetched_tracks_keep_their_cache_time(
    provider: BandcampProvider, mass_mock: Mock, cached_call: str, expiration: int
) -> None:
    """Each cache of a track fetched on its own stores its own cache time, and no stale row."""
    with patch.object(
        provider._client, "get_track", new_callable=AsyncMock, return_value=_api_track(None)
    ):
        await getattr(provider, cached_call)("123-0-789")
        await _let_background_tasks_finish(mass_mock)

    stored = mass_mock.cache.set.await_args
    assert stored.kwargs["expiration"] == expiration
    assert stored.kwargs["allow_expired_cache"] is False


async def test_get_track_not_found(provider: BandcampProvider) -> None:
    """Test track retrieval when not found."""
    with (
        patch.object(provider._client, "get_album", side_effect=BandcampNotFoundError("Not found")),
        pytest.raises(MediaNotFoundError, match=r"Track 123-456-789 not found on Bandcamp"),
    ):
        await provider.get_track("123-456-789")


def test_installed_client_serves_every_method_the_provider_calls() -> None:
    """
    The installed bandcamp-async-api carries every client method the provider calls.

    The provider fixtures mock the client, so a method missing from the
    installed library would never fail a mocked test; this pins the real
    API surface (get_album_lyrics/get_track_lyrics arrived in 0.2.4).
    """
    for name in (
        "search",
        "get_album",
        "get_track",
        "get_artist",
        "get_artist_discography",
        "get_collection_summary",
        "get_collection_items",
        "get_feed",
        "get_album_lyrics",
        "get_track_lyrics",
    ):
        assert callable(getattr(BandcampAPIClient, name, None)), name


def _enable_lyrics(provider: BandcampProvider) -> None:
    """Flip the get_lyrics setting on for an already-built provider fixture."""
    get_value = cast("Mock", provider.config.get_value)
    original = get_value.side_effect
    get_value.side_effect = lambda key, default=None: (
        True if key == CONF_GET_LYRICS else original(key, default)
    )


async def test_get_track_album_path_reuses_album_listing(provider: BandcampProvider) -> None:
    """The album path picks the track from the cached album listing, not a fresh fetch."""
    with (
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_album_tracks,
        patch.object(provider, "_fetch_api_track", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_album_tracks.return_value = [
            Mock(item_id="123-456-788"),
            Mock(item_id="123-456-789"),
        ]

        result = await provider.get_track("123-456-789")

        mock_album_tracks.assert_awaited_once_with("123-456")
        mock_fetch.assert_not_awaited()
        assert result.item_id == "123-456-789"


async def test_get_track_album_path_falls_back_when_track_missing(
    provider: BandcampProvider,
) -> None:
    """A track absent from the album listing (e.g. no streaming URL) uses the fresh path."""
    mock_album = Mock()
    mock_track = Mock()
    mock_track.id = 789
    mock_album.tracks = [mock_track]
    mock_album.artist.id = 123
    mock_album.artist.name = "Test Band"
    mock_album.tralbum_artist = None

    with (
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_album_tracks,
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_album_tracks.return_value = [Mock(item_id="123-456-788")]
        mock_get_album.return_value = mock_album
        mock_converter.return_value = _mapped_track("123-456-789")

        result = await provider.get_track("123-456-789")

        mock_get_album.assert_called_once_with(123, 456)
        assert result.item_id == "123-456-789"


async def test_get_track_attaches_lyrics_when_enabled(provider: BandcampProvider) -> None:
    """With the setting on, the album path fills metadata.lyrics from one album-wide request."""
    _enable_lyrics(provider)
    with (
        patch.object(provider._client, "get_album_lyrics", new_callable=AsyncMock) as mock_lyrics,
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
    ):
        mock_base.return_value = Mock(item_id="123-456-789")
        mock_lyrics.return_value = {789: "la la", 788: None}

        track = await provider.get_track("123-456-789")

        mock_lyrics.assert_awaited_once_with(456)
        assert track.metadata.lyrics == "la la"


async def test_get_track_lyrics_standalone_uses_track_method(
    provider: BandcampProvider,
) -> None:
    """A standalone track (album_id=0) asks the track lyrics method with its own id."""
    _enable_lyrics(provider)
    with (
        patch.object(provider._client, "get_track_lyrics", new_callable=AsyncMock) as mock_lyrics,
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
    ):
        mock_base.return_value = Mock(item_id="123-0-789")
        mock_lyrics.return_value = {789: "text"}

        track = await provider.get_track("123-0-789")

        mock_lyrics.assert_awaited_once_with(789)
        assert track.metadata.lyrics == "text"


async def test_get_track_no_lyrics_calls_when_disabled(provider: BandcampProvider) -> None:
    """With the setting off (the default), get_track makes zero lyrics requests."""
    with (
        patch.object(
            provider._client, "get_album_lyrics", new_callable=AsyncMock
        ) as mock_album_lyrics,
        patch.object(
            provider._client, "get_track_lyrics", new_callable=AsyncMock
        ) as mock_track_lyrics,
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
    ):
        mock_base.return_value = Mock(item_id="123-456-789")

        await provider.get_track("123-456-789")

        mock_album_lyrics.assert_not_awaited()
        mock_track_lyrics.assert_not_awaited()


async def test_get_track_lyrics_error_does_not_fail(provider: BandcampProvider) -> None:
    """A lyrics failure is swallowed: the lookup succeeds and existing text is untouched."""
    _enable_lyrics(provider)
    base_track = Mock(item_id="123-456-789")
    base_track.metadata.lyrics = "existing text"
    with (
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
        patch.object(
            provider._client, "get_album_lyrics", side_effect=BandcampAPIError("boom")
        ) as mock_lyrics,
    ):
        mock_base.return_value = base_track

        track = await provider.get_track("123-456-789")

        mock_lyrics.assert_awaited_once()
        assert track is base_track
        assert track.metadata.lyrics == "existing text"


async def test_get_track_lyrics_none_entry_keeps_existing_text(
    provider: BandcampProvider,
) -> None:
    """A track without lyrics in the map does not clobber text set by the converters."""
    _enable_lyrics(provider)
    base_track = Mock(item_id="123-456-789")
    base_track.metadata.lyrics = "from converter"
    with (
        patch.object(provider._client, "get_album_lyrics", new_callable=AsyncMock) as mock_lyrics,
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
    ):
        mock_base.return_value = base_track
        mock_lyrics.return_value = {789: None}

        track = await provider.get_track("123-456-789")

        assert track.metadata.lyrics == "from converter"


async def test_get_track_lyrics_two_part_id_uses_track_method(
    provider: BandcampProvider,
) -> None:
    """A two-part id (artist-track) normalizes to a standalone track for the lyrics lookup."""
    _enable_lyrics(provider)
    with (
        patch.object(provider._client, "get_track_lyrics", new_callable=AsyncMock) as mock_lyrics,
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
    ):
        mock_base.return_value = Mock(item_id="123-789")
        mock_lyrics.return_value = {789: "text"}

        track = await provider.get_track("123-789")

        mock_lyrics.assert_awaited_once_with(789)
        assert track.metadata.lyrics == "text"


async def test_get_tralbum_lyrics_routes_and_str_keys(provider: BandcampProvider) -> None:
    """The map layer routes album and track ids to the right client method and str-keys."""
    with (
        patch.object(provider._client, "get_album_lyrics", new_callable=AsyncMock) as mock_album,
        patch.object(provider._client, "get_track_lyrics", new_callable=AsyncMock) as mock_track,
    ):
        mock_album.return_value = {788: "one", 789: None}
        mock_track.return_value = {789: "solo"}

        assert await provider._get_tralbum_lyrics(456, True) == {"788": "one", "789": None}
        mock_album.assert_awaited_once_with(456)
        mock_track.assert_not_awaited()

        assert await provider._get_tralbum_lyrics(789, False) == {"789": "solo"}
        mock_track.assert_awaited_once_with(789)


async def test_get_track_lyrics_track_as_album_id_attaches(
    provider: BandcampProvider,
) -> None:
    """
    A track-as-album compound id (X-X shape) routes to the album method and reads its own key.

    Measured: for a standalone track asked as an album, the API answers
    album.id == tracks[0].id. The internal track fallback itself is the
    library's and is pinned by the library's own tests.
    """
    _enable_lyrics(provider)
    with (
        patch.object(provider._client, "get_album_lyrics", new_callable=AsyncMock) as mock_lyrics,
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
    ):
        mock_base.return_value = Mock(item_id="123-456-456")
        mock_lyrics.return_value = {456: "the text"}

        track = await provider.get_track("123-456-456")

        mock_lyrics.assert_awaited_once_with(456)
        assert track.metadata.lyrics == "the text"


async def test_tralbum_lyrics_cached_between_tracks(
    provider: BandcampProvider, mass_mock: Mock
) -> None:
    """The second track of the same album reuses the cached lyrics map: one request total."""
    cache_data: dict[str, object] = {}

    async def fake_get_with_freshness(key: str, **_kwargs: object) -> tuple[object, bool, bool]:
        return (cache_data.get(key), True, key in cache_data)

    async def fake_set(key: str, data: object, **_kwargs: object) -> None:
        cache_data[key] = data

    mass_mock.cache.get_with_freshness.side_effect = fake_get_with_freshness
    mass_mock.cache.set.side_effect = fake_set
    _enable_lyrics(provider)

    with (
        patch.object(provider._client, "get_album_lyrics", new_callable=AsyncMock) as mock_lyrics,
        patch.object(provider, "_get_track_base", new_callable=AsyncMock) as mock_base,
    ):
        mock_base.side_effect = lambda prov_track_id: Mock(item_id=prov_track_id)
        mock_lyrics.return_value = {788: "one", 789: "two"}

        first = await provider.get_track("123-456-788")
        # the cache store runs as a background task; let it finish
        for _ in range(3):
            await asyncio.sleep(0)
        second = await provider.get_track("123-456-789")

        mock_lyrics.assert_awaited_once_with(456)
        assert first.metadata.lyrics == "one"
        assert second.metadata.lyrics == "two"


async def test_get_album_tracks_success(provider: BandcampProvider) -> None:
    """Test successful album tracks retrieval."""
    mock_album = Mock()
    mock_track = Mock()
    mock_track.streaming_url = {"mp3-128": "http://example.com/track.mp3"}
    mock_album.tracks = [mock_track]
    mock_album.title = "Test Album"
    mock_album.art_url = "http://example.com/art.jpg"
    mock_album.artist.id = 123
    mock_album.artist.name = "Test Band"
    mock_album.tralbum_artist = None

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_album.return_value = mock_album
        mock_converter.return_value = _mapped_track("123-456-789")

        result = await provider.get_album_tracks("123-456")

        assert len(result) == 1
        mock_converter.assert_called_once()


async def test_get_artist_albums_success(provider: BandcampProvider) -> None:
    """Test successful artist albums retrieval converts discography items directly."""
    mock_discography = [
        {
            "item_type": "album",
            "band_id": 123,
            "item_id": 456,
            "title": "Test Album",
            "artist_name": "Test Artist",
            "band_name": "Test Artist",
            "art_id": 9876543210,
            "release_date": "21 Feb 2020 00:00:00 GMT",
        },
        {"item_type": "track", "band_id": 123, "item_id": 789},  # should be skipped
    ]

    with patch.object(
        provider._client, "get_artist_discography", new_callable=AsyncMock
    ) as mock_get_discography:
        mock_get_discography.return_value = mock_discography

        result = await provider.get_artist_albums("123")

        mock_get_discography.assert_called_once_with(123)
        assert len(result) == 1
        assert result[0].item_id == "123-456"
        assert result[0].name == "Test Album"
        assert result[0].year == 2020


async def test_get_artist_albums_label_uses_band_id(provider: BandcampProvider) -> None:
    """Test that label discography uses each album's band_id, not the label's ID."""
    mock_discography = [
        {
            "item_type": "album",
            "band_id": 9999,  # actual artist, different from label ID
            "item_id": 100,
            "title": "Artist Album",
            "artist_name": "Some Artist",
            "band_name": "Some Artist",
            "art_id": 1111,
            "release_date": "01 Jan 2023 00:00:00 GMT",
        },
    ]

    with patch.object(
        provider._client, "get_artist_discography", new_callable=AsyncMock
    ) as mock_get_discography:
        mock_get_discography.return_value = mock_discography

        # Query with label ID "555", but album should use band_id 9999
        result = await provider.get_artist_albums("555")

        assert len(result) == 1
        assert result[0].item_id == "9999-100"  # band_id, not label ID
        artists = list(result[0].artists)
        # artist_name == band_name → real artist ID for the album's own band.
        assert artists[0].item_id == "9999"


async def test_get_artist_albums_label_unifies_performer_to_real_band(
    provider: BandcampProvider,
) -> None:
    """
    Listing a label's discography unifies label-released performers to their real band pages.

    When the user navigates to a label artist (e.g. Hip Dozer), the
    discography listing must use real performer band_ids — matching what
    ``get_album`` would emit on click. Otherwise the album list shows
    synthetic IDs while the album detail shows real IDs, sending the
    same artist-name link to two different destinations.
    """
    label_id = 4119123456
    apollo_band_id = 3658985110
    mock_discography = [
        {
            "item_type": "album",
            "band_id": label_id,
            "item_id": 686338649,
            "title": "Night Moves",
            "artist_name": "Apollo Brown",
            "band_name": "Hip Dozer",
            "art_id": 2560657053,
            "release_date": "01 Jan 2020 00:00:00 GMT",
        },
        {
            "item_type": "album",
            "band_id": label_id,
            "item_id": 100,
            "title": "Label Compilation",
            # Band-by-itself row: artist_name == band_name → no lookup.
            "artist_name": "Hip Dozer",
            "band_name": "Hip Dozer",
            "art_id": 1234,
            "release_date": "01 Jun 2021 00:00:00 GMT",
        },
    ]
    secondary_response = [_search_artist_mock(artist_id=apollo_band_id, name="Apollo Brown")]

    with (
        patch.object(
            provider._client,
            "get_artist_discography",
            new_callable=AsyncMock,
            return_value=mock_discography,
        ),
        patch.object(
            provider._client,
            "search",
            new_callable=AsyncMock,
            return_value=secondary_response,
        ) as mock_search,
    ):
        result = await provider.get_artist_albums(str(label_id))

    by_name = {album.name: album for album in result}
    apollo_album = by_name["Night Moves"]
    label_album = by_name["Label Compilation"]

    # Label-released performer with own band page → real band_id, not synthetic.
    assert next(iter(apollo_album.artists)).item_id == str(apollo_band_id)
    # Band-by-itself row: artist_name slug == band_name slug → plain band_id,
    # no lookup attempted.
    assert next(iter(label_album.artists)).item_id == str(label_id)

    # One lookup for the unique unmapped performer; the band-by-itself row
    # short-circuited before reaching the lookup.
    mock_search.assert_awaited_once_with("Apollo Brown")


async def test_get_artist_albums_synthetic_id_filters_discography(
    provider: BandcampProvider,
) -> None:
    """A synthetic artist ID should return only the matching performer's albums."""
    label_id = 441379041
    mock_discography = [
        {
            "item_type": "album",
            "band_id": label_id,
            "item_id": 1938115920,
            "title": "Combined Minds",
            "artist_name": "Mortaja",
            "band_name": "audiophob",
            "art_id": 2825942492,
            "release_date": "18 Oct 2018 00:00:00 GMT",
        },
        {
            "item_type": "album",
            "band_id": label_id,
            "item_id": 4042974093,
            "title": "Basalt",
            "artist_name": "Spherical Disrupted",
            "band_name": "audiophob",
            "art_id": 1234,
            "release_date": "01 Jan 2024 00:00:00 GMT",
        },
        {
            "item_type": "album",
            "band_id": label_id,
            "item_id": 1185688687,
            "title": "Bone Chamber",
            "artist_name": "Mortaja",
            "band_name": "audiophob",
            "art_id": 5678,
            "release_date": "01 Jan 2017 00:00:00 GMT",
        },
    ]

    with (
        patch.object(
            provider._client,
            "get_artist",
            new_callable=AsyncMock,
        ) as mock_get_artist,
        patch.object(
            provider._client, "get_artist_discography", new_callable=AsyncMock
        ) as mock_get_discography,
        patch.object(
            provider._client, "search", new_callable=AsyncMock, return_value=[]
        ) as mock_search,
    ):
        mock_get_artist.return_value = Mock(id=label_id)
        mock_get_artist.return_value.name = "audiophob"
        mock_get_discography.return_value = mock_discography

        result = await provider.get_artist_albums(f"{label_id}:mortaja")

        mock_get_artist.assert_awaited_once_with(label_id)
        mock_get_discography.assert_awaited_once_with(label_id)
        mock_search.assert_awaited_once_with("Mortaja")
        # Only the two Mortaja albums; the Spherical Disrupted entry is filtered out.
        assert {album.name for album in result} == {"Combined Minds", "Bone Chamber"}
        for album in result:
            artist = next(iter(album.artists))
            assert artist.item_id == f"{label_id}:mortaja"


async def test_get_stream_details_success(provider: BandcampProvider) -> None:
    """Test stream details fetches fresh URL and audio format from API."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {"mp3-320": "http://example.com/track.mp3"}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        result = await provider.get_stream_details("123-456-789", MediaType.TRACK)

        mock_get_album.assert_called_once_with(123, 456)
        assert isinstance(result, StreamDetails)
        assert result.item_id == "123-456-789"
        assert result.media_type == MediaType.TRACK
        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://example.com/track.mp3"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 320


async def test_get_stream_details_vbr(provider: BandcampProvider) -> None:
    """Test stream details with VBR mp3-v0 format."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {"mp3-v0": "http://example.com/track-v0.mp3"}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        result = await provider.get_stream_details("123-456-789", MediaType.TRACK)

        assert result.path == "http://example.com/track-v0.mp3"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate is None


async def test_get_stream_details_no_streaming_url(provider: BandcampProvider) -> None:
    """Test stream details when API track has no streaming URL."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        with pytest.raises(UnplayableMediaError, match=r"No streaming URL found"):
            await provider.get_stream_details("123-456-789", MediaType.TRACK)


async def test_get_stream_details_none_streaming_url(provider: BandcampProvider) -> None:
    """Test stream details when API track has streaming_url=None."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = None
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        with pytest.raises(UnplayableMediaError, match=r"No streaming URL found"):
            await provider.get_stream_details("123-456-789", MediaType.TRACK)


async def test_get_stream_details_bypasses_cache(provider: BandcampProvider) -> None:
    """Test that get_stream_details calls API directly, not cached get_track."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_track.streaming_url = {"mp3-128": "http://example.com/track.mp3"}
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with (
        patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track,
    ):
        mock_get_album.return_value = mock_api_album

        result = await provider.get_stream_details("123-456-789", MediaType.TRACK)

        mock_get_album.assert_called_once()
        mock_get_track.assert_not_called()
        assert result.path == "http://example.com/track.mp3"
        assert result.audio_format.content_type == ContentType.MP3


async def test_fetch_api_track_album_path(provider: BandcampProvider) -> None:
    """Test _fetch_api_track with 3-part ID routes through get_album."""
    mock_api_track = Mock()
    mock_api_track.id = 789
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_api_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        api_track, api_album = await provider._fetch_api_track("123-456-789")

        mock_get_album.assert_called_once_with(123, 456)
        assert api_track is mock_api_track
        assert api_album is mock_api_album


async def test_fetch_api_track_standalone_path(provider: BandcampProvider) -> None:
    """Test _fetch_api_track with album_id=0 routes through get_track."""
    mock_api_track = Mock()

    with patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = mock_api_track

        api_track, api_album = await provider._fetch_api_track("123-0-789")

        mock_get_track.assert_called_once_with(123, 789)
        assert api_track is mock_api_track
        assert api_album is None


async def test_fetch_api_track_not_in_album(provider: BandcampProvider) -> None:
    """Test _fetch_api_track raises when track ID not found in album tracks."""
    mock_other_track = Mock()
    mock_other_track.id = 999
    mock_api_album = Mock()
    mock_api_album.tracks = [mock_other_track]

    with patch.object(provider._client, "get_album", new_callable=AsyncMock) as mock_get_album:
        mock_get_album.return_value = mock_api_album

        with pytest.raises(MediaNotFoundError, match=r"not found in album"):
            await provider._fetch_api_track("123-456-789")


async def test_fetch_api_track_not_found_error(provider: BandcampProvider) -> None:
    """Test _fetch_api_track converts BandcampNotFoundError."""
    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=BandcampNotFoundError("Not found"),
        ),
        pytest.raises(MediaNotFoundError, match=r"not found on Bandcamp"),
    ):
        await provider._fetch_api_track("123-456-789")


async def test_fetch_api_track_rate_limit_error(provider: BandcampProvider) -> None:
    """
    Test _fetch_api_track converts BandcampRateLimitError.

    Since @throttle_with_retries is on _fetch_api_track, persistent rate
    limiting exhausts retries and raises RetriesExhausted.
    """
    rate_error = BandcampRateLimitError("Rate limited")
    rate_error.retry_after = 3

    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=rate_error,
        ) as mock_get_album,
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        pytest.raises(RetriesExhausted),
    ):
        await provider._fetch_api_track("123-456-789")

    assert mock_get_album.call_count == provider.throttler.retry_attempts
    assert mock_sleep.call_count == provider.throttler.retry_attempts - 1


@pytest.mark.parametrize("error", [ClientConnectionError("down"), ClientPayloadError("cut")])
async def test_fetch_api_track_retries_a_transport_error(
    provider: BandcampProvider, error: Exception
) -> None:
    """A dropped connection is retried, and the next answer wins."""
    api_album = Mock(tracks=[Mock(id=789)])
    with (
        patch.object(
            provider._client, "get_album", side_effect=[error, api_album]
        ) as mock_get_album,
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        _, album = await provider._fetch_api_track("123-456-789")

    assert album is api_album
    assert mock_get_album.call_count == 2
    assert mock_sleep.call_count == 1


async def test_fetch_api_track_gives_up_after_transport_errors(provider: BandcampProvider) -> None:
    """A transport error on every attempt ends in RetriesExhausted, not a raw aiohttp error."""
    with (
        patch.object(
            provider._client, "get_album", side_effect=ClientConnectionError("down")
        ) as mock_get_album,
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RetriesExhausted),
    ):
        await provider._fetch_api_track("123-456-789")

    assert mock_get_album.call_count == provider.throttler.retry_attempts


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionTimeoutError()])
async def test_fetch_api_track_does_not_retry_a_timeout(
    provider: BandcampProvider, error: Exception
) -> None:
    """A timeout fails at once, because a hang would repeat on every attempt."""
    with (
        patch.object(provider._client, "get_album", side_effect=error) as mock_get_album,
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        pytest.raises(TimeoutError),
    ):
        await provider._fetch_api_track("123-456-789")

    assert mock_get_album.call_count == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.parametrize(
    ("client_method", "call_provider"),
    [
        pytest.param(
            "get_artist_discography", lambda p: p.get_artist_toptracks("123"), id="top_tracks"
        ),
        pytest.param(
            "get_collection_items",
            lambda p: p._browse_person_content(None, CollectionType.WISHLIST),
            id="collection_content",
        ),
        pytest.param(
            "get_collection_items", lambda p: p._browse_person_following(None), id="following"
        ),
        pytest.param(
            "get_collection_items",
            lambda p: p._browse_person_people(CollectionType.FOLLOWERS, "followers"),
            id="people",
        ),
    ],
)
async def test_a_dropped_connection_in_a_nested_fetch_ends_in_retries_exhausted(
    provider: BandcampProvider,
    client_method: str,
    call_provider: Callable[[BandcampProvider], Awaitable[object]],
) -> None:
    """The method that calls Bandcamp retries a dropped connection, and its caller gets the end."""
    with (
        patch.object(
            provider._client, client_method, side_effect=ClientConnectionError("down")
        ) as mock_client_method,
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RetriesExhausted),
    ):
        await call_provider(provider)

    assert mock_client_method.call_count == provider.throttler.retry_attempts


async def test_fetch_api_track_generic_api_error(provider: BandcampProvider) -> None:
    """Test _fetch_api_track converts generic BandcampAPIError to MediaNotFoundError."""
    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=BandcampAPIError("Something went wrong"),
        ),
        pytest.raises(MediaNotFoundError, match=r"Failed to get track 123-456-789"),
    ):
        await provider._fetch_api_track("123-456-789")


def test_split_id_three_parts() -> None:
    """Test split_id with a 3-part compound ID."""
    assert split_id("123-456-789") == (123, 456, 789)


def test_split_id_two_parts() -> None:
    """Test split_id with a 2-part compound ID."""
    assert split_id("123-456") == (123, 456, 0)


def test_split_id_one_part() -> None:
    """Test split_id with a single ID."""
    assert split_id("123") == (123, 0, 0)


async def test_fetch_api_track_two_part_id(provider: BandcampProvider) -> None:
    """Test _fetch_api_track with 2-part ID routes through get_track."""
    # split_id("123-789") returns (123, 789, 0); since track_id=0,
    # the method swaps to album_id=0, track_id=789 and uses get_track.
    mock_api_track = Mock()

    with patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = mock_api_track

        api_track, api_album = await provider._fetch_api_track("123-789")

        mock_get_track.assert_called_once_with(123, 789)
        assert api_track is mock_api_track
        assert api_album is None


async def test_get_stream_details_standalone_track(provider: BandcampProvider) -> None:
    """Test stream details for a standalone track (album_id=0)."""
    mock_api_track = Mock()
    mock_api_track.streaming_url = {"mp3-128": "http://example.com/standalone.mp3"}

    with patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = mock_api_track

        result = await provider.get_stream_details("123-0-789", MediaType.TRACK)

        mock_get_track.assert_called_once_with(123, 789)
        assert isinstance(result, StreamDetails)
        assert result.path == "http://example.com/standalone.mp3"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate == 128


async def test_get_artist_toptracks_success(provider: BandcampProvider) -> None:
    """Test successful artist top tracks retrieval."""
    album = Mock(item_id="123-456", year=2024)

    with (
        patch.object(provider, "get_artist_albums", new_callable=AsyncMock) as mock_get_albums,
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_get_tracks,
    ):
        mock_get_albums.return_value = [album]
        mock_get_tracks.return_value = [_mapped_track("123-456-789")]

        result = await provider.get_artist_toptracks("123")

        assert len(result) == 1
        mock_get_albums.assert_called_once_with("123")


def _mapped_track(item_id: str, *, available: bool = True) -> Track:
    """Build a track whose Bandcamp mapping has the given availability."""
    return Track(
        item_id=item_id,
        provider="bandcamp_test",
        name=item_id,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="bandcamp",
                provider_instance="bandcamp_test",
                available=available,
            )
        },
    )


async def test_get_artist_toptracks_skips_tracks_without_a_stream(
    provider: BandcampProvider,
) -> None:
    """Tracks without a stream do not take the places of playable top tracks."""
    preorder = Mock(item_id="1-20", year=2026)
    older = Mock(item_id="1-10", year=2024)
    oldest = Mock(item_id="1-5", year=2020)
    listings = {
        "1-20": [
            _mapped_track("1-20-1"),
            _mapped_track("1-20-2", available=False),
            _mapped_track("1-20-3", available=False),
        ],
        "1-10": [_mapped_track("1-10-1"), _mapped_track("1-10-2"), _mapped_track("1-10-3")],
        "1-5": [_mapped_track("1-5-1")],
    }
    provider.top_tracks_limit = 3

    with (
        patch.object(
            provider,
            "get_artist_albums",
            new_callable=AsyncMock,
            return_value=[older, oldest, preorder],
        ),
        patch.object(
            provider,
            "get_album_tracks",
            new_callable=AsyncMock,
            side_effect=lambda album_id: listings[album_id],
        ) as mock_get_tracks,
    ):
        result = await provider.get_artist_toptracks("1")

    assert [track.item_id for track in result] == ["1-20-1", "1-10-1", "1-10-2"]
    # the limit is reached in the second album, so the oldest album is not read
    assert mock_get_tracks.await_args_list == [call("1-20"), call("1-10")]


async def test_get_artist_toptracks_keeps_playable_tracks_while_the_provider_reloads(
    provider: BandcampProvider,
) -> None:
    """Top tracks, cached for 30 days, do not depend on the loaded providers."""
    album = Mock(item_id="1-10", year=2024)
    await set_global_cache_values({"available_providers": {"other_instance"}})

    with (
        patch.object(provider, "get_artist_albums", new_callable=AsyncMock, return_value=[album]),
        patch.object(
            provider,
            "get_album_tracks",
            new_callable=AsyncMock,
            return_value=[_mapped_track("1-10-1"), _mapped_track("1-10-2", available=False)],
        ),
    ):
        result = await provider.get_artist_toptracks("1")

    assert [track.item_id for track in result] == ["1-10-1"]


async def test_get_library_artists_success(provider: BandcampProvider) -> None:
    """Test successful library artists retrieval."""
    collection_items = [
        Mock(item_type="band", item_id=100, band_id=100),
        Mock(item_type="album", item_id=200, band_id=300),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_artist.return_value = Mock()

        artists = [artist async for artist in provider.get_library_artists()]

        assert len(artists) == 2
        assert mock_get_artist.call_count == 2


async def test_get_library_artists_no_identity(provider: BandcampProvider) -> None:
    """Test that library artists returns nothing without identity."""
    provider._client.identity = None
    artists = [artist async for artist in provider.get_library_artists()]
    assert len(artists) == 0


async def test_get_library_albums_success(provider: BandcampProvider) -> None:
    """Test successful library albums retrieval."""
    collection_items = [
        Mock(item_type="album", item_id=456, band_id=123),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = Mock()

        albums = [album async for album in provider.get_library_albums()]

        assert len(albums) == 1
        mock_get_album.assert_called_once_with("123-456")


async def test_get_library_tracks_success(provider: BandcampProvider) -> None:
    """Test successful library tracks retrieval."""
    mock_track = Mock()

    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            new_callable=AsyncMock,
            return_value=[Mock(item_type="album", item_id=456, band_id=123)],
        ),
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_get_tracks,
    ):
        mock_get_tracks.return_value = [mock_track]

        tracks = [track async for track in provider.get_library_tracks()]

        assert len(tracks) == 1
        mock_get_tracks.assert_called_once_with("123-456")
        # the album IDs come from the collection, the albums themselves are not needed
        mock_get_album.assert_not_awaited()


SYNC_ERRORS = [
    pytest.param(MediaNotFoundError("gone"), id="not_found"),
    pytest.param(InvalidDataError("robot check"), id="unusable_answer"),
    pytest.param(RetriesExhausted("rate limit"), id="retries_exhausted"),
    pytest.param(TimeoutError(), id="timeout"),
]


def _collection_albums(*album_ids: int) -> list[Mock]:
    """Create collection items for albums of band 123."""
    return [Mock(item_type="album", item_id=album_id, band_id=123) for album_id in album_ids]


@pytest.mark.parametrize("error", SYNC_ERRORS)
async def test_get_library_albums_skips_a_failing_album(
    provider: BandcampProvider, error: Exception
) -> None:
    """One album that fails is reported as skipped, and the other albums still sync."""
    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            new_callable=AsyncMock,
            return_value=_collection_albums(1, 2, 3),
        ),
        patch.object(
            provider, "get_album", new_callable=AsyncMock, side_effect=["album 1", error, "album 3"]
        ),
        patch.object(provider, "report_skipped_sync_item") as mock_report,
    ):
        albums = [album async for album in provider.get_library_albums()]

    assert albums == ["album 1", "album 3"]
    mock_report.assert_called_once_with(MediaType.ALBUM, "123-2", error)


async def test_get_library_albums_stops_on_a_login_failure(provider: BandcampProvider) -> None:
    """A wrong identity token still stops the sync, it is no item to skip."""
    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            new_callable=AsyncMock,
            return_value=_collection_albums(1, 2),
        ),
        patch.object(
            provider, "get_album", new_callable=AsyncMock, side_effect=LoginFailed("wrong token")
        ),
        patch.object(provider, "report_skipped_sync_item") as mock_report,
        pytest.raises(LoginFailed),
    ):
        _ = [album async for album in provider.get_library_albums()]

    mock_report.assert_not_called()


@pytest.mark.parametrize("error", SYNC_ERRORS)
async def test_get_library_artists_skips_a_failing_artist(
    provider: BandcampProvider, error: Exception
) -> None:
    """One artist that fails is reported as skipped, and the other artists still sync."""
    collection = [
        Mock(item_type="band", item_id=100, band_id=100),
        Mock(item_type="album", item_id=1, band_id=200),
    ]

    async def fake_get_artist(band_id: str) -> str:
        if band_id == "100":
            raise error
        return f"artist {band_id}"

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock, return_value=collection
        ),
        patch.object(provider, "get_artist", side_effect=fake_get_artist),
        patch.object(provider, "report_skipped_sync_item") as mock_report,
    ):
        artists = [artist async for artist in provider.get_library_artists()]

    assert artists == ["artist 200"]
    mock_report.assert_called_once_with(MediaType.ARTIST, "100", error)


@pytest.mark.parametrize("error", SYNC_ERRORS)
async def test_get_library_tracks_skips_the_tracks_of_a_failing_album(
    provider: BandcampProvider, error: Exception
) -> None:
    """An album whose tracks fail holds back the track deletions, the other tracks still sync."""
    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            new_callable=AsyncMock,
            return_value=_collection_albums(1, 2, 3),
        ),
        patch.object(
            provider,
            "get_album_tracks",
            new_callable=AsyncMock,
            side_effect=[["track 1"], error, ["track 3"]],
        ),
        patch.object(provider, "report_skipped_sync_item") as mock_report,
    ):
        tracks = [track async for track in provider.get_library_tracks()]

    assert tracks == ["track 1", "track 3"]
    # no track ID is known, so the core keeps all library tracks out of the deletion pass
    mock_report.assert_called_once_with(MediaType.TRACK, None, error)


def _collection_track(track_id: int, band_id: int, album_id: int | None) -> Mock:
    """Create a collection item for a single track purchase."""
    return Mock(item_type="track", item_id=track_id, band_id=band_id, album_id=album_id)


async def test_get_library_tracks_yields_single_track_purchases(
    provider: BandcampProvider,
) -> None:
    """A single track purchase syncs next to the tracks of the album purchases."""
    collection = [
        *_collection_albums(1),
        _collection_track(789, band_id=55, album_id=None),
        _collection_track(790, band_id=56, album_id=900),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock, return_value=collection
        ),
        patch.object(
            provider, "get_album_tracks", new_callable=AsyncMock, return_value=["album track"]
        ) as mock_album_tracks,
        patch.object(
            provider,
            "_get_track_base",
            new_callable=AsyncMock,
            side_effect=["single", "album part"],
        ) as mock_track,
    ):
        tracks = [track async for track in provider.get_library_tracks()]

    assert tracks == ["album track", "single", "album part"]
    mock_album_tracks.assert_awaited_once_with("123-1")
    assert mock_track.await_args_list == [call("55-0-789"), call("56-900-790")]


async def test_get_library_albums_ignores_single_track_purchases(
    provider: BandcampProvider,
) -> None:
    """A single track purchase is no library album."""
    collection = [*_collection_albums(1), _collection_track(789, band_id=55, album_id=None)]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock, return_value=collection
        ),
        patch.object(provider, "get_album", new_callable=AsyncMock, return_value="album") as mock,
    ):
        albums = [album async for album in provider.get_library_albums()]

    assert albums == ["album"]
    mock.assert_awaited_once_with("123-1")


def _collection_package(package_id: int, album_id: int | None, band_id: int = 123) -> Mock:
    """Create a collection item for a package, for example a record."""
    return Mock(
        item_type="package",
        item_id=package_id,
        band_id=band_id,
        tralbum_type="a" if album_id else None,
        tralbum_id=album_id,
    )


async def test_library_sync_takes_the_album_of_a_package(provider: BandcampProvider) -> None:
    """An album bought as a package syncs, once, with its artist and its tracks."""
    collection = [
        *_collection_albums(1),
        _collection_package(900, album_id=2, band_id=55),
        # the album 123-1 again, bought as a record
        _collection_package(901, album_id=1),
        # merchandise without a digital album
        _collection_package(902, album_id=None),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock, return_value=collection
        ),
        patch.object(
            provider, "get_album", new_callable=AsyncMock, side_effect=lambda album_id: album_id
        ),
        patch.object(
            provider,
            "get_album_tracks",
            new_callable=AsyncMock,
            side_effect=lambda album_id: [f"track of {album_id}"],
        ),
        patch.object(
            provider, "get_artist", new_callable=AsyncMock, side_effect=lambda band_id: band_id
        ),
    ):
        albums = [album async for album in provider.get_library_albums()]
        tracks = [track async for track in provider.get_library_tracks()]
        artists = [artist async for artist in provider.get_library_artists()]

    assert albums == ["123-1", "55-2"]
    assert tracks == ["track of 123-1", "track of 55-2"]
    assert sorted(map(str, artists)) == ["123", "55"]


@pytest.mark.parametrize("error", SYNC_ERRORS)
async def test_get_library_tracks_skips_a_failing_single_track(
    provider: BandcampProvider, error: Exception
) -> None:
    """A single track that fails holds back the track deletions, the other tracks still sync."""
    collection = [
        _collection_track(789, band_id=55, album_id=None),
        _collection_track(790, band_id=55, album_id=None),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock, return_value=collection
        ),
        patch.object(
            provider, "_get_track_base", new_callable=AsyncMock, side_effect=[error, "track 790"]
        ),
        patch.object(provider, "report_skipped_sync_item") as mock_report,
    ):
        tracks = [track async for track in provider.get_library_tracks()]

    assert tracks == ["track 790"]
    mock_report.assert_called_once_with(MediaType.TRACK, None, error)


def test_split_id_malformed_non_numeric() -> None:
    """Test split_id raises InvalidDataError on non-numeric input."""
    with pytest.raises(InvalidDataError, match=r"Malformed Bandcamp ID"):
        split_id("abc-def")


def test_split_id_malformed_empty() -> None:
    """Test split_id raises InvalidDataError on empty string."""
    with pytest.raises(InvalidDataError, match=r"Malformed Bandcamp ID"):
        split_id("")


async def test_fetch_api_track_login_error(provider: BandcampProvider) -> None:
    """Test _fetch_api_track converts BandcampMustBeLoggedInError to LoginFailed."""
    with (
        patch.object(
            provider._client,
            "get_album",
            side_effect=BandcampMustBeLoggedInError("Must be logged in"),
        ),
        pytest.raises(LoginFailed, match=r"Wrong Bandcamp identity token"),
    ):
        await provider._fetch_api_track("123-456-789")


# --- Feed tests (the feed powers a recommendation row and a browse path) ---


async def test_browse_feed_returns_tracks(provider: BandcampProvider) -> None:
    """Test browsing the feed slug resolves to the feed tracks (the play path for the folder)."""
    feed_track = Mock()
    with patch.object(provider, "_get_feed_tracks", new_callable=AsyncMock) as mock_feed:
        mock_feed.return_value = [feed_track]

        result = await provider.browse("bandcamp_test://feed")

        assert result == [feed_track]


async def test_get_feed_tracks_filters_non_streamable(provider: BandcampProvider) -> None:
    """Test _get_feed_tracks skips tracks without a streaming URL and caches the result."""
    streamable = Mock(streaming_url={"mp3-128": "https://example.com/feed.mp3"})
    silent = Mock(streaming_url=None)
    converted = Mock()

    with (
        patch.object(provider, "_fetch_feed", new_callable=AsyncMock) as mock_fetch,
        patch.object(
            provider._converters, "track_from_feed", return_value=converted
        ) as mock_convert,
    ):
        mock_fetch.return_value = Mock(track_list=[streamable, silent])

        result = await provider._get_feed_tracks()

        mock_convert.assert_called_once_with(streamable)
        assert result == [converted]
        cast("AsyncMock", provider.mass.cache.set).assert_called_once()


async def test_get_feed_tracks_cache_hit(provider: BandcampProvider) -> None:
    """Test _get_feed_tracks returns cached tracks without hitting the API."""
    cached = [Mock()]

    with (
        patch.object(provider.mass.cache, "get", new_callable=AsyncMock, return_value=cached),
        patch.object(provider, "_fetch_feed", new_callable=AsyncMock) as mock_fetch,
    ):
        result = await provider._get_feed_tracks()

        mock_fetch.assert_not_called()
        assert result == cached


# --- Browse tests ---


async def test_browse_feature_supported(provider: BandcampProvider) -> None:
    """Test that BROWSE is in supported features."""
    assert ProviderFeature.BROWSE in provider.supported_features


async def test_browse_root_with_identity(provider: BandcampProvider) -> None:
    """Test browse root returns standard folders plus Wishlist and Following."""
    provider._client.identity = "mock_token"

    with patch.object(
        type(provider).__bases__[0], "browse", new_callable=AsyncMock
    ) as mock_super_browse:
        mock_super_browse.return_value = [
            BrowseFolder(
                item_id="artists", provider="bandcamp_test", path="bandcamp_test://artists", name=""
            ),
            BrowseFolder(
                item_id="albums", provider="bandcamp_test", path="bandcamp_test://albums", name=""
            ),
        ]

        result = await provider.browse("bandcamp_test://")

        assert len(result) == 6
        folder_ids = [f.item_id for f in result if isinstance(f, BrowseFolder)]
        assert "wishlist" in folder_ids
        assert "following" in folder_ids
        assert "fans" in folder_ids
        assert "followers" in folder_ids

        wishlist_folder = next(
            f for f in result if isinstance(f, BrowseFolder) and f.item_id == "wishlist"
        )
        assert wishlist_folder.path == "bandcamp_test://wishlist"
        assert wishlist_folder.name == "Wishlist"

        following_folder = next(
            f for f in result if isinstance(f, BrowseFolder) and f.item_id == "following"
        )
        assert following_folder.path == "bandcamp_test://following"
        assert following_folder.name == "Following"


async def test_browse_root_without_identity(provider: BandcampProvider) -> None:
    """Test browse root without identity omits Wishlist and Following."""
    provider._client.identity = None

    with patch.object(
        type(provider).__bases__[0], "browse", new_callable=AsyncMock
    ) as mock_super_browse:
        mock_super_browse.return_value = [
            BrowseFolder(
                item_id="artists", provider="bandcamp_test", path="bandcamp_test://artists", name=""
            ),
        ]

        result = await provider.browse("bandcamp_test://")

        assert len(result) == 1
        folder_ids = [f.item_id for f in result if isinstance(f, BrowseFolder)]
        assert "wishlist" not in folder_ids
        assert "following" not in folder_ids


async def test_browse_standard_subpath_delegates_to_super(provider: BandcampProvider) -> None:
    """Test that standard subpaths like 'artists' delegate to super().browse()."""
    with patch.object(
        type(provider).__bases__[0], "browse", new_callable=AsyncMock
    ) as mock_super_browse:
        mock_super_browse.return_value = [Mock(), Mock()]

        result = await provider.browse("bandcamp_test://artists")

        mock_super_browse.assert_called_once_with("bandcamp_test://artists")
        assert len(result) == 2


def _collection_entry(item_type: str, item_id: int, **fields: Any) -> CollectionItem:
    """Build a collection or wishlist entry of band 123."""
    return CollectionItem(item_type=item_type, item_id=item_id, band_id=123, **fields)


async def test_browse_wishlist_returns_albums_and_tracks(provider: BandcampProvider) -> None:
    """Test browsing wishlist returns the albums and tracks of the list, with no request each."""
    collection_items = [
        _collection_entry("album", 456, tralbum_id=456),
        _collection_entry("track", 789, tralbum_id=789),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track,
    ):
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://wishlist")

        mock_get_collection.assert_called_once_with(CollectionType.WISHLIST, fan_id=None)
        assert [(type(item), item.item_id) for item in result] == [
            (Album, "123-456"),
            (Track, "123-0-789"),
        ]
        mock_get_album.assert_not_awaited()
        mock_get_track.assert_not_awaited()


async def test_browse_person_content_takes_the_album_of_a_package(
    provider: BandcampProvider,
) -> None:
    """A package gives its digital album, and an album owned twice shows once."""
    collection_items = [
        _collection_entry("package", 4197129855, tralbum_id=3846833501, tralbum_type="a"),
        _collection_entry("album", 3846833501, tralbum_id=3846833501, tralbum_type="a"),
        _collection_entry("package", 4003807767, tralbum_id=626289772, tralbum_type="a"),
        _collection_entry("package", 1, tralbum_id=None, tralbum_type=None),
    ]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock, return_value=collection_items
    ):
        result = await provider._browse_person_content(42, CollectionType.COLLECTION)

    assert [item.item_id for item in result] == ["123-3846833501", "123-626289772"]


async def test_browse_wishlist_login_error(provider: BandcampProvider) -> None:
    """Test wishlist browse raises LoginFailed on auth error."""
    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            side_effect=BandcampMustBeLoggedInError("Must be logged in"),
        ),
        pytest.raises(LoginFailed),
    ):
        await provider.browse("bandcamp_test://wishlist")


async def test_browse_wishlist_rate_limit(provider: BandcampProvider) -> None:
    """Test wishlist browse raises on rate limit after retries."""
    rate_error = BandcampRateLimitError("Rate limited")
    rate_error.retry_after = 3

    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            side_effect=rate_error,
        ),
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RetriesExhausted),
    ):
        await provider.browse("bandcamp_test://wishlist")


async def test_browse_following_returns_artists(provider: BandcampProvider) -> None:
    """Test browsing following returns the artists of the following list."""
    collection_items = [
        FollowingItem(band_id=100, name="Artist1"),
        FollowingItem(band_id=200, name="Artist2"),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://following")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING, fan_id=None)
        assert [item.item_id for item in result] == ["100", "200"]
        mock_get_artist.assert_not_awaited()


async def test_browse_following_login_error(provider: BandcampProvider) -> None:
    """Test following browse raises LoginFailed on auth error."""
    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            side_effect=BandcampMustBeLoggedInError("Must be logged in"),
        ),
        pytest.raises(LoginFailed),
    ):
        await provider.browse("bandcamp_test://following")


async def test_browse_wishlist_ignores_unknown_item_types(provider: BandcampProvider) -> None:
    """Test that wishlist browse ignores items with unknown item_type."""
    collection_items = [
        _collection_entry("band", 100),
        _collection_entry("album", 456, tralbum_id=456),
    ]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://wishlist")

        assert [item.item_id for item in result] == ["123-456"]


# --- _map_api_errors context manager tests ---


async def test_map_api_errors_login_error(provider: BandcampProvider) -> None:
    """Test _map_api_errors maps BandcampMustBeLoggedInError to LoginFailed."""
    with pytest.raises(LoginFailed, match="Wrong Bandcamp identity token"):
        async with provider._map_api_errors("test context"):
            raise BandcampMustBeLoggedInError("Must be logged in")


async def test_map_api_errors_rate_limit(provider: BandcampProvider) -> None:
    """Test _map_api_errors maps BandcampRateLimitError to ResourceTemporarilyUnavailable."""
    rate_error = BandcampRateLimitError("Rate limited")
    rate_error.retry_after = 5

    with pytest.raises(ResourceTemporarilyUnavailable, match="rate limit"):
        async with provider._map_api_errors("test context"):
            raise rate_error


async def test_map_api_errors_generic_api_error(provider: BandcampProvider) -> None:
    """Test _map_api_errors maps BandcampAPIError to MediaNotFoundError with context."""
    with pytest.raises(MediaNotFoundError, match="my custom context: Something went wrong"):
        async with provider._map_api_errors("my custom context"):
            raise BandcampAPIError("Something went wrong")


async def test_map_api_errors_not_found_message(provider: BandcampProvider) -> None:
    """Test _map_api_errors gives an unknown item its own message."""
    with pytest.raises(MediaNotFoundError, match=r"^Album 1-2 not found on Bandcamp$"):
        async with provider._map_api_errors("Failed", not_found="Album 1-2 not found on Bandcamp"):
            raise BandcampNotFoundError("No such album")


async def test_map_api_errors_no_exception(provider: BandcampProvider) -> None:
    """Test _map_api_errors passes through when no exception is raised."""
    async with provider._map_api_errors("test context"):
        pass  # no exception


# --- _browse_person_root tests ---


def test_browse_person_root_returns_five_subfolders(provider: BandcampProvider) -> None:
    """Test _browse_person_root returns 5 sub-folders for a person."""
    folders = provider._browse_person_root(42, "bandcamp_test://fans/42")

    assert len(folders) == 5
    names = [f.name for f in folders]
    assert names == ["Collection", "Wishlist", "Following", "Fans", "Followers"]
    for folder in folders:
        assert folder.path.startswith("bandcamp_test://fans/42/")
        assert folder.item_id.startswith("person_42_")


# --- _people_to_folders tests ---


def test_people_to_folders_with_images(provider: BandcampProvider) -> None:
    """Test _people_to_folders creates folders with thumbnails."""
    people = [
        _fan_mock(1, "Alice", "http://example.com/alice.jpg", "https://bandcamp.com/alice"),
        _fan_mock(2, "Bob", "http://example.com/bob.jpg", "https://bandcamp.com/bob"),
    ]

    folders = provider._people_to_folders(people, "bandcamp_test://fans")

    assert len(folders) == 2
    assert folders[0].name == "Alice"
    assert folders[0].path == "bandcamp_test://fans/alice"
    assert folders[0].image is not None
    assert folders[0].image.type == ImageType.THUMB
    assert folders[0].image.path == "http://example.com/alice.jpg"
    # Verify slug→fan_id mapping was stored
    assert provider._slug_to_fan_id["alice"] == 1
    assert provider._slug_to_fan_id["bob"] == 2


def test_people_to_folders_without_image(provider: BandcampProvider) -> None:
    """Test _people_to_folders handles missing image_url."""
    people = [_fan_mock(1, "NoPhoto", url="https://bandcamp.com/nophoto")]
    folders = provider._people_to_folders(people, "bandcamp_test://fans")

    assert len(folders) == 1
    assert folders[0].image is None
    assert folders[0].path == "bandcamp_test://fans/nophoto"


def test_people_to_folders_missing_name(provider: BandcampProvider) -> None:
    """Test _people_to_folders falls back to 'User {id}' when name is empty."""
    people = [_fan_mock(99, None)]
    folders = provider._people_to_folders(people, "bandcamp_test://fans")

    assert folders[0].name == "User 99"
    # No URL → falls back to numeric fan_id in path
    assert folders[0].path == "bandcamp_test://fans/99"


# --- _fan_slug unit tests ---


def test_fan_slug_extracts_from_url() -> None:
    """Test _fan_slug extracts slug from a standard Bandcamp URL."""
    person = _fan_mock(1, "Alice", url="https://bandcamp.com/alice")
    assert BandcampProvider._fan_slug(person) == "alice"


def test_fan_slug_strips_trailing_slash() -> None:
    """Test _fan_slug handles trailing slash in URL."""
    person = _fan_mock(1, "Alice", url="https://bandcamp.com/alice/")
    assert BandcampProvider._fan_slug(person) == "alice"


def test_fan_slug_none_url() -> None:
    """Test _fan_slug returns None when url is None."""
    person = _fan_mock(1, "Alice", url=None)
    assert BandcampProvider._fan_slug(person) is None


def test_fan_slug_empty_string_url() -> None:
    """Test _fan_slug returns None when url is empty string."""
    person = _fan_mock(1, "Alice", url="")
    assert BandcampProvider._fan_slug(person) is None


# --- _resolve_person_segment unit tests ---


async def test_resolve_person_segment_slug_hit(provider: BandcampProvider) -> None:
    """Test slug cache hit takes priority over numeric parse."""
    provider._slug_to_fan_id["42"] = 999  # slug "42" maps to fan 999
    assert await provider._resolve_person_segment("42") == 999  # slug wins over int parse


async def test_resolve_person_segment_numeric(provider: BandcampProvider) -> None:
    """Test numeric segment returns int when no slug match."""
    assert await provider._resolve_person_segment("123") == 123


async def test_resolve_person_segment_unknown_slug(provider: BandcampProvider) -> None:
    """Test unknown non-numeric slug triggers rebuild, returns None if still missing."""
    with patch.object(provider, "_rebuild_slug_cache", new_callable=AsyncMock) as mock_rebuild:
        assert await provider._resolve_person_segment("nonexistent") is None
        mock_rebuild.assert_called_once()


async def test_resolve_person_segment_zero(provider: BandcampProvider) -> None:
    """Test zero is returned as valid int (caller validates)."""
    assert await provider._resolve_person_segment("0") == 0


async def test_resolve_person_segment_rebuild_finds_slug(provider: BandcampProvider) -> None:
    """Test unknown slug is resolved after _rebuild_slug_cache populates the map."""

    async def fake_rebuild() -> None:
        provider._slug_to_fan_id["yerhot"] = 12345

    with patch.object(provider, "_rebuild_slug_cache", side_effect=fake_rebuild):
        assert await provider._resolve_person_segment("yerhot") == 12345


def test_people_to_folders_no_url_falls_back_to_id(provider: BandcampProvider) -> None:
    """Test _people_to_folders uses numeric fan_id when url is None."""
    people = [_fan_mock(77, "NoUrl")]
    folders = provider._people_to_folders(people, "bandcamp_test://fans")

    assert folders[0].path == "bandcamp_test://fans/77"
    assert "77" not in provider._slug_to_fan_id


# --- _browse_person dispatch routing tests ---


async def test_browse_fans_top_level(provider: BandcampProvider) -> None:
    """Test browsing 'fans' at top level fetches authenticated user's fans."""
    collection_items = [_fan_mock(1, "Fan1", url="https://bandcamp.com/fan1")]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://fans")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING_FANS, fan_id=None)
        assert len(result) == 1
        assert isinstance(result[0], BrowseFolder)
        assert result[0].path == "bandcamp_test://fans/fan1"


async def test_browse_followers_top_level(provider: BandcampProvider) -> None:
    """Test browsing 'followers' at top level fetches authenticated user's followers."""
    collection_items = [_fan_mock(2, "Follower1", url="https://bandcamp.com/follower1")]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://followers")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWERS, fan_id=None)
        assert len(result) == 1
        assert isinstance(result[0], BrowseFolder)
        assert result[0].path == "bandcamp_test://followers/follower1"


async def test_browse_fans_person_id_shows_subfolders(provider: BandcampProvider) -> None:
    """Test browsing 'fans/42' returns 5 sub-folders for person 42."""
    result = await provider.browse("bandcamp_test://fans/42")

    assert len(result) == 5
    names = [f.name for f in result if isinstance(f, BrowseFolder)]
    assert "Collection" in names
    assert "Wishlist" in names
    assert "Following" in names
    assert "Fans" in names
    assert "Followers" in names


async def test_browse_person_collection(provider: BandcampProvider) -> None:
    """Test browsing fans/42/collection fetches person's collection."""
    collection_items = [_collection_entry("album", 456, tralbum_id=456)]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://fans/42/collection")

        mock_get_collection.assert_called_once_with(CollectionType.COLLECTION, fan_id=42)
        assert [item.item_id for item in result] == ["123-456"]


async def test_browse_person_wishlist(provider: BandcampProvider) -> None:
    """Test browsing fans/42/wishlist fetches person's wishlist."""
    collection_items = [_collection_entry("album", 789, tralbum_id=789)]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://fans/42/wishlist")

        mock_get_collection.assert_called_once_with(CollectionType.WISHLIST, fan_id=42)
        assert [item.item_id for item in result] == ["123-789"]


async def test_browse_person_following(provider: BandcampProvider) -> None:
    """Test browsing fans/42/following fetches person's followed artists."""
    collection_items = [FollowingItem(band_id=100, name="Artist1")]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://fans/42/following")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING, fan_id=42)
        assert [(item.item_id, item.name) for item in result] == [("100", "Artist1")]
        mock_get_artist.assert_not_awaited()


async def test_browse_person_fans(provider: BandcampProvider) -> None:
    """Test browsing fans/42/fans fetches person 42's fans."""
    collection_items = [_fan_mock(99, "SubFan", url="https://bandcamp.com/subfan")]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://fans/42/fans")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING_FANS, fan_id=42)
        assert len(result) == 1
        assert isinstance(result[0], BrowseFolder)
        assert result[0].path == "bandcamp_test://fans/42/fans/subfan"


async def test_browse_person_followers(provider: BandcampProvider) -> None:
    """Test browsing followers/42/followers fetches person 42's followers."""
    collection_items = [_fan_mock(88, "SubFollower", url="https://bandcamp.com/subfollower")]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://followers/42/followers")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWERS, fan_id=42)
        assert len(result) == 1
        assert isinstance(result[0], BrowseFolder)
        assert result[0].path == "bandcamp_test://followers/42/followers/subfollower"


async def test_browse_deep_nesting(provider: BandcampProvider) -> None:
    """Test deep social graph traversal: fans/42/fans/99 shows person 99's sub-folders."""
    result = await provider.browse("bandcamp_test://fans/42/fans/99")

    assert len(result) == 5
    # Paths should include the full prefix
    for folder in result:
        assert isinstance(folder, BrowseFolder)
        assert folder.path.startswith("bandcamp_test://fans/42/fans/99/")


async def test_browse_deep_nesting_with_slug(provider: BandcampProvider) -> None:
    """Test slug-based navigation: fans/alice/fans resolves alice to fan_id."""
    # Pre-populate slug mapping (as if we had previously browsed fans)
    provider._slug_to_fan_id["alice"] = 42

    collection_items = [
        _fan_mock(99, "Bob", url="https://bandcamp.com/bob"),
    ]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider.browse("bandcamp_test://fans/alice/fans")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING_FANS, fan_id=42)
        assert len(result) == 1
        assert isinstance(result[0], BrowseFolder)
        assert result[0].path == "bandcamp_test://fans/alice/fans/bob"


async def test_browse_slug_person_root(provider: BandcampProvider) -> None:
    """Test navigating to a slug-identified person shows 5 sub-folders."""
    provider._slug_to_fan_id["teancom"] = 42

    result = await provider.browse("bandcamp_test://fans/teancom")

    assert len(result) == 5
    for folder in result:
        assert isinstance(folder, BrowseFolder)
        assert folder.path.startswith("bandcamp_test://fans/teancom/")


async def test_browse_person_invalid_id_zero(provider: BandcampProvider) -> None:
    """Test that person ID of 0 raises InvalidDataError."""
    with pytest.raises(InvalidDataError, match="Invalid person ID"):
        await provider.browse("bandcamp_test://fans/0")


async def test_browse_person_invalid_id_negative(provider: BandcampProvider) -> None:
    """Test that negative person ID raises InvalidDataError."""
    with pytest.raises(InvalidDataError, match="Invalid person ID"):
        await provider.browse("bandcamp_test://fans/-1")


async def test_browse_person_unknown_subcategory(provider: BandcampProvider) -> None:
    """Test that an unknown sub-category raises InvalidDataError."""
    with (
        patch.object(provider, "_rebuild_slug_cache", new_callable=AsyncMock),
        pytest.raises(InvalidDataError, match="Unknown browse sub-category"),
    ):
        await provider.browse("bandcamp_test://fans/42/playlists")


async def test_browse_person_invalid_path_no_id(provider: BandcampProvider) -> None:
    """Test that sub-category without valid person ID raises InvalidDataError."""
    with (
        patch.object(provider, "_rebuild_slug_cache", new_callable=AsyncMock),
        pytest.raises(InvalidDataError, match="Invalid browse path"),
    ):
        await provider.browse("bandcamp_test://fans/abc/collection")


# --- _browse_person_content with explicit person_id ---


async def test_browse_person_content_with_person_id(provider: BandcampProvider) -> None:
    """Test _browse_person_content with explicit person_id passes fan_id."""
    collection_items = [_collection_entry("album", 456, tralbum_id=456)]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider._browse_person_content(42, CollectionType.COLLECTION)

        mock_get_collection.assert_called_once_with(CollectionType.COLLECTION, fan_id=42)
        assert [item.item_id for item in result] == ["123-456"]


async def test_browse_person_content_caches_results(provider: BandcampProvider) -> None:
    """Test _browse_person_content caches non-empty results."""
    collection_items = [_collection_entry("album", 456, tralbum_id=456)]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        await provider._browse_person_content(42, CollectionType.WISHLIST)

        mock_cache_set = cast("AsyncMock", provider.mass.cache.set)
        mock_cache_set.assert_called_once()
        cache_key = mock_cache_set.call_args[0][0]
        assert "42" in cache_key
        assert CollectionType.WISHLIST.value in cache_key


async def test_browse_person_content_cache_hit(provider: BandcampProvider) -> None:
    """Test _browse_person_content returns cached result without hitting API."""
    cached_items = [
        Album(
            item_id="1-100",
            provider="bandcamp",
            name="Cached Album",
            provider_mappings=set(),
        ).to_dict(),
        Track(
            item_id="1-100-200",
            provider="bandcamp",
            name="Cached Track",
            provider_mappings=set(),
        ).to_dict(),
    ]

    with (
        patch.object(provider.mass.cache, "get", new_callable=AsyncMock, return_value=cached_items),
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
    ):
        result = await provider._browse_person_content(42, CollectionType.COLLECTION)

        mock_get_collection.assert_not_called()
        assert len(result) == 2
        assert isinstance(result[0], Album)
        assert isinstance(result[1], Track)
        assert result[0].name == "Cached Album"
        assert result[1].name == "Cached Track"


async def test_browse_person_content_cache_hit_stale_data(
    provider: BandcampProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _browse_person_content falls through to API on stale/corrupt cache."""
    stale_cache = [{"garbage": True}]

    with (
        patch.object(provider.mass.cache, "get", new_callable=AsyncMock, return_value=stale_cache),
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
    ):
        mock_get_collection.return_value = []
        result = await provider._browse_person_content(42, CollectionType.COLLECTION)

        mock_get_collection.assert_called_once()
        assert result == []
        assert "Stale cache" in caplog.text


async def test_browse_person_content_empty_cached_with_short_ttl(
    provider: BandcampProvider,
) -> None:
    """Test empty results are cached with CACHE_EMPTY_RESULTS TTL."""
    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = []

        result = await provider._browse_person_content(42, CollectionType.COLLECTION)

        assert result == []
        mock_cache_set = cast("AsyncMock", provider.mass.cache.set)
        mock_cache_set.assert_called_once()
        assert mock_cache_set.call_args.kwargs["expiration"] == CACHE_EMPTY_RESULTS


async def test_browse_person_content_nonempty_cached_with_normal_ttl(
    provider: BandcampProvider,
) -> None:
    """Test non-empty results are cached with CACHE_USER_LISTS TTL."""
    collection_items = [_collection_entry("album", 456, tralbum_id=456)]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        await provider._browse_person_content(42, CollectionType.COLLECTION)

        mock_cache_set = cast("AsyncMock", provider.mass.cache.set)
        mock_cache_set.assert_called_once()
        assert mock_cache_set.call_args.kwargs["expiration"] == CACHE_USER_LISTS


async def test_browse_person_content_api_error(provider: BandcampProvider) -> None:
    """Test _browse_person_content maps generic API error via _map_api_errors."""
    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            side_effect=BandcampAPIError("API Error"),
        ),
        pytest.raises(MediaNotFoundError, match="Failed to get"),
    ):
        await provider._browse_person_content(42, CollectionType.COLLECTION)


# --- _browse_person_following with explicit person_id ---


async def test_browse_person_following_with_person_id(provider: BandcampProvider) -> None:
    """Test _browse_person_following with explicit person_id."""
    collection_items = [FollowingItem(band_id=100, name="Artist1")]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider._browse_person_following(42)

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING, fan_id=42)
        assert [(item.item_id, item.name) for item in result] == [("100", "Artist1")]


async def test_browse_person_following_cache_hit(provider: BandcampProvider) -> None:
    """Test _browse_person_following returns cached Artist objects without calling API."""
    cached_artists = [
        Artist(
            item_id="100",
            provider="bandcamp",
            name="Cached Artist",
            provider_mappings=set(),
        ),
    ]

    with (
        patch.object(
            provider.mass.cache, "get", new_callable=AsyncMock, return_value=cached_artists
        ),
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
    ):
        result = await provider._browse_person_following(42)

        mock_get_collection.assert_not_called()
        assert len(result) == 1
        assert isinstance(result[0], Artist)
        assert result[0].name == "Cached Artist"


async def test_browse_person_following_sends_no_band_request(provider: BandcampProvider) -> None:
    """The following list gives the artists, with no band request for each of them."""
    collection_items = [
        FollowingItem(
            band_id=100,
            name="With page",
            url="https://withpage.bandcamp.com",
            image_url="https://f4.bcbits.com/img/46508512_0.jpg",
        ),
        FollowingItem(band_id=200, name="Without page"),
    ]

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_band,
    ):
        mock_get_collection.return_value = collection_items

        result = await provider._browse_person_following(42)

    assert [(item.item_id, item.name) for item in result] == [
        ("100", "With page"),
        ("200", "Without page"),
    ]
    assert [len(item.metadata.images or []) for item in result] == [1, 0]
    mock_band.assert_not_awaited()


# --- _browse_person_people tests ---


async def test_browse_person_people_with_person_id(provider: BandcampProvider) -> None:
    """Test _browse_person_people with explicit person_id fetches their fans."""
    collection_items = [_fan_mock(10, "Fan", "http://img.jpg", "https://bandcamp.com/thefan")]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = collection_items

        result = await provider._browse_person_people(
            CollectionType.FOLLOWING_FANS, "bandcamp_test://fans/42/fans", person_id=42
        )

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING_FANS, fan_id=42)
        assert len(result) == 1
        assert isinstance(result[0], BrowseFolder)
        assert result[0].path == "bandcamp_test://fans/42/fans/thefan"


async def test_browse_person_people_api_error(provider: BandcampProvider) -> None:
    """Test _browse_person_people maps API error via _map_api_errors."""
    with (
        patch.object(
            provider,
            "_get_all_collection_items",
            side_effect=BandcampAPIError("API Error"),
        ),
        pytest.raises(MediaNotFoundError, match="Failed to get"),
    ):
        await provider._browse_person_people(
            CollectionType.FOLLOWERS, "bandcamp_test://followers", person_id=42
        )


async def test_browse_person_people_cache_hit_rebuilds_slugs(
    provider: BandcampProvider,
) -> None:
    """Test _browse_person_people deserializes cached dicts and repopulates slugs."""
    cached_folders = [
        BrowseFolder(
            item_id="person_111",
            provider="bandcamp_test",
            path="bandcamp_test://fans/coolslug",
            name="Cool User",
        ),
        BrowseFolder(
            item_id="person_222",
            provider="bandcamp_test",
            path="bandcamp_test://fans/anotherslug",
            name="Another User",
        ),
    ]

    async def fake_cache_get(key: str, **kwargs: object) -> list[BrowseFolder] | None:  # noqa: ARG001
        if "_browse_person_people_" in key:
            return cached_folders
        return None

    provider._slug_to_fan_id.clear()

    with patch.object(provider.mass.cache, "get", new_callable=AsyncMock) as mock_cache_get:
        mock_cache_get.side_effect = fake_cache_get

        result = await provider._browse_person_people(
            CollectionType.FOLLOWING_FANS, "bandcamp_test://fans"
        )

    assert len(result) == 2
    assert all(isinstance(f, BrowseFolder) for f in result)
    assert result[0].name == "Cool User"
    assert result[1].name == "Another User"
    assert provider._slug_to_fan_id["coolslug"] == 111
    assert provider._slug_to_fan_id["anotherslug"] == 222


async def test_rebuild_slug_cache_calls_browse_person_people(
    provider: BandcampProvider,
) -> None:
    """Test _rebuild_slug_cache fetches both fans and followers lists."""
    with patch.object(provider, "_browse_person_people", new_callable=AsyncMock) as mock_browse:
        mock_browse.return_value = []

        await provider._rebuild_slug_cache()

        assert mock_browse.call_count == 2
        calls = mock_browse.call_args_list
        assert calls[0].args == (CollectionType.FOLLOWING_FANS, "bandcamp_test://fans")
        assert calls[1].args == (CollectionType.FOLLOWERS, "bandcamp_test://followers")


# --- Pagination tests ---


def _make_collection_page(
    items: list[Mock],
    has_more: bool = False,
    last_token: str | None = None,
) -> Mock:
    """Create a mock CollectionSummary page."""
    page = Mock()
    page.items = items
    page.has_more = has_more
    page.last_token = last_token
    return page


async def test_get_all_collection_items_single_page(provider: BandcampProvider) -> None:
    """Test _get_all_collection_items with a single page."""
    page = _make_collection_page(
        [Mock(item_type="album", item_id=1, band_id=10)],
        has_more=False,
    )
    with patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = page

        result = await provider._get_all_collection_items(CollectionType.COLLECTION)

        assert len(result) == 1
        mock_get.assert_called_once()


async def test_get_all_collection_items_multiple_pages(provider: BandcampProvider) -> None:
    """Test _get_all_collection_items follows pagination across multiple pages."""
    page1 = _make_collection_page(
        [Mock(item_type="album", item_id=i, band_id=10) for i in range(50)],
        has_more=True,
        last_token="token_page2",
    )
    page2 = _make_collection_page(
        [Mock(item_type="album", item_id=i, band_id=10) for i in range(50, 100)],
        has_more=True,
        last_token="token_page3",
    )
    page3 = _make_collection_page(
        [Mock(item_type="album", item_id=i, band_id=10) for i in range(100, 120)],
        has_more=False,
    )

    with patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [page1, page2, page3]

        result = await provider._get_all_collection_items(CollectionType.COLLECTION)

        assert len(result) == 120
        assert mock_get.call_count == 3
        # First call has no older_than_token (positional arg index 1)
        assert mock_get.call_args_list[0].args[1] is None
        # Subsequent calls pass the last_token from the previous page
        assert mock_get.call_args_list[1].args[1] == "token_page2"
        assert mock_get.call_args_list[2].args[1] == "token_page3"


async def test_get_all_collection_items_stops_on_missing_last_token(
    provider: BandcampProvider,
) -> None:
    """Test _get_all_collection_items stops when has_more is True but last_token is None."""
    page = _make_collection_page(
        [Mock(item_type="album", item_id=1, band_id=10)],
        has_more=True,
        last_token=None,
    )
    with patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = page

        result = await provider._get_all_collection_items(CollectionType.COLLECTION)

        assert len(result) == 1
        mock_get.assert_called_once()


async def test_get_all_collection_items_passes_fan_id(provider: BandcampProvider) -> None:
    """Test _get_all_collection_items forwards fan_id to the API client."""
    page = _make_collection_page([], has_more=False)
    with patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = page

        await provider._get_all_collection_items(CollectionType.WISHLIST, fan_id=42)

        mock_get.assert_called_once_with(CollectionType.WISHLIST, None, 42)


async def test_get_library_albums_paginates(provider: BandcampProvider) -> None:
    """Test get_library_albums yields albums from all pages."""
    page1 = _make_collection_page(
        [Mock(item_type="album", item_id=i, band_id=10) for i in range(1, 4)],
        has_more=True,
        last_token="tok2",
    )
    page2 = _make_collection_page(
        [Mock(item_type="album", item_id=i, band_id=10) for i in range(4, 6)],
        has_more=False,
    )

    with (
        patch.object(provider._client, "get_collection_items", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get.side_effect = [page1, page2]
        mock_get_album.return_value = Mock()

        albums = [album async for album in provider.get_library_albums()]

        assert len(albums) == 5
        assert mock_get.call_count == 2


async def test_get_library_artists_paginates(provider: BandcampProvider) -> None:
    """Test get_library_artists yields artists from all pages."""
    page1 = _make_collection_page(
        [Mock(item_type="album", item_id=1, band_id=100)],
        has_more=True,
        last_token="tok2",
    )
    page2 = _make_collection_page(
        [Mock(item_type="album", item_id=2, band_id=200)],
        has_more=False,
    )

    with (
        patch.object(provider._client, "get_collection_items", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get.side_effect = [page1, page2]
        mock_get_artist.return_value = Mock()

        artists = [artist async for artist in provider.get_library_artists()]

        assert len(artists) == 2
        assert mock_get.call_count == 2
        # Band IDs 100 and 200 from pages 1 and 2, converted to str per base class contract
        called_ids = {call.args[0] for call in mock_get_artist.call_args_list}
        assert called_ids == {"100", "200"}


async def test_get_all_collection_items_error_mid_pagination(
    provider: BandcampProvider,
) -> None:
    """Test that an API error on page 2 propagates without returning partial results."""
    page1 = _make_collection_page(
        [Mock(item_type="album", item_id=1, band_id=10)],
        has_more=True,
        last_token="token_page2",
    )

    with patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [page1, BandcampRateLimitError(retry_after=3)]

        with pytest.raises(BandcampRateLimitError):
            await provider._get_all_collection_items(CollectionType.COLLECTION)

        assert mock_get.call_count == 2


async def test_browse_person_content_returns_only_resolved_items(
    provider: BandcampProvider,
) -> None:
    """
    Test that _browse_person_content returns only resolved Album/Track objects.

    Regression test: a previous version reused the same list variable for both
    the raw API items and the resolved results, which mixed CollectionItem
    objects into the returned list.
    """
    raw_items = [
        _collection_entry("album", 456, tralbum_id=456),
        _collection_entry("track", 789, tralbum_id=789),
    ]

    with patch.object(
        provider, "_get_all_collection_items", new_callable=AsyncMock
    ) as mock_get_collection:
        mock_get_collection.return_value = raw_items

        result = await provider._browse_person_content(42, CollectionType.WISHLIST)

        assert [type(item) for item in result] == [Album, Track]
        # Verify no raw CollectionItem objects leaked into the result
        for item in result:
            assert item is not raw_items[0]
            assert item is not raw_items[1]


async def test_get_all_collection_items_detects_token_loop(
    provider: BandcampProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _get_all_collection_items stops when the same token repeats."""
    stuck_page = _make_collection_page(
        [Mock(item_type="album", item_id=1, band_id=10)],
        has_more=True,
        last_token="same_token_forever",
    )

    with patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = stuck_page

        result = await provider._get_all_collection_items(CollectionType.COLLECTION)

        # Should fetch page 1 (token not yet seen), then page 2 (token repeated → stop)
        assert mock_get.call_count == 2
        assert len(result) == 2
        assert "Pagination loop detected" in caplog.text


async def test_get_all_collection_items_raises_on_a_token_loop_when_complete(
    provider: BandcampProvider,
) -> None:
    """A repeated page token raises instead of returning a short list, if the caller asks."""
    stuck_page = _make_collection_page(
        [Mock(item_type="album", item_id=1, band_id=10)],
        has_more=True,
        last_token="same_token_forever",
    )

    with patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = stuck_page

        with pytest.raises(ResourceTemporarilyUnavailable, match="same_token_forever"):
            await provider._get_all_collection_items(
                CollectionType.COLLECTION, require_complete=True
            )

        assert mock_get.call_count == 2


@pytest.mark.parametrize(
    ("library_method", "item_method"),
    [
        ("get_library_artists", "get_artist"),
        ("get_library_albums", "get_album"),
        ("get_library_tracks", "get_album_tracks"),
    ],
)
async def test_library_sync_stops_on_a_token_loop(
    provider: BandcampProvider, library_method: str, item_method: str
) -> None:
    """A library sync fails on a repeated page token, so the core deletes no library item."""
    stuck_page = _make_collection_page(
        [Mock(item_type="album", item_id=1, band_id=10)],
        has_more=True,
        last_token="same_token_forever",
    )

    with (
        patch.object(provider, "_fetch_collection_page", new_callable=AsyncMock) as mock_get,
        patch.object(provider, item_method, new_callable=AsyncMock) as mock_item,
    ):
        mock_get.return_value = stuck_page

        with pytest.raises(ResourceTemporarilyUnavailable):
            _ = [item async for item in getattr(provider, library_method)()]

        mock_item.assert_not_awaited()
