"""Test Plex provider helper functions."""

from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from music_assistant.providers.plex.helpers import (
    get_explicit,
    get_musicbrainz_id,
    is_library_scan_finished,
    parse_plex_lyrics_payload,
    resolve_server_auth_token,
)

SYNCED_JSON = (
    '{"MediaContainer": {"Lyrics": [{"Line": ['
    '{"Span": [{"text": "Hello ", "startOffset": 12000},'
    ' {"text": "world", "startOffset": 12000}]},'
    '{"Span": [{"text": "second line", "startOffset": 15500}]}'
    "]}]}}"
)

LRC_TEXT = "[00:12.00]Hello world\n[00:15.50]second line\n"

PLAIN_TEXT = "Hello world\nsecond line"

VALID_MBID = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"


def test_parse_lyrics_structured_json_synced() -> None:
    """Structured Plex JSON with offsets parses to a synced LRC string."""
    result = parse_plex_lyrics_payload(SYNCED_JSON)
    assert result == ("[00:12.00]Hello world\n[00:15.50]second line", True)


def test_parse_lyrics_raw_lrc() -> None:
    """Raw LRC text is detected as synced and returned verbatim (stripped)."""
    assert parse_plex_lyrics_payload(LRC_TEXT) == (LRC_TEXT.strip(), True)


def test_parse_lyrics_plain_text() -> None:
    """Plain text without timestamps is returned as unsynced lyrics."""
    assert parse_plex_lyrics_payload(PLAIN_TEXT) == (PLAIN_TEXT, False)


@pytest.mark.parametrize("content", ["", "   ", "\n\n"])
def test_parse_lyrics_empty(content: str) -> None:
    """Empty payloads yield no lyrics."""
    assert parse_plex_lyrics_payload(content) is None


def test_parse_lyrics_json_unsynced() -> None:
    """Structured JSON without offsets falls back to plain unsynced text."""
    payload = '{"MediaContainer": {"Lyrics": [{"Line": [{"Span": [{"text": "no timing"}]}]}]}}'
    assert parse_plex_lyrics_payload(payload) == ("no timing", False)


def test_parse_lyrics_malformed_json_as_plain() -> None:
    """Malformed JSON that is not LRC is treated as plain text."""
    assert parse_plex_lyrics_payload("{not valid json") == ("{not valid json", False)


def test_parse_lyrics_no_lyrics_envelope() -> None:
    """Plex's no-lyrics envelope yields None, not the raw JSON as plain text."""
    payload = '{"MediaContainer":{"size":1,"Lyrics":[{}]}}'
    assert parse_plex_lyrics_payload(payload) is None


def test_parse_lyrics_long_offset_no_minute_wrap() -> None:
    """Offsets beyond one hour keep counting minutes instead of wrapping at 60."""
    payload = (
        '{"MediaContainer": {"Lyrics": [{"Line": ['
        '{"Span": [{"text": "late line", "startOffset": 3661230}]}'
        "]}]}}"
    )
    assert parse_plex_lyrics_payload(payload) == ("[61:01.23]late line", True)


def _guid_elem(guid_id: str) -> Mock:
    elem = Mock()
    elem.attrib = {"id": guid_id}
    return elem


def _plex_obj(
    *,
    guids: list[str] | None = None,
    attrib: dict[str, str] | None = None,
) -> Mock:
    obj = Mock()
    obj._data.findall.return_value = [_guid_elem(guid_id) for guid_id in (guids or [])]
    obj._data.attrib = attrib or {}
    return obj


def test_get_musicbrainz_id_from_guid() -> None:
    """A mbid:// guid yields the bare MusicBrainz identifier."""
    obj = _plex_obj(guids=["plex://album/abc", f"mbid://{VALID_MBID}"])
    assert get_musicbrainz_id(obj) == VALID_MBID


def test_get_musicbrainz_id_no_mbid() -> None:
    """Objects without a mbid:// guid return None."""
    obj = _plex_obj(guids=["plex://album/abc"])
    assert get_musicbrainz_id(obj) is None


@pytest.mark.parametrize(
    ("content_rating", "expected"),
    [("explicit", True), ("Explicit", True), ("clean", False), ("", None), (None, None)],
)
def test_get_explicit(content_rating: str | None, expected: bool | None) -> None:
    """Content rating maps to explicit only for the 'explicit' value."""
    attrib = {"contentRating": content_rating} if content_rating is not None else {}
    assert get_explicit(_plex_obj(attrib=attrib)) is expected


def _activity(event: str, activity_type: str = "library.update.section") -> dict[str, Any]:
    """Build a Plex activity notification."""
    return {
        "NotificationContainer": {
            "type": "activity",
            "ActivityNotification": [{"event": event, "Activity": {"type": activity_type}}],
        }
    }


@pytest.mark.parametrize(
    ("notification", "expected"),
    [
        (_activity("ended"), True),
        (_activity("started"), False),
        (_activity("updated"), False),
        (_activity("ended", "media.generate.music.analysis"), False),
        ({"NotificationContainer": {"type": "playing"}}, False),
        ({}, False),
    ],
)
def test_is_library_scan_finished(notification: dict[str, Any], expected: bool) -> None:
    """Only the end of a library scan counts, not its progress or other activities."""
    assert is_library_scan_finished(notification) is expected


ACCOUNT_TOKEN = "account-token"
RESOURCE_TOKEN = "resource-token"
SERVER_IP = "192.168.1.10"
SERVER_PORT = "32400"


def _plex_resource(
    *,
    owned: bool,
    address: str = SERVER_IP,
    port: int = 32400,
    provides: str = "server",
) -> Mock:
    connection = Mock()
    connection.address = address
    connection.port = port
    resource = Mock()
    resource.owned = owned
    resource.provides = provides
    resource.accessToken = RESOURCE_TOKEN
    resource.connections = [connection]
    return resource


def _plex_account(*resources: Mock) -> Mock:
    account = Mock()
    account.resources.return_value = list(resources)
    return account


def test_resolve_server_auth_token_owned_server() -> None:
    """A server owned by the account keeps using the account-level token."""
    account = _plex_account(_plex_resource(owned=True))
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == ACCOUNT_TOKEN
    )


def test_resolve_server_auth_token_shared_server() -> None:
    """A server shared with the account resolves to that resource's own access token."""
    account = _plex_account(_plex_resource(owned=False))
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == RESOURCE_TOKEN
    )


def test_resolve_server_auth_token_skips_non_server_resource() -> None:
    """A resource at the same address that is not a server is ignored."""
    account = _plex_account(_plex_resource(owned=False, provides="player"))
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == ACCOUNT_TOKEN
    )


@pytest.mark.parametrize(
    ("address", "port"),
    [("10.0.0.5", 32400), (SERVER_IP, 32401)],
)
def test_resolve_server_auth_token_no_matching_connection(address: str, port: int) -> None:
    """A configured address matching no resource connection keeps the account token."""
    account = _plex_account(_plex_resource(owned=False, address=address, port=port))
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == ACCOUNT_TOKEN
    )


def test_resolve_server_auth_token_matches_shared_among_several_resources() -> None:
    """The shared server is found even when the account also holds unrelated resources."""
    account = _plex_account(
        _plex_resource(owned=True, address="10.0.0.5"),
        _plex_resource(owned=False, provides="server,player"),
    )
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == RESOURCE_TOKEN
    )


def test_resolve_server_auth_token_plextv_unreachable() -> None:
    """A failure while querying plex.tv falls back to the account token instead of raising."""
    account = Mock()
    account.resources.side_effect = requests.exceptions.ConnectionError("plex.tv unreachable")
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == ACCOUNT_TOKEN
    )


def test_resolve_server_auth_token_builds_account_when_not_supplied() -> None:
    """Without a pre-authenticated account one is built from the account token."""
    account = _plex_account(_plex_resource(owned=False))
    with patch(
        "music_assistant.providers.plex.helpers.MyPlexAccount", return_value=account
    ) as account_cls:
        result = resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT)
    account_cls.assert_called_once_with(token=ACCOUNT_TOKEN)
    assert result == RESOURCE_TOKEN


def test_resolve_server_auth_token_shared_server_without_access_token() -> None:
    """A shared server without its own access token falls back to the account token."""
    resource = _plex_resource(owned=False)
    resource.accessToken = None
    account = _plex_account(resource)
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == ACCOUNT_TOKEN
    )


def test_resolve_server_auth_token_resource_without_provides() -> None:
    """A resource that does not advertise what it provides is skipped, not fatal."""
    resource = _plex_resource(owned=False)
    resource.provides = None
    account = _plex_account(resource, _plex_resource(owned=False, address="10.0.0.5"))
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, SERVER_IP, SERVER_PORT, myplex_account=account)
        == ACCOUNT_TOKEN
    )
