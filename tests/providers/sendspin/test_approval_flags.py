"""Tests for the Sendspin player needs_setup state (pairing/consent/audio input)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

from music_assistant.providers.sendspin.constants import CONF_SOURCE_APPROVAL_DISMISSED
from music_assistant.providers.sendspin.player import SendspinBasePlayer, SendspinSourcePlayer

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient

    from music_assistant.providers.sendspin.provider import SendspinProvider


def _make_api(
    *,
    active_roles: tuple[str, ...] = (),
    negotiated_role_ids: tuple[str, ...] = (),
    unpaired_access: bool = False,
    encrypted: bool = True,
) -> Any:
    return SimpleNamespace(
        info_or_none=SimpleNamespace(unpaired_access=SimpleNamespace(enabled=unpaired_access)),
        connection_security=SimpleNamespace() if encrypted else None,
        active_roles=active_roles,
        negotiated_role_ids=list(negotiated_role_ids),
        roles_by_family=lambda family: [
            role for role in active_roles if role.startswith(f"{family}@")
        ],
    )


def _make_player(
    api: Any, *, cls: type[SendspinBasePlayer] = SendspinBasePlayer, dismissed: bool = False
) -> SendspinBasePlayer:
    player = cls.__new__(cls)
    player._player_id = "client-1"
    player._provider = cast(
        "SendspinProvider", SimpleNamespace(pairing_config_snapshot=lambda _client_id: None)
    )
    player.api = cast("SendspinClient", api)
    player.mass = mock.MagicMock()
    player.mass.config.get_raw_player_config_value = mock.Mock(return_value=dismissed)
    return player


def test_unpaired_client_needs_setup() -> None:
    """An unpaired encrypted client reports pairing_required, guest-capable or not."""
    for unpaired_access in (False, True):
        player = _make_player(_make_api(unpaired_access=unpaired_access))
        assert player.needs_setup is True
        assert player.setup_reason == "pairing_required"


def test_client_with_active_roles_needs_no_setup() -> None:
    """An allowed (or paired) client without a pending input carries no setup state."""
    player = _make_player(_make_api(active_roles=("player@v1",), unpaired_access=True))
    assert player.needs_setup is False
    assert player.setup_reason is None


def test_legacy_unencrypted_client_needs_no_setup() -> None:
    """A legacy unencrypted client plays as-is and never prompts."""
    player = _make_player(_make_api(encrypted=False, unpaired_access=True))
    assert player.needs_setup is False


def test_undecided_audio_input_keeps_the_device_usable() -> None:
    """
    A combo with an active player role stays available while its input is undecided.

    Devices granted unpaired access before the input decision existed must not
    become unavailable on upgrade; the pending input only drives the setup flow.
    """
    api = _make_api(
        active_roles=("player@v1",),
        negotiated_role_ids=("player@v1", "source@v1"),
        unpaired_access=True,
    )
    player = _make_player(api)
    assert player.needs_setup is False
    assert player._source_input_pending is True


def test_declined_audio_input_clears_the_pending_state() -> None:
    """The persisted don't-use-audio-input choice resolves the pending input."""
    api = _make_api(
        active_roles=("player@v1",),
        negotiated_role_ids=("player@v1", "source@v1"),
        unpaired_access=True,
    )
    player = _make_player(api, dismissed=True)
    assert player._source_input_pending is False
    player.mass.config.get_raw_player_config_value.assert_called_once_with(
        "client-1", CONF_SOURCE_APPROVAL_DISMISSED, False
    )


def test_active_source_role_clears_the_pending_state() -> None:
    """Once the source role runs (paired client), nothing is pending."""
    api = _make_api(
        active_roles=("player@v1", "source@v1"),
        negotiated_role_ids=("player@v1", "source@v1"),
        unpaired_access=True,
    )
    player = _make_player(api)
    assert player.needs_setup is False
    assert player._source_input_pending is False


def test_source_only_client_gets_no_consent_shortcut() -> None:
    """A capture-only client pairs instead: unpaired consent would grant it nothing."""
    api = _make_api(negotiated_role_ids=("source@v1",), unpaired_access=True)
    player = _make_player(api, cls=SendspinSourcePlayer)
    assert player.needs_setup is True
    assert player._offers_unpaired_consent is False
