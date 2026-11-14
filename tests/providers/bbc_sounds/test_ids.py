"""
Tests for ID handling in the BBC Sounds Provider.

The Sounds API has the concept of networks, which we would usually refer to as radio
stations. Normally, a network has exactly one service, and they share an ID. E.g. Radio
One has a network bbc_radio_one, with a service bbc_radio_one.

Radio Four's network is bbc_radio_four, with two services: bbc_radio_four and
bbc_radio_fourmw, only the FM service is available to stream. Some endpoints take and
return a network, some take and return a service.

As of auntie-sounds v3.0.0, bbc_radio_four and bbc_radio_fourfm are converted correctly
depending on which endpoint is being requested.

As the main endpoint to list the stations returns bbc_radio_fourfm, we've standardised
on that being the item_id used in Music Assistant.

Related, but not tested here as not quite the same:
- bbc_radio_scotland_fm is the default service of bbc_radio_scotland
- bbc_radio_orkney and bbc_radio_shetland are opt-outs bbc_radio_scotland_fm
- BBC Scotland Extra is the MW simulcast (bbc_radio_scotland_mw)
- bbc_radio_wales_fm -> bbc_radio_wales
- BBC Wales Extra is the AM simulcast (bbc_radio_wales_am)
- bbc_radio_solent_west_dorset - West Dorset opt-out service of Radio Solent
"""

from unittest.mock import AsyncMock

import pytest
from auntie_sounds.parser import Parser
from auntie_sounds.playback import PlaybackService
from auntie_sounds.requests import RequestManager
from auntie_sounds.schedule import ScheduleService
from auntie_sounds.stations import StationService
from music_assistant_models.enums import MediaType

from music_assistant.providers.bbc_sounds import BBCSoundsProvider

from .conftest import RADIO_FOUR_SERVICE_ID

RADIO_FOUR_LOGO = (
    "https://sounds.files.bbci.co.uk/3.12.0/networks/bbc_radio_four/{type}_{size}.{format}"
)

# Taken from the API's real responses
RADIO_FOUR_SEARCH_RESPONSE = {
    "data": [
        {
            "type": "inline_display_module",
            "id": "live_search",
            "data": [
                {
                    "type": "live_search_result_item",
                    "id": "search_result_bbc_radio_fourfm",
                    "urn": "urn:bbc:radio:search_result:bbc_radio_fourfm",
                    "now": {
                        "type": "broadcast_search_result",
                        "id": "broadcast_search_result_bbc_radio_fourfm",
                        "urn": "urn:bbc:radio:network:bbc_radio_four",
                        "service_id": "bbc_radio_fourfm",
                        "episode_image_url": None,
                        "station_image_url": RADIO_FOUR_LOGO,
                        "station_name": "Radio 4",
                        "title": "Moral Maze",
                        "short_synopsis": "Live debate.",
                        "progress": None,
                        "duration": None,
                    },
                }
            ],
        }
    ]
}
RADIO_FOUR_PLAYABLE_RESPONSE = {
    "type": "playable_item",
    "id": "bbc_radio_fourfm",
    "urn": "urn:bbc:radio:network:bbc_radio_four",
    "network": {
        "id": "bbc_radio_four",
        "key": "radio4",
        "short_title": "Radio 4",
        "logo_url": RADIO_FOUR_LOGO,
        "network_type": "master_brand",
    },
    "titles": {"primary": "In Our Time", "secondary": "09:00 - 09:45", "tertiary": "17/09/2026"},
    "synopses": {"short": "Ideas.", "medium": None, "long": None},
    "duration": {"value": 2700, "label": "45 mins"},
    "progress": {"value": 1436, "label": "21 mins left"},
    "availability": {"from": None, "to": None, "label": "Live"},
    "release": {"date": "2026-09-17T00:00:00Z", "label": "17 Sep 2026"},
}


def _mock(service: object) -> AsyncMock:
    """Return a client service, which the client fixture replaces with a mock, as that mock."""
    assert isinstance(service, AsyncMock)
    return service


class TestRadioFourId:
    """Radio 4 has a network id and a service id, but MA should only ever see one."""

    async def test_station_search_uses_the_service_id(self, provider: BBCSoundsProvider) -> None:
        """Test a station found by search has Radio 4's service id."""
        parser = Parser()
        _mock(provider.client.content).search.return_value = parser.parse_search(
            RADIO_FOUR_SEARCH_RESPONSE
        )

        results = await provider.search("radio 4", [MediaType.RADIO])

        assert [radio.item_id for radio in results.radio] == [RADIO_FOUR_SERVICE_ID]

    @pytest.mark.usefixtures("cache_tasks")
    @pytest.mark.parametrize("station_id", ["bbc_radio_four", RADIO_FOUR_SERVICE_ID])
    async def test_live_lookup_uses_the_service_id(
        self, provider: BBCSoundsProvider, station_id: str
    ) -> None:
        """Test a station looked up by either Radio 4 id has the service id."""
        requests = AsyncMock(spec=RequestManager)
        requests.get_json_response.return_value = RADIO_FOUR_PLAYABLE_RESPONSE
        playback = AsyncMock(spec=PlaybackService)
        playback.get_live_streams.return_value = ["https://example.com/radio4.m3u8"]
        provider.client.stations = StationService(
            playback=playback, schedules=AsyncMock(spec=ScheduleService), requests=requests
        )

        radio = await provider.get_radio(station_id)

        assert radio.item_id == RADIO_FOUR_SERVICE_ID
