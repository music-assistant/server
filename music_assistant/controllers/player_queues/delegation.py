"""
Playback delegation for the Player Queues controller.

A queue can be owned by an external session instead of Music Assistant: while the
queue's current item is a live AudioSource declaring ``queue_capabilities``, queue
commands are forwarded to the owning plugin (``_get_delegated_source`` is the single
gate the command handlers use). The counterpart at enqueue time is the playback
redirect: a music provider may hand a play request to such a session
(``MusicProvider.get_playback_delegate`` / ``play_on_delegate``, e.g. Spotify content
riding a Spotify Connect session), in which case the MA queue plays the session's
AudioSource rather than the individual items. Owns no per-queue state; it is mixed
into the controller and reads the controller's ``PlayerQueueData`` records.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ProviderFeature, QueueOption
from music_assistant_models.media_items import AudioSource, BrowseFolder

from music_assistant.controllers.player_queues.base import _PlayerQueuesBase
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import (
        MediaItemType,
        PlayableMediaItemType,
        SourceQueueCapabilities,
    )


class PlaybackDelegationMixin(_PlayerQueuesBase):
    """Identify session-owned queues and redirect play requests into external sessions."""

    def _get_delegated_source(
        self, queue_id: str
    ) -> tuple[AudioSource, SourceQueueCapabilities, PluginProvider] | None:
        """
        Return the AudioSource owning the queue's commands, its capabilities and owning plugin.

        While the queue's current item is an AudioSource declaring ``queue_capabilities``,
        the external session owns the queue: commands are forwarded to the owning plugin
        and the mirrored options event updates the queue state afterwards. This matches
        the player-layer proxy: no playback-state gate, because a plugin may map a paused
        session onto a stopped player (Spotify Connect does) — a genuinely dead session
        is the owning plugin's call, surfaced as its localized not-active error. Returns
        None — queue commands then apply to the MA queue as usual — when the current item
        is not such an AudioSource (a transport-only source keeps queue commands with MA)
        or when the owning plugin provider is no longer available.

        :param queue_id: The queue to inspect.
        """
        if (queue_data := self._queue_data.get(queue_id)) is None:
            return None
        current_item = queue_data.queue.current_item
        if current_item is None or current_item.media_item is None:
            return None
        media_item = current_item.media_item
        if not isinstance(media_item, AudioSource):
            return None
        caps = media_item.queue_capabilities
        if caps is None:
            return None
        provider = self.mass.get_provider(media_item.provider)
        if not isinstance(provider, PluginProvider):
            return None
        # Belt-and-suspenders: a queue item carrying media_type=AUDIO_SOURCE
        # can only have come from a provider that declared the feature, but
        # a feature flag flipped off at runtime (provider reload, config
        # change) would leave on_source_control raising NotImplementedError.
        if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
            return None
        return media_item, caps, provider

    async def _apply_playback_delegation(
        self,
        queue_id: str,
        media_items: list[MediaItemType],
        option: QueueOption | None,
        context: MediaItemType | BrowseFolder | None,
        start_item: PlayableMediaItemType | str | None,
    ) -> list[MediaItemType]:
        """
        Redirect the play request into an external session when a provider delegates it.

        Returns the media items the normal enqueue should continue with: the delegate
        AudioSource when the whole batch was redirected into one session, an empty list
        when the request was handled entirely session-side (enqueue onto the already
        active session), or the (possibly reduced) original items.

        :param queue_id: The queue the play request targets.
        :param media_items: The resolved playable items of the play request.
        :param option: The (settled) enqueue option of the play request.
        :param context: The single original container the request expanded from, if any.
        :param start_item: Optional item (or uri) within the context to start at.
        """
        if not media_items or option is None:
            return media_items
        add_family = option in (QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT)
        delegate_cache: dict[str, tuple[AudioSource, MusicProvider] | None] = {}
        item_delegates = [
            await self._get_item_delegate(queue_id, item, add_family, delegate_cache)
            for item in media_items
        ]
        if all(delegated is not None for delegated in item_delegates) and (
            len({delegated[0].uri for delegated in item_delegates if delegated is not None}) == 1
        ):
            delegate, provider = cast(
                "tuple[AudioSource, MusicProvider]",
                item_delegates[0],
            )
            self.logger.info(
                "Redirecting playback of %d item(s) into external session %s",
                len(media_items),
                delegate.name,
            )
            await provider.play_on_delegate(
                delegate,
                cast("list[PlayableMediaItemType]", media_items),
                option,
                queue_id,
                context=None if isinstance(context, BrowseFolder) else context,
                start_item=start_item,
            )
            if add_family:
                # the items live in the session's own queue; nothing to load into the
                # MA queue (the session's AudioSource is already its current item)
                return []
            # the session plays the items; the MA queue plays the session
            return [delegate]
        # The batch cannot ride one session. Items with a normal playback path (any
        # available mapping on a provider that streams itself) play through the
        # regular path; in a mixed batch the delegate-only items are dropped. A batch
        # where NOTHING plays normally is kept whole, so the delegate-only provider's
        # own actionable error surfaces at stream time instead of a silent drop.
        plays_normally = [self._has_normal_playback_path(item) for item in media_items]
        if all(plays_normally) or not any(plays_normally):
            return media_items
        dropped = [
            item for item, normal in zip(media_items, plays_normally, strict=True) if not normal
        ]
        self.logger.warning(
            "Skipping %d item(s) (%s): mixing session-redirected items with other content "
            "in one queue is not supported yet - play them separately",
            len(dropped),
            ", ".join(item.name for item in dropped[:5]),
        )
        return [item for item, normal in zip(media_items, plays_normally, strict=True) if normal]

    def _has_normal_playback_path(self, item: MediaItemType) -> bool:
        """Return whether any of the item's mappings plays through the regular streaming path."""
        for mapping in item.provider_mappings:
            if not mapping.available:
                continue
            provider = self.mass.get_provider(mapping.provider_instance)
            if isinstance(provider, MusicProvider) and not provider.playback_requires_delegate:
                return True
        return False

    async def _get_item_delegate(
        self,
        queue_id: str,
        item: MediaItemType,
        add_family: bool,
        delegate_cache: dict[str, tuple[AudioSource, MusicProvider] | None],
    ) -> tuple[AudioSource, MusicProvider] | None:
        """
        Return the external session (and owning music provider) that should play the item.

        :param queue_id: The queue the play request targets.
        :param item: The resolved media item to find a playback delegate for.
        :param add_family: Whether the request enqueues onto the session (ADD/NEXT family)
            rather than starting playback, which gates on the enqueueable media types.
        :param delegate_cache: Per-request cache of each provider's delegate answer.
        """
        # stable order: provider_mappings is a set, and every item of the batch must
        # pick the same delegate when its mappings offer more than one (e.g. a library
        # item mapped to two accounts of the same service)
        mappings = sorted(
            item.provider_mappings,
            key=lambda mapping: (mapping.provider_domain, mapping.provider_instance),
        )
        for mapping in mappings:
            if not mapping.available:
                continue
            if mapping.provider_instance not in delegate_cache:
                provider = self.mass.get_provider(mapping.provider_instance)
                if not isinstance(provider, MusicProvider):
                    delegate_cache[mapping.provider_instance] = None
                    continue
                try:
                    delegate = await provider.get_playback_delegate(queue_id)
                except Exception as err:
                    self.logger.warning(
                        "Error resolving playback delegate from %s: %s", provider.name, err
                    )
                    delegate = None
                delegate_cache[mapping.provider_instance] = (
                    (delegate, provider) if delegate is not None else None
                )
            if (delegated := delegate_cache[mapping.provider_instance]) is None:
                continue
            caps = delegated[0].queue_capabilities
            if caps is None:
                continue
            allowed = caps.enqueueable_media_types if add_family else caps.playable_media_types
            if item.media_type in allowed:
                return delegated
        return None
