"""
Tests that playback only ever reaches the music sources the listening user may use.

Ownership and sharing live on the source itself, so which instance may serve a queue item is
derived from the queue's playback user. A source another member keeps to themselves is never a
candidate, the user's own sources are tried before the ones merely shared with them, and the
sibling-instance stand-in that lets one account serve another instance's mapping stays inside
that same set. A queue that never had a user reaches the sources shared with everyone only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    ProviderSharing,
    ProviderType,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

USER_ID = "listener"
OTHER_USER_ID = "housemate"
ITEM_ID = "track-1"

# the listener's own account, and a second account of the same catalog owned by someone else
OWN_TIDAL = "tidal--mine"
OTHER_TIDAL = "tidal--theirs"
# a source of another member, on a catalog the listener has no account for
OTHER_SPOTIFY = "spotify--theirs"
# sources nobody owns: one open to everyone, one only to members (so not to a guest)
EVERYONE_QOBUZ = "qobuz--everyone"
MEMBERS_DEEZER = "deezer--members"
# a plugin instance, which is no music source and carries no access record
PLUGIN_INSTANCE = "smart_playlist"


def _mapping(instance: str, content_type: ContentType = ContentType.MP3) -> ProviderMapping:
    """
    Build a provider mapping for the test track.

    :param instance: The provider instance the mapping points at.
    :param content_type: Drives the mapping's quality score, so a lossless type makes the
        mapping sort ahead of the others.
    """
    return ProviderMapping(
        item_id=ITEM_ID,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
        audio_format=AudioFormat(content_type=content_type),
    )


def _queue_item(*mappings: ProviderMapping) -> QueueItem:
    """Build a queue item whose track carries the given provider mappings."""
    media_item = Track(
        item_id=ITEM_ID,
        provider=mappings[0].provider_instance,
        name="Song",
        provider_mappings=set(mappings),
    )
    return QueueItem(
        queue_id="q1",
        queue_item_id="qi1",
        name="Song",
        duration=180,
        media_item=media_item,
    )


def _cached_details(instance: str) -> StreamDetails:
    """Build the stream details an earlier resolution left on a queue item."""
    return StreamDetails(
        provider=instance,
        item_id=ITEM_ID,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/cached.mp3",
        duration=180,
    )


def _provider(instance: str) -> MagicMock:
    """Build a loaded streaming music provider that resolves its own stream details."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance
    provider.domain = instance.split("--", maxsplit=1)[0]
    provider.type = ProviderType.MUSIC
    provider.available = True
    provider.is_streaming_provider = True
    provider.get_stream_details = AsyncMock(
        return_value=StreamDetails(
            provider=instance,
            item_id=ITEM_ID,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path="http://test.invalid/item.mp3",
            duration=180,
        )
    )
    return provider


def _audio(
    providers: dict[str, MagicMock],
    access: dict[str, ProviderAccess | None],
    user: User | None,
) -> StreamsAudio:
    """
    Build a StreamsAudio playing for the given user over the given music sources.

    :param providers: The loaded provider instances, by instance id.
    :param access: The access record of each configured music source; None means a household
        source. Every loaded instance needs an entry, as a user's sources are derived from
        the configs.
    :param user: The queue's playback user, or None for anonymous playback.
    """
    mass = MagicMock()
    mass.providers = list(providers.values())
    mass.get_provider.side_effect = lambda instance, **_kwargs: providers.get(instance)
    mass.player_queues.queue_data_or_none.return_value = MagicMock(
        userid=user.user_id if user else None
    )
    mass.webserver.auth.get_user = AsyncMock(return_value=user)
    set_music_source_access(mass, access)
    mass.streams.get_config_value.return_value = -17
    return StreamsAudio(mass)


def _user(user_id: str = USER_ID, role: str = UserRole.USER) -> User:
    """Build an enabled user."""
    return User(user_id=user_id, username=user_id, role=role)


def _private(owner: str) -> ProviderAccess:
    """Build the access record of a source its owner keeps to themselves."""
    return ProviderAccess(owner=owner, sharing=ProviderSharing.PRIVATE)


async def test_a_private_source_of_another_member_is_never_a_candidate() -> None:
    """Another member's private account never serves a play, whatever its quality."""
    own = _provider(OWN_TIDAL)
    theirs = _provider(OTHER_SPOTIFY)
    audio = _audio(
        {OWN_TIDAL: own, OTHER_SPOTIFY: theirs},
        {OWN_TIDAL: _private(USER_ID), OTHER_SPOTIFY: _private(OTHER_USER_ID)},
        _user(),
    )

    streamdetails = await audio.get_stream_details(
        # the blocked mapping sorts first, so only the exclusion keeps it out
        queue_item=_queue_item(_mapping(OTHER_SPOTIFY, ContentType.FLAC), _mapping(OWN_TIDAL))
    )

    assert streamdetails.provider == OWN_TIDAL
    theirs.get_stream_details.assert_not_awaited()


async def test_an_own_source_is_tried_before_a_shared_one() -> None:
    """The listener's own account plays the track, even at a lower quality."""
    own = _provider(OWN_TIDAL)
    shared = _provider(EVERYONE_QOBUZ)
    audio = _audio(
        {OWN_TIDAL: own, EVERYONE_QOBUZ: shared},
        {OWN_TIDAL: _private(USER_ID), EVERYONE_QOBUZ: None},
        _user(),
    )

    streamdetails = await audio.get_stream_details(
        queue_item=_queue_item(_mapping(EVERYONE_QOBUZ, ContentType.FLAC), _mapping(OWN_TIDAL))
    )

    assert streamdetails.provider == OWN_TIDAL
    shared.get_stream_details.assert_not_awaited()


async def test_sibling_widening_stays_inside_the_allowed_sources() -> None:
    """
    A mapping on someone else's account is served by the listener's own account of the catalog.

    The same item id works on every account of a streaming catalog, so the mapping is widened
    to sibling instances - but only to those the listener may actually use.
    """
    own = _provider(OWN_TIDAL)
    theirs = _provider(OTHER_TIDAL)
    third = _provider("tidal--guest")
    audio = _audio(
        {OWN_TIDAL: own, OTHER_TIDAL: theirs, "tidal--guest": third},
        {
            OWN_TIDAL: _private(USER_ID),
            OTHER_TIDAL: _private(OTHER_USER_ID),
            "tidal--guest": _private("guest"),
        },
        _user(),
    )

    streamdetails = await audio.get_stream_details(queue_item=_queue_item(_mapping(OTHER_TIDAL)))

    assert streamdetails.provider == OWN_TIDAL
    theirs.get_stream_details.assert_not_awaited()
    third.get_stream_details.assert_not_awaited()


async def test_anonymous_playback_only_reaches_sources_shared_with_everyone() -> None:
    """A queue that never had a user plays through the household's open sources only."""
    everyone = _provider(EVERYONE_QOBUZ)
    members = _provider(MEMBERS_DEEZER)
    audio = _audio(
        {EVERYONE_QOBUZ: everyone, MEMBERS_DEEZER: members},
        {
            EVERYONE_QOBUZ: ProviderAccess(sharing=ProviderSharing.EVERYONE),
            MEMBERS_DEEZER: ProviderAccess(sharing=ProviderSharing.MEMBERS),
        },
        None,
    )

    streamdetails = await audio.get_stream_details(
        queue_item=_queue_item(_mapping(MEMBERS_DEEZER, ContentType.FLAC), _mapping(EVERYONE_QOBUZ))
    )

    assert streamdetails.provider == EVERYONE_QOBUZ
    members.get_stream_details.assert_not_awaited()


async def test_a_plugin_mapping_stays_a_candidate_for_a_restricted_user() -> None:
    """A plugin is no music source, so its mapping plays for a user with restricted sources."""
    own = _provider(OWN_TIDAL)
    plugin = _provider(PLUGIN_INSTANCE)
    plugin.type = ProviderType.PLUGIN
    plugin.is_streaming_provider = False
    audio = _audio(
        {OWN_TIDAL: own, PLUGIN_INSTANCE: plugin},
        # the plugin is absent from the configured music sources on purpose
        {OWN_TIDAL: _private(USER_ID), OTHER_SPOTIFY: _private(OTHER_USER_ID)},
        _user(),
    )

    streamdetails = await audio.get_stream_details(
        queue_item=_queue_item(_mapping(PLUGIN_INSTANCE))
    )

    assert streamdetails.provider == PLUGIN_INSTANCE


async def test_cached_details_of_a_source_no_longer_allowed_are_dropped() -> None:
    """Details resolved while a source was still the listener's are not replayed once it is not."""
    own = _provider(OWN_TIDAL)
    theirs = _provider(OTHER_SPOTIFY)
    audio = _audio(
        {OWN_TIDAL: own, OTHER_SPOTIFY: theirs},
        {OWN_TIDAL: _private(USER_ID), OTHER_SPOTIFY: _private(OTHER_USER_ID)},
        _user(),
    )
    queue_item = _queue_item(_mapping(OWN_TIDAL))
    queue_item.streamdetails = _cached_details(OTHER_SPOTIFY)

    streamdetails = await audio.get_stream_details(queue_item=queue_item)

    assert streamdetails.provider == OWN_TIDAL
    theirs.get_stream_details.assert_not_awaited()


async def test_cached_details_of_a_plugin_instance_are_reused() -> None:
    """A plugin is no music source, so its cached details keep serving a restricted user."""
    plugin = _provider(PLUGIN_INSTANCE)
    plugin.type = ProviderType.PLUGIN
    plugin.is_streaming_provider = False
    audio = _audio(
        {PLUGIN_INSTANCE: plugin},
        # the plugin is absent from the configured music sources on purpose
        {OWN_TIDAL: _private(USER_ID), OTHER_SPOTIFY: _private(OTHER_USER_ID)},
        _user(),
    )
    queue_item = _queue_item(_mapping(PLUGIN_INSTANCE))
    cached = _cached_details(PLUGIN_INSTANCE)
    queue_item.streamdetails = cached

    streamdetails = await audio.get_stream_details(queue_item=queue_item)

    assert streamdetails is cached
    plugin.get_stream_details.assert_not_awaited()


async def test_a_track_only_on_a_blocked_source_reports_it_as_unavailable() -> None:
    """A track that lives on another member's account alone is reported, never streamed."""
    own = _provider(OWN_TIDAL)
    theirs = _provider(OTHER_SPOTIFY)
    audio = _audio(
        {OWN_TIDAL: own, OTHER_SPOTIFY: theirs},
        {OWN_TIDAL: _private(USER_ID), OTHER_SPOTIFY: _private(OTHER_USER_ID)},
        _user(),
    )

    with pytest.raises(MediaNotFoundError) as err:
        await audio.get_stream_details(queue_item=_queue_item(_mapping(OTHER_SPOTIFY)))

    assert err.value.translation_key == "media_not_available_for_user"
    theirs.get_stream_details.assert_not_awaited()
