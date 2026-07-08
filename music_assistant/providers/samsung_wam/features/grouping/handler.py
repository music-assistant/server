"""Grouping handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pywam.lib import api_call

from music_assistant.providers.samsung_wam.features.base import (
    WamPlayerFeatureBase,
    handle_pywam_errors,
    retry_command,
)

if TYPE_CHECKING:
    from music_assistant.providers.samsung_wam.player import WamPlayer


class GroupingHandler(WamPlayerFeatureBase):
    """Encapsulates all grouping commands sent to a speaker."""

    @retry_command()
    @handle_pywam_errors
    async def form_group(self, group_children: list[WamPlayer], group_name: str) -> None:
        """
        Instruct the speaker to form a group with specified members.

        :param group_children: A list of WamPlayer instances to join.
        :param group_name: The name to apply to the newly formed group.
        """
        child_players = [{"ip": p.ip_address, "mac": p.player_id} for p in group_children]

        group_command = api_call.set_multispk_group_mainspk(
            name=group_name,
            spknum=len(child_players) + 1,
            audiosourcemacaddr=self.player.player_id,
            audiosourcename=self.player.name,
            subspeakers=child_players,
        )
        await self.speaker.client.request(group_command)

    @retry_command()
    @handle_pywam_errors
    async def leave_group(self) -> None:
        """Instruct the speaker to leave its current group."""
        await self.speaker.client.request(api_call.set_ungroup())
