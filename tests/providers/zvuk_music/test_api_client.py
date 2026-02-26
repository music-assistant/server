"""Tests for ZvukMusicClient in provider/api_client.py."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from zvuk_music.exceptions import (
    BadRequestError,
    BotDetectedError,
    GraphQLError,
    NetworkError,
    NotFoundError,
    TimedOutError,
    UnauthorizedError,
)

from music_assistant.providers.zvuk_music.api_client import ZvukMusicClient, handle_zvuk_errors

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(token: str = "test-token") -> ZvukMusicClient:  # noqa: S107
    """Create a ZvukMusicClient with a fake token and mocked http session."""
    return ZvukMusicClient(token=token, http_session=MagicMock())


def _make_connected_client() -> tuple[ZvukMusicClient, MagicMock]:
    """Create a ZvukMusicClient with _client already set (simulates post-connect state).

    :return: Tuple of (client, inner_mock) where inner_mock is the mocked ClientAsync.
    """
    zvuk_client = _make_client()
    inner = MagicMock()
    zvuk_client._client = inner
    zvuk_client._user_id = "42"
    return zvuk_client, inner


# ---------------------------------------------------------------------------
# Tests for handle_zvuk_errors decorator
# ---------------------------------------------------------------------------


class TestHandleZvukErrors:
    """Tests for the handle_zvuk_errors decorator."""

    @pytest.mark.asyncio
    async def test_unauthorized_error_raises_login_failed(self) -> None:
        """UnauthorizedError is mapped to LoginFailed."""

        @handle_zvuk_errors()
        async def failing(_self: object) -> None:
            raise UnauthorizedError("bad token")

        with pytest.raises(LoginFailed):
            await failing(None)

    @pytest.mark.asyncio
    async def test_network_error_raises_resource_temporarily_unavailable(self) -> None:
        """NetworkError is mapped to ResourceTemporarilyUnavailable."""

        @handle_zvuk_errors()
        async def failing(_self: object) -> None:
            raise NetworkError("connection reset")

        with pytest.raises(ResourceTemporarilyUnavailable):
            await failing(None)

    @pytest.mark.asyncio
    async def test_timed_out_error_raises_resource_temporarily_unavailable(self) -> None:
        """TimedOutError is mapped to ResourceTemporarilyUnavailable."""

        @handle_zvuk_errors()
        async def failing(_self: object) -> None:
            raise TimedOutError("timeout")

        with pytest.raises(ResourceTemporarilyUnavailable):
            await failing(None)

    @pytest.mark.asyncio
    async def test_bad_request_error_raises_resource_temporarily_unavailable(self) -> None:
        """BadRequestError is mapped to ResourceTemporarilyUnavailable."""

        @handle_zvuk_errors()
        async def failing(_self: object) -> None:
            raise BadRequestError("bad request")

        with pytest.raises(ResourceTemporarilyUnavailable):
            await failing(None)

    @pytest.mark.asyncio
    async def test_bot_detected_error_raises_provider_unavailable(self) -> None:
        """BotDetectedError is mapped to ProviderUnavailableError."""

        @handle_zvuk_errors()
        async def failing(_self: object) -> None:
            raise BotDetectedError("bot detected")

        with pytest.raises(ProviderUnavailableError):
            await failing(None)

    @pytest.mark.asyncio
    async def test_not_found_returns_sentinel_value_when_provided(self) -> None:
        """NotFoundError returns not_found_return when the param is set."""

        @handle_zvuk_errors(not_found_return=None)
        async def failing(_self: object) -> str | None:
            raise NotFoundError("not found")

        result = await failing(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_not_found_empty_list_sentinel(self) -> None:
        """not_found_return=[] returns an empty list on NotFoundError."""

        @handle_zvuk_errors(not_found_return=[])
        async def failing(_self: object) -> list[str]:
            raise NotFoundError("not found")

        result = await failing(None)
        assert result == []

    @pytest.mark.asyncio
    async def test_not_found_error_reraised_when_no_sentinel(self) -> None:
        """NotFoundError is re-raised when not_found_return is not provided."""

        @handle_zvuk_errors()
        async def failing(_self: object) -> None:
            raise NotFoundError("not found")

        with pytest.raises(NotFoundError):
            await failing(None)

    @pytest.mark.asyncio
    async def test_success_returns_value(self) -> None:
        """The decorated function returns its value normally on success."""

        @handle_zvuk_errors(not_found_return=None)
        async def succeeding(_self: object) -> str:
            return "ok"

        result = await succeeding(None)
        assert result == "ok"


# ---------------------------------------------------------------------------
# Tests for connect()
# ---------------------------------------------------------------------------


class TestConnect:
    """Tests for ZvukMusicClient.connect()."""

    @pytest.mark.asyncio
    async def test_connect_calls_init_and_is_authorized(self) -> None:
        """connect() calls ClientAsync(token=...).init() and is_authorized()."""
        client = _make_client(token="my-token")

        mock_inner = MagicMock()
        mock_inner.init = AsyncMock(return_value=mock_inner)
        mock_inner.is_authorized = AsyncMock(return_value=True)
        profile = MagicMock()
        profile.result.id = 99
        mock_inner.get_profile = AsyncMock(return_value=profile)

        with patch(
            "music_assistant.providers.zvuk_music.api_client.ClientAsync",
            return_value=mock_inner,
        ):
            await client.connect()

        mock_inner.init.assert_awaited_once()
        mock_inner.is_authorized.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_sets_user_id_from_profile(self) -> None:
        """connect() sets _user_id from profile.result.id."""
        client = _make_client()

        mock_inner = MagicMock()
        mock_inner.init = AsyncMock(return_value=mock_inner)
        mock_inner.is_authorized = AsyncMock(return_value=True)
        profile = MagicMock()
        profile.result.id = 777
        mock_inner.get_profile = AsyncMock(return_value=profile)

        with patch(
            "music_assistant.providers.zvuk_music.api_client.ClientAsync",
            return_value=mock_inner,
        ):
            await client.connect()

        assert client._user_id == "777"

    @pytest.mark.asyncio
    async def test_connect_raises_login_failed_when_not_authorized(self) -> None:
        """connect() raises LoginFailed when is_authorized() returns False."""
        client = _make_client()

        mock_inner = MagicMock()
        mock_inner.init = AsyncMock(return_value=mock_inner)
        mock_inner.is_authorized = AsyncMock(return_value=False)

        with (
            patch(
                "music_assistant.providers.zvuk_music.api_client.ClientAsync",
                return_value=mock_inner,
            ),
            pytest.raises(LoginFailed),
        ):
            await client.connect()

    @pytest.mark.asyncio
    async def test_connect_raises_login_failed_on_unauthorized_error(self) -> None:
        """connect() raises LoginFailed when ClientAsync.init() raises UnauthorizedError."""
        client = _make_client()

        mock_inner = MagicMock()
        mock_inner.init = AsyncMock(side_effect=UnauthorizedError("bad token"))

        with (
            patch(
                "music_assistant.providers.zvuk_music.api_client.ClientAsync",
                return_value=mock_inner,
            ),
            pytest.raises(LoginFailed),
        ):
            await client.connect()

    @pytest.mark.asyncio
    async def test_connect_raises_resource_temporarily_unavailable_on_network_error(
        self,
    ) -> None:
        """connect() raises ResourceTemporarilyUnavailable on NetworkError."""
        client = _make_client()

        mock_inner = MagicMock()
        mock_inner.init = AsyncMock(side_effect=NetworkError("timeout"))

        with (
            patch(
                "music_assistant.providers.zvuk_music.api_client.ClientAsync",
                return_value=mock_inner,
            ),
            pytest.raises(ResourceTemporarilyUnavailable),
        ):
            await client.connect()


# ---------------------------------------------------------------------------
# Tests for _ensure_connected()
# ---------------------------------------------------------------------------


class TestEnsureConnected:
    """Tests for ZvukMusicClient._ensure_connected()."""

    def test_raises_provider_unavailable_when_client_is_none(self) -> None:
        """_ensure_connected() raises ProviderUnavailableError if _client is None."""
        client = _make_client()
        assert client._client is None

        with pytest.raises(ProviderUnavailableError):
            client._ensure_connected()

    def test_returns_client_when_connected(self) -> None:
        """_ensure_connected() returns the inner client when connected."""
        client, inner = _make_connected_client()
        result = client._ensure_connected()
        assert result is inner


# ---------------------------------------------------------------------------
# Tests for get_collection()
# ---------------------------------------------------------------------------


class TestGetCollection:
    """Tests for ZvukMusicClient.get_collection() — regression for bug B2."""

    @pytest.mark.asyncio
    async def test_returns_none_on_not_found_error(self) -> None:
        """get_collection() returns None on NotFoundError (bug B2 regression)."""
        client, inner = _make_connected_client()
        inner.get_collection = AsyncMock(side_effect=NotFoundError("not found"))

        result = await client.get_collection()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_collection_on_success(self) -> None:
        """get_collection() returns the collection object on success."""
        client, inner = _make_connected_client()
        mock_collection = MagicMock()
        inner.get_collection = AsyncMock(return_value=mock_collection)

        result = await client.get_collection()

        assert result is mock_collection


# ---------------------------------------------------------------------------
# Tests for get_editorial_playlist_ids()
# ---------------------------------------------------------------------------


class TestGetEditorialPlaylistIds:
    """Tests for ZvukMusicClient.get_editorial_playlist_ids()."""

    def _patch_request(
        self, client: ZvukMusicClient, inner: MagicMock, response: dict[str, Any] | None
    ) -> None:
        """Wire inner._request.get to return the given response."""
        inner._request.get = AsyncMock(return_value=response)
        setattr(client, "_ensure_connected", MagicMock(return_value=inner))

    @pytest.mark.asyncio
    async def test_returns_string_ids_not_ints(self) -> None:
        """get_editorial_playlist_ids() returns str IDs, not ints."""
        client, inner = _make_connected_client()
        self._patch_request(
            client,
            inner,
            {
                "page": {
                    "data": [
                        {"type": "playlist", "id": 123},
                        {"type": "playlist", "id": 456},
                    ]
                }
            },
        )

        result = await client.get_editorial_playlist_ids()

        assert result == ["123", "456"]
        assert all(isinstance(i, str) for i in result)

    @pytest.mark.asyncio
    async def test_skips_non_playlist_items(self) -> None:
        """get_editorial_playlist_ids() skips items that are not type 'playlist'."""
        client, inner = _make_connected_client()
        self._patch_request(
            client,
            inner,
            {
                "page": {
                    "data": [
                        {"type": "playlist", "id": 10},
                        {"type": "track", "id": 99},
                        {"type": "playlist", "id": 20},
                    ]
                }
            },
        )

        result = await client.get_editorial_playlist_ids()

        assert result == ["10", "20"]

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_api_returns_none(self) -> None:
        """get_editorial_playlist_ids() returns [] when the API returns None."""
        client, inner = _make_connected_client()
        self._patch_request(client, inner, None)

        result = await client.get_editorial_playlist_ids()

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_data_missing(self) -> None:
        """get_editorial_playlist_ids() returns [] when nested 'data' key is absent."""
        client, inner = _make_connected_client()
        self._patch_request(client, inner, {"page": {}})

        result = await client.get_editorial_playlist_ids()

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_items_with_null_id(self) -> None:
        """get_editorial_playlist_ids() skips items with id=None."""
        client, inner = _make_connected_client()
        self._patch_request(
            client,
            inner,
            {"page": {"data": [{"type": "playlist", "id": None}, {"type": "playlist", "id": 7}]}},
        )

        result = await client.get_editorial_playlist_ids()

        assert result == ["7"]


# ---------------------------------------------------------------------------
# Tests for get_direct_stream_url()
# ---------------------------------------------------------------------------


class TestGetDirectStreamUrl:
    """Tests for ZvukMusicClient.get_direct_stream_url()."""

    def _patch_request(self, client: ZvukMusicClient, response: dict[str, Any] | None) -> AsyncMock:
        """Wire inner._request.get to return the given response."""
        inner = MagicMock()
        inner._request.get = AsyncMock(return_value=response)
        setattr(client, "_ensure_connected", MagicMock(return_value=inner))
        return cast("AsyncMock", inner._request.get)

    @pytest.mark.asyncio
    async def test_returns_stream_url_from_result(self) -> None:
        """get_direct_stream_url() returns the stream URL from the API response."""
        client = _make_client()
        self._patch_request(client, {"stream": "https://cdn.zvuk.com/track.flac"})

        result = await client.get_direct_stream_url("12345", "flac")

        assert result == "https://cdn.zvuk.com/track.flac"

    @pytest.mark.asyncio
    async def test_returns_none_when_stream_key_missing(self) -> None:
        """get_direct_stream_url() returns None when 'stream' key is absent."""
        client = _make_client()
        self._patch_request(client, {"expire": 0})

        result = await client.get_direct_stream_url("12345", "flac")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_api_returns_none(self) -> None:
        """get_direct_stream_url() returns None when API response is None."""
        client = _make_client()
        self._patch_request(client, None)

        result = await client.get_direct_stream_url("12345", "high")

        assert result is None

    @pytest.mark.asyncio
    async def test_passes_quality_and_id_params(self) -> None:
        """get_direct_stream_url() passes quality and id as query params."""
        client = _make_client()
        mock_get = self._patch_request(client, {"stream": "https://cdn.zvuk.com/t.mp3"})

        await client.get_direct_stream_url("99999", "mid")

        _args, kwargs = mock_get.call_args.args, mock_get.call_args.kwargs
        assert kwargs["params"]["quality"] == "mid"
        assert kwargs["params"]["id"] == "99999"


# ---------------------------------------------------------------------------
# Tests for get_lyrics()
# ---------------------------------------------------------------------------


class TestGetLyrics:
    """Tests for ZvukMusicClient.get_lyrics()."""

    def _patch_request(self, client: ZvukMusicClient, response: dict[str, Any] | None) -> None:
        """Wire inner._request.get to return the given response."""
        inner = MagicMock()
        inner._request.get = AsyncMock(return_value=response)
        setattr(client, "_ensure_connected", MagicMock(return_value=inner))

    @pytest.mark.asyncio
    async def test_returns_none_when_result_is_none(self) -> None:
        """get_lyrics() returns None when API returns None."""
        client = _make_client()
        self._patch_request(client, None)

        result = await client.get_lyrics("12345")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_lyrics_key_missing(self) -> None:
        """get_lyrics() returns None when result has no 'lyrics' key."""
        client = _make_client()
        self._patch_request(client, {"type": "subtitle"})

        result = await client.get_lyrics("12345")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_lyrics_value_is_falsy(self) -> None:
        """get_lyrics() returns None when lyrics value is None/empty."""
        client = _make_client()
        self._patch_request(client, {"lyrics": None, "type": "subtitle"})

        result = await client.get_lyrics("12345")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_dict_when_lyrics_present(self) -> None:
        """get_lyrics() returns the full result dict when lyrics are present."""
        client = _make_client()
        payload = {"lyrics": "Some lyrics text", "type": "lyrics", "translation": None}
        self._patch_request(client, payload)

        result = await client.get_lyrics("12345")

        assert result == payload

    @pytest.mark.asyncio
    async def test_returns_lrc_dict_for_subtitle_type(self) -> None:
        """get_lyrics() returns the dict for synced (LRC) lyrics."""
        client = _make_client()
        lrc = "[00:00.68]First line\n[00:04.71]Second line\n"
        payload = {"lyrics": lrc, "type": "subtitle", "translation": None}
        self._patch_request(client, payload)

        result = await client.get_lyrics("12345")

        assert result is not None
        assert result["lyrics"] == lrc
        assert result["type"] == "subtitle"


# ---------------------------------------------------------------------------
# Tests for like_track() / unlike_track()
# ---------------------------------------------------------------------------


class TestLikeUnlikeTrack:
    """Tests for ZvukMusicClient.like_track() and unlike_track()."""

    @pytest.mark.asyncio
    async def test_like_track_returns_true_on_success(self) -> None:
        """like_track() returns True when the API call succeeds."""
        client, inner = _make_connected_client()
        inner.like_track = AsyncMock(return_value=True)

        result = await client.like_track("123")

        assert result is True
        inner.like_track.assert_awaited_once_with("123")

    @pytest.mark.asyncio
    async def test_like_track_returns_false_on_bad_request_error(self) -> None:
        """like_track() returns False when BadRequestError is raised."""
        client, inner = _make_connected_client()
        inner.like_track = AsyncMock(side_effect=BadRequestError("bad request"))

        result = await client.like_track("123")

        assert result is False

    @pytest.mark.asyncio
    async def test_like_track_returns_false_on_network_error(self) -> None:
        """like_track() returns False when NetworkError is raised."""
        client, inner = _make_connected_client()
        inner.like_track = AsyncMock(side_effect=NetworkError("connection error"))

        result = await client.like_track("123")

        assert result is False

    @pytest.mark.asyncio
    async def test_like_track_returns_false_on_graphql_error(self) -> None:
        """like_track() returns False when GraphQLError is raised."""
        client, inner = _make_connected_client()
        inner.like_track = AsyncMock(side_effect=GraphQLError("graphql error"))

        result = await client.like_track("123")

        assert result is False

    @pytest.mark.asyncio
    async def test_unlike_track_returns_true_on_success(self) -> None:
        """unlike_track() returns True when the API call succeeds."""
        client, inner = _make_connected_client()
        inner.unlike_track = AsyncMock(return_value=True)

        result = await client.unlike_track("456")

        assert result is True
        inner.unlike_track.assert_awaited_once_with("456")

    @pytest.mark.asyncio
    async def test_unlike_track_returns_false_on_bad_request_error(self) -> None:
        """unlike_track() returns False when BadRequestError is raised."""
        client, inner = _make_connected_client()
        inner.unlike_track = AsyncMock(side_effect=BadRequestError("bad request"))

        result = await client.unlike_track("456")

        assert result is False

    @pytest.mark.asyncio
    async def test_unlike_track_returns_false_on_network_error(self) -> None:
        """unlike_track() returns False when NetworkError is raised."""
        client, inner = _make_connected_client()
        inner.unlike_track = AsyncMock(side_effect=NetworkError("connection error"))

        result = await client.unlike_track("456")

        assert result is False
