"""Test Bandcamp Provider integration."""

from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from bandcamp_async_api import (
    BandcampAPIError,
    BandcampMustBeLoggedInError,
    BandcampNotFoundError,
    BandcampRateLimitError,
    SearchResultAlbum,
    SearchResultArtist,
    SearchResultTrack,
)
from bandcamp_async_api.models import CollectionType
from music_assistant_models.enums import (
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
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import Album, Artist, BrowseFolder, Track
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.bandcamp import BandcampProvider, split_id
from music_assistant.providers.bandcamp.constants import (
    CACHE_EMPTY_RESULTS,
    CACHE_USER_LISTS,
    DEFAULT_TOP_TRACKS_LIMIT,
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
        )


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
) -> Mock:
    """Construct a SearchResultAlbum mock with concrete attribute values."""
    item = Mock(spec=SearchResultAlbum)
    item.id = album_id
    item.name = "Album"
    item.artist_id = artist_id
    item.artist_name = artist_name
    item.artist_url = ""
    item.url = ""
    item.image_url = None
    return item


def _search_artist_mock(
    *, artist_id: int = 123, name: str = "Test Artist", is_label: bool = False
) -> Mock:
    """Construct a SearchResultArtist mock with concrete attribute values."""
    item = Mock(spec=SearchResultArtist)
    item.id = artist_id
    item.name = name
    item.url = ""
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
    label_band = _search_artist_mock(artist_id=label_id, name="audiophob")
    label_album = _search_album_mock(
        artist_id=label_id, artist_name="audiophob", album_id=1198969224
    )
    mortaja_album = _search_album_mock(
        artist_id=label_id, artist_name="Mortaja", album_id=1938115920
    )

    with patch.object(provider._client, "search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [label_band, label_album, mortaja_album]

        results = await provider.search("Mortaja", [MediaType.ALBUM, MediaType.ARTIST], limit=10)

        artist_ids = {a.item_id for a in results.artists}
        # Real label artist + synthetic Mortaja-on-audiophob.
        assert artist_ids == {str(label_id), f"{label_id}:mortaja"}

        # The album by the label itself should link to the real ID;
        # the Mortaja album should link to the synthetic.
        album_artist_ids = {next(iter(cast("Album", a).artists)).item_id for a in results.albums}
        assert album_artist_ids == {str(label_id), f"{label_id}:mortaja"}


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


async def test_search_without_identity(provider: BandcampProvider) -> None:
    """Test search returns empty results without identity token."""
    provider._client.identity = None

    results = await provider.search("test query", [MediaType.TRACK])

    assert len(results.tracks) == 0
    assert len(results.albums) == 0
    assert len(results.artists) == 0


async def test_search_api_error(provider: BandcampProvider) -> None:
    """Test search handles API errors gracefully."""
    with (
        patch.object(provider._client, "search", side_effect=BandcampAPIError("API Error")),
        pytest.raises(InvalidDataError, match="Unexpected error during Bandcamp search"),
    ):
        await provider.search("test query", [MediaType.TRACK])


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
        # Per-page performers don't have their own Bandcamp page, so the
        # synthetic carries the hosting band's URL (audiophob), matching
        # what the search-emission path passes through.
        assert result.uri == "https://audiophob.bandcamp.com"
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


async def test_get_synthetic_artist_rate_limit_at_band_lookup(
    provider: BandcampProvider,
) -> None:
    """
    Rate-limit on the initial band lookup converts to ResourceTemporarilyUnavailable.

    Tests ``_get_synthetic_artist`` directly to bypass the public method's
    ``@throttle_with_retries`` decorator. The decorator's job is to retry
    on this exception; our job is to make sure we *raise* it with the
    backoff hint preserved so the decorator (and any other caller) can
    use it.
    """
    band_id = 441379041
    with patch.object(provider._client, "get_artist", new_callable=AsyncMock) as mock_get_artist:
        mock_get_artist.side_effect = BandcampRateLimitError("Rate limited", retry_after=42)
        with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
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
        with pytest.raises(ResourceTemporarilyUnavailable) as exc_info:
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
    """Test successful track retrieval."""
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
        mock_converter.return_value = Mock()

        result = await provider.get_track("123-456-789")

        mock_get_album.assert_called_once_with(123, 456)
        assert result is not None


async def test_get_track_standalone(provider: BandcampProvider) -> None:
    """Test get_track for a standalone track (album_id=0) uses get_track API path."""
    mock_album_obj = Mock()
    mock_album_obj.id = 456
    mock_album_obj.title = "Standalone Album"
    mock_album_obj.art_url = "http://example.com/art.jpg"

    mock_api_track = Mock()
    mock_api_track.album = mock_album_obj
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
        mock_converter.return_value = Mock()

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
    """Test get_track for a standalone track where api_track.album is None."""
    mock_api_track = Mock()
    mock_api_track.album = None
    mock_api_track.tralbum_artist = None
    mock_api_track.artist.id = 123
    mock_api_track.artist.name = "Test Band"

    with (
        patch.object(provider._client, "get_track", new_callable=AsyncMock) as mock_get_track,
        patch.object(provider._converters, "track_from_api") as mock_converter,
    ):
        mock_get_track.return_value = mock_api_track
        mock_converter.return_value = Mock()

        result = await provider.get_track("123-0-789")

        mock_get_track.assert_called_once_with(123, 789)
        mock_converter.assert_called_once_with(
            track=mock_api_track,
            album_id=None,
            album_name="",
            album_image_url="",
            tralbum_artist=None,
            artist_item_id="123",
        )
        assert result is not None


async def test_get_track_not_found(provider: BandcampProvider) -> None:
    """Test track retrieval when not found."""
    with (
        patch.object(provider._client, "get_album", side_effect=BandcampNotFoundError("Not found")),
        pytest.raises(MediaNotFoundError, match=r"Track 123-456-789 not found on Bandcamp"),
    ):
        await provider.get_track("123-456-789")


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
        mock_converter.return_value = Mock()

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

    with patch.object(
        provider._client, "get_artist_discography", new_callable=AsyncMock
    ) as mock_get_discography:
        mock_get_discography.return_value = mock_discography

        result = await provider.get_artist_albums(f"{label_id}:mortaja")

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

        with pytest.raises(MediaNotFoundError, match=r"No streaming URL found"):
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

        with pytest.raises(MediaNotFoundError, match=r"No streaming URL found"):
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
    mock_album = Mock()
    mock_track = Mock()

    with (
        patch.object(provider, "get_artist_albums", new_callable=AsyncMock) as mock_get_albums,
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_get_tracks,
    ):
        mock_get_albums.return_value = [mock_album]
        mock_get_tracks.return_value = [mock_track]

        result = await provider.get_artist_toptracks("123")

        assert len(result) == 1
        mock_get_albums.assert_called_once_with("123")


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
        patch.object(provider, "get_library_albums") as mock_get_albums,
        patch.object(provider, "get_album_tracks", new_callable=AsyncMock) as mock_get_tracks,
    ):
        # Make get_library_albums an async generator
        async def mock_albums_gen() -> AsyncGenerator[Mock]:
            yield Mock(item_id="123-456")

        mock_get_albums.return_value = mock_albums_gen()
        mock_get_tracks.return_value = [mock_track]

        tracks = [track async for track in provider.get_library_tracks()]

        assert len(tracks) == 1
        mock_get_tracks.assert_called_once_with("123-456")


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
        pytest.raises(LoginFailed, match=r"login is invalid or expired"),
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


async def test_browse_wishlist_returns_albums_and_tracks(provider: BandcampProvider) -> None:
    """Test browsing wishlist returns resolved albums and tracks."""
    collection_items = [
        Mock(item_type="album", item_id=456, band_id=123),
        Mock(item_type="track", item_id=789, band_id=123),
    ]

    mock_album = Mock()
    mock_track = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = mock_album
        mock_get_track.return_value = mock_track

        result = await provider.browse("bandcamp_test://wishlist")

        mock_get_collection.assert_called_once_with(CollectionType.WISHLIST, fan_id=None)
        mock_get_album.assert_called_once_with("123-456")
        mock_get_track.assert_called_once_with("123-0-789")
        assert len(result) == 2
        assert mock_album in result
        assert mock_track in result


async def test_browse_wishlist_skips_failed_items(provider: BandcampProvider) -> None:
    """Test that wishlist browse skips items that fail to resolve."""
    collection_items = [
        Mock(item_type="album", item_id=456, band_id=123),
        Mock(item_type="album", item_id=789, band_id=123),
    ]

    mock_album = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.side_effect = [mock_album, MediaNotFoundError("not found")]

        result = await provider.browse("bandcamp_test://wishlist")

        assert len(result) == 1


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
    """Test browsing following returns resolved artists."""
    collection_items = [
        Mock(spec=["band_id", "name"], band_id=100, name="Artist1"),
        Mock(spec=["band_id", "name"], band_id=200, name="Artist2"),
    ]

    mock_artist_1 = Mock()
    mock_artist_2 = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_artist.side_effect = [mock_artist_1, mock_artist_2]

        result = await provider.browse("bandcamp_test://following")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING, fan_id=None)
        assert mock_get_artist.call_count == 2
        assert len(result) == 2


async def test_browse_following_skips_failed_artists(provider: BandcampProvider) -> None:
    """Test that following browse skips artists that fail to resolve."""
    collection_items = [
        Mock(spec=["band_id", "name"], band_id=100, name="Found"),
        Mock(spec=["band_id", "name"], band_id=200, name="NotFound"),
    ]

    mock_artist = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_artist.side_effect = [mock_artist, MediaNotFoundError("not found")]

        result = await provider.browse("bandcamp_test://following")

        assert len(result) == 1


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
        Mock(item_type="band", item_id=100, band_id=100),
        Mock(item_type="album", item_id=456, band_id=123),
    ]

    mock_album = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = mock_album

        result = await provider.browse("bandcamp_test://wishlist")

        assert len(result) == 1
        mock_get_album.assert_called_once_with("123-456")


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
    with pytest.raises(MediaNotFoundError, match="my custom context"):
        async with provider._map_api_errors("my custom context"):
            raise BandcampAPIError("Something went wrong")


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
    collection_items = [Mock(item_type="album", item_id=456, band_id=123)]
    mock_album = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = mock_album

        result = await provider.browse("bandcamp_test://fans/42/collection")

        mock_get_collection.assert_called_once_with(CollectionType.COLLECTION, fan_id=42)
        assert len(result) == 1
        assert result[0] is mock_album


async def test_browse_person_wishlist(provider: BandcampProvider) -> None:
    """Test browsing fans/42/wishlist fetches person's wishlist."""
    collection_items = [Mock(item_type="album", item_id=789, band_id=123)]
    mock_album = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = mock_album

        result = await provider.browse("bandcamp_test://fans/42/wishlist")

        mock_get_collection.assert_called_once_with(CollectionType.WISHLIST, fan_id=42)
        assert len(result) == 1
        assert result[0] is mock_album


async def test_browse_person_following(provider: BandcampProvider) -> None:
    """Test browsing fans/42/following fetches person's followed artists."""
    collection_items = [Mock(spec=["band_id", "name"], band_id=100, name="Artist1")]
    mock_artist = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_artist.return_value = mock_artist

        result = await provider.browse("bandcamp_test://fans/42/following")

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING, fan_id=42)
        assert len(result) == 1
        assert result[0] is mock_artist


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
    collection_items = [Mock(item_type="album", item_id=456, band_id=123)]
    mock_album = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = mock_album

        result = await provider._browse_person_content(42, CollectionType.COLLECTION)

        mock_get_collection.assert_called_once_with(CollectionType.COLLECTION, fan_id=42)
        assert len(result) == 1
        assert result[0] is mock_album


async def test_browse_person_content_caches_results(provider: BandcampProvider) -> None:
    """Test _browse_person_content caches non-empty results."""
    collection_items = [Mock(item_type="album", item_id=456, band_id=123)]
    mock_album = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = mock_album

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
    collection_items = [Mock(item_type="album", item_id=456, band_id=123)]
    mock_album = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_album.return_value = mock_album

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
    collection_items = [Mock(spec=["band_id", "name"], band_id=100, name="Artist1")]
    mock_artist = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_artist.return_value = mock_artist

        result = await provider._browse_person_following(42)

        mock_get_collection.assert_called_once_with(CollectionType.FOLLOWING, fan_id=42)
        assert len(result) == 1
        assert result[0] is mock_artist


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


async def test_browse_person_following_skips_not_found(provider: BandcampProvider) -> None:
    """Test _browse_person_following logs warning and skips unfound artists."""
    collection_items = [
        Mock(spec=["band_id", "name"], band_id=100, name="Found"),
        Mock(spec=["band_id", "name"], band_id=200, name="NotFound"),
    ]
    mock_artist = Mock()

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_artist", new_callable=AsyncMock) as mock_get_artist,
    ):
        mock_get_collection.return_value = collection_items
        mock_get_artist.side_effect = [mock_artist, MediaNotFoundError("not found")]

        result = await provider._browse_person_following(42)

        assert len(result) == 1


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
        Mock(item_type="album", item_id=456, band_id=123),
        Mock(item_type="track", item_id=789, band_id=123),
    ]
    mock_album = Mock(spec=Album)
    mock_track = Mock(spec=Track)

    with (
        patch.object(
            provider, "_get_all_collection_items", new_callable=AsyncMock
        ) as mock_get_collection,
        patch.object(provider, "get_album", new_callable=AsyncMock) as mock_get_album,
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track,
    ):
        mock_get_collection.return_value = raw_items
        mock_get_album.return_value = mock_album
        mock_get_track.return_value = mock_track

        result = await provider._browse_person_content(42, CollectionType.WISHLIST)

        assert len(result) == 2
        assert result[0] is mock_album
        assert result[1] is mock_track
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
