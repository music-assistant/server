"""Asynchronous Python client for the Radio Browser API."""
from __future__ import annotations

import asyncio
import random
import socket
from dataclasses import dataclass
from typing import Any

import aiohttp
import async_timeout
import backoff
import pycountry
from aiodns import DNSResolver
from aiohttp import hdrs
from pydantic import parse_obj_as
from yarl import URL

from .const import FilterBy, Order
from .exceptions import (
    RadioBrowserConnectionError,
    RadioBrowserConnectionTimeoutError,
    RadioBrowserError,
)
from .models import Country, Language, Station, Stats, Tag


@dataclass
class RadioBrowser:
    """Main class for handling connections with the Radio Browser API."""

    user_agent: str

    request_timeout: float = 8.0
    session: aiohttp.client.ClientSession | None = None

    _close_session: bool = False
    _host: str | None = None

    @backoff.on_exception(backoff.expo, RadioBrowserConnectionError, max_tries=5, logger=None)
    async def _request(
        self,
        uri: str = "",
        method: str = hdrs.METH_GET,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Handle a request to the Radio Browser API.

        A generic method for sending/handling HTTP requests done against
        the Radio Browser API.

        Args:
            uri: Request URI, for example `stats`.
            method: HTTP method to use for the request.E.g., "GET" or "POST".
            params: Dictionary of data to send to the Radio Browser API.

        Returns:
            A Python dictionary (JSON decoded) with the response from the
            Radio Browser API.

        Raises:
            RadioBrowserConnectionError: An error occurred while communitcation with
                the Radio Browser API.
            RadioBrowserConnectionTimeoutError: A timeout occurred while communicating
                with the Radio Browser API.
            RadioBrowserError: Received an unexpected response from the
                Radio Browser API.
        """
        if self._host is None:
            resolver = DNSResolver()
            result = await resolver.query("_api._tcp.radio-browser.info", "SRV")
            random.shuffle(result)
            self._host = result[0].host

        url = URL.build(scheme="https", host=self._host, path="/json/").join(URL(uri))

        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._close_session = True

        if params:
            for key, value in params.items():
                if isinstance(value, bool):
                    params[key] = str(value).lower()
        try:
            async with async_timeout.timeout(self.request_timeout):
                print(method)
                print(url)
                print(params)
                response = await self.session.request(
                    method,
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/json",
                    },
                    params=params,
                    raise_for_status=True,
                )

            content_type = response.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                raise RadioBrowserError(response.status, {"message": await response.text()})
            return await response.json()

        except asyncio.TimeoutError as exception:
            self._host = None
            raise RadioBrowserConnectionTimeoutError(
                "Timeout occurred while connecting to the Radio Browser API"
            ) from exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            self._host = None
            raise RadioBrowserConnectionError(
                "Error occurred while communicating with the Radio Browser API"
            ) from exception

    async def stats(self) -> Stats:
        """Get Radio Browser service stats.

        Returns:
            A Stats object, with information about the Radio Browser API.
        """
        response = await self._request("stats")
        return Stats.parse_obj(response)

    async def station_click(self, *, uuid: str) -> None:
        """Register click on a station.

        Increase the click count of a station by one. This should be called
        every time when a user starts playing a stream to mark the stream more
        popular than others. Every call to this endpoint from the same IP
        address and for the same station only gets counted once per day.

        Args:
            uuid: UUID of the station.
        """
        await self._request(f"url/{uuid}")

    async def countries(
        self,
        *,
        hide_broken: bool = False,
        limit: int = 100000,
        offset: int = 0,
        order: Order = Order.NAME,
        reverse: bool = False,
    ) -> list[Country]:
        """Get list of available countries.

        Args:
            hide_broken: Do not count broken stations.
            limit: Limit the number of results.
            offset: Offset the results.
            order: Order the results.
            reverse: Reverse the order of the results.

        Returns:
            A Stats object, with information about the Radio Browser API.
        """
        countries = await self._request(
            "countrycodes",
            params={
                "hidebroken": hide_broken,
                "limit": limit,
                "offset": offset,
                "order": order.value,
                "reverse": reverse,
            },
        )

        for country in countries:
            country["code"] = country["name"]
            # https://github.com/frenck/python-radios/issues/19
            if country["name"] == "XK":
                country["name"] = "Kosovo"
            elif resolved_country := pycountry.countries.get(alpha_2=country["name"]):
                country["name"] = resolved_country.name

        # Because we enrichted the countries we need to re-order in this case
        if order == Order.NAME:
            countries.sort(key=lambda country: country["name"])

        return parse_obj_as(list[Country], countries)

    async def languages(
        self,
        *,
        hide_broken: bool = False,
        limit: int = 100000,
        offset: int = 0,
        order: Order = Order.NAME,
        reverse: bool = False,
    ) -> list[Language]:
        """Get list of available languages.

        Args:
            hide_broken: Do not count broken stations.
            limit: Limit the number of results.
            offset: Offset the results.
            order: Order the results.
            reverse: Reverse the order of the results.

        Returns:
            A list of Language objects.
        """
        languages = await self._request(
            "languages",
            params={
                "hidebroken": hide_broken,
                "offset": offset,
                "order": order.value,
                "reverse": reverse,
                "limit": limit,
            },
        )

        for language in languages:
            language["name"] = language["name"].title()

        return parse_obj_as(list[Language], languages)

    async def search(
        self,
        *,
        filter_by: FilterBy | None = None,
        filter_term: str | None = None,
        hide_broken: bool = False,
        limit: int = 100000,
        offset: int = 0,
        order: Order = Order.NAME,
        reverse: bool = False,
        name: str | None = None,
        name_exact: bool = False,
        country: str | None = "",
        country_exact: bool = False,
        # countrycode: str | None = None,
        # state: str | None = None,
        state_exact: bool = False,
        # language: str | None = None,
        language_exact: bool = False,
        # tag: str | None = None,
        tag_exact: bool = False,
        # codec: str | None = None,
        bitrate_min: int = 0,
        bitrate_max: int = 1000000,
    ) -> list[Station]:
        """Get list of radio stations.

        Args:
            hide_broken: Do not count broken stations.
            limit: Limit the number of results.
            offset: Offset the results.
            order: Order the results.
            reverse: Reverse the order of the results.
            name: Search by name.
            name_exact: Search by exact name.
            country: Search by country.
            country_exact: Search by exact country.
            countrycode:  Search by exact countrycode.
            state:  Search by state.
            state_exact:  Search by exact state.
            language:  Search by language.
            language_exact:  Search by exact language.
            tag:  Search by tag.
            tag_exact:  Search by exact tag.
            codec:  Search by codec.
            bitrate_min:  Search by minimum bitrate.
            bitrate_max:  Search by maximum bitrate.

        Returns:
            A list of Station objects.
        """
        uri = "stations/search"
        if filter_by is not None:
            uri = f"{uri}/{filter_by.value}"
            if filter_term is not None:
                uri = f"{uri}/{filter_term}"

        stations = await self._request(
            uri,
            params={
                "hidebroken": hide_broken,
                "offset": offset,
                "order": order.value,
                "reverse": reverse,
                "limit": limit,
                "name": name,
                "name_exact": name_exact,
                "country": country,
                "country_exact": country_exact,
                # "countrycode": countrycode,
                # "state": state,
                "state_exact": state_exact,
                # "language": language,
                "language_exact": language_exact,
                # "tag": tag,
                "tag_exact": tag_exact,
                # "codec": codec,
                "bitrate_min": bitrate_min,
                "bitrate_max": bitrate_max,
            },
        )
        return parse_obj_as(list[Station], stations)

    async def station(self, *, uuid: str) -> Station | None:
        """Get station by UUID.

        Args:
            uuid: UUID of the station.

        Returns:
            A  Station object if found.
        """
        stations = await self.stations(
            filter_by=FilterBy.UUID,
            filter_term=uuid,
            limit=1,
        )
        if not stations:
            return None
        return stations[0]

    async def stations(
        self,
        *,
        filter_by: FilterBy | None = None,
        filter_term: str | None = None,
        hide_broken: bool = False,
        limit: int = 100000,
        offset: int = 0,
        order: Order = Order.NAME,
        reverse: bool = False,
    ) -> list[Station]:
        """Get list of radio stations.

        Args:
            filter_by: Filter the results by a specific field.
            filter_term: Search term to filter the results.
            hide_broken: Do not count broken stations.
            limit: Limit the number of results.
            offset: Offset the results.
            order: Order the results.
            reverse: Reverse the order of the results.

        Returns:
            A list of Station objects.
        """
        uri = "stations"
        if filter_by is not None:
            uri = f"{uri}/{filter_by.value}"
            if filter_term is not None:
                uri = f"{uri}/{filter_term}"

        stations = await self._request(
            uri,
            params={
                "hidebroken": hide_broken,
                "offset": offset,
                "order": order.value,
                "reverse": reverse,
                "limit": limit,
            },
        )
        return parse_obj_as(list[Station], stations)

    async def tags(
        self,
        *,
        hide_broken: bool = False,
        limit: int = 100000,
        offset: int = 0,
        order: Order = Order.NAME,
        reverse: bool = False,
    ) -> list[Tag]:
        """Get list of available tags.

        Args:
            hide_broken: Do not count broken stations.
            limit: Limit the number of results.
            offset: Offset the results.
            order: Order the results.
            reverse: Reverse the order of the results.

        Returns:
            A list of Tags objects.
        """
        tags = await self._request(
            "tags",
            params={
                "hidebroken": hide_broken,
                "offset": offset,
                "order": order.value,
                "reverse": reverse,
                "limit": limit,
            },
        )
        return parse_obj_as(list[Tag], tags)

    async def close(self) -> None:
        """Close open client session."""
        if self.session and self._close_session:
            await self.session.close()

    async def __aenter__(self) -> RadioBrowser:
        """Async enter.

        Returns:
            The RadioBrowser object.
        """
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        """Async exit.

        Args:
            _exc_info: Exec type.
        """
        await self.close()
