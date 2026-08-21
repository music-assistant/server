"""
DEMO/TEMPLATE Plugin Provider for Music Assistant.

This is an empty plugin provider with no actual implementation.
Its meant to get started developing a new plugin provider for Music Assistant.

Use it as a reference to discover what methods exists and what they should return.
Also it is good to look at existing plugin providers to get a better understanding.

In general, a plugin provider does not have any mandatory implementation details.
It provides additional functionality to Music Assistant and most often it will
interact with the existing core controllers and event logic. For example a Scrobble plugin.

If your plugin needs to communicate with external services or devices, you need to
use a dedicated (async) library for that. You can add these dependencies to the
manifest.json file in the requirements section,
which is a list of (versioned!) python modules (pip syntax) that should be installed
when the provider is selected by the user.

To add a new plugin provider to Music Assistant, you need to create a new folder
in the providers folder with the name of your provider (e.g. 'my_plugin_provider').
In that folder you should create (at least) a __init__.py file and a manifest.json file.

Optional is an icon.svg file that will be used as the icon for the provider in the UI,
but we also support that you specify a material design icon in the manifest.json file.

IMPORTANT NOTE:
We strongly recommend developing on either macOS or Linux and start your development
environment by running the setup.sh scripts in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.
See also our general DEVELOPMENT.md guide in the repository for more information.

"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    ContentType,
    EventType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioSource,
    Playlist,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.enums import RepeatMode, SourceControl
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import (
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        RecommendationFolder,
        Track,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# stable id for the single AudioSource this demo provider exposes;
# combined with the provider instance_id this forms the persistent browse/play uri
# (e.g. `<instance_id>://audio_source/main`) listed under "Live Inputs". AudioSource
# items are not favoritable / library-backed in MA core today.
AUDIO_SOURCE_ID = "main"

SUPPORTED_FEATURES = {
    # MANDATORY
    # this constant should contain a set of provider-level features
    # that your provider supports or an empty set if none.
    # see the ProviderFeature enum for all available features
    # at time of writing the only plugin-specific feature is the
    # 'AUDIO_SOURCE' feature which indicates that this provider can
    # provide a (single) audio source to Music Assistant, such as a live stream.
    # we add this feature here to demonstrate the concept.
    ProviderFeature.AUDIO_SOURCE
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    # an instance of the provider.
    return MyDemoPluginprovider(mass, manifest, config, SUPPORTED_FEATURES)


class MyDemoPluginprovider(PluginProvider):
    """
    Example/demo Plugin provider.

    Note that this is always subclassed from PluginProvider,
    which in turn is a subclass of the generic Provider model.

    The base implementation already takes care of some convenience methods,
    such as the mass object and the logger. Take a look at the base class
    for more information on what is available.

    Just like with any other subclass, make sure that if you override
    any of the default methods (such as __init__), you call the super() method.
    In most cases its not needed to override any of the builtin methods and you only
    implement the abc methods with your actual implementation.
    """

    # tracks which queue currently owns the exclusive AudioSource. Set in
    # on_source_selected (NOT in get_stream_details — that path also runs from
    # queue preload, where claiming would block a later cross-queue handoff).
    _in_use_by_queue: str | None = None
    # tracks the active stream_session_id for the current stream request.
    # Paired with _in_use_by_queue: same-queue reconnects refresh this token
    # without changing _in_use_by_queue, so stream loops and generator
    # finallys must guard their lock release on both still matching.
    _active_session_id: str | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for this (existing) provider instance.

        Return an empty tuple when the provider has no options. Interactive setup
        input (if any) is collected by a ``setup_flow.py`` module; one-shot buttons
        are declared here as ``ConfigEntryType.ACTION`` entries and handled in
        ``handle_config_action``.
        """
        return ()

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called after the provider has been fully loaded into Music Assistant.
        # you can use this for instance to trigger custom (non-mdns) discovery of plugins
        # or any other logic that needs to run after the provider is fully loaded.

        # as reference we will subscribe here to an event on the MA eventbus
        # this is just an example and you can remove this if not needed.
        async def handle_event(event: MassEvent) -> None:
            if event.event == EventType.MEDIA_ITEM_PLAYED:
                # example implementation of handling a media item played event
                self.logger.info("Media item played event received: %s", event.data)

        self.mass.subscribe(handle_event, EventType.MEDIA_ITEM_PLAYED)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is unloaded from Music Assistant.
        # this means also when the provider is getting reloaded

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        # OPTIONAL
        # Will only be called if ProviderFeature.AUDIO_SOURCE is declared.
        #
        # Return one or more AudioSource MediaItems describing the live
        # inputs this plugin offers. AudioSources show up under the global
        # "Live Inputs" browse node and can be played on any player via
        # the standard play_media flow. Capability flags (can_play_pause,
        # can_seek, can_next_previous) drive which control buttons the UI
        # surfaces and which commands the player controller proxies through
        # to on_source_control.
        #
        # Most plugins expose a single source; for those, build it once in
        # __init__ and return the cached instance here. Plugins that expose
        # multiple sources (e.g. a hardware bridge with multiple inputs) can
        # rebuild the list at call time.
        return [
            AudioSource(
                item_id=AUDIO_SOURCE_ID,
                provider=self.instance_id,
                name=self.name,
                provider_mappings={
                    ProviderMapping(
                        item_id=AUDIO_SOURCE_ID,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        audio_format=AudioFormat(
                            content_type=ContentType.PCM_S16LE,
                            sample_rate=44100,
                            bit_depth=16,
                            channels=2,
                        ),
                    )
                },
                can_play_pause=False,
                can_seek=False,
                can_next_previous=False,
                exclusive=True,
                allow_external_trigger=False,
            )
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return StreamDetails for streaming an item owned by this plugin."""
        # OPTIONAL
        # Called for any playable item this plugin exposes; media_type tells you
        # which kind. This demo only has an AudioSource.
        #
        # MUST be side-effect-free — no exclusivity claim, no busy raise.
        # MA calls this from both the streaming path AND from queue preload, so
        # mutating provider state here would let a preload accidentally claim
        # the source and block a subsequent cross-queue handoff. Ownership is
        # claimed in on_source_selected (which fires only on the actual stream
        # request, not on preload) — see the example below.
        #
        # Return a StreamDetails with stream_type=CUSTOM when audio comes from
        # an async generator (get_audio_stream below), or stream_type=NAMED_PIPE
        # plus a path when audio comes from a named pipe / file. stream_metadata
        # carries the initial track info; update it at runtime via
        # mass.streams.update_stream_metadata(queue_id, ...) — same mechanism
        # ICY radio metadata uses.
        #
        # Silence-during-pause contract:
        # - stream_type=CUSTOM: the server wraps your audio generator with a
        #   silence-keepalive, so you can just stop yielding bytes while the
        #   upstream device is paused. The wrapper inserts silence frames at
        #   the declared PCM format and the player stays connected.
        # - stream_type=NAMED_PIPE: the underlying process MUST keep writing
        #   silence to the pipe during pause (most popular binaries like
        #   shairport-sync and librespot do this in passthrough mode). If
        #   the producer actually stops writing, ffmpeg will block and the
        #   player will eventually disconnect.
        if item_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(title=self.name),
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield raw audio bytes for the given streamdetails."""
        # OPTIONAL
        # Will only be called when get_stream_details returned
        # stream_type=StreamType.CUSTOM. Yield bytes in the PCM format declared
        # by streamdetails.audio_format. Release any per-stream resources in a
        # try/finally — the consumer closes the generator when playback ends.
        #
        # Lock release pattern: snapshot BOTH the queue id AND the active
        # session id at stream start, then guard the finally release on both
        # still matching. A queue_id-only guard is unsafe for same-queue
        # reconnects: when the player drops + reopens the same URL,
        # on_source_selected fires again with a fresh stream_session_id (but
        # the same queue id), and the prior generator's teardown would
        # otherwise clear the lock that now belongs to the new session.
        consumer_queue = self._in_use_by_queue
        captured_session_id = self._active_session_id
        # 100ms of silence at 44.1kHz/16bit/stereo PCM — matches the audio_format
        # declared in get_stream_details. Replace with your actual byte source.
        pcm_chunk = b"\x00" * (44100 * 2 * 2 // 10)
        try:
            for _ in range(100):
                yield pcm_chunk
        finally:
            if (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                self._in_use_by_queue = None

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | bool | RepeatMode | None = None,
    ) -> None:
        """Handle a playback control command for the active AudioSource."""
        # OPTIONAL
        # Called when the AudioSource is the active queue item and the user
        # invokes a control whose capability flag (can_play_pause, can_seek,
        # can_next_previous) is True. value carries SEEK position in seconds
        # and is unused for other actions.
        # Plugins that advertise no controls do not need to override this.
        # Volume sync (e.g. mirroring the upstream app's volume slider) lives
        # on the separate on_volume_change hook below.
        raise NotImplementedError

    async def on_volume_change(self, source_id: str, volume: int) -> None:
        """React to a volume change on the player streaming this AudioSource."""
        # OPTIONAL
        # Implement when the plugin wants to sync the upstream device's volume
        # display with MA (e.g. Spotify Connect mirroring the Spotify app slider).
        # Fired only on the direct queue owner — group volume changes fire once
        # at the group level, not per child.

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """React to an AudioSource being selected for playback on a player."""
        # OPTIONAL — fires only on the actual stream request (not on queue
        # preload). This is the single point where exclusive AudioSources
        # claim ownership: doing it here (instead of in get_stream_details)
        # keeps the preload path side-effect-free so a player-switch handoff
        # is not blocked at queue prep time.
        #
        # Plugins MUST store stream_session_id so the matching
        # on_source_unselected callback can reject stale teardowns from
        # superseded same-queue requests. Typical pattern for exclusive
        # sources:
        #
        #     if self._active_player_id and self._active_player_id != player_id:
        #         await self.mass.players.cmd_stop(self._active_player_id)
        #     self._in_use_by_queue = queue_id  # claim (overwrites prior queue's)
        #     self._active_session_id = stream_session_id
        #     self._active_player_id = player_id
        #
        # If allow_player_switch is False and the requesting player is not the
        # configured target, redirect via mass.player_queues.play_media(target,
        # uri) and then RAISE so the original (disallowed) request does not
        # continue into get_stream_details and stream to the wrong player.

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """React to MA tearing down this AudioSource's stream from a queue."""
        # OPTIONAL — fires in the queue-item stream handler's finally block, so
        # it runs regardless of how streaming ended (normal completion, client
        # disconnect, exception). Lets NAMED_PIPE plugins release ownership
        # without depending on an external session event.
        #
        # Guard with a stream_session_id match — NOT just a queue_id match —
        # so a stale callback from a superseded same-queue request (player
        # drops + reopens the same URL before the prior finally fires) cannot
        # clear the live claim of the new stream:
        #
        #     if self._active_session_id != stream_session_id:
        #         return
        #     self._active_session_id = None
        #     if self._in_use_by_queue == queue_id:
        #         self._in_use_by_queue = None

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform a search against this plugin's content."""
        # OPTIONAL
        # Will only be called if ProviderFeature.SEARCH is declared.
        # Return a SearchResults with items belonging to this plugin (typically
        # Playlist items whose provider_mappings point back to this plugin).
        # The global search controller interleaves the result with results
        # from other providers.
        return SearchResults()

    async def get_similar_tracks(self, track: Track, limit: int = 25) -> list[Track]:
        """Retrieve a list of similar tracks for the given track."""
        # OPTIONAL
        # Will only be called if ProviderFeature.SIMILAR_TRACKS is declared.
        # Results should be Track objects with provider_mappings pointing to
        # existing music providers so MA's playback path resolves normally.
        return []

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this plugin's available recommendation rows, without items."""
        # OPTIONAL
        # Will only be called if ProviderFeature.RECOMMENDATIONS is declared.
        # Return the recommendation rows this plugin offers as
        # RecommendationFolder objects WITHOUT items (leave the default empty
        # items list). This must be fast: hardcoded or locally cached row
        # descriptors only, no backend calls — MA uses it to instantly render
        # the row shells and the recommendations settings page.
        # Keep each row's item_id stable across calls and restarts: the user's
        # per-row preferences (enabled/hidden, order) are keyed on it.
        return []

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Get the items for a single recommendation row."""
        # OPTIONAL
        # Will only be called if ProviderFeature.RECOMMENDATIONS is declared.
        # Called separately (possibly in parallel) for each enabled row
        # returned by get_recommendations; any (slow) backend fetching belongs
        # here, not in get_recommendations. Rows may contain Playlist items
        # pointing back to this plugin via their provider_mappings; users can
        # add such a Playlist to their library through the standard
        # add-to-library flow.
        # Return an empty UniqueList for an unknown item_id.
        return UniqueList()

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this plugin's contents."""
        # OPTIONAL
        # Will only be called if ProviderFeature.BROWSE is declared.
        # Plugins surfacing playlists should yield Playlist items here. Each
        # Playlist MUST have at least one ProviderMapping pointing back to
        # this plugin's instance_id/domain so MA can resolve get_playlist /
        # get_playlist_tracks against the correct provider when the user adds
        # it to their library.
        return []

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return details of a single playlist owned by this plugin."""
        # OPTIONAL
        # Implement when surfacing playlists via browse / recommendations so
        # MA can refresh metadata once a user adds it to their library.
        return Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name="Example Playlist",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return a page of tracks for a playlist owned by this plugin."""
        # OPTIONAL
        # Implement when surfacing playlists. Tracks SHOULD carry
        # ProviderMappings pointing to real music providers so MA's playback
        # path resolves normally.
        return []
