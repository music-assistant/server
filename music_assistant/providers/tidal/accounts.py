"""Rules for Tidal accounts shared between provider instances."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import CONF_USER_ID, DOMAIN

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant


async def get_account_holders(
    mass: MusicAssistant, user_id: str, exclude_instance_id: str | None
) -> list[ProviderConfig]:
    """
    Return every other Tidal instance, enabled or not, that is signed in with the account.

    :param mass: The Music Assistant instance.
    :param user_id: The Tidal user id to look for.
    :param exclude_instance_id: An instance to leave out, usually the one being checked.
    """
    holders = []
    for config in await mass.config.get_provider_configs(provider_domain=DOMAIN):
        if config.instance_id == exclude_instance_id:
            continue
        stored = mass.config.get_provider_setup_value(config.instance_id, CONF_USER_ID)
        if stored and str(stored) == user_id:
            holders.append(config)
    return holders


async def get_winning_account_holder(
    mass: MusicAssistant, user_id: str, instance_id: str
) -> ProviderConfig | None:
    """
    Return the instance that keeps a Tidal account over the given instance, if any.

    Tidal allows one stream per account, so when several instances share an account the
    enabled instance with the lowest instance id keeps it and the others must not load.

    :param mass: The Music Assistant instance.
    :param user_id: The Tidal user id the given instance is signed in with.
    :param instance_id: The instance to check the account for.
    """
    winners = [
        config
        for config in await get_account_holders(mass, user_id, instance_id)
        if config.enabled and config.instance_id < instance_id
    ]
    return min(winners, key=lambda config: config.instance_id, default=None)
