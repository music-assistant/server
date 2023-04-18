"""Asynchronous Python client for the Radio Browser API."""

from enum import Enum


class Order(str, Enum):
    """Enum holding the order types."""

    BITRATE = "bitrate"
    CHANGE_TIMESTAMP = "changetimestamp"
    CLICK_COUNT = "clickcount"
    CLICK_TIMESTAMP = "clicktimestamp"
    CLICK_TREND = "clicktrend"
    CODE = "code"
    CODEC = "codec"
    COUNTRY = "country"
    FAVICON = "favicon"
    HOMEPAGE = "homepage"
    LANGUAGE = "language"
    LAST_CHECK_OK = "lastcheckok"
    LAST_CHECK_TIME = "lastchecktime"
    NAME = "name"
    RANDOM = "random"
    STATE = "state"
    STATION_COUNT = "stationcount"
    TAGS = "tags"
    URL = "url"
    VOTES = "votes"


class FilterBy(str, Enum):
    """Enum holding possible filter by types for radio stations."""

    UUID = "byuuid"
    NAME = "byname"
    NAME_EXACT = "bynameexact"
    CODEC = "bycodec"
    CODEC_EXACT = "bycodecexact"
    COUNTRY = "bycountry"
    COUNTRY_EXACT = "bycountryexact"
    COUNTRY_CODE_EXACT = "bycountrycodeexact"
    STATE = "bystate"
    STATE_EXACT = "bystateexact"
    LANGUAGE = "bylanguage"
    LANGUAGE_EXACT = "bylanguageexact"
    TAG = "bytag"
    TAG_EXACT = "bytagexact"
