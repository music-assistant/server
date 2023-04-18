"""Models for the Radio Browser API."""
from __future__ import annotations

from datetime import datetime
from typing import cast

import pycountry
from awesomeversion import AwesomeVersion
from pydantic import BaseModel, Field, validator


class Stats(BaseModel):
    """Object holding the Radio Browser stats."""

    supported_version: int
    software_version: AwesomeVersion
    status: str
    stations: int
    stations_broken: int
    tags: int
    clicks_last_hour: int
    clicks_last_day: int
    languages: int
    countries: int


class Station(BaseModel):
    """Object information for a station from the Radio Browser."""

    bitrate: int
    change_uuid: str = Field(..., alias="changeuuid")
    click_count: int = Field(..., alias="clickcount")
    click_timestamp: datetime | None = Field(..., alias="clicktimestamp_iso8601")
    click_trend: int = Field(..., alias="clicktrend")
    codec: str
    country_code: str = Field(..., alias="countrycode")
    favicon: str
    latitude: float | None = Field(..., alias="geo_lat")
    longitude: float | None = Field(..., alias="geo_long")
    has_extended_info: bool
    hls: bool
    homepage: str
    iso_3166_2: str | None
    language: list[str]
    language_codes: list[str] = Field(..., alias="languagecodes")
    lastchange_time: datetime | None = Field(..., alias="lastchangetime_iso8601")
    lastcheckok: bool
    last_check_ok_time: datetime | None = Field(..., alias="lastcheckoktime_iso8601")
    last_check_time: datetime | None = Field(..., alias="lastchecktime_iso8601")
    last_local_check_time: datetime | None = Field(..., alias="lastlocalchecktime_iso8601")
    name: str
    ssl_error: int
    state: str
    uuid: str = Field(..., alias="stationuuid")
    tags: list[str]
    url_resolved: str
    url: str
    votes: int

    @validator("tags", "language", "language_codes", pre=True)
    @classmethod
    def _split(cls, value: str) -> list[str]:  # noqa: F841
        """Split comma separated string into a list of strings.

        Arguments:
            value: Comma separated string.

        Returns:
            List of strings.
        """
        return [item.strip() for item in value.split(",")]

    @property
    def country(self) -> str | None:
        """Return country name of this station.

        Returns:
            Country name or None if no country code is set.
        """
        if resolved_country := pycountry.countries.get(alpha_2=self.country_code):
            return cast(str, resolved_country.name)
        return None


class Country(BaseModel):
    """Object information for a Counbtry from the Radio Browser."""

    code: str
    name: str
    station_count: str = Field(..., alias="stationcount")

    @property
    def favicon(self) -> str:
        """Return the favicon URL for the country.

        Returns:
            URL to the favicon.
        """
        return f"https://flagcdn.com/256x192/{self.code.lower()}.png"


class Language(BaseModel):
    """Object information for a Language from the Radio Browser."""

    code: str | None = Field(..., alias="iso_639")
    name: str
    station_count: str = Field(..., alias="stationcount")

    @property
    def favicon(self) -> str | None:
        """Return the favicon URL for the language.

        Returns:
            URL to the favicon.
        """
        if self.code:
            return f"https://flagcdn.com/256x192/{self.code.lower()}.png"
        return None


class Tag(BaseModel):
    """Object information for a Tag from the Radio Browser."""

    name: str
    station_count: str = Field(..., alias="stationcount")
