"""
Canned MCP prompts.

These prompts hand the LLM a small, opinionated playbook for common tasks
("find a song and play it on a specific speaker", "now playing summary",
"build a party playlist") so an LLM client can chain MCP tools without
re-deriving the workflow each time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import CONF_RES_PROMPTS

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


def register_prompts(mcp: Any, config: ProviderConfig) -> None:
    """Register canned prompts on the FastMCP root, gated by ``CONF_RES_PROMPTS``."""
    if not config.get_value(CONF_RES_PROMPTS):
        return

    @mcp.prompt(name="find_and_play")  # type: ignore[untyped-decorator, unused-ignore]
    def find_and_play(query: str = "", target_player: str = "") -> str:
        """Search and play media on a player."""
        target = target_player or "<the user's preferred player>"
        request = query or "<from the user message>"
        return (
            f"Find the best match for the user's request: '{request}'.\n"
            "Use library_search_tracks (and library_search_albums or "
            "library_search_artists if needed) to identify the right URI.\n"
            "If every search returns no results, the item is not available in "
            "the user's library or enabled providers — tell the user it could "
            "not be found and stop. Do not retry the same searches or call "
            "unrelated tools.\n"
            f"If '{target}' is not already a player_id, resolve it by calling "
            "players_list_players and fuzzy-matching the name.\n"
            "Then call playback_play_media with queue_id set to that "
            "player_id and the resolved URI.\n"
            "Finally, call queue_get_active_queue to confirm the new state "
            "and report it back. For positional inserts via queue_add_to_queue "
            "with index, read QueueBrief.next_insertable_index from "
            "queue_get_active_queue — not array position alone."
        )

    @mcp.prompt(name="curate_party_playlist")  # type: ignore[untyped-decorator, unused-ignore]
    def party_playlist(theme: str = "indie 2010s", length_minutes: int = 60) -> str:
        """Build a party playlist."""
        return (
            f"Curate a playlist of roughly {length_minutes} minutes around "
            f"the theme: '{theme}'.\n"
            "Use library_search_tracks repeatedly with varied sub-queries "
            "(genres, eras, similar artists) and metadata_recommendations "
            "to seed candidates.\n"
            "Pick tracks the user would dance to.\n"
            "Then call playlists_create_playlist with a descriptive name, "
            "and playlists_add_tracks to fill it.\n"
            "Report the playlist URI when done."
        )

    @mcp.prompt(name="now_playing_summary")  # type: ignore[untyped-decorator, unused-ignore]
    def now_playing(player_id: str = "") -> str:
        """Summarise what's currently playing on a player (or all players)."""
        if player_id:
            return (
                f"Use queue_get_active_queue with player_id='{player_id}' "
                "to fetch the current queue.\n"
                "Summarise the now-playing track (title, artist, album, "
                "time remaining) and the next two upcoming items in 3-4 "
                "sentences."
            )
        return (
            "List players via players_list_players (pass "
            "include_unavailable=True for offline devices, "
            "include_disabled=True for admin-disabled devices). "
            "For each player whose state is 'playing', fetch its active "
            "queue and summarise the now-playing track. Group by room "
            "when possible. A player whose state is 'synced' is playing "
            "as part of another group — its active queue belongs to the "
            "group's player_id, not its own."
        )
