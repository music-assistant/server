"""Shared constants for the MusicBrainz provider."""

from __future__ import annotations

from music_assistant_models.enums import LinkType, ProviderFeature

LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

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
