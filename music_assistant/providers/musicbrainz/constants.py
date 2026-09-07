"""Shared constants for the MusicBrainz provider."""

from __future__ import annotations

from music_assistant_models.enums import LinkType, ProviderFeature

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
