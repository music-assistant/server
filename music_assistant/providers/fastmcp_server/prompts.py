"""Canned discovery-first MCP prompts for common Music Assistant workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import CONF_RES_PROMPTS

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


def register_prompts(mcp: Any, config: ProviderConfig) -> None:
    """Register canned prompts on the FastMCP root when prompt resources are enabled."""
    if not config.get_value(CONF_RES_PROMPTS):
        return

    @mcp.prompt(name="find_and_play")  # type: ignore[untyped-decorator, unused-ignore]
    def find_and_play(query: str = "", target_player: str = "") -> str:
        """Search for media, play it, and verify the active queue."""
        target = target_player or "<the user's preferred player>"
        request = query or "<from the user message>"
        return (
            f"Find the best match for '{request}' and play it on '{target}'.\n"
            "Use search_tools for media search, players, play media, and active queue. "
            "For each selected canonical ma_api:* command, call get_tool_schema before call_tool.\n"
            "The likely native commands are music/search, players/all, "
            "player_queues/play_media, and player_queues/get_active_queue, but use the "
            "canonical names returned by discovery. If search has no results, report that "
            "the item is unavailable and stop instead of retrying unrelated commands. "
            "After playback, read the active queue and report the confirmed state."
        )

    @mcp.prompt(name="curate_party_playlist")  # type: ignore[untyped-decorator, unused-ignore]
    def party_playlist(theme: str = "indie 2010s", length_minutes: int = 60) -> str:
        """Build a themed party playlist using discovered native commands."""
        return (
            f"Curate roughly {length_minutes} minutes around '{theme}'.\n"
            "Use search_tools to discover media search, recommendations, playlist creation, "
            "and playlist track-add commands. For each selected canonical ma_api:* command, "
            "call get_tool_schema before call_tool. Likely native command suffixes include "
            "music/search, music/recommendations, music/playlists/create_playlist, and "
            "music/playlists/add_playlist_tracks; trust discovery for the exact names and "
            "arguments. Report the resulting playlist URI."
        )

    @mcp.prompt(name="now_playing_summary")  # type: ignore[untyped-decorator, unused-ignore]
    def now_playing(player_id: str = "") -> str:
        """Summarize current playback for one player or all active players."""
        scope = (
            f"the active queue for player_id '{player_id}'"
            if player_id
            else "players/all, followed by the active queue for each playing player"
        )
        return (
            f"Summarize {scope}.\n"
            "Use search_tools to discover player and active-queue commands. For each selected "
            "canonical ma_api:* command, call get_tool_schema before call_tool. The likely "
            "native commands are players/all and player_queues/get_active_queue. Summarize "
            "the now-playing item and next two queue items in 3-4 sentences; treat synced "
            "players as members of their leader's queue."
        )
