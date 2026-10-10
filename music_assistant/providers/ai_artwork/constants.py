"""Constants for the AI Artwork provider."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

CONF_IMAGE_ENGINE: Final = "image_engine"
CONF_TEXT_ENGINE: Final = "text_engine"
CONF_USE_PROMPT_ENGINE: Final = "use_prompt_engine"
CONF_DESCRIPTIONS: Final = "generate_descriptions"
CONF_STYLE: Final = "style"
CONF_CUSTOM_PROMPT: Final = "custom_prompt"
CONF_TITLE_OVERLAY: Final = "title_overlay"
CONF_PLAYLISTS: Final = "playlists"
CONF_EXTRA_PLAYLISTS: Final = "extra_playlists"
CONF_KEEP_EXISTING_ARTWORK: Final = "keep_existing_artwork"
CONF_MAX_PER_HOUR: Final = "max_generations_per_hour"
CONF_ACTION_REGENERATE: Final = "regenerate"

PLAYLISTS_SMART: Final = "smart"
PLAYLISTS_ALL: Final = "all"

DEFAULT_MAX_PER_HOUR: Final = 30

# directory inside the storage path holding the generated covers
IMAGES_DIR: Final = "ai_artwork"
IMAGE_EXTENSION: Final = ".jpg"
CANVAS_SIZE: Final = 1024

# playlist_metadata's collage is regenerated on every refresh, so it never counts as artwork
# worth keeping over a generated cover
REPLACEABLE_IMAGE_DOMAINS: Final = frozenset({"playlist_metadata"})

# image models take a while, a slow one well over a minute
IMAGE_TIMEOUT_SECONDS: Final = 300
# a visual concept is a sentence or two, so this only has to cover a slow local model
PROMPT_TIMEOUT_SECONDS: Final = 60
# a reply this long is not the sentence or two that was asked for
MAX_CONCEPT_CHARS: Final = 600

# the tracks a playlist's context is drawn from, and how much of it ends up in the prompt
MAX_CONTEXT_TRACKS: Final = 500
MAX_CONTEXT_ITEMS: Final = 10
MAX_CONTEXT_GENRES: Final = 3

# the descriptions live in the cache, kept until the playlist (or the text engine) changes
CACHE_CATEGORY_DESCRIPTIONS: Final = 1
CACHE_KEY_GENERATION: Final = "generation"
CACHE_KEEP_SECONDS: Final = 10 * 365 * 24 * 3600
# the prompt asks for 140 characters, this only cuts off a model that ignores that
MAX_DESCRIPTION_CHARS: Final = 200
# the listener's most-played tracks by the playlist's artists, to flavor its description
MAX_FAVORITES: Final = 6

# separates the title from the artists in a seed label, as the smart playlist provider writes it
SEED_SEPARATOR: Final = " \u2013 "

# files written this recently may not be referenced by the library yet
ORPHAN_GRACE_SECONDS: Final = 300


class Style(StrEnum):
    """The artwork style presets."""

    MUSIC_ASSISTANT = "music_assistant"
    SPOTIFY = "spotify"
    APPLE_MUSIC = "apple_music"
    CUSTOM = "custom"


class TitleLayout(StrEnum):
    """Where and how the title overlay is drawn."""

    TOP_LEFT_LARGE = "top_left_large"
    CENTER = "center"
    BOTTOM_LEFT_SMALL = "bottom_left_small"


@dataclass(frozen=True, kw_only=True)
class StylePreset:
    """The prompt fragments and title overlay layout of a style."""

    art: str
    # how the image model should set the title, when it draws the title itself
    typography: str
    # the area to keep clear when the title is overlaid afterwards
    title_area: str
    layout: TitleLayout


BASE_PROMPT: Final = (
    'Create square album cover art for a music playlist called "{name}". {context}{concept} {style}'
)

DEFAULT_CONCEPT: Final = (
    "Capture the mood of the playlist name as a striking visual scene or concept "
    "(do not depict real artists)."
)
CONCEPT_FROM_ENGINE: Final = "Visual concept: {concept} (do not depict real artists)."

TITLE_RULES: Final = (
    "Spell the title exactly. No other text, subtitles, logos, watermarks or artist names."
)
NO_TEXT_RULES: Final = (
    "No text, letters, numbers, logos or watermarks anywhere in the image. Keep the {area} "
    "calm and uncluttered, a title will be set there afterwards."
)
FULL_BLEED: Final = (
    "Full-bleed: the art fills the entire square edge to edge, no border, frame, mockup or margins."
)

STYLE_PRESETS: Final[dict[Style, StylePreset]] = {
    Style.SPOTIFY: StylePreset(
        art=(
            "Style: a Spotify editorial playlist cover. One striking full-bleed image (stylized "
            "photo or bold illustration) with a vivid duotone or saturated color wash that sets "
            "the mood, simple uncluttered composition, cohesive with a series of playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, set large in a bold '
            "modern geometric sans-serif (like Spotify's Circular or Gotham Black), tight letter "
            "spacing, clean and crisp, high contrast against the image, placed confidently in the "
            "upper-left or lower-left area."
        ),
        title_area="upper-left area",
        layout=TitleLayout.TOP_LEFT_LARGE,
    ),
    Style.MUSIC_ASSISTANT: StylePreset(
        art=(
            "Style: a Music Assistant playlist cover. Abstract flowing sound waves, soft light "
            "trails and gentle geometric shapes in Music Assistant's brand blues (bright sky blue "
            "#03A9F4 fading into deep navy) on a dark background, with a subtle glow and depth, "
            "modern and calm, cohesive with a series of playlist covers. Let the waves take on "
            "the mood of the playlist through their rhythm and a single accent color."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, centered, set in a clean '
            "bold modern sans-serif (like Inter or Roboto), white, crisp and legible against the "
            "dark background."
        ),
        title_area="center",
        layout=TitleLayout.CENTER,
    ),
    Style.APPLE_MUSIC: StylePreset(
        art=(
            "Style: an Apple Music editorial playlist cover. A soft, blurred, vivid mesh gradient "
            "of two to four rich colors that flow into each other, with an optional subtle, "
            "out-of-focus hint of the scene, smooth and luminous with fine grain, minimal and "
            "elegant, cohesive with a series of playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, small, in white, set in '
            "a clean bold sans-serif (like SF Pro Display), in the bottom-left corner over a "
            "subtle dark vignette."
        ),
        title_area="bottom-left corner",
        layout=TitleLayout.BOTTOM_LEFT_SMALL,
    ),
}

# custom templates have no typography of their own, so an overlay uses the Spotify layout
CUSTOM_LAYOUT: Final = TitleLayout.TOP_LEFT_LARGE

DEFAULT_CUSTOM_PROMPT: Final = (
    'Create square album cover art for a music playlist called "{name}". {context}{concept} '
    "Style: a retro 1970s screen-printed poster with bold flat shapes and a limited warm palette. "
    "No text. Full-bleed, no border or frame."
)

CONCEPT_PROMPT: Final = (
    "You art-direct the covers of music playlists. In one or two sentences, describe a single "
    'striking visual scene or concept for the cover of a playlist called "{name}". {context}'
    "Describe only the imagery: no text, typography, logos or real people. "
    "Reply with only the description."
)

DESCRIPTION_PROMPT: Final = """You write playlist descriptions for Spotify's editorial team.
Write the description for a personal playlist called "{name}".
What defines it: {seeds}
The listener's most-played songs in this lane (use for flavor, mention at most one): {favorites}

Rules:
- One or two short sentences, 140 characters max in total.
- Confident, evocative and specific, like "Melodic house for the hours after midnight." or
  "Pop-punk you still know every word to. The Maine, The Rocket Summer and more."
- Those examples show the tone only: write your own wording, never reuse their phrases.
- You may name two or three artists from the seeds (only real artist names, not song titles).
- Never use these clichés: perfect for, turn it up, vibes, anthems, road trip, sing along,
  relive, feel-good, get the party started, journey.
- No emojis, hashtags or quotes. Reply with only the description."""
