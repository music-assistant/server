"""Search the MCP tool catalog for meta-tool discovery."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .constants import META_TOOL_NAMES
from .schema import build_tool_schema

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_TOKEN_SPLIT = re.compile(r"[\s_\-./]+")

# Filler words that carry no tool-selection signal. Dropped from queries so a
# natural phrase ("pause the music") matches as well as terse keywords
# ("pause"). Kept deliberately conservative — articles, prepositions, pronouns
# and conjunctions only, never verbs that might name an action.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "to",
        "for",
        "of",
        "on",
        "in",
        "into",
        "my",
        "me",
        "i",
        "it",
        "its",
        "is",
        "are",
        "be",
        "and",
        "or",
        "with",
        "please",
        "you",
        "your",
        "up",
        "some",
        "any",
        "all",
        "from",
        "at",
        "by",
        "as",
        "so",
        "then",
        "now",
    }
)

# Domain synonyms — agents phrase requests one way, the tools name things
# another ("song" → "track", "speaker" → "player").
_SYNONYMS = {
    "song": "track",
    "songs": "track",
    "tune": "track",
    "tunes": "track",
    "speaker": "player",
    "speakers": "players",
    "device": "player",
    "devices": "players",
}

# Generic terms dropped only when more specific tokens remain, so "music" does
# not force-match the literal "Music Assistant" in unrelated descriptions while
# a bare "music" query still searches.
_GENERIC_TERMS = frozenset({"music", "audio"})

_BROAD_QUERY_HINT = (
    "Query matched many tools. Narrow with namespace + action, e.g. "
    "'library albums', 'library tracks', 'playback play', 'players list'."
)

_EMPTY_QUERY_HINT = (
    "No matches. Use natural phrases with namespace + action, e.g. "
    "'library search albums', 'playback play media', 'players list', 'volume set'."
)

# Intent tuning — avoids play_pause tying playback_play_media on "playback play".
_EXPLICIT_PAUSE_TOKENS = frozenset({"pause"})
_EXPLICIT_RESUME_TOKENS = frozenset({"resume"})
_PLAY_CONTENT_TOKENS = frozenset(
    {"album", "albums", "track", "tracks", "media", "uri", "playlist", "radio", "artist", "artists"}
)
_PLAY_START_TOKENS = frozenset({"play", "start", "queue"})

_PLAY_MEDIA_WORKFLOW: dict[str, Any] = {
    "task": "play_media_on_player",
    "summary": "Play an album, track, or playlist on a speaker (multi-step).",
    "steps": [
        {
            "tool": "library_search_albums",
            "purpose": "Find the album URI (skip if you already have a URI).",
            "arguments": {"query": "<album or artist name>", "limit": 10},
        },
        {
            "tool": "library_search_tracks",
            "purpose": "Alternative: find a track URI when not playing a full album.",
            "arguments": {"query": "<track name>", "limit": 10},
        },
        {
            "tool": "players_list_players",
            "purpose": "List players; use player_id as queue_id (fuzzy-match names like 'office quads' → 'BRAVIA Theatre Quad').",
            "arguments": {},
        },
        {
            "tool": "playback_play_media",
            "purpose": "Start playback — queue_id is the player_id from the previous step.",
            "arguments": {"queue_id": "<player_id>", "uri": "<uri from search>"},
        },
    ],
}


def tokenize_query(text: str) -> list[str]:
    """Split a query into lowercase tokens (min length 2)."""
    return [t for t in _TOKEN_SPLIT.split(text.casefold()) if len(t) >= 2]


def normalize_query_tokens(tokens: list[str]) -> list[str]:
    """
    Clean raw query tokens for matching: drop filler words and apply synonyms.

    Stopwords are removed, known synonyms are rewritten to the vocabulary the
    tools actually use, and generic terms (``music``/``audio``) are dropped only
    when more specific tokens remain. Falls back to the un-stripped tokens when
    normalisation would otherwise empty the query (e.g. a bare ``music``).
    """
    cleaned = [_SYNONYMS.get(t, t) for t in tokens if t not in _STOPWORDS]
    specific = [t for t in cleaned if t not in _GENERIC_TERMS]
    return specific or cleaned


def score_tool_match(name: str, description: str, query_tokens: list[str]) -> int:
    """
    Score how well a tool matches *query_tokens*.

    Tool names use ``namespace_action`` segments; queries often use spaces
    (``library search albums``). Underscores are treated as word boundaries.
    Tokens that match the name score highest, then the description; unmatched
    tokens do not disqualify the tool, but a tool that only matches the
    description must cover at least half the query tokens to count.
    """
    if not query_tokens:
        return 0

    name_tokens = tokenize_query(name.replace("_", " "))
    desc_tokens = tokenize_query(description)
    name_spaced = name.replace("_", " ").casefold()
    desc_cf = description.casefold()

    score = 0
    matched = 0
    name_hits = 0
    exact_name_hits = 0
    for token in query_tokens:
        token_score = 0
        in_name = False
        if token in name_tokens:
            token_score = 30
            in_name = True
            exact_name_hits += 1
        elif any(
            seg.startswith(token) or token.startswith(seg) for seg in name_tokens if len(seg) >= 3
        ):
            token_score = 20
            in_name = True
        elif token in name_spaced:
            token_score = 15
            in_name = True
        elif token in desc_cf:
            token_score = 8
        elif any(
            word.startswith(token) or token.startswith(word)
            for word in desc_tokens
            if len(word) >= 3
        ):
            token_score = 5

        if token_score:
            matched += 1
            name_hits += int(in_name)
            score += token_score

    if score == 0:
        return 0

    # Soft match: unmatched filler/synonym tokens no longer disqualify a tool,
    # but to avoid a single stray description hit dragging in noise we require
    # either a name match or at least half the query tokens to land somewhere.
    if name_hits == 0 and matched * 2 < len(query_tokens):
        return 0

    # Prefer tools whose name segments cover more query tokens exactly.
    score += exact_name_hits * 5

    # Shorter names with same coverage are usually more specific (e.g. albums vs tracks).
    if exact_name_hits == len(query_tokens):
        score += max(0, 40 - len(name_tokens) * 3)

    return score


def apply_intent_adjustments(name: str, query_tokens: list[str], base_score: int) -> int:
    """Nudge rankings for common agent intents (play vs pause, list vs get)."""
    if base_score == 0:
        return 0

    score = base_score
    has_pause_intent = any(t in _EXPLICIT_PAUSE_TOKENS for t in query_tokens)
    has_resume_intent = any(t in _EXPLICIT_RESUME_TOKENS for t in query_tokens)
    has_play_content = any(t in _PLAY_CONTENT_TOKENS for t in query_tokens)
    has_play_start = any(t in _PLAY_START_TOKENS for t in query_tokens)

    if name == "playback_play_media":
        if has_play_content:
            score += 25
        if has_play_start and not has_pause_intent:
            score += 15
    elif name == "playback_pause":
        if has_pause_intent:
            score += 25
    elif name == "playback_resume":
        if has_resume_intent:
            score += 25
    elif name == "playback_play_pause":
        if has_pause_intent or has_resume_intent:
            score -= 35
        elif has_play_start and has_play_content:
            score -= 40
        elif has_play_start and not has_pause_intent:
            score -= 15
    elif name == "players_list_players" and any(
        t in {"list", "players", "player"} for t in query_tokens
    ):
        score += 10
    elif name == "playback_play_index" and "index" not in query_tokens:
        score -= 25
    elif name == "players_get_player" and "list" in query_tokens:
        score -= 20
    elif name == "players_ungroup_player" and "ungroup" in query_tokens:
        score += 15
    elif name == "players_group_player" and "ungroup" in query_tokens:
        score -= 25

    return score


def detect_workflow(query_tokens: list[str]) -> dict[str, Any] | None:
    """Return a multi-step playbook when the query looks like a play request."""
    if not any(t in _PLAY_START_TOKENS or t == "playback" for t in query_tokens):
        return None
    has_play_content = any(t in _PLAY_CONTENT_TOKENS for t in query_tokens)
    has_pause_intent = any(t in _EXPLICIT_PAUSE_TOKENS for t in query_tokens)
    if has_pause_intent and not has_play_content:
        return None
    return _PLAY_MEDIA_WORKFLOW


def _pick_recommended(matches: list[dict[str, Any]]) -> str | None:
    if not matches:
        return None
    top = matches[0]
    if len(matches) == 1:
        return str(top["name"])
    top_score = int(top.get("score", 0))
    second_score = int(matches[1].get("score", 0))
    if top_score >= second_score * 1.5 or top_score >= second_score + 20:
        return str(top["name"])
    return None


async def search_tool_catalog(
    query: str,
    *,
    list_tools: Callable[..., Awaitable[Any]],
    get_tool: Callable[[str], Awaitable[Any]],
    is_tool_visible: Callable[[str], Awaitable[bool]],
    include_schema: bool = True,
    limit: int = 25,
) -> dict[str, Any]:
    """
    Return tools matching *query*, ranked by relevance.

    Matching tokenizes the query and tool name (``library_search_albums`` matches
    ``library search albums``). Results include a relevance ``score`` and an
    optional ``recommended`` tool when one match is clearly best.
    """
    query_tokens = normalize_query_tokens(tokenize_query(query.strip()))
    if not query_tokens:
        return {"query": query, "count": 0, "tools": [], "hint": _EMPTY_QUERY_HINT}

    all_tools = await list_tools(run_middleware=False)
    ranked: list[tuple[int, str, str]] = []

    for listed in all_tools:
        name = str(getattr(listed, "name", "") or "")
        if not name or name in META_TOOL_NAMES:
            continue
        if not await is_tool_visible(name):
            continue

        description = str(getattr(listed, "description", "") or "")
        score = apply_intent_adjustments(
            name, query_tokens, score_tool_match(name, description, query_tokens)
        )
        if score > 0:
            ranked.append((score, name, description))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    cap = max(1, min(limit, 100))

    matches: list[dict[str, Any]] = []
    for score, name, description in ranked[:cap]:
        entry: dict[str, Any] = {
            "name": name,
            "description": description,
            "score": score,
        }
        if include_schema:
            tool = await get_tool(name)
            if tool is not None:
                entry.update(build_tool_schema(tool))
        matches.append(entry)

    result: dict[str, Any] = {"query": query, "count": len(matches), "tools": matches}
    recommended = _pick_recommended(matches)
    if recommended:
        result["recommended"] = recommended
    workflow = detect_workflow(query_tokens)
    if workflow is not None:
        result["workflow"] = workflow
    if not matches:
        result["hint"] = _EMPTY_QUERY_HINT
    elif len(matches) >= 8 and len(query_tokens) == 1:
        result["hint"] = _BROAD_QUERY_HINT
    return result
