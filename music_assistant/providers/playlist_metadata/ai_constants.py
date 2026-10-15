"""Constants for the AI generated artwork and descriptions of the Playlist Metadata provider."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

CONF_IMAGE_ENGINE: Final = "ai_image_engine"
CONF_TEXT_ENGINE: Final = "ai_text_engine"
CONF_USE_PROMPT_ENGINE: Final = "ai_use_prompt_engine"
CONF_DESCRIPTIONS: Final = "ai_descriptions"
CONF_STYLE: Final = "ai_style"
CONF_CUSTOM_PROMPT: Final = "ai_custom_prompt"
CONF_TITLE_OVERLAY: Final = "ai_title_overlay"
CONF_PLAYLISTS: Final = "ai_playlists"
CONF_EXTRA_PLAYLISTS: Final = "ai_extra_playlists"
CONF_MAX_PER_HOUR: Final = "ai_max_per_hour"
CONF_ACTION_REGENERATE: Final = "ai_regenerate"
CONF_PREVIEW_PLAYLIST: Final = "ai_preview_playlist"
CONF_PREVIEW_STYLE: Final = "ai_preview_style"
CONF_PREVIEW_CUSTOM_PROMPT: Final = "ai_preview_custom_prompt"
CONF_ACTION_PREVIEW: Final = "ai_preview"
CONF_PREVIEW_IMAGE: Final = "ai_preview_image"
CONF_PREVIEW_DESCRIPTION: Final = "ai_preview_description"

# the value of the provider's artwork template that has the covers generated with AI
TEMPLATE_AI: Final = "ai_generated"

PLAYLISTS_SMART: Final = "smart"
PLAYLISTS_ALL: Final = "all"

DEFAULT_MAX_PER_HOUR: Final = 30

# the generated covers share the collage directory, told apart by this file name prefix
IMAGE_PREFIX: Final = "ai_"
IMAGE_EXTENSION: Final = ".jpg"
CANVAS_SIZE: Final = 1024
# an overlaid title takes at most this many lines; past that the font shrinks, down to
# this fraction of its size, before more lines are allowed
MAX_TITLE_LINES: Final = 3
MIN_TITLE_SCALE: Final = 0.4
# previews are sent to the settings page inline, so they are kept small
PREVIEW_SIZE: Final = 512
# an image larger than this is refused before it is decoded, as decoding takes memory per pixel
MAX_SOURCE_PIXELS: Final = 4096 * 4096
# the preview covers generated at the same time, to go easy on the image engine
PREVIEW_CONCURRENCY: Final = 2

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
# the failed attempts of a cover or description, kept until it changes
CACHE_CATEGORY_FAILURES: Final = 2
# a failed cover or description is tried again after these delays, and after the last one
# only once something that shapes it changes (or everything is regenerated)
FAILURE_BACKOFF_SECONDS: Final = (3600, 6 * 3600, 24 * 3600)
CACHE_KEY_GENERATION: Final = "generation"
# the times of the recent generations, so the hourly limit survives a reload or restart
CACHE_KEY_GENERATED_AT: Final = "generated_at"
CACHE_KEY_DESCRIBED_AT: Final = "described_at"
# a refresh shortly after loading picks up changed settings and playlists added meanwhile
CATCH_UP_DELAY_SECONDS: Final = 120
CACHE_KEEP_SECONDS: Final = 10 * 365 * 24 * 3600
# the prompt asks for 140 characters, this only cuts off a model that ignores that
MAX_DESCRIPTION_CHARS: Final = 200
# the listener's most-played tracks by the playlist's artists, to flavor its description
MAX_FAVORITES: Final = 6

# separates the title from the artists in a seed label, as the smart playlist provider writes it
SEED_SEPARATOR: Final = " \u2013 "


class Style(StrEnum):
    """The artwork style presets."""

    MUSIC_ASSISTANT = "music_assistant"
    NEON = "neon"
    BOLD = "bold"
    SOFT_GRADIENT = "soft_gradient"
    RISOGRAPH = "risograph"
    GOUACHE = "gouache"
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
    Style.BOLD: StylePreset(
        art=(
            "Style: a bold editorial playlist cover. One striking full-bleed image (stylized "
            "photo or bold illustration) with a vivid duotone or saturated color wash that sets "
            "the mood, simple uncluttered composition, cohesive with a series of playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, set large in a bold '
            "modern geometric sans-serif (like Circular or Gotham Black), tight letter "
            "spacing, clean and crisp, high contrast against the image, placed confidently in the "
            "upper-left or lower-left area."
        ),
        title_area="upper-left area",
        layout=TitleLayout.TOP_LEFT_LARGE,
    ),
    Style.MUSIC_ASSISTANT: StylePreset(
        art=(
            "Style: a Music Assistant playlist cover. Modern, clean and full of vibe: a rich, "
            "atmospheric gradient of two to three colors chosen specifically for this playlist's "
            "mood and genre, so covers in a series each get their own distinct palette (do not "
            "default to purple and orange), smooth and luminous with a soft light bloom and fine "
            "film grain. The hero is a detailed white line-art spot illustration that symbolizes "
            "the playlist's theme, like the icons on Music Assistant's genre tiles: one main, "
            "clearly recognizable object (an instrument, vehicle, object or scene) with a few "
            "related details such as sparkles, sound lines, stars or motion lines, drawn with "
            "thick and thin rounded white strokes, with a few accents filled in one bright color "
            "from the palette. It is drawn as an extreme close-up, so large that it does not fit "
            "on the cover: it fills the upper and right part of the square and runs off the top "
            "and right edges, cropped by them like a zoomed-in detail. The illustration stays "
            "within the upper and right two thirds of the square; the lower-left area, where the "
            "title goes, is plain gradient with no lines or details at all, gently darkening "
            "there. Cohesive with a series of playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, in a bold, clean modern '
            "sans-serif (like Inter Bold), white, crisp and perfectly legible, large, in the "
            "clear lower-left corner, with nothing drawn behind or across it."
        ),
        title_area="lower-left area",
        layout=TitleLayout.BOTTOM_LEFT_SMALL,
    ),
    Style.NEON: StylePreset(
        art=(
            "Style: a Music Assistant playlist cover. A night scene that captures the playlist, "
            "drawn as glowing neon light lines and long-exposure light trails over a nearly black "
            "background, like a neon sign or light painting: the main subject outlined in sky "
            "blue #03A9F4 neon with one second neon color from the mood, soft bloom, reflections "
            "and haze. Electric, nocturnal, minimal, cohesive with a series of playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, as a crisp white '
            "neon-tube script or clean sans-serif sign, centered near the bottom."
        ),
        title_area="lower area",
        layout=TitleLayout.BOTTOM_LEFT_SMALL,
    ),
    Style.SOFT_GRADIENT: StylePreset(
        art=(
            "Style: a soft gradient editorial playlist cover. A soft, blurred, vivid mesh gradient "
            "of two to four rich colors that flow into each other, with an optional subtle, "
            "out-of-focus hint of the scene, smooth and luminous with fine grain, minimal and "
            "elegant, cohesive with a series of playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, small, in white, set in '
            "a clean bold sans-serif (like Inter or Helvetica Neue), in the bottom-left corner over a "
            "subtle dark vignette."
        ),
        title_area="bottom-left corner",
        layout=TitleLayout.BOTTOM_LEFT_SMALL,
    ),
    Style.RISOGRAPH: StylePreset(
        art=(
            "Style: a Music Assistant playlist cover. A two-color risograph print: a bold "
            "illustration of a scene that captures the playlist, printed in sky blue #03A9F4 and "
            "one fluorescent warm ink (pink, orange or yellow, matching the mood) on off-white "
            "paper, with visible halftone dots, slight ink misregistration, overprint where the "
            "colors overlap and paper grain. Indie, handmade, joyful, cohesive with a series of "
            "playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, large, in a chunky '
            "condensed sans-serif printed in the same ink, at the top."
        ),
        title_area="top band",
        layout=TitleLayout.TOP_LEFT_LARGE,
    ),
    Style.GOUACHE: StylePreset(
        art=(
            "Style: a Music Assistant playlist cover. A warm, painterly editorial illustration in "
            "gouache: a scene that captures the playlist with simplified shapes, visible brush "
            "texture and a rich, harmonious palette led by the mood of the playlist, with a touch "
            "of sky blue #03A9F4 somewhere in the scene (the sky, water, a light, clothing). "
            "Feels like a magazine cover illustration, nostalgic and human, cohesive with a "
            "series of playlist covers."
        ),
        typography=(
            'Typography: the playlist title "{title}" as the only text, in a clean bold '
            "sans-serif, white or cream, in a calm area at the top."
        ),
        title_area="top area",
        layout=TitleLayout.TOP_LEFT_LARGE,
    ),
}

# custom templates have no typography of their own, so an overlay uses the Bold layout
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

DESCRIPTION_PROMPT: Final = """You write playlist descriptions for the editorial team of a music streaming service.
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
