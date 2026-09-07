"""Constants for the VRT MAX music provider."""

from __future__ import annotations

import aiohttp

from .models import VrtStation

# Browse subpath prefixes (after "<instance>://").
BROWSE_RADIOS = "radios"
BROWSE_RADIO_PROGRAMS = "radio"
BROWSE_PODCASTS = "podcasts"

# Tile typenames that identify a program/podcast row on a landing page.
RADIO_ROW_TYPE = "RadioProgramTile"
PODCAST_ROW_TYPE = "PodcastProgramTile"

# Only radio-archive episodes carry a played-songs tracklist (podcasts never do).
# Each tracklist costs several requests, and the chapters are only rendered in the now
# playing view, so they are fetched for the newest few episodes rather than the whole
# archive: the ones a listener opens, for a fraction of the traffic.
TRACKLIST_EPISODES = 10
# How many of those tracklists may be fetched at once.
TRACKLIST_CONCURRENCY = 4

# MA gives a provider search 8 seconds (SEARCH_PROVIDER_SOFT_TIMEOUT) before it contributes
# empty results, so the catalogue queries are given a deadline inside that window rather
# than working on beyond the point where the answer is still wanted.
SEARCH_TIMEOUT = 6


STATIONS: tuple[VrtStation, ...] = (
    VrtStation(
        id="radio1",
        name="Radio 1",
        stream_url="http://icecast.vrtcdn.be/radio1-high.mp3",
        aac_url="http://icecast.vrtcdn.be/radio1.aac",
        logo_url="https://images.vrt.be/orig/2025/06/19/2a99563b-7503-4906-81fb-aa6bc91bfa08.png",
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
        logo_url="https://images.vrt.be/orig/2025/06/19/d7fc517e-2fc8-4467-b1d0-90a32b1c7334.png",
        tagline=None,
    ),
    VrtStation(
        id="klara",
        name="Klara",
        stream_url="http://icecast.vrtcdn.be/klara-high.mp3",
        aac_url="http://icecast.vrtcdn.be/klara.aac",
        logo_url="https://images.vrt.be/orig/2025/06/19/1434fa63-eb65-4f65-b465-26919982d6fc.png",
        tagline="Blijf verwonderd",
    ),
    VrtStation(
        id="klara-continuo",
        name="Klara Continuo",
        stream_url="http://icecast.vrtcdn.be/klaracontinuo-high.mp3",
        aac_url="http://icecast.vrtcdn.be/klaracontinuo.aac",
        logo_url="https://images.vrt.be/orig/2025/01/14/7be19ff7-11e4-4f37-95f3-fc7eec0b2e90.png",
        tagline="Non-stop klassieke muziek",
    ),
    VrtStation(
        id="studio-brussel",
        name="Studio Brussel",
        stream_url="http://icecast.vrtcdn.be/stubru-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru.aac",
        logo_url="https://images.vrt.be/orig/2023/12/08/a6d153f0-95cb-11ee-b483-02b7b76bf47f.png",
        tagline="Life is Music",
    ),
    VrtStation(
        id="stubru-tijdloze",
        name="StuBru De Tijdloze",
        stream_url="http://icecast.vrtcdn.be/stubru_tijdloze-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_tijdloze.aac",
        logo_url="https://images.vrt.be/orig/2025/06/19/a8150bd0-4af0-4c4f-bdaf-193f579adba6.png",
        tagline="Altijd en overal de beste Tijdloze muziek",
    ),
    VrtStation(
        id="stubru-bruut",
        name="StuBru Zware Gitaren",
        stream_url="http://icecast.vrtcdn.be/stubru_bruut-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_bruut.aac",
        logo_url="https://images.vrt.be/orig/2023/11/09/832ffc6f-7ee3-11ee-91d7-02b7b76bf47f.png",
        tagline="Alleen maar stevige gitaren",
    ),
    VrtStation(
        id="stubru-de-jaren-nul",
        name="StuBru De Jaren Nul",
        stream_url="http://icecast.vrtcdn.be/stubru_dejarennul-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_dejarennul.aac",
        logo_url="https://images.vrt.be/orig/2024/02/01/83631b8f-c0e8-11ee-b483-02b7b76bf47f.png",
        tagline=None,
    ),
    VrtStation(
        id="stubru-vuurland",
        name="StuBru Vuurland",
        stream_url="http://icecast.vrtcdn.be/stubru_tgs-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_tgs.aac",
        logo_url="https://images.vrt.be/orig/2023/11/02/3b4f9c73-797d-11ee-91d7-02b7b76bf47f.png",
        tagline=None,
    ),
    VrtStation(
        id="stubru-untz",
        name="StuBru UNTZ",
        stream_url="http://icecast.vrtcdn.be/stubru_untz-high.mp3",
        aac_url="http://icecast.vrtcdn.be/stubru_untz.aac",
        logo_url="https://images.vrt.be/orig/2023/11/02/d1a04b27-797d-11ee-91d7-02b7b76bf47f.png",
        tagline="The party never stops",
    ),
    VrtStation(
        id="mnm",
        name="MNM",
        stream_url="http://icecast.vrtcdn.be/mnm-high.mp3",
        aac_url="http://icecast.vrtcdn.be/mnm.aac",
        logo_url="https://images.vrt.be/orig/2025/06/19/f0be1c65-3f98-43d1-b962-ded0f3bca602.png",
        tagline="Music and More",
    ),
    VrtStation(
        id="mnm-hits",
        name="MNM Hits",
        stream_url="http://icecast.vrtcdn.be/mnm_hits-high.mp3",
        aac_url="http://icecast.vrtcdn.be/mnm_hits.aac",
        logo_url="https://images.vrt.be/orig/2024/08/30/7026d62c-1e03-4906-9895-fb030fd52e3a.png",
        tagline="Music and More - The Hits",
    ),
    VrtStation(
        id="ketnet-hits",
        name="Ketnet Hits",
        stream_url="http://icecast.vrtcdn.be/ketnetradio-high.mp3",
        aac_url="http://icecast.vrtcdn.be/ketnetradio.aac",
        logo_url="https://images.vrt.be/orig/2024/10/09/8002f6df-dce0-4c05-9dad-ce9d7081832a.png",
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
    # Mirrors the web player's own client version. Refresh from the `x-vrt-client-version`
    # request header the vrtmax.be player sends (visible in the browser network tab).
    "x-vrt-client-version": "1.5.15",
}
GRAPHQL_TIMEOUT = aiohttp.ClientTimeout(total=25)
# Ceiling on catalogue requests per second, so no code path can burst against VRT.
REQUEST_RATE_LIMIT = 5

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

# VRT's image CDN serves renditions as "/w<width>hx/". 1280px comfortably covers the
# largest artwork the interface shows, at a fraction of the original's size.
IMAGE_RENDITION = "/w1280hx/"
