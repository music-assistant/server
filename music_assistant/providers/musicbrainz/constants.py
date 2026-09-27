"""Shared constants for the MusicBrainz provider."""

from __future__ import annotations

from music_assistant_models.enums import AlbumType, LinkType, MediaType, ProviderFeature

LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

# A recording search lists only a few of the releases a song appeared on, and for a much
# reissued song those can all be reissues. The release group knows when it was first
# released, but a group that predates the listed releases by only a few years is usually
# just a single issued ahead of its album or a regional edition, where the listed release
# is the safer answer. Only a wider gap means the search saw nothing but reissues.
# Measured against hand-dated songs: a smaller gap corrects as often as it misleads.
MIN_FIRST_RELEASE_CORRECTION_YEARS = 5

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.RECOMMENDATIONS,
}

# Mapping from MusicBrainz URL relation "type" slug to our LinkType enum.
# See https://musicbrainz.org/relationships/artist-url for the full set.
URL_RELATION_TYPE_MAPPING: dict[str, LinkType] = {
    "wikipedia": LinkType.WIKIPEDIA,
    "allmusic": LinkType.ALLMUSIC,
    "last.fm": LinkType.LASTFM,
    "official homepage": LinkType.WEBSITE,
}

# Social network relations use a single MB type but multiple destinations,
# so we sniff the URL host to pick a more specific LinkType.
SOCIAL_HOST_MAPPING: tuple[tuple[str, LinkType], ...] = (
    ("facebook.com", LinkType.FACEBOOK),
    ("instagram.com", LinkType.INSTAGRAM),
    ("tiktok.com", LinkType.TIKTOK),
    ("twitter.com", LinkType.TWITTER),
    ("x.com", LinkType.TWITTER),
)

# The MusicBrainz entity a media type identifies with, as named in URL relations.
URL_RELATION_ENTITY: dict[MediaType, str] = {
    MediaType.ARTIST: "artist",
    MediaType.ALBUM: "release",
    MediaType.TRACK: "recording",
}

# Provider domains whose items MusicBrainz links to by a canonical share URL, in the
# order they are reverse-looked up: the most widely linked services first.
REVERSE_URL_DOMAINS: tuple[str, ...] = ("spotify", "deezer", "tidal", "apple_music", "ytmusic")

# Bounds on the requests one identity resolution spends per lookup leg.
MAX_REVERSE_URL_LOOKUPS = 3
MAX_REF_ITEMS = 3
MAX_BARCODE_DETAIL_FETCHES = 2
# MusicBrainz' per-request maximum: a release group with more editions than fit on one
# page is not identified at all, so the page is as large as it can be.
RELEASE_GROUP_BROWSE_LIMIT = 100

# A recording's length may deviate this much from the track's duration and still be it.
RECORDING_LENGTH_TOLERANCE_MS = 8000

# MusicBrainz release group types to album types; a secondary type (in this order of
# precedence) overrides the primary type.
SECONDARY_TYPE_MAPPING: dict[str, AlbumType] = {
    "Compilation": AlbumType.COMPILATION,
    "Soundtrack": AlbumType.SOUNDTRACK,
    "Live": AlbumType.LIVE,
}
PRIMARY_TYPE_MAPPING: dict[str, AlbumType] = {
    "Album": AlbumType.ALBUM,
    "Single": AlbumType.SINGLE,
    "EP": AlbumType.EP,
}
