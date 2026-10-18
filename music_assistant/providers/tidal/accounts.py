"""Rules for Tidal accounts shared between provider instances."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderSharing
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PROVIDERS

from .constants import (
    CONF_USER_ID,
    DOMAIN,
    ERROR_ACCOUNT_IN_USE,
    ERROR_ACCOUNT_IN_USE_PRIVATE,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

NAMEABLE_SHARING = ProviderSharing.EVERYONE


def get_account_holders(
    mass: MusicAssistant, user_id: str, exclude_instance_id: str | None
) -> list[ProviderConfig]:
    """
    Return every other Tidal instance, enabled or not, that is signed in with the account.

    The stored configs are read directly, so the result does not depend on which user
    triggered the check.

    :param mass: The Music Assistant instance.
    :param user_id: The Tidal user id to look for.
    :param exclude_instance_id: An instance to leave out, usually the one being checked.
    """
    holders = []
    for raw_conf in mass.config.get(CONF_PROVIDERS, {}).values():
        if raw_conf["domain"] != DOMAIN or raw_conf["instance_id"] == exclude_instance_id:
            continue
        stored = mass.config.get_provider_setup_value(raw_conf["instance_id"], CONF_USER_ID)
        if stored and str(stored) == user_id:
            holders.append(cast("ProviderConfig", ProviderConfig.parse([], raw_conf)))
    return holders


def get_winning_account_holder(
    mass: MusicAssistant, user_id: str, instance_id: str
) -> ProviderConfig | None:
    """
    Return the enabled instance that keeps a Tidal account from the given instance, if any.

    Tidal allows one stream per account, so when several instances share an account the
    enabled instance with the lowest instance id keeps it.

    :param mass: The Music Assistant instance.
    :param user_id: The Tidal user id the given instance is signed in with.
    :param instance_id: The instance to check the account for.
    """
    lower = [
        config
        for config in get_account_holders(mass, user_id, instance_id)
        if config.enabled and config.instance_id < instance_id
    ]
    return min(lower, key=lambda config: config.instance_id, default=None)


def get_loaded_account_holders(
    mass: MusicAssistant, user_id: str, instance_id: str
) -> list[ProviderConfig]:
    """
    Return the other enabled instances that are running on the account right now.

    :param mass: The Music Assistant instance.
    :param user_id: The Tidal user id to look for.
    :param instance_id: The instance to leave out, usually the one being checked.
    """
    return [
        config
        for config in get_account_holders(mass, user_id, instance_id)
        if config.enabled and is_loaded(mass, config.instance_id)
    ]


def is_loaded(mass: MusicAssistant, instance_id: str) -> bool:
    """
    Return whether the instance is currently running.

    :param mass: The Music Assistant instance.
    :param instance_id: The instance to look up.
    """
    # return_unavailable skips get_provider's fallback to a sibling instance of the same domain
    return mass.get_provider(instance_id, return_unavailable=True) is not None


def is_nameable(config: ProviderConfig) -> bool:
    """
    Return whether the instance may be named in an error shown to any user of another instance.

    :param config: The instance to check.
    """
    return config.access is None or config.access.sharing == NAMEABLE_SHARING


def account_in_use_error(keeper: ProviderConfig, translation_owner: str) -> SetupFailedError:
    """
    Return the error telling a user that another instance keeps the Tidal account.

    The keeper is named only when every user may see it, see ``is_nameable``.

    :param keeper: The instance that keeps the account.
    :param translation_owner: The translation owner of the provider the error is raised for.
    """
    if not is_nameable(keeper):
        return SetupFailedError(
            "This Tidal account is already used by another Tidal instance",
            translation_key=ERROR_ACCOUNT_IN_USE_PRIVATE,
            translation_owner=translation_owner,
        )
    keeper_name = keeper.name or keeper.default_name or keeper.instance_id
    return SetupFailedError(
        f"This Tidal account is already used by {keeper_name}",
        translation_key=ERROR_ACCOUNT_IN_USE,
        translation_args=[keeper_name],
        translation_owner=translation_owner,
    )
