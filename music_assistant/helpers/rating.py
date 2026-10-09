"""
Translate ratings embedded in file tags into Music Assistant's favorite state.

Ratings live in file tags in several formats and scales, while Music Assistant's
data model carries only the tri-state ``favorite`` flag. This module owns that
translation so every provider that imports ratings maps them the same way, and
so a threshold in a provider's settings always means the same thing.

Every mapping here is monotonic: a higher value in the file can never produce a
lower rating. That is why the values taggers write are not mixed into a single
lookup -- taggers disagree about what a given byte means, and mixing them makes
a higher byte mean fewer stars. The scale a library was tagged with is an
explicit setting instead of a guess made per file.

Normalization is deliberately separate from the flag mapping:

* :func:`popm_to_rating` and :func:`tag_value_to_rating` turn a format's own
  scale into a neutral rating,
* :func:`favorite_from_rating` turns a neutral rating into the tri-state flag.

Only the read direction exists today. A provider that lets the rating be changed
from inside Music Assistant needs the inverse (a rating back into a tag value),
and somewhere to persist a rating the file does not carry, so that counterpart
belongs here beside the functions above.
"""

from __future__ import annotations

# Ratings are normalized to 0.0-10.0, the same scale the Plex provider exposes in
# its configuration, so a threshold means the same thing in every provider.
MAX_RATING: float = 10.0

_MAX_STARS: float = 5.0

# How the POPM byte is read. ID3's popularimeter is a single byte and taggers
# disagree about what the values mean, so this is chosen once per library.
POPM_SCALE_WINDOWS: str = "windows"
POPM_SCALE_ITUNES: str = "itunes"

# Windows Media Player and Explorer write 1/64/128/196/255 and read these bands,
# which also covers MediaMonkey's 23/64/118/186/252 and the half star values in
# between. See: https://en.wikipedia.org/wiki/ID3#ID3v2_star_rating_tag_issue
_POPM_BANDS_WINDOWS: tuple[tuple[int, int, float], ...] = (
    (1, 31, 1.0),
    (32, 95, 2.0),
    (96, 159, 3.0),
    (160, 223, 4.0),
    (224, 255, 5.0),
)

# iTunes squeezes five stars into 20/40/60/80/100, so its bands sit lower.
# Reading those values with the bands above turns an iTunes four star rating (80)
# into two stars, which is why the scale is a setting rather than a guess.
_POPM_BANDS_ITUNES: tuple[tuple[int, int, float], ...] = (
    (1, 29, 1.0),
    (30, 49, 2.0),
    (50, 69, 3.0),
    (70, 89, 4.0),
    (90, 255, 5.0),
)

_POPM_BANDS: dict[str, tuple[tuple[int, int, float], ...]] = {
    POPM_SCALE_WINDOWS: _POPM_BANDS_WINDOWS,
    POPM_SCALE_ITUNES: _POPM_BANDS_ITUNES,
}

# How the Vorbis RATING field and the MP4 RATING tag are read: a 0-100 value
# (foobar2000, MusicBee, MediaMonkey) or a 1-5 star value.
TAG_SCALE_PERCENT: str = "percent"
TAG_SCALE_STARS: str = "stars"

_PERCENTAGE_MAX: float = 100.0


def popm_to_rating(popm_rating: int, scale: str = POPM_SCALE_WINDOWS) -> float | None:
    """
    Convert an ID3 POPM byte to a normalized rating.

    :param popm_rating: The POPM frame rating, 0-255 (0 means unrated/unknown).
    :param scale: The POPM scale to read the byte with, see :data:`POPM_SCALE_WINDOWS`.
    """
    if popm_rating <= 0:
        return None
    for lower, upper, stars in _POPM_BANDS.get(scale, _POPM_BANDS_WINDOWS):
        if lower <= popm_rating <= upper:
            return (stars / _MAX_STARS) * MAX_RATING
    return None


def tag_value_to_rating(value: float, scale: str = TAG_SCALE_PERCENT) -> float | None:
    """
    Convert a Vorbis RATING or MP4 RATING value to a normalized rating.

    :param value: The rating as written in the file, 0-100 or 0-5.
    :param scale: The scale to read the value with, see :data:`TAG_SCALE_PERCENT`.
    """
    if value <= 0:
        return None
    if scale == TAG_SCALE_STARS:
        return (min(value, _MAX_STARS) / _MAX_STARS) * MAX_RATING
    return (min(value, _PERCENTAGE_MAX) / _PERCENTAGE_MAX) * MAX_RATING


def favorite_from_rating(
    rating: float | None,
    *,
    favorite_threshold: float,
    dislike_threshold: float,
) -> bool | None:
    """
    Derive the tri-state favorite flag from a normalized rating.

    Returns None when the file carries no rating, or one that sits in the neutral
    band between the two thresholds.

    :param rating: The normalized rating, 0.0-10.0, or None when unrated.
    :param favorite_threshold: Minimum rating to consider the item a favorite.
    :param dislike_threshold: Maximum rating to consider the item a dislike.
    """
    if rating is None:
        return None
    if rating >= favorite_threshold:
        return True
    if rating <= dislike_threshold:
        return False
    return None
