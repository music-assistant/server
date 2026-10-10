"""
Translate ratings embedded in file tags into Music Assistant's favorite state.

Taggers disagree about the scale a rating is written in, so the scale a library
was tagged with is a provider setting and every mapping here is monotonic: a
higher value in the file never produces a lower rating.
"""

from __future__ import annotations

# Ratings are normalized to 0.0-10.0, the scale the Plex provider exposes in its
# configuration, so a threshold means the same thing in every provider.
MAX_RATING: float = 10.0

_MAX_STARS: float = 5.0

POPM_SCALE_WINDOWS: str = "windows"
POPM_SCALE_ITUNES: str = "itunes"

# Windows Media Player and Explorer write 1/64/128/196/255 and read these bands,
# which also covers MediaMonkey's 23/64/118/186/252.
_POPM_BANDS_WINDOWS: tuple[tuple[int, int, float], ...] = (
    (1, 31, 1.0),
    (32, 95, 2.0),
    (96, 159, 3.0),
    (160, 223, 4.0),
    (224, 255, 5.0),
)

# iTunes squeezes five stars into 20/40/60/80/100, so its bands sit lower. Read
# with the bands above, an iTunes four star rating (80) would be two stars.
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

TAG_SCALE_PERCENT: str = "percent"
TAG_SCALE_STARS: str = "stars"

_PERCENTAGE_MAX: float = 100.0


def popm_to_rating(popm_rating: int, scale: str = POPM_SCALE_WINDOWS) -> float | None:
    """
    Return the normalized rating for an ID3 POPM value.

    :param popm_rating: The POPM rating, 0-255. 0 means unrated and returns None.
    :param scale: The scale to read the value with, see :data:`POPM_SCALE_WINDOWS`.
    """
    if popm_rating <= 0:
        return None
    for lower, upper, stars in _POPM_BANDS.get(scale, _POPM_BANDS_WINDOWS):
        if lower <= popm_rating <= upper:
            return (stars / _MAX_STARS) * MAX_RATING
    return None


def tag_value_to_rating(value: float, scale: str = TAG_SCALE_PERCENT) -> float | None:
    """
    Return the normalized rating for a Vorbis RATING or MP4 RATING value.

    :param value: The value as written in the file. 0 means unrated and returns None.
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
    Return the favorite state for a normalized rating.

    :param rating: The normalized rating, 0.0-10.0, or None when unrated.
    :param favorite_threshold: Minimum rating to return True.
    :param dislike_threshold: Maximum rating to return False. A rating between the
        two thresholds returns None, as does an unrated item.
    """
    if rating is None:
        return None
    if rating >= favorite_threshold:
        return True
    if rating <= dislike_threshold:
        return False
    return None
