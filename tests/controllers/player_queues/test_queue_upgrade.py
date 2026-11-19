"""Tests for the saved queues of an installation from before the player split, after the upgrade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import RepeatMode
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)
from music_assistant.controllers.player_queues.state import PlayerQueueData

# the real server with fake providers, which migrates the stored config at its boot
from tests.scenarios.conftest import scenario  # noqa: F401
from tests.scenarios.test_lifecycle import (
    DEN,
    PRE_SPLIT_DEN_TV,
    PRE_SPLIT_WEB_PLAYER,
    WEB_PLAYER,
)

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

BROWSER = "sendspin_web_3f2a"
WRAPPER = "upf3eba71c"


async def test_saved_queues_of_a_browser_and_a_wrapper_survive_the_upgrade(
    scenario: Scenario,  # noqa: F811
) -> None:
    """
    The saved queue of a browser player and of a universal player come back after the upgrade.

    The migration deletes the record of the browser player, as the user did not customise it:
    once it connects again, its queue has its items, position and settings, as does the queue of
    the universal player, which is a player of its own protocols from then on.
    """
    sendspin = await scenario.add_provider("sendspin")
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    sendspin.protocol(BROWSER, "Web Browser", session_only=True, **WEB_PLAYER)
    airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)
    scenario.set_present("airplay_den", True)
    scenario.set_present("cast_den", True)
    for queue_id in (BROWSER, WRAPPER):
        await _save_queue_before_the_split(scenario, queue_id)

    await scenario.seed_pre_split_config(PRE_SPLIT_DEN_TV | {BROWSER: PRE_SPLIT_WEB_PLAYER})
    await scenario.idle()

    assert BROWSER not in [config.player_id for config in await scenario.player_configs()]
    await scenario.register(scenario.protocol(BROWSER))
    await scenario.idle()

    for queue_id in (BROWSER, WRAPPER):
        queue = scenario.queue(queue_id)
        assert queue is not None
        items = scenario.mass.player_queues.items(queue_id)
        assert [item.queue_item_id for item in items] == [f"{queue_id}_1", f"{queue_id}_2"]
        assert (queue.current_index, queue.resume_pos) == (1, 37)
        assert (queue.repeat_mode, queue.shuffle_enabled) == (RepeatMode.ALL, True)
        assert scenario.mass.player_queues.queue_data(queue_id).crossfade_override is True


async def _save_queue_before_the_split(scene: Scenario, queue_id: str) -> None:
    """
    Store the saved queue of a player as a version from before the split stored it.

    Two tracks, the second one current and part way in, repeat and shuffle on and the
    crossfade pinned.

    :param scene: The running scenario.
    :param queue_id: The id of the queue, which is the id of its player.
    """
    items = []
    for index in (1, 2):
        track_id = f"{queue_id}_track_{index}"
        item = QueueItem(
            queue_id=queue_id, queue_item_id=f"{queue_id}_{index}", name=track_id, duration=180
        )
        item.media_item = Track(
            item_id=track_id,
            provider="test",
            name=track_id,
            duration=180,
            provider_mappings={
                ProviderMapping(item_id=track_id, provider_domain="test", provider_instance="test")
            },
        )
        items.append(item)
    saved = PlayerQueueData(
        queue=PlayerQueue(
            queue_id=queue_id,
            active=False,
            display_name=queue_id,
            available=False,
            items=len(items),
            current_index=1,
            resume_pos=37,
            repeat_mode=RepeatMode.ALL,
            shuffle_enabled=True,
        ),
        items=items,
        crossfade_override=True,
    )
    for category, data in (
        (CACHE_CATEGORY_PLAYER_QUEUE_STATE, saved.to_cache()),
        (CACHE_CATEGORY_PLAYER_QUEUE_ITEMS, saved.items_to_cache()),
    ):
        await scene.mass.cache.set(
            key=queue_id,
            data=data,
            provider=scene.mass.player_queues.domain,
            category=category,
            persistent=True,
        )
