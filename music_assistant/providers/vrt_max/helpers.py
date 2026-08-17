"""Typed station data + VRT MAX GraphQL API client for the VRT MAX provider."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    from aiohttp import ClientSession


@dataclass(frozen=True, slots=True)
class VrtStation:
    """A single VRT MAX live radio station."""

    id: str
    name: str
    stream_url: str
    aac_url: str | None = None
    logo_url: str | None = None
    tagline: str | None = None


STATIONS: tuple[VrtStation, ...] = (
    VrtStation(
        id="radio1",
        name="Radio 1",
        stream_url="http://icecast.vrtcdn.be/radio1-high.mp3",
        aac_url="http://icecast.vrtcdn.be/radio1.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio1.png",
        tagline="Altijd Benieuwd",
    ),
    VrtStation(
        id="radio1-classics",
        name="Radio 1 Classics",
        stream_url="http://icecast.vrtcdn.be/radio1_classics-high.mp3",
        aac_url="http://icecast.vrtcdn.be/radio1_classics.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio1classics.png",
        tagline="Een eindeloze stroom aan onsterfelijke klassiekers",
    ),
    VrtStation(
        id="radio2-antwerpen",
        name="Radio 2 Antwerpen",
        stream_url="http://icecast.vrtcdn.be/ra2ant-high.mp3",
        aac_url="http://icecast.vrtcdn.be/ra2ant.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio2ant.png",
        tagline="De grootste familie",
    ),
    VrtStation(
        id="radio2-vlaams-brabant",
        name="Radio 2 Vlaams-Brabant",
        stream_url="http://icecast.vrtcdn.be/ra2vlb-high.mp3",
        aac_url="http://icecast.vrtcdn.be/ra2vlb.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio2vlbr.png",
        tagline="De grootste familie",
    ),
    VrtStation(
        id="radio2-limburg",
        name="Radio 2 Limburg",
        stream_url="http://icecast.vrtcdn.be/ra2lim-high.mp3",
        aac_url="http://icecast.vrtcdn.be/ra2lim.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio2lim.png",
        tagline="De grootste familie",
    ),
    VrtStation(
        id="radio2-oost-vlaanderen",
        name="Radio 2 Oost-Vlaanderen",
        stream_url="http://icecast.vrtcdn.be/ra2ovl-high.mp3",
        aac_url="http://icecast.vrtcdn.be/ra2ovl.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio2ovl.png",
        tagline="De grootste familie",
    ),
    VrtStation(
        id="radio2-west-vlaanderen",
        name="Radio 2 West-Vlaanderen",
        stream_url="http://icecast.vrtcdn.be/ra2wvl-high.mp3",
        aac_url="http://icecast.vrtcdn.be/ra2wvl.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio2wvl.png",
        tagline="De grootste familie",
    ),
    VrtStation(
        id="radio2-relax",
        name="Radio 2 Relax",
        stream_url="http://icecast.vrtcdn.be/radio2_relax-high.mp3",
        aac_url="http://icecast.vrtcdn.be/radio2_relax.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/radio2relax.png",
        tagline="Ontspannen genieten met Radio 2",
    ),
    VrtStation(
        id="radio-bene",
        name="Radio Bene",
        stream_url="http://icecast.vrtcdn.be/radiobene-high.mp3",
        aac_url="http://icecast.vrtcdn.be/radiobene.aac",
        logo_url=None,
        tagline=None,
    ),
    VrtStation(
        id="klara",
        name="Klara",
        stream_url="http://icecast.vrtcdn.be/klara-high.mp3",
        aac_url="http://icecast.vrtcdn.be/klara.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/klara.png",
        tagline="Blijf verwonderd",
    ),
    VrtStation(
        id="klara-continuo",
        name="Klara Continuo",
        stream_url="http://icecast.vrtcdn.be/klaracontinuo-high.mp3",
        aac_url="http://icecast.vrtcdn.be/klaracontinuo.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/klaracontinuo.png",
        tagline="Non-stop klassieke muziek",
    ),
    VrtStation(
        id="studio-brussel",
        name="Studio Brussel",
        stream_url="http://icecast.vrtcdn.be/stubru-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/stubru.png",
        tagline="Life is Music",
    ),
    VrtStation(
        id="stubru-tijdloze",
        name="StuBru De Tijdloze",
        stream_url="http://icecast.vrtcdn.be/stubru_tijdloze-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_tijdloze.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/detijdloze.png",
        tagline="Altijd en overal de beste Tijdloze muziek",
    ),
    VrtStation(
        id="stubru-bruut",
        name="StuBru Bruut",
        stream_url="http://icecast.vrtcdn.be/stubru_bruut-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_bruut.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/bruut.png",
        tagline="Alleen maar stevige gitaren",
    ),
    VrtStation(
        id="stubru-de-jaren-nul",
        name="StuBru De Jaren Nul",
        stream_url="http://icecast.vrtcdn.be/stubru_dejarennul-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_dejarennul.aac",
        logo_url=None,
        tagline=None,
    ),
    VrtStation(
        id="stubru-vuurland",
        name="StuBru Vuurland",
        stream_url="http://icecast.vrtcdn.be/stubru_tgs-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_tgs.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/vuurland.png",
        tagline=None,
    ),
    VrtStation(
        id="stubru-untz",
        name="StuBru UNTZ",
        stream_url="http://icecast.vrtcdn.be/stubru_untz-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_untz.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/untz.png",
        tagline="The party never stops",
    ),
    VrtStation(
        id="mnm",
        name="MNM",
        stream_url="http://icecast.vrtcdn.be/mnm-high.mp3",
        aac_url="http://icecast.vrtcdn.be/mnm.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/mnm.png",
        tagline="Music and More",
    ),
    VrtStation(
        id="mnm-hits",
        name="MNM Hits",
        stream_url="http://icecast.vrtcdn.be/mnm_hits-high.mp3",
        aac_url="http://icecast.vrtcdn.be/mnm_hits.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/mnmhits.png",
        tagline="Music and More - The Hits",
    ),
    VrtStation(
        id="ketnet-hits",
        name="Ketnet Hits",
        stream_url="http://icecast.vrtcdn.be/ketnetradio-high.mp3",
        aac_url="http://icecast.vrtcdn.be/ketnetradio.aac",
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/ketnethits.png",
        tagline="De hipste hits op een rijtje",
    ),
    VrtStation(
        id="vrtnws",
        name="VRT NWS",
        stream_url="https://icecast.vrtcdn.be/vrtnws-high.mp3",
        aac_url=None,
        logo_url="https://radioplayer.vrt.be/iframe/img/channelLogos/vrtnws.png",
        tagline="Ieder moment het meest recente nieuws",
    ),
)

STATIONS_BY_ID: dict[str, VrtStation] = {s.id: s for s in STATIONS}


# ---------------------------------------------------------------------------
# VRT MAX GraphQL catalogue API
#
# All catalogue browsing goes through a single page-path-keyed GraphQL endpoint.
# It is fully anonymous (no Authorization header). Page ids are the site URL
# paths, which we reuse verbatim as provider item ids (stable and reversible).
# VRT changes this API periodically; keep every endpoint/query in this module.
# ---------------------------------------------------------------------------

# Used with the Bearer token for user-scoped queries (Mijn lijst, progress) and
# anonymously for everything else. A public-only mirror exists at
# `.../vrtnu-api/graphql/public/v1` (no auth) - a fallback should VRT ever start
# requiring auth on this endpoint for anonymous catalogue/playlist queries.
GRAPHQL_URL = "https://www.vrt.be/vrtnu-api/graphql/v1"
GRAPHQL_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "x-vrt-client-name": "WEB",
    "x-vrt-client-version": "1.5.15",
    "User-Agent": "Music Assistant",
}
GRAPHQL_TIMEOUT = aiohttp.ClientTimeout(total=25)

# ---------------------------------------------------------------------------
# On-demand streaming + authentication
#
# On-demand (listen-back / podcast) episodes need an authenticated, BE-geo
# verified token. We obtain it via the VRT SSO login (username/password ->
# identity token) and exchange it for a short-lived vrtPlayerToken. The token
# gates *resolution* only; the resolved _nodrm_ HLS manifest is DRM-free.
# ---------------------------------------------------------------------------

TOKEN_URL = (
    "https://media-services-public.vrt.be/vualto-video-aggregator-web/rest/external/v2/tokens"
)
AGGREGATOR_URL = "https://media-services-public.vrt.be/media-aggregator/v2"
AGGREGATOR_CLIENT = "vrtnu-web@PROD"
# Per-user playback progress ("resume points"), keyed by the episode's aud media id.
RESUMEPOINTS_URL = "https://ddt.profiel.vrt.be/resumePoints"
# Snap the resume position to 0/end within this many seconds of the start/end.
_RESUMEPOINT_MARGIN = 30
SSO_INIT_URL = "https://www.vrt.be/vrtnu/sso/login?scope=openid,mid"
SSO_LOGIN_URL = "https://login.vrt.be/perform_login"
# VRT's login endpoint expects a browser-like client.
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) Gecko/20100101 Firefox/137.0"
# Refresh a player token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60.0

# Landing pages (channel-grouped programs / podcast sections).
RADIO_LANDING_PAGE = "/vrtmax/radio/"
PODCAST_LANDING_PAGE = "/vrtmax/luister/"
# The authenticated favourites page ("Mijn lijst").
FAVOURITES_PAGE = "/vrtmax/mijn-lijst/"

# How many tiles to request per GraphQL page while paginating a component.
_PAGE_SIZE = 100

# Tile typenames that carry playable on-demand audio episodes.
_EPISODE_TILE_TYPES = frozenset({"RadioEpisodeTile", "PodcastEpisodeTile"})
# Tile typenames that represent programs/podcasts (a folder of episodes).
_PROGRAM_TILE_TYPES = frozenset({"RadioProgramTile", "PodcastProgramTile"})
# Favourite tile typenames that map to an MA Podcast (radio archives + podcasts).
# Video (`ProgramTile`) and channel (`TopicTile`) favourites are intentionally skipped.
_FAVOURITE_TILE_TYPES = frozenset({"RadioProgramTile", "PodcastProgramTile"})

# Shared tile selection (fields common to every ITile, plus the episode-only
# formattedDuration on the concrete episode types).
_TILE_FIELDS = """
      __typename
      ... on ITile {
        objectId
        title
        description
        image { templateUrl }
        primaryMeta { value }
        progress { progressInSeconds durationInSeconds completed }
        action {
          __typename
          ... on LinkAction { link }
        }
      }
      ... on RadioEpisodeTile { formattedDuration }
      ... on PodcastEpisodeTile { formattedDuration }
      ... on SongTile { startDate }
"""

_QUERY_SEARCH = """
query SearchTileList($listId: ID!, $first: Int!) {
  list(listId: $listId) {
    __typename
    ... on PaginatedTileList {
      paginatedItems(first: $first) {
        edges {
          node {
TILE_FIELDS
          }
        }
      }
    }
  }
}
""".replace("TILE_FIELDS", _TILE_FIELDS)

_QUERY_LANDING = """
query ThemePage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on ThemePage {
      title
      components {
        __typename
        ... on PaginatedTileList {
          title
          componentId
          paginatedItems(first: 1) {
            edges { node { __typename } }
          }
        }
      }
    }
  }
}
"""

_QUERY_COMPONENT = """
query Component($componentId: ID!, $first: Int!, $after: ID) {
  component(id: $componentId) {
    __typename
    ... on PaginatedTileList {
      title
      paginatedItems(first: $first, after: $after) {
        pageInfo { endCursor hasNextPage }
        edges {
          node {
TILE_FIELDS
          }
        }
      }
    }
  }
}
""".replace("TILE_FIELDS", _TILE_FIELDS)

# Episode lists live under `components -> ContainerNavigation -> items (tabs)`.
# Single-season programs put a PaginatedTileList directly under a tab; multi-season
# podcasts nest another ContainerNavigation (the season selector) one level deeper.
_SEASON_LIST_FIELDS = """
                title
                componentId
                paginatedItems(first: 1) {
                  edges { node { __typename } }
                }
"""

_PROGRAM_PAGE_FIELDS = """
      title
      brand
      header {
        __typename
        ... on PageHeader {
          richDescription { text }
          image { templateUrl }
          secondaryMeta { value }
        }
      }
      components {
        __typename
        ... on ContainerNavigation {
          items {
            title
            components {
              __typename
              ... on PresentersList { presenters { title type } }
              ... on PaginatedTileList {
SEASON_LIST_FIELDS
              }
              ... on ContainerNavigation {
                items {
                  title
                  components {
                    __typename
                    ... on PaginatedTileList {
SEASON_LIST_FIELDS
                    }
                  }
                }
              }
            }
          }
        }
      }
""".replace("SEASON_LIST_FIELDS", _SEASON_LIST_FIELDS)

_QUERY_PROGRAM = """
query ProgramPage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioProgramPage {
PROGRAM_FIELDS
    }
    ... on PodcastProgramPage {
PROGRAM_FIELDS
    }
  }
}
""".replace("PROGRAM_FIELDS", _PROGRAM_PAGE_FIELDS)

_EPISODE_PAGE_FIELDS = """
      title
      header {
        __typename
        ... on IPageHeader {
          richDescription { text }
          image { templateUrl }
          primaryMeta { value }
        }
      }
      player {
        __typename
        ... on MediaPlayer {
          title
          subtitle
          image { templateUrl }
        }
      }
"""

_QUERY_EPISODE = """
query EpisodePage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
EPISODE_FIELDS
    }
    ... on PodcastEpisodePage {
EPISODE_FIELDS
    }
  }
}
""".replace("EPISODE_FIELDS", _EPISODE_PAGE_FIELDS)

_STREAM_PLAYER_FIELDS = """
      player {
        modes {
          __typename
          ... on AudioPlayerMode {
            streamId
            durationInSeconds
          }
        }
      }
"""

_QUERY_STREAM = """
query EpisodeStream($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
STREAM_PLAYER_FIELDS
    }
    ... on PodcastEpisodePage {
STREAM_PLAYER_FIELDS
    }
  }
}
""".replace("STREAM_PLAYER_FIELDS", _STREAM_PLAYER_FIELDS)


_QUERY_FAVOURITE_ACTION = """
query FavouriteAction($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on IPage {
      header {
        __typename
        ... on IPageHeader {
          actionItems {
            action {
              __typename
              ... on FavoriteAction { id favorite }
            }
          }
        }
      }
    }
  }
}
"""

_MUTATION_SET_FAVOURITE = """
mutation setFavorite($input: FavoriteActionInput!) {
  setFavorite(input: $input) {
    actionItem {
      action {
        __typename
        ... on FavoriteAction { id favorite }
      }
    }
  }
}
"""

_QUERY_FAVOURITES = """
query FavoritesPage($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on FavoritesPage {
      components {
        __typename
        ... on ContainerNavigation {
          items {
            title
            components {
              __typename
              ... on PaginatedTileList {
                componentId
                paginatedItems(first: 100) {
                  pageInfo { endCursor hasNextPage }
                  edges {
                    node {
                      __typename
                      ... on ITile {
                        action { __typename ... on LinkAction { link } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


_RESUME_PLAYER_FIELDS = """
      player {
        progress { progressInSeconds durationInSeconds completed }
        modes {
          __typename
          ... on AudioPlayerMode {
            durationInSeconds
            resumePointTemplate { mediaId mediaName }
          }
        }
      }
"""

_QUERY_RESUME = """
query EpisodeResume($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
RESUME_PLAYER_FIELDS
    }
    ... on PodcastEpisodePage {
RESUME_PLAYER_FIELDS
    }
  }
}
""".replace("RESUME_PLAYER_FIELDS", _RESUME_PLAYER_FIELDS)


_EPISODE_MENU_FIELDS = """
      player {
        modes {
          __typename
          ... on AudioPlayerMode { broadcastStartDate }
        }
      }
      menu { items { title componentId } }
"""

_QUERY_EPISODE_MENU = """
query EpisodeMenu($pageId: ID!) {
  page(id: $pageId) {
    __typename
    ... on RadioEpisodePage {
EPISODE_MENU_FIELDS
    }
    ... on PodcastEpisodePage {
EPISODE_MENU_FIELDS
    }
  }
}
""".replace("EPISODE_MENU_FIELDS", _EPISODE_MENU_FIELDS)

# The playlist menu id resolves to a ContainerNavigationItem wrapping the actual
# song PaginatedTileList; this query digs out that inner list's componentId.
_QUERY_PLAYLIST_TAB = """
query PlaylistTab($componentId: ID!) {
  component(id: $componentId) {
    __typename
    ... on ContainerNavigationItem {
      components {
        __typename
        ... on PaginatedTileList { componentId tileContentType }
      }
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class VrtStreamInfo:
    """The playable stream reference for an on-demand episode."""

    stream_id: str
    duration: int = 0


@dataclass(frozen=True, slots=True)
class VrtChapter:
    """A tracklist entry (played song) mapped to an episode chapter."""

    position: int
    name: str
    start: float  # seconds from the episode start
    end: float | None = None


@dataclass(frozen=True, slots=True)
class VrtResumeTarget:
    """The resume-point write target for an on-demand episode."""

    media_id: str
    media_name: str
    duration: int = 0


@dataclass(frozen=True, slots=True)
class VrtProgress:
    """The user's playback progress for an on-demand episode."""

    completed: bool
    position: int  # seconds


@dataclass(frozen=True, slots=True)
class VrtRow:
    """A single tile row on a landing (ThemePage) page."""

    title: str
    component_id: str
    tile_type: str | None


@dataclass(frozen=True, slots=True)
class VrtProgramTile:
    """A program/podcast tile (a folder of episodes) parsed from a tile list."""

    page_id: str
    title: str
    description: str | None = None
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class VrtSeason:
    """A paginable episode list (a season / listen-back tab) within a program page."""

    title: str | None
    component_id: str


@dataclass(frozen=True, slots=True)
class VrtProgram:
    """A radio program archive or podcast (maps to an MA Podcast)."""

    page_id: str
    title: str
    description: str | None = None
    image_url: str | None = None
    publisher: str | None = None
    presenters: tuple[str, ...] = ()
    seasons: tuple[VrtSeason, ...] = ()


@dataclass(frozen=True, slots=True)
class VrtEpisode:
    """A single on-demand episode (maps to an MA PodcastEpisode)."""

    page_id: str
    title: str
    description: str | None = None
    image_url: str | None = None
    duration: int = 0
    date_label: str | None = None
    fully_played: bool = False
    resume_position: int = 0  # seconds


class VrtApiError(Exception):
    """Raised when the VRT GraphQL API returns an error or unexpected payload."""


class VrtAuthError(Exception):
    """Raised when VRT authentication (SSO login / token exchange) fails."""


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
        # Cache of episode resume-point targets (stable per episode).
        self._resume_targets: dict[str, VrtResumeTarget] = {}

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
            raise VrtApiError(f"No program page for {page_id!r}")

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

    async def iter_episodes(self, page_id: str) -> AsyncGenerator[VrtEpisode]:
        """
        Yield all listen-back episodes of a program/podcast, across every season.

        :param page_id: The program/podcast page path.
        """
        program = await self.get_program(page_id)
        for season in program.seasons:
            async for episode in self.iter_season_episodes(season.component_id):
                yield episode

    async def get_episode(self, page_id: str) -> VrtEpisode:
        """
        Return metadata for a single episode page.

        :param page_id: The episode page path.
        """
        data = await self._graphql(_QUERY_EPISODE, {"pageId": page_id})
        page = data.get("page")
        if not isinstance(page, dict) or not page.get("__typename"):
            raise VrtApiError(f"No episode page for {page_id!r}")
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
        raise VrtApiError(f"No audio stream for {page_id!r}")

    async def resolve_ondemand_hls(self, stream_id: str, player_token: str) -> str:
        """
        Resolve an on-demand streamId to a DRM-free HLS manifest URL.

        :param stream_id: The `{pubId}${audId}` stream id from get_stream_info.
        :param player_token: An authenticated vrtPlayerToken.
        """
        url = f"{AGGREGATOR_URL}/media-items/{stream_id}"
        params = {"vrtPlayerToken": player_token, "client": AGGREGATOR_CLIENT}
        async with self._session.get(
            url,
            params=params,
            headers={"User-Agent": "Music Assistant"},
            timeout=GRAPHQL_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise VrtApiError(f"Aggregator returned HTTP {resp.status}: {body[:200]}")
            body = await resp.json()
        hls_urls: list[str] = []
        for target in body.get("targetUrls") or []:
            if not isinstance(target, dict) or target.get("type") != "hls":
                continue
            target_url = target.get("url")
            if isinstance(target_url, str) and target_url:
                hls_urls.append(target_url)
        # Prefer the DRM-free variant; fall back to the first HLS url.
        for hls_url in hls_urls:
            if "_nodrm_" in hls_url:
                return hls_url
        if hls_urls:
            return hls_urls[0]
        raise VrtApiError("No HLS stream in aggregator response")

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

        Cached in-memory; the target is stable per episode.

        :param page_id: The episode page path.
        """
        cached = self._resume_targets.get(page_id)
        if cached is not None:
            return cached
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
                target = VrtResumeTarget(
                    media_id=media_id,
                    media_name=(template.get("mediaName") or "")
                    if isinstance(template, dict)
                    else "",
                    duration=int(duration) if isinstance(duration, (int, float)) else 0,
                )
                self._resume_targets[page_id] = target
                return target
        raise VrtApiError(f"No resume target for {page_id!r}")

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
            "User-Agent": "Music Assistant",
        }
        url = f"{RESUMEPOINTS_URL}/{target.media_id}"
        async with self._session.post(
            url, json=payload, headers=headers, timeout=GRAPHQL_TIMEOUT
        ) as resp:
            if resp.status not in (200, 201, 204):
                body = await resp.text()
                raise VrtApiError(f"resumePoints returned HTTP {resp.status}: {body[:200]}")

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
        async with self._session.post(
            GRAPHQL_URL, json=payload, headers=headers, timeout=GRAPHQL_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
        if not isinstance(body, dict):
            raise VrtApiError("Unexpected GraphQL response")
        if body.get("errors"):
            self._logger.debug("VRT GraphQL errors: %s", body["errors"])
            raise VrtApiError(str(body["errors"]))
        data = body.get("data")
        if not isinstance(data, dict):
            raise VrtApiError("GraphQL response without data")
        return data


class VrtMaxAuth:
    """
    Manages VRT MAX authentication for on-demand playback.

    Performs the SSO username/password login to obtain an identity token, then
    exchanges it for a short-lived vrtPlayerToken which it caches until shortly
    before expiry. A single lock serialises concurrent refreshes.
    """

    def __init__(
        self,
        session: ClientSession,
        logger: logging.Logger,
        username: str,
        password: str,
    ) -> None:
        """
        Initialize the auth manager.

        :param session: Shared aiohttp session (used for the token exchange).
        :param logger: Logger for diagnostics.
        :param username: VRT account email (empty disables on-demand).
        :param password: VRT account password (empty disables on-demand).
        """
        self._session = session
        self._logger = logger
        self._username = username
        self._password = password
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._identity_token: str | None = None
        self._login_expiry: float = 0.0
        self._player_token: str | None = None
        self._player_token_expiry: float = 0.0

    @property
    def enabled(self) -> bool:
        """Return True when credentials are configured."""
        return bool(self._username and self._password)

    async def get_access_token(self) -> str:
        """Return a valid access token (Bearer) for user-scoped GraphQL calls."""
        if not self.enabled:
            raise VrtAuthError("VRT account credentials are required")
        async with self._lock:
            await self._ensure_login()
            assert self._access_token is not None
            return self._access_token

    async def get_player_token(self) -> str:
        """Return a valid vrtPlayerToken, logging in / refreshing as needed."""
        if not self.enabled:
            raise VrtAuthError("VRT account credentials are required for on-demand playback")
        async with self._lock:
            if (
                self._player_token
                and time.time() < self._player_token_expiry - _TOKEN_EXPIRY_MARGIN
            ):
                return self._player_token
            await self._ensure_login()
            assert self._identity_token is not None
            token, expiry = await self._request_player_token(self._identity_token)
            self._player_token = token
            self._player_token_expiry = expiry
            return token

    async def _ensure_login(self) -> None:
        """Ensure a valid access + identity token, performing the SSO login if needed."""
        if (
            self._access_token
            and self._identity_token
            and time.time() < self._login_expiry - _TOKEN_EXPIRY_MARGIN
        ):
            return
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(
            cookie_jar=jar, headers={"User-Agent": BROWSER_UA}
        ) as session:
            async with session.get(SSO_INIT_URL, timeout=GRAPHQL_TIMEOUT) as resp:
                await resp.read()
            xsrf = _cookie_value(jar, "OIDCXSRF")
            if not xsrf:
                raise VrtAuthError("VRT SSO init failed (no OIDCXSRF cookie)")
            payload = {
                "clientId": "vrtnu-site",
                "loginID": self._username,
                "password": self._password,
            }
            async with session.post(
                SSO_LOGIN_URL, json=payload, headers={"OIDCXSRF": xsrf}, timeout=GRAPHQL_TIMEOUT
            ) as resp:
                info = await resp.json(content_type=None)
            if not isinstance(info, dict) or info.get("errorCode") != 0:
                message = (info or {}).get("errorMessage") or "invalid credentials"
                raise VrtAuthError(f"VRT login failed: {message}")
            redirect_url = info.get("redirectUrl")
            if not redirect_url:
                raise VrtAuthError("VRT login returned no redirect url")
            async with session.get(redirect_url, timeout=GRAPHQL_TIMEOUT) as resp:
                await resp.read()
            access_token = _cookie_value(jar, "vrtnu-site_profile_at")
            identity_token = _cookie_value(jar, "vrtnu-site_profile_vt")
            if not access_token or not identity_token:
                raise VrtAuthError("VRT login did not yield the expected tokens")
            self._access_token = access_token
            self._identity_token = identity_token
            self._login_expiry = min(_jwt_expiry(access_token), _jwt_expiry(identity_token))

    async def _request_player_token(self, identity_token: str) -> tuple[str, float]:
        """Exchange an identity token for a vrtPlayerToken and its expiry epoch."""
        payload = {"identityToken": identity_token, "playerInfo": ""}
        async with self._session.post(
            TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Music Assistant"},
            timeout=GRAPHQL_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
        token = body.get("vrtPlayerToken") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise VrtAuthError("No vrtPlayerToken in token response")
        return token, _jwt_expiry(token)


def _cookie_value(jar: aiohttp.CookieJar, name: str) -> str | None:
    """Return the value of a cookie by name from a cookie jar."""
    for cookie in jar:
        if cookie.key == name and cookie.value:
            return cookie.value
    return None


def _jwt_expiry(token: str) -> float:
    """Return the `exp` claim (epoch seconds) of a JWT, or now+30min on failure."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except IndexError, ValueError, binascii.Error, json.JSONDecodeError:
        pass
    return time.time() + 1800


# Map the GraphQL `brand` slug to a human channel name (fallback: prettified slug).
_BRAND_NAMES = {
    "radio1": "Radio 1",
    "radio2": "Radio 2",
    "klara": "Klara",
    "stubru": "Studio Brussel",
    "mnm": "MNM",
    "ketnet": "Ketnet",
    "vrtnws": "VRT NWS",
    "sporza": "Sporza",
}


def _brand_display_name(brand: Any) -> str | None:
    """Return a human channel name for a VRT brand slug."""
    if not isinstance(brand, str) or not brand:
        return None
    return _BRAND_NAMES.get(brand, brand.replace("-", " ").title())


# Media-kind labels that appear in a header meta breadcrumb but are not presenters.
_META_NON_PRESENTER = frozenset(
    {"radio", "podcast", "tv", "kijk", "luister", "fragment", "fragmenten", "clip"}
)


def _presenters_from_header(header: Any, channel_name: str | None) -> tuple[str, ...]:
    """
    Extract presenter names from a PageHeader's secondaryMeta breadcrumb.

    The breadcrumb is like [mediatype, channel, presenter]; drop the media-kind
    label, the channel name and season counts, keeping the presenter(s).
    """
    if not isinstance(header, dict):
        return ()
    names: list[str] = []
    for entry in header.get("secondaryMeta") or []:
        value = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(value, str) or not value:
            continue
        if value.lower() in _META_NON_PRESENTER or "seizoen" in value.lower():
            continue
        if channel_name and value == channel_name:
            continue
        if value not in names:
            names.append(value)
    return tuple(names)


def _collect_presenters(components: Any) -> tuple[str, ...]:
    """Collect presenter names from a program page's PresentersList components."""
    if not isinstance(components, list):
        return ()
    names: list[str] = []
    for comp in components:
        if not isinstance(comp, dict) or comp.get("__typename") != "ContainerNavigation":
            continue
        for item in comp.get("items") or []:
            if not isinstance(item, dict):
                continue
            for sub in item.get("components") or []:
                if not isinstance(sub, dict) or sub.get("__typename") != "PresentersList":
                    continue
                for presenter in sub.get("presenters") or []:
                    title = presenter.get("title") if isinstance(presenter, dict) else None
                    if isinstance(title, str) and title and title not in names:
                        names.append(title)
    return tuple(names)


def _collect_seasons(components: Any, seasons: list[VrtSeason]) -> None:
    """
    Recursively collect listen-back episode lists from a program page.

    Walks `ContainerNavigation` tabs (and the nested season-selector navigation
    used by multi-season podcasts), appending each PaginatedTileList that holds
    episode tiles. Scheduled/upcoming tabs are skipped.
    """
    if not isinstance(components, list):
        return
    for comp in components:
        if not isinstance(comp, dict):
            continue
        typename = comp.get("__typename")
        if typename == "ContainerNavigation":
            for item in comp.get("items") or []:
                if not isinstance(item, dict):
                    continue
                # Skip upcoming/scheduled broadcasts - not yet playable.
                if (item.get("title") or "").lower().startswith("gepland"):
                    continue
                _collect_seasons(item.get("components"), seasons)
        elif typename == "PaginatedTileList":
            if _first_node_type(comp.get("paginatedItems")) not in _EPISODE_TILE_TYPES:
                continue
            cid = comp.get("componentId")
            if isinstance(cid, str):
                seasons.append(VrtSeason(title=comp.get("title"), component_id=cid))


def _parse_iso(value: Any) -> datetime | None:
    """Parse a VRT ISO-8601 timestamp (e.g. '2026-08-09T10:00:00.000Z')."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _first_broadcast_start(player: Any) -> datetime | None:
    """Return the AudioPlayerMode broadcastStartDate from a player payload."""
    if not isinstance(player, dict):
        return None
    for mode in player.get("modes") or []:
        if isinstance(mode, dict) and mode.get("__typename") == "AudioPlayerMode":
            return _parse_iso(mode.get("broadcastStartDate"))
    return None


def _playlist_component_id(menu: Any) -> str | None:
    """Return the 'Playlist' tab componentId from an episode page menu."""
    if not isinstance(menu, dict):
        return None
    for item in menu.get("items") or []:
        if not isinstance(item, dict):
            continue
        if (item.get("title") or "").lower() == "playlist":
            component_id = item.get("componentId")
            if isinstance(component_id, str) and component_id:
                return component_id
    return None


def _song_list_component_id(component: Any) -> str | None:
    """Return the inner song PaginatedTileList componentId from a playlist tab."""
    if not isinstance(component, dict):
        return None
    for sub in component.get("components") or []:
        if not isinstance(sub, dict) or sub.get("__typename") != "PaginatedTileList":
            continue
        if sub.get("tileContentType") == "song":
            component_id = sub.get("componentId")
            if isinstance(component_id, str) and component_id:
                return component_id
    return None


def _search_list_id(entity_type: str, result_type: str, query: str) -> str:
    """Build the base64 `listId` for a faceted keyword search."""
    search = {
        "facets": [{"name": "entitytype", "values": [entity_type]}],
        "resultType": result_type,
        "q": query,
    }
    raw = f"o%14|{json.dumps(search)}|{result_type}%"
    return "$" + base64.b64encode(raw.encode()).decode()


def _favourite_id(node: Any) -> str | None:
    """Return the page id of a favourite tile if it maps to a Podcast, else None."""
    if not isinstance(node, dict) or node.get("__typename") not in _FAVOURITE_TILE_TYPES:
        return None
    return _link(node)


def _first_node_type(paginated_items: Any) -> str | None:
    """Return the __typename of the first node in a paginatedItems payload."""
    if not isinstance(paginated_items, dict):
        return None
    for edge in paginated_items.get("edges") or []:
        node = edge.get("node") if isinstance(edge, dict) else None
        if isinstance(node, dict):
            return node.get("__typename")
    return None


def _image_url(image: Any) -> str | None:
    """Extract a usable image URL from an Image object."""
    if not isinstance(image, dict):
        return None
    url = image.get("templateUrl")
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    # Observed template urls are already fully-qualified 'orig' urls; drop any
    # leftover sizing placeholders defensively.
    return re.sub(r"\{[^}]*\}", "", url)


def _link(node: dict[str, Any]) -> str | None:
    """Return the LinkAction target (page path) of a tile node."""
    action = node.get("action")
    if isinstance(action, dict) and action.get("__typename") == "LinkAction":
        link = action.get("link")
        if isinstance(link, str) and link:
            return link
    return None


def _header_meta(header: Any) -> list[Any]:
    """Return the primaryMeta list of a PageHeader."""
    if isinstance(header, dict):
        meta = header.get("primaryMeta")
        if isinstance(meta, list):
            return meta
    return []


def _first_meta(meta: list[Any]) -> str | None:
    """Return the first non-empty primaryMeta value."""
    for entry in meta:
        if isinstance(entry, dict):
            value = entry.get("value")
            if isinstance(value, str) and value:
                return value
    return None


def _parse_header(header: Any) -> tuple[str | None, str | None]:
    """Return (description, image_url) from a PageHeader object."""
    if not isinstance(header, dict):
        return None, None
    description = None
    rich = header.get("richDescription")
    if isinstance(rich, dict):
        text = rich.get("text")
        if isinstance(text, str) and text.strip():
            description = text.strip()
    return description, _image_url(header.get("image"))


def _parse_program_tile(node: dict[str, Any]) -> VrtProgramTile | None:
    """Parse a program/podcast tile node into a VrtProgramTile."""
    page_id = _link(node)
    title = node.get("title")
    if not page_id or not isinstance(title, str) or not title:
        return None
    description = node.get("description")
    if not isinstance(description, str) or not description:
        description = None
    return VrtProgramTile(
        page_id=page_id,
        title=title,
        description=description,
        image_url=_image_url(node.get("image")),
    )


def _parse_episode_tile(node: dict[str, Any]) -> VrtEpisode | None:
    """Parse an episode tile node into a VrtEpisode."""
    page_id = _link(node)
    title = node.get("title")
    if not page_id or not isinstance(title, str) or not title:
        return None
    description = node.get("description")
    if not isinstance(description, str) or not description:
        description = None
    date_label = _first_meta(node.get("primaryMeta") or [])
    progress = node.get("progress")
    fully_played = False
    resume_position = 0
    if isinstance(progress, dict):
        fully_played = bool(progress.get("completed"))
        pos = progress.get("progressInSeconds")
        resume_position = int(pos) if isinstance(pos, (int, float)) else 0
    return VrtEpisode(
        page_id=page_id,
        title=title,
        description=description,
        image_url=_image_url(node.get("image")),
        duration=_parse_duration(node.get("formattedDuration")),
        date_label=date_label,
        fully_played=fully_played,
        resume_position=resume_position,
    )


def _parse_duration(formatted: Any) -> int:
    """Parse a formatted duration like '60 min' or '1 u 5 min' into seconds."""
    if not isinstance(formatted, str):
        return 0
    seconds = 0
    hours = re.search(r"(\d+)\s*(?:u|uur|h)\b", formatted)
    if hours:
        seconds += int(hours.group(1)) * 3600
    minutes = re.search(r"(\d+)\s*min", formatted)
    if minutes:
        seconds += int(minutes.group(1)) * 60
    if seconds == 0:
        # bare number - assume minutes
        bare = re.fullmatch(r"\s*(\d+)\s*", formatted)
        if bare:
            seconds = int(bare.group(1)) * 60
    return seconds
