"""Test the Flow discovery setting in the Deezer provider options."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from deezer_python_gql import GraphQLClientError
from deezer_python_gql.generated.enums import DiscoveryTuner, DiscoveryTunerInput
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import ActionUnavailable

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.providers.deezer.constants import (
    ACTION_FLOW_TUNER_DEFAULT,
    ACTION_FLOW_TUNER_DISCOVERY,
)
from music_assistant.providers.deezer.provider import DeezerProvider


@pytest.fixture
def gql(provider: DeezerProvider) -> AsyncMock:
    """Stub the GraphQL client."""
    client = AsyncMock()
    provider.gql_client = client
    return client


def _tuner(value: DiscoveryTuner) -> SimpleNamespace:
    """Return a get_flow_tuner result holding the given setting."""
    return SimpleNamespace(flow_tuner=SimpleNamespace(discovery_tuner=value))


async def test_options_leave_out_the_setting_while_loading(
    provider: DeezerProvider, gql: AsyncMock
) -> None:
    """Before the provider is loaded there is no client, so only the fixed entries show."""
    entries = await provider.get_config_entries()

    assert entries == (CONF_ENTRY_UNOFFICIAL_PROVIDER,)
    gql.get_flow_tuner.assert_not_called()


@pytest.mark.parametrize(
    ("current", "state", "action"),
    [
        (DiscoveryTuner.DISCOVERY, "flow_tuner_state_discovery", ACTION_FLOW_TUNER_DEFAULT),
        (DiscoveryTuner.DEFAULT, "flow_tuner_state_default", ACTION_FLOW_TUNER_DISCOVERY),
    ],
)
async def test_options_show_the_setting_deezer_reports(
    provider: DeezerProvider, gql: AsyncMock, current: DiscoveryTuner, state: str, action: str
) -> None:
    """The options show the setting as Deezer reports it and offer the other one."""
    provider.available = True
    gql.get_flow_tuner.return_value = _tuner(current)

    _, label, button = await provider.get_config_entries()

    assert label.type == ConfigEntryType.LABEL
    assert label.translation_key == state
    assert button.type == ConfigEntryType.ACTION
    assert button.action == action


async def test_options_survive_a_failed_read(provider: DeezerProvider, gql: AsyncMock) -> None:
    """A Deezer error while reading the setting leaves the rest of the options intact."""
    provider.available = True
    gql.get_flow_tuner.side_effect = GraphQLClientError("down")

    assert await provider.get_config_entries() == (CONF_ENTRY_UNOFFICIAL_PROVIDER,)


@pytest.mark.parametrize(
    ("action", "target"),
    [
        (ACTION_FLOW_TUNER_DISCOVERY, DiscoveryTunerInput.DISCOVERY),
        (ACTION_FLOW_TUNER_DEFAULT, DiscoveryTunerInput.DEFAULT),
    ],
)
async def test_button_sets_the_setting_it_names(
    provider: DeezerProvider, gql: AsyncMock, action: str, target: DiscoveryTunerInput
) -> None:
    """A button sets exactly the value it names and shows the options again."""
    provider.available = True
    gql.get_flow_tuner.return_value = _tuner(DiscoveryTuner.DEFAULT)

    entries = await provider.handle_config_action(action)

    gql.set_flow_discovery_tuner.assert_awaited_once_with(discovery_tuner=target)
    assert entries


async def test_unknown_button_is_refused(provider: DeezerProvider) -> None:
    """A button the provider does not know goes to the base class, which refuses it."""
    with pytest.raises(ActionUnavailable):
        await provider.handle_config_action("unknown")
