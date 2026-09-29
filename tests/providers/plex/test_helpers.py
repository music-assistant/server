"""Test Plex provider helper functions."""

from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests
from plexapi.exceptions import BadRequest

from music_assistant.providers.plex.helpers import (
    PlexServerAccessError,
    get_explicit,
    get_musicbrainz_id,
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


ACCOUNT_TOKEN = "account-token"
RESOURCE_TOKEN = "resource-token"
PLEX_URL = "http://192.168.1.10:32400"
MACHINE_ID = "server-abc"


def _plex_resource(
    *,
    owned: bool,
    machine_id: str = MACHINE_ID,
    provides: str | None = "server",
) -> Mock:
    resource = Mock()
    resource.owned = owned
    resource.provides = provides
    resource.accessToken = RESOURCE_TOKEN
    resource.clientIdentifier = machine_id
    return resource


def _plex_account(*resources: Mock) -> Mock:
    account = Mock()
    account.resources.return_value = list(resources)
    return account


def _plex_session(machine_id: str = MACHINE_ID) -> Mock:
    """Build a requests session whose /identity response reports the given machine id."""
    session = Mock(spec=requests.Session)
    session.get.return_value.json.return_value = {
        "MediaContainer": {"machineIdentifier": machine_id}
    }
    return session


def _resolve(account: Mock) -> str:
    return resolve_server_auth_token(
        ACCOUNT_TOKEN, PLEX_URL, _plex_session(), myplex_account=account
    )


def test_resolve_server_auth_token_owned_server() -> None:
    """A server owned by the account keeps using the account-level token."""
    assert _resolve(_plex_account(_plex_resource(owned=True))) == ACCOUNT_TOKEN


def test_resolve_server_auth_token_shared_server() -> None:
    """A server shared with the account resolves to that resource's own access token."""
    session = _plex_session()
    account = _plex_account(_plex_resource(owned=False))
    assert (
        resolve_server_auth_token(ACCOUNT_TOKEN, PLEX_URL, session, myplex_account=account)
        == RESOURCE_TOKEN
    )
    session.get.assert_called_once_with(
        f"{PLEX_URL}/identity", headers={"Accept": "application/json"}, timeout=10
    )


def test_resolve_server_auth_token_skips_non_server_resource() -> None:
    """A resource with the same identifier that is not a server is ignored."""
    with pytest.raises(PlexServerAccessError):
        _resolve(_plex_account(_plex_resource(owned=False, provides="player")))


def test_resolve_server_auth_token_no_matching_resource() -> None:
    """A server that isn't among the account's resources fails instead of using the account token."""
    with pytest.raises(PlexServerAccessError):
        _resolve(_plex_account(_plex_resource(owned=False, machine_id="other-server")))


def test_resolve_server_auth_token_matches_shared_among_several_resources() -> None:
    """The shared server is found even when the account also holds unrelated resources."""
    other = _plex_resource(owned=True, machine_id="other-server")
    other.accessToken = "other-token"
    account = _plex_account(other, _plex_resource(owned=False, provides="server,player"))
    assert _resolve(account) == RESOURCE_TOKEN


def test_resolve_server_auth_token_plextv_error() -> None:
    """A plex.tv error while listing the resources fails instead of using the account token."""
    account = Mock()
    account.resources.side_effect = BadRequest("(401) unauthorized")
    with pytest.raises(PlexServerAccessError):
        _resolve(account)


def test_resolve_server_auth_token_plextv_unreachable() -> None:
    """plex.tv being unreachable surfaces as a connection error, not as the account token."""
    account = Mock()
    account.resources.side_effect = requests.exceptions.ConnectionError("plex.tv unreachable")
    with pytest.raises(requests.exceptions.ConnectionError):
        _resolve(account)


def test_resolve_server_auth_token_builds_account_when_not_supplied() -> None:
    """Without a pre-authenticated account one is built from the account token."""
    account = _plex_account(_plex_resource(owned=False))
    with patch(
        "music_assistant.providers.plex.helpers.MyPlexAccount", return_value=account
    ) as account_cls:
        result = resolve_server_auth_token(ACCOUNT_TOKEN, PLEX_URL, _plex_session())
    account_cls.assert_called_once_with(token=ACCOUNT_TOKEN)
    assert result == RESOURCE_TOKEN


@pytest.mark.parametrize("access_token", [None, ""])
def test_resolve_server_auth_token_shared_server_without_access_token(
    access_token: str | None,
) -> None:
    """A shared server without its own access token fails instead of using the account token."""
    resource = _plex_resource(owned=False)
    resource.accessToken = access_token
    with pytest.raises(PlexServerAccessError):
        _resolve(_plex_account(resource))


def test_resolve_server_auth_token_resource_without_provides() -> None:
    """A resource that does not advertise what it provides is skipped, not fatal."""
    resource = _plex_resource(owned=False, provides=None)
    account = _plex_account(resource, _plex_resource(owned=False, machine_id="other-server"))
    with pytest.raises(PlexServerAccessError):
        _resolve(account)


@pytest.mark.parametrize(
    "payload", [{}, {"MediaContainer": {}}, []], ids=["empty", "no-machine-id", "not-a-dict"]
)
def test_resolve_server_auth_token_server_without_identity(payload: Any) -> None:
    """A server that doesn't report its machine identifier can't be matched."""
    session = _plex_session()
    session.get.return_value.json.return_value = payload
    account = _plex_account(_plex_resource(owned=False))
    with pytest.raises(PlexServerAccessError):
        resolve_server_auth_token(ACCOUNT_TOKEN, PLEX_URL, session, myplex_account=account)
    account.resources.assert_not_called()


def test_resolve_server_auth_token_server_unreachable() -> None:
    """An unreachable server surfaces as a connection error."""
    session = _plex_session()
    session.get.side_effect = requests.exceptions.ConnectionError("unreachable")
    with pytest.raises(requests.exceptions.ConnectionError):
        resolve_server_auth_token(ACCOUNT_TOKEN, PLEX_URL, session, myplex_account=_plex_account())
