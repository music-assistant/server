"""Unit tests for meta-tool catalog search scoring."""

from __future__ import annotations

from music_assistant.providers.fastmcp_server.meta_tools.catalog import (
    apply_intent_adjustments,
    detect_workflow,
    normalize_query_tokens,
    score_tool_match,
    tokenize_query,
)

# Realistic descriptions for the tools that compete on conversational queries.
_PAUSE_DESC = (
    "Pause playback on the given queue. Always pauses — unlike play_pause, this does not toggle."
)
_RESUME_DESC = (
    "Resume paused playback on the given queue. Always resumes — unlike "
    "play_pause, this does not toggle."
)
_PLAY_PAUSE_DESC = "Toggle play/pause on the given queue. Playing pauses, paused resumes."
_PLAY_MEDIA_DESC = (
    "Load and start playing an album, track, playlist, or radio station on a player queue."
)
_SEARCH_TRACKS_DESC = "Search for tracks by free-text query across all enabled music providers."
_PLAY_INDEX_DESC = "Start playing the item at the given position in the existing queue."
_LIST_PLAYERS_DESC = (
    "List players known to Music Assistant. Returns PlayerBrief items with "
    "player_id, name, state, powered and the currently playing item. state "
    "summarises usability — the normal playback states (idle / playing / "
    "paused / ...)."
)


def _best(query: str, candidates: dict[str, str]) -> tuple[str, int]:
    """Rank *candidates* for *query* the way ``search_tool_catalog`` does."""
    tokens = normalize_query_tokens(tokenize_query(query))
    ranked = sorted(
        (
            (name, apply_intent_adjustments(name, tokens, score_tool_match(name, desc, tokens)))
            for name, desc in candidates.items()
        ),
        key=lambda item: -item[1],
    )
    return ranked[0]


def test_tokenize_splits_spaces_and_underscores() -> None:
    """Spaces and underscores tokenize identically so phrases match tool names."""
    assert tokenize_query("library search albums") == ["library", "search", "albums"]
    assert tokenize_query("library_search_albums") == ["library", "search", "albums"]


def test_library_search_albums_matches_natural_phrase() -> None:
    """A natural-language phrase scores against the underscored tool name."""
    name = "library_search_albums"
    description = (
        "Search for albums by free-text query across all enabled music providers. "
        "Returns AlbumBrief items with uri, name, artists and year."
    )
    tokens = tokenize_query("library search albums artist")
    score = score_tool_match(name, description, tokens)
    assert score > 0


def test_library_search_albums_beats_tracks_for_album_query() -> None:
    """An album query ranks the albums tool above the tracks tool."""
    albums_desc = "Search for albums by free-text query across all enabled music providers."
    tracks_desc = "Search for tracks by free-text query across all enabled music providers."
    tokens = tokenize_query("library search albums")
    albums_score = score_tool_match("library_search_albums", albums_desc, tokens)
    tracks_score = score_tool_match("library_search_tracks", tracks_desc, tokens)
    assert albums_score > tracks_score


def test_library_search_phrase_matches_underscored_name() -> None:
    """A partial namespace phrase still matches the underscored tool name."""
    score = score_tool_match(
        "library_search_albums",
        "Search for albums by free-text query.",
        tokenize_query("library search"),
    )
    assert score > 0


def test_playback_does_not_match_library_query() -> None:
    """A library query must not match an unrelated playback tool."""
    score = score_tool_match(
        "playback_play_media",
        "Play one or more media URIs on the given player queue.",
        tokenize_query("library search albums"),
    )
    assert score == 0


def test_single_token_search_matches_search_tools() -> None:
    """A bare ``search`` token still matches a search tool."""
    score = score_tool_match(
        "library_search_albums",
        "Search for albums by free-text query.",
        tokenize_query("search"),
    )
    assert score > 0


def test_playback_play_prefers_play_media_over_play_pause() -> None:
    """A play query ranks play_media above the play/pause toggle."""
    media_desc = "Load and start playing media. uri: album, track, playlist."
    pause_desc = "Toggle play/pause on the given queue."
    tokens = tokenize_query("playback play")
    media = apply_intent_adjustments(
        "playback_play_media", tokens, score_tool_match("playback_play_media", media_desc, tokens)
    )
    pause = apply_intent_adjustments(
        "playback_play_pause", tokens, score_tool_match("playback_play_pause", pause_desc, tokens)
    )
    assert media > pause


def test_play_album_includes_workflow() -> None:
    """A play-album query returns the multi-step play workflow."""
    workflow = detect_workflow(tokenize_query("play album"))
    assert workflow is not None
    assert workflow["task"] == "play_media_on_player"
    assert workflow["steps"][-1]["tool"] == "playback_play_media"


def test_pause_only_query_has_no_play_workflow() -> None:
    """A pause query is not treated as a play request, so no workflow is returned."""
    assert detect_workflow(tokenize_query("playback pause")) is None


def test_ungroup_prefers_ungroup_player_over_group_player() -> None:
    """An ungroup query ranks the ungroup tool above the group tool."""
    group_desc = "Add a player to another player's sync group so both play in lockstep."
    ungroup_desc = "Remove a player from its sync group so it plays independently again."
    tokens = tokenize_query("ungroup player")
    group = apply_intent_adjustments(
        "players_group_player", tokens, score_tool_match("players_group_player", group_desc, tokens)
    )
    ungroup = apply_intent_adjustments(
        "players_ungroup_player",
        tokens,
        score_tool_match("players_ungroup_player", ungroup_desc, tokens),
    )
    assert ungroup > group


def test_pause_prefers_pause_over_play_pause() -> None:
    """An explicit pause query ranks the non-toggling pause tool above play_pause."""
    pause_desc = "Pause playback on the given queue. Always pauses — unlike play_pause, this does not toggle."
    toggle_desc = "Toggle play/pause on the given queue. Playing pauses, paused resumes."
    tokens = tokenize_query("playback pause")
    explicit = apply_intent_adjustments(
        "playback_pause", tokens, score_tool_match("playback_pause", pause_desc, tokens)
    )
    toggle = apply_intent_adjustments(
        "playback_play_pause", tokens, score_tool_match("playback_play_pause", toggle_desc, tokens)
    )
    assert explicit > toggle


def test_resume_prefers_resume_over_play_pause() -> None:
    """An explicit resume query ranks the non-toggling resume tool above play_pause."""
    resume_desc = "Resume paused playback on the given queue. Always resumes — unlike play_pause, this does not toggle."
    toggle_desc = "Toggle play/pause on the given queue. Playing pauses, paused resumes."
    tokens = tokenize_query("resume playback")
    explicit = apply_intent_adjustments(
        "playback_resume", tokens, score_tool_match("playback_resume", resume_desc, tokens)
    )
    toggle = apply_intent_adjustments(
        "playback_play_pause", tokens, score_tool_match("playback_play_pause", toggle_desc, tokens)
    )
    assert explicit > toggle


def test_normalize_drops_stopwords_and_applies_synonyms() -> None:
    """Filler words are stripped and synonyms rewritten to tool vocabulary."""
    assert normalize_query_tokens(tokenize_query("pause the music")) == ["pause"]
    assert normalize_query_tokens(tokenize_query("play this song")) == ["play", "track"]
    assert normalize_query_tokens(tokenize_query("play it on my speakers")) == ["play", "players"]


def test_normalize_keeps_generic_term_when_alone() -> None:
    """A bare generic term is kept so the query is not emptied entirely."""
    assert normalize_query_tokens(tokenize_query("music")) == ["music"]


def test_pause_the_music_prefers_pause() -> None:
    """The conversational 'pause the music' resolves to the explicit pause tool."""
    name, score = _best(
        "pause the music",
        {
            "playback_pause": _PAUSE_DESC,
            "playback_play_pause": _PLAY_PAUSE_DESC,
            "players_list_players": _LIST_PLAYERS_DESC,
        },
    )
    assert name == "playback_pause"
    assert score > 0


def test_resume_the_music_prefers_resume() -> None:
    """The conversational 'resume the music' resolves to the explicit resume tool."""
    name, score = _best(
        "resume the music",
        {
            "playback_resume": _RESUME_DESC,
            "playback_play_pause": _PLAY_PAUSE_DESC,
            "players_list_players": _LIST_PLAYERS_DESC,
        },
    )
    assert name == "playback_resume"
    assert score > 0


def test_play_this_song_prefers_play_media() -> None:
    """The conversational 'play this song' resolves to play_media, not search/toggle."""
    name, score = _best(
        "play this song",
        {
            "playback_play_media": _PLAY_MEDIA_DESC,
            "library_search_tracks": _SEARCH_TRACKS_DESC,
            "playback_play_pause": _PLAY_PAUSE_DESC,
            "playback_play_index": _PLAY_INDEX_DESC,
        },
    )
    assert name == "playback_play_media"
    assert score > 0


def test_description_only_match_below_half_coverage_is_dropped() -> None:
    """A desc-only hit that covers under half the query tokens does not match."""
    score = score_tool_match(
        "library_search_albums",
        "Search for albums by free-text query. Mentions track once.",
        ["zzz", "yyy", "track"],
    )
    assert score == 0


def test_description_only_match_at_half_coverage_survives() -> None:
    """A desc-only hit covering half the query tokens still counts."""
    score = score_tool_match(
        "library_search_albums",
        "Search for albums by free-text query. Mentions track once.",
        ["zzz", "track"],
    )
    assert score > 0
