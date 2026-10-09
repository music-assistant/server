"""
Translate ratings embedded in file tags into Music Assistant's favorite state.

Ratings live in file tags in several formats and scales (ID3 ``POPM``, Vorbis
``RATING``, the MP4 ``rate`` atom), while Music Assistant's data model carries
only the tri-state ``favorite`` flag. This module owns that translation so every
provider that imports ratings maps them the same way and the thresholds in the
provider settings always mean the same thing.

Normalization is deliberately a separate step from the flag mapping:

* :func:`popm_to_rating` / :func:`percentage_to_rating` turn a format specific
  tag value into a neutral rating,
* :func:`favorite_from_rating` turns a neutral rating into the tri-state flag.

Only the read direction exists today, on purpose: it is the half that needs no
place to store a value the file itself does not carry. A provider that lets the
rating be changed from inside Music Assistant needs the inverse (a rating back
into a tag value) as well as somewhere to persist it, so that counterpart
belongs here next to the functions above rather than in a provider -- keeping
both directions in one module is the reason the two steps are split.
"""

from __future__ import annotations

# Ratings are normalized to 0.0-10.0, the same scale the Plex provider exposes in
# its configuration, so a threshold means the same thing in every provider.
MAX_RATING: float = 10.0

_MAX_STARS: float = 5.0

# ID3 POPM is a single byte (1-255, 0 means "unknown") and every tagger writes
# different values for the same star rating: Windows Media Player and Explorer
# write 1/64/128/196/255, iTunes writes 20/40/60/80/100, and MediaMonkey writes
# its own ladder (23/64/118/186/252 on recent versions).
#
# There is no mapping everyone agrees on. Microsoft's *read* bands (1-31 / 32-95
# / 96-159 / 160-223 / 224-255) are the most commonly cited, but they misread
# iTunes values badly -- an iTunes 4 star rating of 80 falls in the 2 star band
# -- which matters because iTunes is one of the taggers this feature exists for.
# So the byte is resolved to the nearest value the common taggers actually
# write instead. Real files carry those exact values, so a rating round-trips to
# the stars that were set, whichever of the three applications wrote it.
# See: https://en.wikipedia.org/wiki/ID3#ID3v2_star_rating_tag_issue
_POPM_ANCHORS: tuple[tuple[int, float], ...] = (
    (1, 1.0),
    (13, 1.0),
    (20, 1.0),
    (23, 1.0),
    (26, 1.0),
    (40, 2.0),
    (51, 2.0),
    (54, 2.0),
    (60, 3.0),
    (64, 2.0),
    (80, 4.0),
    (100, 5.0),
    (102, 3.0),
    (118, 3.0),
    (128, 3.0),
    (178, 4.0),
    (186, 4.0),
    (196, 4.0),
    (230, 5.0),
    (242, 5.0),
    (252, 5.0),
    (255, 5.0),
)

# Vorbis RATING and the MP4 rate atom are written as a 0-100 value by the taggers
# that support them (foobar2000, MusicBee, MediaMonkey), unlike ID3's 0-255 byte.
_PERCENTAGE_MAX: float = 100.0


def popm_to_rating(popm_rating: int) -> float | None:
    """
    Convert an ID3 POPM byte to a normalized rating.

    :param popm_rating: The POPM frame rating, 0-255 (0 means unrated/unknown).
    """
    if popm_rating <= 0:
        return None
    nearest = min(_POPM_ANCHORS, key=lambda anchor: abs(anchor[0] - popm_rating))
    return (nearest[1] / _MAX_STARS) * MAX_RATING


def percentage_to_rating(value: float) -> float | None:
    """
    Convert a 0-100 rating (Vorbis RATING, MP4 rate) to a normalized rating.

    :param value: The rating as written in the file, 0-100.
    """
    if value <= 0:
        return None
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
