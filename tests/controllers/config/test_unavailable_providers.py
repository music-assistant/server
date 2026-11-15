"""Tests for provider configs whose provider is no longer available (no manifest)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing, ProviderStatus, ProviderType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.mass import MusicAssistant

AVAILABLE_DOMAIN = "here_music"
AVAILABLE_INSTANCE = f"{AVAILABLE_DOMAIN}--aaaa"
GONE_MUSIC_INSTANCE = "gone_music--bbbb"
GONE_PLAYER_INSTANCE = "gone_player"
BUILTIN_DOMAIN = "builtin_thing"
GONE_PLAYER_ID = "gone_player_1"
OWNER_ID = "owner"
OTHER_ID = "other"


@pytest.fixture
def unavailable_mass(mass_minimal: MusicAssistant) -> MusicAssistant:
    """
    Provide a minimal server with one available provider and two without a manifest.

    :param mass_minimal: The minimal server to configure.
    """
    for domain, builtin in ((AVAILABLE_DOMAIN, False), (BUILTIN_DOMAIN, True)):
        mass_minimal._provider_manifests[domain] = ProviderManifest(
            type=ProviderType.MUSIC,
            domain=domain,
            name=f"{domain} provider",
            description="",
            codeowners=[],
            builtin=builtin,
        )
    for instance_id, prov_type in (
        (AVAILABLE_INSTANCE, ProviderType.MUSIC),
        (GONE_MUSIC_INSTANCE, ProviderType.MUSIC),
        (GONE_PLAYER_INSTANCE, ProviderType.PLAYER),
        (BUILTIN_DOMAIN, ProviderType.MUSIC),
    ):
        mass_minimal.config.set(
            f"{CONF_PROVIDERS}/{instance_id}",
            {
                "type": prov_type.value,
                "domain": instance_id.split("--")[0],
                "instance_id": instance_id,
                "values": {},
            },
        )
    mass_minimal.config.set(
        f"{CONF_PLAYERS}/{GONE_PLAYER_ID}",
        {"player_id": GONE_PLAYER_ID, "provider": GONE_PLAYER_INSTANCE},
    )
    mass_minimal.music = MagicMock()
    for method in (
        "cleanup_provider_shortcuts",
        "cleanup_provider",
        "cleanup_library_shortcuts",
        "cleanup_provider_images",
    ):
        setattr(mass_minimal.music, method, AsyncMock())
    mass_minimal.players = MagicMock()
    mass_minimal.players.__iter__.return_value = iter([])
    return mass_minimal


async def test_default_result_leaves_unavailable_entries_out(
    unavailable_mass: MusicAssistant,
) -> None:
    """Without the flag the list holds only providers with a manifest, and get raises."""
    configs = await unavailable_mass.config.get_provider_configs()

    assert [x.instance_id for x in configs] == [AVAILABLE_INSTANCE, BUILTIN_DOMAIN]
    with pytest.raises(KeyError, match="Unknown provider domain"):
        await unavailable_mass.config.get_provider_config(GONE_MUSIC_INSTANCE)


async def test_flag_returns_unavailable_entries_with_the_reason(
    unavailable_mass: MusicAssistant,
) -> None:
    """With the flag an entry without a provider comes back unavailable, nothing is stored."""
    configs = await unavailable_mass.config.get_provider_configs(include_unavailable=True)

    by_id = {x.instance_id: x for x in configs}
    assert set(by_id) == {
        AVAILABLE_INSTANCE,
        BUILTIN_DOMAIN,
        GONE_MUSIC_INSTANCE,
        GONE_PLAYER_INSTANCE,
    }
    gone = by_id[GONE_MUSIC_INSTANCE]
    assert gone.status == ProviderStatus.UNAVAILABLE
    assert gone.last_error is not None
    assert gone.last_error.error_code == ProviderUnavailableError.error_code
    assert gone.last_error.translation_key == "provider_no_longer_available"
    assert by_id[AVAILABLE_INSTANCE].status != ProviderStatus.UNAVAILABLE
    assert unavailable_mass.config.get(f"{CONF_PROVIDERS}/{GONE_MUSIC_INSTANCE}/last_error") is None


async def test_flag_filters_like_the_other_entries(unavailable_mass: MusicAssistant) -> None:
    """The type and domain filters apply to unavailable entries too."""
    configs = await unavailable_mass.config.get_provider_configs(
        provider_type=ProviderType.PLAYER, include_unavailable=True
    )
    assert [x.instance_id for x in configs] == [GONE_PLAYER_INSTANCE]
    configs = await unavailable_mass.config.get_provider_configs(
        provider_domain="gone_music", include_unavailable=True
    )
    assert [x.instance_id for x in configs] == [GONE_MUSIC_INSTANCE]


async def test_a_member_does_not_see_a_hidden_unavailable_source(
    unavailable_mass: MusicAssistant,
) -> None:
    """An unavailable music source of someone else stays hidden from a member."""
    unavailable_mass.config.set(
        f"{CONF_PROVIDERS}/{GONE_MUSIC_INSTANCE}/access",
        ProviderAccess(owner=OWNER_ID, sharing=ProviderSharing.PRIVATE).to_dict(),
    )

    set_current_user(User(user_id=OTHER_ID, username=OTHER_ID, role=UserRole.USER))
    configs = await unavailable_mass.config.get_provider_configs(include_unavailable=True)
    assert GONE_MUSIC_INSTANCE not in {x.instance_id for x in configs}

    set_current_user(User(user_id=OWNER_ID, username=OWNER_ID, role=UserRole.USER))
    configs = await unavailable_mass.config.get_provider_configs(include_unavailable=True)
    assert GONE_MUSIC_INSTANCE in {x.instance_id for x in configs}


async def test_get_with_flag_returns_the_unavailable_entry(
    unavailable_mass: MusicAssistant,
) -> None:
    """Get returns the same unavailable entry as the list instead of raising."""
    conf = await unavailable_mass.config.get_provider_config(
        GONE_MUSIC_INSTANCE, include_unavailable=True
    )

    assert conf.domain == "gone_music"
    assert conf.status == ProviderStatus.UNAVAILABLE
    assert conf.last_error is not None
    assert conf.last_error.translation_key == "provider_no_longer_available"


async def test_remove_cleans_up_an_unavailable_music_source(
    unavailable_mass: MusicAssistant,
) -> None:
    """Removing an unavailable music source runs the library cleanup and deletes the entry."""
    music = cast("MagicMock", unavailable_mass.music)

    with patch.object(unavailable_mass, "unload_provider", AsyncMock()) as mock_unload:
        await unavailable_mass.config.remove_provider_config(GONE_MUSIC_INSTANCE)

    assert unavailable_mass.config.get(f"{CONF_PROVIDERS}/{GONE_MUSIC_INSTANCE}") is None
    mock_unload.assert_awaited_once_with(GONE_MUSIC_INSTANCE, True)
    music.cleanup_provider_shortcuts.assert_awaited_once_with(GONE_MUSIC_INSTANCE)
    music.cleanup_provider.assert_awaited_once_with(GONE_MUSIC_INSTANCE)
    music.cleanup_library_shortcuts.assert_awaited_once()


async def test_remove_cleans_up_an_unavailable_player_provider(
    unavailable_mass: MusicAssistant,
) -> None:
    """Removing an unavailable player provider deletes the configs of its players."""
    players = cast("MagicMock", unavailable_mass.players)

    with patch.object(unavailable_mass, "unload_provider", AsyncMock()):
        await unavailable_mass.config.remove_provider_config(GONE_PLAYER_INSTANCE)

    assert unavailable_mass.config.get(f"{CONF_PROVIDERS}/{GONE_PLAYER_INSTANCE}") is None
    players.delete_player_config.assert_called_once_with(GONE_PLAYER_ID)


async def test_a_builtin_provider_is_still_refused(unavailable_mass: MusicAssistant) -> None:
    """A builtin provider may not be removed."""
    with (
        patch.object(unavailable_mass, "unload_provider", AsyncMock()) as mock_unload,
        pytest.raises(RuntimeError, match="can not be removed"),
    ):
        await unavailable_mass.config.remove_provider_config(BUILTIN_DOMAIN)

    assert unavailable_mass.config.get(f"{CONF_PROVIDERS}/{BUILTIN_DOMAIN}") is not None
    mock_unload.assert_not_awaited()
