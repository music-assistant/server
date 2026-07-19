"""Unit tests for the AirPlay provider (sync_adjust migration)."""

import logging
from unittest.mock import MagicMock

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.providers.airplay.constants import CONF_SYNC_ADJUST_RESET_MARKER
from music_assistant.providers.airplay.provider import AirPlayProvider

INSTANCE_ID = "airplay"


def _make_provider(
    marker_set: bool,
    player_configs: dict[str, dict[str, object]],
    stored_sync_adjust: dict[str, int],
) -> AirPlayProvider:
    """
    Build a bare provider wired to a mocked config store.

    :param marker_set: Whether the one-time migration already ran.
    :param player_configs: Raw player config store contents.
    :param stored_sync_adjust: Persisted sync_adjust value per player id.
    """
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.mass = MagicMock()
    prov.logger = logging.getLogger("test.airplay.provider")
    prov.config = MagicMock()
    prov.config.instance_id = INSTANCE_ID
    prov.mass.config.get_raw_provider_config_value.return_value = marker_set
    prov.mass.config.get.return_value = player_configs
    prov.mass.config.get_raw_player_config_value.side_effect = lambda player_id, _key, default=0: (
        stored_sync_adjust.get(player_id, default)
    )
    return prov


def test_sync_adjust_migration_resets_stored_values() -> None:
    """Persisted sync_adjust values are reset once and the marker is written."""
    player_configs = {
        "apaaa": {"player_id": "apaaa", "provider": INSTANCE_ID},
        "apbbb": {"player_id": "apbbb", "provider": INSTANCE_ID},
        # player of another provider must be left alone
        "sonos1": {"player_id": "sonos1", "provider": "sonos"},
    }
    prov = _make_provider(
        marker_set=False,
        player_configs=player_configs,
        stored_sync_adjust={"apaaa": 120, "apbbb": 0, "sonos1": 250},
    )

    prov._migrate_sync_adjust()

    # only the airplay player with a non-zero offset is reset
    prov.mass.config.set_raw_player_config_value.assert_called_once_with(
        "apaaa", CONF_SYNC_ADJUST, 0
    )
    # the one-time marker is written afterwards
    prov.mass.config.set_raw_provider_config_value.assert_called_once_with(
        INSTANCE_ID, CONF_SYNC_ADJUST_RESET_MARKER, True
    )


def test_sync_adjust_migration_runs_only_once() -> None:
    """With the marker set, the migration must not touch player configs again."""
    player_configs = {"apaaa": {"player_id": "apaaa", "provider": INSTANCE_ID}}
    prov = _make_provider(
        marker_set=True,
        player_configs=player_configs,
        stored_sync_adjust={"apaaa": 120},
    )

    prov._migrate_sync_adjust()

    prov.mass.config.set_raw_player_config_value.assert_not_called()
    prov.mass.config.set_raw_provider_config_value.assert_not_called()
