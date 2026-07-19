"""
TypedDict models for the official TIDAL API (openapi.tidal.com/v2).

DO NOT EDIT BY HAND. This file is generated from the vendored OpenAPI spec.
Regenerate with: python scripts/tidal_openapi/generate_models.py
"""
# ruff: noqa

from typing import Literal, NotRequired, TypedDict


class Copyright(TypedDict):
    text: str


class ExternalLinkMeta(TypedDict):
    type: Literal[
        "TIDAL_SHARING",
        "TIDAL_USER_SHARING",
        "TIDAL_AUTOPLAY_ANDROID",
        "TIDAL_AUTOPLAY_IOS",
        "TIDAL_AUTOPLAY_WEB",
        "TWITTER",
        "FACEBOOK",
        "INSTAGRAM",
        "TIKTOK",
        "SNAPCHAT",
        "OFFICIAL_HOMEPAGE",
        "CASHAPP_CONTRIBUTIONS",
        "ARTIST_CLAIM_PROVIDER_REDIRECT",
        "STRIPE_AUTHORIZATION_REDIRECT",
        "SQUARE_AUTHORIZATION_REDIRECT",
    ]


class ExternalLink(TypedDict):
    href: str
    meta: ExternalLinkMeta


class PlaylistsAttributes(TypedDict):
    accessType: Literal["PUBLIC", "UNLISTED"]
    bounded: bool
    createdAt: str
    description: NotRequired[str]
    duration: NotRequired[str]
    externalLinks: list[ExternalLink]
    lastModifiedAt: str
    name: str
    numberOfFollowers: int
    numberOfItems: NotRequired[int]
    playlistType: Literal["EDITORIAL", "USER", "MIX", "ARTIST"]


class TracksAttributes(TypedDict):
    accessType: NotRequired[Literal["PUBLIC", "UNLISTED", "PRIVATE"]]
    ai: NotRequired[bool]
    availability: NotRequired[list[Literal["STREAM", "DJ", "STEM"]]]
    bpm: NotRequired[float]
    copyright: NotRequired[Copyright]
    createdAt: NotRequired[str]
    duration: str
    explicit: bool
    externalLinks: NotRequired[list[ExternalLink]]
    isrc: str
    key: Literal[
        "UNKNOWN",
        "C",
        "CSharp",
        "D",
        "Eb",
        "E",
        "F",
        "FSharp",
        "G",
        "Ab",
        "A",
        "Bb",
        "B",
    ]
    keyScale: Literal[
        "UNKNOWN",
        "MAJOR",
        "MINOR",
        "AEOLIAN",
        "BLUES",
        "DORIAN",
        "HARMONIC_MINOR",
        "LOCRIAN",
        "LYDIAN",
        "MIXOLYDIAN",
        "PENTATONIC_MAJOR",
        "PHRYGIAN",
        "MELODIC_MINOR",
        "PENTATONIC_MINOR",
    ]
    mediaTags: list[str]
    popularity: float
    spotlighted: NotRequired[bool]
    title: str
    toneTags: NotRequired[list[str]]
    version: NotRequired[str]


class AlbumsAttributes(TypedDict):
    accessType: NotRequired[Literal["PUBLIC", "UNLISTED", "PRIVATE"]]
    ai: NotRequired[bool]
    albumType: Literal["ALBUM", "EP", "SINGLE"]
    availability: NotRequired[list[Literal["STREAM", "DJ", "STEM"]]]
    barcodeId: str
    copyright: NotRequired[Copyright]
    createdAt: NotRequired[str]
    duration: str
    explicit: bool
    externalLinks: NotRequired[list[ExternalLink]]
    mediaTags: list[str]
    numberOfItems: int
    numberOfVolumes: int
    popularity: float
    releaseDate: NotRequired[str]
    title: str
    type: NotRequired[Literal["ALBUM", "EP", "SINGLE"]]
    version: NotRequired[str]


class ArtistsAttributes(TypedDict):
    contributionsEnabled: NotRequired[bool]
    contributionsSalesPitch: NotRequired[str]
    externalLinks: NotRequired[list[ExternalLink]]
    handle: NotRequired[str]
    name: str
    ownerType: NotRequired[Literal["LABEL", "USER", "MIXED"]]
    popularity: float
    spotlighted: NotRequired[bool]
