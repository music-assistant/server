"""
Handles the naming of objects for the BBC Sounds provider.

This might seem a bit much to have its own module, but the many-to-many mapping of
auntie-sounds objects to Music Assistant objects, as well as multiple ways to consume
them, and also display then, requires a consistent interface for naming things.
"""

import unicodedata
from contextlib import suppress
from datetime import date, datetime
from enum import Flag, auto

from auntie_sounds import SCHEDULE_TIMEZONE
from auntie_sounds.models import (
    LiveStation,
    PlayableItem,
    RadioClip,
    Station,
    StationSearchResult,
)

import music_assistant.helpers.datetime as dt

UNKNOWN_TITLE = "Unknown programme"
UNKNOWN_STATION = "Unknown station"
LIVE_MARKER = "«LIVE»"
REWOUND_MARKER = "«Rewound»"
UNPREFIXED_STATIONS = frozenset({"CBeebies Radio"})
TITLE_END_PUNCTUATION = ("!", "?", ".", ":")
# Networks that name no station, e.g. the BBC News website's clips, which every other
# title in a BBC app would share
GENERIC_NETWORKS = frozenset({"bbc_webonly"})
STATION_PREFIX = "BBC"


class TitleParts(Flag):
    """
    The parts of a programme's title, combined into the form each place shows.

    A title reads ``{marker} {start} {brand} • {series} • {episode}: {synopsis} · {date}``,
    with each part left out unless its flag is set and the programme has it. The date
    goes last, as the BBC Sounds app shows it, so it doesn't break up the sentence.
    """

    ON_AIR = auto()
    ON_AIR_REWIND = auto()
    START = auto()
    # The programme or podcast, e.g. Today or Intrigue
    BRAND = auto()
    # The season, which only podcasts with seasons have
    SERIES = auto()
    # The episode's own title, e.g. Business news
    EPISODE = auto()
    DATE = auto()
    SYNOPSIS = auto()

    # Search results, collections, recommendations and the player
    LISTING = BRAND | EPISODE | DATE | SYNOPSIS
    # A station's schedule for one day, where the folder gives the date. A list row has
    # one line, so the synopsis is left to the item's description.
    SCHEDULE = START | BRAND | EPISODE
    # A podcast's episode list, where the podcast gives the brand
    PODCAST = SERIES | EPISODE | DATE | SYNOPSIS
    # A curated playlist, e.g. the latest news, which mixes episodes with clips that only
    # have a headline. Its items are all recent, so a date and synopsis add little.
    PLAYLIST = BRAND | EPISODE
    # A programme on air now, in the catch-up folder and the queue
    LIVE = ON_AIR | SCHEDULE
    # Now playing between tracks, where the programme is on now so needs no time
    STATION = BRAND | EPISODE | SYNOPSIS
    # Now playing a programme played from live rewind, at the live edge
    LIVE_EDGE = ON_AIR | STATION
    # Now playing a programme played from live rewind, behind the live edge
    LIVE_REWIND = ON_AIR_REWIND | STATION


def programme_title(item: PlayableItem, parts: TitleParts = TitleParts.LISTING) -> str:
    """
    Return the display title of a programme.

    For example ``06:30 Today • Business news: News and current affairs``.

    :param item: A schedule item, radio show or podcast episode.
    :param parts: The parts to include, normally one of the named combinations.
    """
    titles = item.titles

    # A podcast with seasons has a third title, and then secondary is the season
    # A live station's tertiary title is the date and its secondary is the time slot
    has_season = bool(titles.tertiary) and not isinstance(item, LiveStation)

    episode = titles.entity_title or (titles.tertiary if has_season else titles.secondary)

    # A clip's titles often hold only its headline, so say where it was published instead
    brand = programme_source(item) if isinstance(item, RadioClip) else titles.primary

    # Prevent adding the brand (station/network) to the clip title if it already
    # starts with it, and the following character isn't alphanumeric.
    # E.g. "BBC Four" Matches "BBC Four: Title", but not "BBC Fourth in recent poll"
    if (
        TitleParts.EPISODE in parts
        and brand
        and episode
        and episode.startswith(brand)
        and not episode[len(brand) : len(brand) + 1].isalnum()
    ):
        brand = None
    names = [
        name
        for flag, name in (
            (TitleParts.BRAND, brand),
            (TitleParts.SERIES, titles.secondary if has_season else None),
            (TitleParts.EPISODE, episode),
        )
        if flag in parts and name
    ]
    title = " • ".join(names) or UNKNOWN_TITLE

    if TitleParts.START in parts and item.start:
        title = f"{_to_time(item.start)} {title}"
    if TitleParts.ON_AIR in parts:
        title = f"{LIVE_MARKER} {title}"
    if TitleParts.ON_AIR_REWIND in parts:
        title = f"{REWOUND_MARKER} {title}"
    if TitleParts.SYNOPSIS in parts and item.synopses and item.synopses.short:
        # A title that ends in its own punctuation, e.g. "Up Against The Clock!", needs no colon
        separator = " " if title.endswith(TITLE_END_PUNCTUATION) else ": "
        title += f"{separator}{item.synopses.short}"
    if TitleParts.DATE in parts and (when := item.start or release_date(item)):
        # A full stop straight before the date's dot reads oddly, but an ellipsis stays
        if title.endswith(".") and not title.endswith(".."):
            title = title.removesuffix(".")
        title += f" · {_to_date(when)}"
    return title


def release_date(item: PlayableItem) -> datetime | None:
    """Return the release date of an item, or when it became available if it has none."""
    for details, key in ((item.release, "date"), (item.availability, "from")):
        if details and (value := details.get(key)):
            with suppress(TypeError, ValueError):
                return dt.from_iso_string(value)
    return None


def schedule_date_title(day: date, today: date) -> str:
    """Return a folder name for a schedule date, e.g. "Today" or "Sunday 8th November"."""
    match (day - today).days:
        case 0:
            return "Today"
        case -1:
            return "Yesterday"
        case 1:
            return "Tomorrow"
    return f"{day:%A} {day.day}{_ordinal(day.day)} {day:%B}"


def station_name(station: Station | LiveStation | StationSearchResult) -> str:
    """Return a station name, e.g. Radio 4, without the BBC prefix."""
    match station:
        case Station():
            name = station.title
        case StationSearchResult():
            name = station.station_name
        case _:
            name = station.network.short_title if station.network else None
    return name or UNKNOWN_STATION


def full_station_name(
    *,
    station: Station | LiveStation | StationSearchResult | None = None,
    station_name_string: str | None = None,
) -> str:
    """
    Return a full station name with the BBC prefix, if appropriate.

    BBC refers to its own stations without the BBC prefix, but in the context of a list
    of radio stations in Music Assistant, "BBC Radio 1" is more useful as a display name
    than "Radio 1".

    Can be called with a station object, or an existing station name.

    e.g. CBeebies, BBC Radio Four
    """
    if station is not None and station_name_string is not None:
        raise TypeError("full_station_name() takes station or station_name_string, not both")
    if station is not None:
        name = station_name(station)
    elif station_name_string is not None:
        name = station_name_string
    else:
        raise TypeError("full_station_name() requires station or station_name_string")

    if name == UNKNOWN_STATION:
        return name
    if (
        name.startswith(STATION_PREFIX)
        or name in UNPREFIXED_STATIONS
        or not _is_latin_script(name)  # These titles already have the equivalent of 'BBC' in
    ):
        return name
    return f"{STATION_PREFIX} {name}"


def station_programme_title(station: LiveStation | Station) -> str | None:
    """Return what a station is playing now, e.g. "07:00 - 10:30 • Radio 1 Breakfast"."""
    titles = station.titles
    if not titles.primary:
        return None
    return " • ".join(filter(None, (titles.secondary, titles.primary)))


def programme_source(item: PlayableItem) -> str | None:
    """Return the programme an item is from, else its station, if either says anything."""
    if item.container and item.container.title:
        return item.container.title
    if (
        item.network
        and item.network.id not in GENERIC_NETWORKS
        and item.network.short_title is not None
    ):
        return full_station_name(station_name_string=item.network.short_title)
    return None


def _is_latin_script(name: str) -> bool:
    return all(unicodedata.name(char, "").startswith("LATIN") for char in name if char.isalpha())


def _ordinal(day: int) -> str:
    """Return the English ordinal suffix for a day of the month."""
    if 4 <= day <= 20 or 24 <= day <= 30:
        return "th"
    return ("st", "nd", "rd")[day % 10 - 1]  # codespell:ignore


def _to_time(when: datetime) -> str:
    """Return the UK time of day, e.g. 06:30, as the BBC schedules in UK time."""
    return f"{when.astimezone(SCHEDULE_TIMEZONE):%H:%M}"


def _to_date(when: datetime) -> str:
    """Return the UK date as the BBC Sounds app writes it, e.g. 12 Oct 2026."""
    day = when.astimezone(SCHEDULE_TIMEZONE)
    return f"{day.day} {day:%b %Y}"
