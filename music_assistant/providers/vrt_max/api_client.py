"""Async client for the VRT MAX GraphQL catalogue API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp

from music_assistant.helpers.throttle_retry import Throttler

from .constants import (
    _EPISODE_TILE_TYPES,
    _PAGE_SIZE,
    _PROGRAM_TILE_TYPES,
    _RESUMEPOINT_MARGIN,
    AGGREGATOR_CLIENT,
    AGGREGATOR_URL,
    FAVOURITES_PAGE,
    GRAPHQL_HEADERS,
    GRAPHQL_TIMEOUT,
    GRAPHQL_URL,
    REQUEST_RATE_LIMIT,
    RESUMEPOINTS_URL,
)
from .models import (
    VrtApiError,
    VrtChapter,
    VrtEpisode,
    VrtNotFoundError,
    VrtProgram,
    VrtProgramTile,
    VrtProgress,
    VrtResumeTarget,
    VrtRow,
    VrtSeason,
    VrtStreamInfo,
)
from .parsers import (
    _brand_display_name,
    _collect_presenters,
    _collect_seasons,
    _favourite_id,
    _first_broadcast_start,
    _first_meta,
    _first_node_type,
    _header_meta,
    _image_url,
    _parse_episode_tile,
    _parse_header,
    _parse_iso,
    _parse_program_tile,
    _playlist_component_id,
    _presenters_from_header,
    _search_list_id,
    _song_list_component_id,
)
from .queries import (
    _MUTATION_SET_FAVOURITE,
    _QUERY_COMPONENT,
    _QUERY_EPISODE,
    _QUERY_EPISODE_MENU,
    _QUERY_FAVOURITE_ACTION,
    _QUERY_FAVOURITES,
    _QUERY_LANDING,
    _QUERY_PLAYLIST_TAB,
    _QUERY_PROGRAM,
    _QUERY_RESUME,
    _QUERY_SEARCH,
    _QUERY_STREAM,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    from aiohttp import ClientSession


class VrtMaxClient:
    """
    Thin async client for the VRT MAX GraphQL catalogue API.

    Keeps all endpoint, query and parsing logic isolated from the MA provider
    glue so it can be patched in one place when VRT changes its API.
    """

    def __init__(self, session: ClientSession, logger: logging.Logger) -> None:
        """
        Initialize the client.

        :param session: Shared aiohttp session (use the MA http session).
        :param logger: Logger for diagnostics.
        """
        self._session = session
        self._logger = logger
        # Paced so that a burst of concurrent work (opening a programme fetches a
        # tracklist per episode) can never hammer VRT.
        self._throttler = Throttler(rate_limit=REQUEST_RATE_LIMIT, period=1.0)

    async def get_landing_rows(self, page_id: str) -> list[VrtRow]:
        """
        Return the tile rows of a landing (ThemePage) page.

        :param page_id: The landing page path, e.g. '/vrtmax/radio/'.
        """
        data = await self._graphql(_QUERY_LANDING, {"pageId": page_id})
        page = data.get("page") or {}
        rows: list[VrtRow] = []
        for comp in page.get("components") or []:
            if not isinstance(comp, dict) or comp.get("__typename") != "PaginatedTileList":
                continue
            component_id = comp.get("componentId")
            if not isinstance(component_id, str):
                continue
            rows.append(
                VrtRow(
                    title=comp.get("title") or "",
                    component_id=component_id,
                    tile_type=_first_node_type(comp.get("paginatedItems")),
                )
            )
        return rows

    async def search_podcast_programs(self, query: str, limit: int) -> list[VrtProgramTile]:
        """Search podcasts by keyword, returning program tiles."""
        tiles: list[VrtProgramTile] = []
        for node in await self._search_nodes("podcast-program", "listen", query, limit):
            if node.get("__typename") == "PodcastProgramTile":
                tile = _parse_program_tile(node)
                if tile:
                    tiles.append(tile)
        return tiles

    async def search_radio_episodes(self, query: str, limit: int) -> list[VrtEpisode]:
        """Search radio archives by keyword, returning matching episodes."""
        episodes: list[VrtEpisode] = []
        for node in await self._search_nodes("radio-episode", "radio-episode", query, limit):
            if node.get("__typename") == "RadioEpisodeTile":
                episode = _parse_episode_tile(node)
                if episode:
                    episodes.append(episode)
        return episodes

    async def iter_programs(self, component_id: str) -> AsyncGenerator[VrtProgramTile]:
        """
        Yield all program/podcast tiles of a component, following pagination.

        :param component_id: The base64 component id of a PaginatedTileList.
        """
        async for node in self._iter_component_nodes(component_id):
            if node.get("__typename") not in _PROGRAM_TILE_TYPES:
                continue
            tile = _parse_program_tile(node)
            if tile:
                yield tile

    async def get_program(self, page_id: str) -> VrtProgram:
        """
        Return a program/podcast page (title, description, artwork, listen-back seasons).

        :param page_id: The program/podcast page path.
        """
        data = await self._graphql(_QUERY_PROGRAM, {"pageId": page_id})
        page = data.get("page")
        if not isinstance(page, dict) or not page.get("__typename"):
            raise VrtNotFoundError(f"No program page for {page_id!r}")

        description, image_url = _parse_header(page.get("header"))
        seasons: list[VrtSeason] = []
        _collect_seasons(page.get("components"), seasons)
        publisher = _brand_display_name(page.get("brand"))
        presenters = _collect_presenters(page.get("components"))
        if not presenters:
            # Radio archive pages expose the presenter via the header meta breadcrumb
            # (mediatype / channel / presenter) instead of a PresentersList.
            presenters = _presenters_from_header(page.get("header"), publisher)

        return VrtProgram(
            page_id=page_id,
            title=page.get("title") or page_id,
            description=description,
            image_url=image_url,
            publisher=publisher,
            presenters=presenters,
            seasons=tuple(seasons),
        )

    async def iter_season_episodes(
        self, component_id: str, access_token: str | None = None
    ) -> AsyncGenerator[VrtEpisode]:
        """
        Yield all episodes of a single season/listen-back list, following pagination.

        :param component_id: The base64 component id of an episode PaginatedTileList.
        :param access_token: Optional user token; when given, each episode carries the
            user's played/resume progress.
        """
        async for node in self._iter_component_nodes(component_id, bearer=access_token):
            if node.get("__typename") not in _EPISODE_TILE_TYPES:
                continue
            episode = _parse_episode_tile(node)
            if episode:
                yield episode

    async def get_episode(self, page_id: str) -> VrtEpisode:
        """
        Return metadata for a single episode page.

        :param page_id: The episode page path.
        """
        data = await self._graphql(_QUERY_EPISODE, {"pageId": page_id})
        page = data.get("page")
        if not isinstance(page, dict) or not page.get("__typename"):
            raise VrtNotFoundError(f"No episode page for {page_id!r}")
        description, header_image = _parse_header(page.get("header"))
        date_label = _first_meta(_header_meta(page.get("header")))
        player = page.get("player")
        if not isinstance(player, dict):
            player = {}
        title = player.get("title") or page.get("title") or page_id
        image_url = _image_url(player.get("image")) or header_image
        return VrtEpisode(
            page_id=page_id,
            title=title,
            description=description,
            image_url=image_url,
            date_label=date_label,
        )

    async def get_stream_info(self, page_id: str) -> VrtStreamInfo:
        """
        Return the audio streamId and duration for an on-demand episode page.

        :param page_id: The episode page path.
        """
        data = await self._graphql(_QUERY_STREAM, {"pageId": page_id})
        page = data.get("page")
        player = page.get("player") if isinstance(page, dict) else None
        for mode in (player or {}).get("modes") or []:
            if not isinstance(mode, dict) or mode.get("__typename") != "AudioPlayerMode":
                continue
            stream_id = mode.get("streamId")
            if isinstance(stream_id, str) and stream_id:
                duration = mode.get("durationInSeconds")
                return VrtStreamInfo(
                    stream_id=stream_id,
                    duration=int(duration) if isinstance(duration, (int, float)) else 0,
                )
        raise VrtNotFoundError(f"No audio stream for {page_id!r}")

    async def resolve_ondemand_hls(self, stream_id: str, player_token: str) -> str:
        """
        Resolve an on-demand streamId to a DRM-free HLS manifest URL.

        :param stream_id: The `{pubId}${audId}` stream id from get_stream_info.
        :param player_token: An authenticated vrtPlayerToken.
        """
        url = f"{AGGREGATOR_URL}/media-items/{stream_id}"
        params = {"vrtPlayerToken": player_token, "client": AGGREGATOR_CLIENT}
        try:
            async with self._session.get(
                url,
                params=params,
                timeout=GRAPHQL_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise VrtApiError(f"Aggregator returned HTTP {resp.status}: {body[:200]}")
                body = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise VrtApiError(f"Aggregator request failed: {err}") from err
        hls_urls: list[str] = []
        for target in body.get("targetUrls") or []:
            if not isinstance(target, dict) or target.get("type") != "hls":
                continue
            target_url = target.get("url")
            if isinstance(target_url, str) and target_url:
                hls_urls.append(target_url)
        # Only the DRM-free rendition is playable. Falling back to another target would
        # hand back a manifest that needs a decryption key we neither hold nor are
        # entitled to, so playback would fail anyway - with a far less obvious reason.
        for hls_url in hls_urls:
            if "_nodrm_" in hls_url:
                return hls_url
        raise VrtNotFoundError(f"No DRM-free HLS stream for {stream_id!r}")

    async def get_episode_chapters(self, page_id: str) -> list[VrtChapter]:
        """
        Return the episode's tracklist (played songs) as chapters.

        The playlist is discovered from the episode page's `menu` (a
        ContainerNavigationItem wrapping the song list); offsets are computed
        from the broadcast start time.

        :param page_id: The episode page path.
        """
        data = await self._graphql(_QUERY_EPISODE_MENU, {"pageId": page_id})
        page = data.get("page")
        if not isinstance(page, dict):
            return []
        broadcast_start = _first_broadcast_start(page.get("player"))
        tab_id = _playlist_component_id(page.get("menu"))
        if not tab_id or broadcast_start is None:
            return []

        tab = await self._graphql(_QUERY_PLAYLIST_TAB, {"componentId": tab_id})
        song_list_id = _song_list_component_id(tab.get("component"))
        if not song_list_id:
            return []

        songs: list[tuple[str, str | None, float]] = []
        async for node in self._iter_component_nodes(song_list_id):
            if node.get("__typename") != "SongTile":
                continue
            start = _parse_iso(node.get("startDate"))
            title = node.get("title")
            if start is None or not isinstance(title, str) or not title:
                continue
            offset = (start - broadcast_start).total_seconds()
            artist = node.get("description")
            songs.append((title, artist if isinstance(artist, str) and artist else None, offset))

        songs.sort(key=lambda s: s[2])
        chapters: list[VrtChapter] = []
        for index, (title, artist, offset) in enumerate(songs):
            start_seconds = max(0.0, offset)
            end = max(0.0, songs[index + 1][2]) if index + 1 < len(songs) else None
            name = f"{title} - {artist}" if artist else title
            chapters.append(VrtChapter(position=index + 1, name=name, start=start_seconds, end=end))
        return chapters

    async def get_progress(self, page_id: str, access_token: str) -> VrtProgress:
        """
        Return the user's playback progress (resume point) for an episode.

        :param page_id: The episode page path.
        :param access_token: A user access token (Bearer) - progress is per-user.
        """
        data = await self._graphql(_QUERY_RESUME, {"pageId": page_id}, access_token)
        page = data.get("page")
        player = page.get("player") if isinstance(page, dict) else None
        progress = (player or {}).get("progress")
        if not isinstance(progress, dict):
            return VrtProgress(completed=False, position=0)
        position = progress.get("progressInSeconds")
        return VrtProgress(
            completed=bool(progress.get("completed")),
            position=int(position) if isinstance(position, (int, float)) else 0,
        )

    async def get_resume_target(self, page_id: str) -> VrtResumeTarget:
        """
        Return the resume-point write target (media id + name + duration) for an episode.

        :param page_id: The episode page path.
        """
        data = await self._graphql(_QUERY_RESUME, {"pageId": page_id})
        page = data.get("page")
        player = page.get("player") if isinstance(page, dict) else None
        for mode in (player or {}).get("modes") or []:
            if not isinstance(mode, dict) or mode.get("__typename") != "AudioPlayerMode":
                continue
            template = mode.get("resumePointTemplate")
            media_id = template.get("mediaId") if isinstance(template, dict) else None
            if isinstance(media_id, str) and media_id:
                duration = mode.get("durationInSeconds")
                return VrtResumeTarget(
                    media_id=media_id,
                    media_name=(template.get("mediaName") or "")
                    if isinstance(template, dict)
                    else "",
                    duration=int(duration) if isinstance(duration, (int, float)) else 0,
                )
        raise VrtNotFoundError(f"No resume target for {page_id!r}")

    async def post_resume_point(
        self,
        target: VrtResumeTarget,
        position: int,
        access_token: str,
        *,
        total: int | None = None,
    ) -> None:
        """
        Write the user's playback progress (resume point) for an episode.

        :param target: The resume target from get_resume_target.
        :param position: Playback position in seconds.
        :param access_token: A user access token (Bearer).
        :param total: Total duration in seconds (defaults to the target's duration).
        """
        total_seconds = total if total is not None else target.duration
        at = max(0, position)
        if total_seconds:
            if at < _RESUMEPOINT_MARGIN:
                at = 0
            elif at > total_seconds - _RESUMEPOINT_MARGIN:
                at = total_seconds
        payload = {
            "at": at,
            "total": total_seconds,
            "gdpr": f"{target.media_name} beluisterd tot {at} seconden.",
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        url = f"{RESUMEPOINTS_URL}/{target.media_id}"
        try:
            async with self._session.post(
                url, json=payload, headers=headers, timeout=GRAPHQL_TIMEOUT
            ) as resp:
                if resp.status not in (200, 201, 204):
                    body = await resp.text()
                    raise VrtApiError(f"resumePoints returned HTTP {resp.status}: {body[:200]}")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VrtApiError(f"resumePoints request failed: {err}") from err

    async def iter_favourite_ids(self, access_token: str) -> AsyncGenerator[str]:
        """
        Yield the page ids of favourited podcasts and radio programmes ("Mijn lijst").

        Requires an authenticated access token; video and channel favourites are skipped.

        :param access_token: A user access token (Bearer) from the auth manager.
        """
        data = await self._graphql(_QUERY_FAVOURITES, {"pageId": FAVOURITES_PAGE}, access_token)
        page = data.get("page")
        if not isinstance(page, dict):
            return
        seen: set[str] = set()
        seen_components: set[str] = set()
        for comp in page.get("components") or []:
            if not isinstance(comp, dict) or comp.get("__typename") != "ContainerNavigation":
                continue
            for item in comp.get("items") or []:
                if not isinstance(item, dict):
                    continue
                for sub in item.get("components") or []:
                    if not isinstance(sub, dict) or sub.get("__typename") != "PaginatedTileList":
                        continue
                    component_id = sub.get("componentId")
                    # A favourites list appears both under "Alles" and its own tab;
                    # process each unique component only once.
                    if isinstance(component_id, str):
                        if component_id in seen_components:
                            continue
                        seen_components.add(component_id)
                    paginated = sub.get("paginatedItems") or {}
                    for edge in paginated.get("edges") or []:
                        node = edge.get("node") if isinstance(edge, dict) else None
                        page_id = _favourite_id(node)
                        if page_id and page_id not in seen:
                            seen.add(page_id)
                            yield page_id
                    page_info = paginated.get("pageInfo") or {}
                    if page_info.get("hasNextPage") and isinstance(component_id, str):
                        async for node in self._iter_component_nodes(
                            component_id, after=page_info.get("endCursor"), bearer=access_token
                        ):
                            page_id = _favourite_id(node)
                            if page_id and page_id not in seen:
                                seen.add(page_id)
                                yield page_id

    async def get_favourite_action(
        self, page_id: str, access_token: str
    ) -> tuple[str | None, bool]:
        """
        Return the (favourite action id, is_favourite) for a programme/podcast page.

        The action id is user- and content-specific and only present when authenticated.

        :param page_id: The programme/podcast page path.
        :param access_token: A user access token (Bearer).
        """
        data = await self._graphql(_QUERY_FAVOURITE_ACTION, {"pageId": page_id}, access_token)
        page = data.get("page")
        header = page.get("header") if isinstance(page, dict) else None
        for entry in (header or {}).get("actionItems") or []:
            action = entry.get("action") if isinstance(entry, dict) else None
            if isinstance(action, dict) and action.get("__typename") == "FavoriteAction":
                action_id = action.get("id")
                if isinstance(action_id, str) and action_id:
                    return action_id, bool(action.get("favorite"))
        return None, False

    async def set_favourite(self, action_id: str, favourite: bool, access_token: str) -> None:
        """
        Add or remove a programme/podcast from the user's 'Mijn lijst'.

        :param action_id: The FavoriteAction id from get_favourite_action.
        :param favourite: True to add, False to remove.
        :param access_token: A user access token (Bearer).
        """
        await self._graphql(
            _MUTATION_SET_FAVOURITE,
            {"input": {"favorite": favourite, "id": action_id}},
            access_token,
        )

    async def _search_nodes(
        self, entity_type: str, result_type: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Run a faceted search and return the raw tile nodes."""
        list_id = _search_list_id(entity_type, result_type, query)
        data = await self._graphql(_QUERY_SEARCH, {"listId": list_id, "first": limit})
        result = data.get("list")
        items = result.get("paginatedItems") if isinstance(result, dict) else None
        nodes: list[dict[str, Any]] = []
        for edge in (items or {}).get("edges") or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                nodes.append(node)
        return nodes

    async def _iter_component_nodes(
        self, component_id: str, after: str | None = None, bearer: str | None = None
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield raw tile nodes of a component, following Relay pagination."""
        while True:
            data = await self._graphql(
                _QUERY_COMPONENT,
                {"componentId": component_id, "first": _PAGE_SIZE, "after": after},
                bearer,
            )
            comp = data.get("component") or {}
            items = comp.get("paginatedItems") or {}
            for edge in items.get("edges") or []:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    yield node
            page_info = items.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return
            after = page_info.get("endCursor")
            if not after:
                return

    async def _graphql(
        self, query: str, variables: dict[str, Any], bearer: str | None = None
    ) -> dict[str, Any]:
        """Execute a GraphQL query and return its `data` object."""
        payload = {"query": query, "variables": variables}
        headers = GRAPHQL_HEADERS
        if bearer:
            headers = {**GRAPHQL_HEADERS, "Authorization": f"Bearer {bearer}"}
        try:
            async with (
                self._throttler,
                self._session.post(
                    GRAPHQL_URL, json=payload, headers=headers, timeout=GRAPHQL_TIMEOUT
                ) as resp,
            ):
                resp.raise_for_status()
                body = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise VrtApiError(f"GraphQL request failed: {err}") from err
        if not isinstance(body, dict):
            raise VrtApiError("Unexpected GraphQL response")
        if body.get("errors"):
            self._logger.debug("VRT GraphQL errors: %s", body["errors"])
            raise VrtApiError(str(body["errors"]))
        data = body.get("data")
        if not isinstance(data, dict):
            raise VrtApiError("GraphQL response without data")
        return data
