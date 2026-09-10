"""
One-shot conversion of the per user music source restrictions into access records.

Which music sources a user was allowed to use lived on the user, as an allow-list of
provider instance ids. It now lives on the source itself: the member owning it plus who
it is shared with. This reads the old filters once and gives every music source the
access record that reproduces exactly what each user could see, then clears the filters.

Unlike the `settings.json` migrations in `migrations.py`, it needs the users from the
auth database, so it runs from `MusicAssistant.start()` once the webserver is set up -
and before the providers load, so no provider is ever served an access record that is
still to be written.

TODO: remove after 2.11 release
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderSharing, ProviderType

from music_assistant.constants import (
    CONF_PROVIDER_ACCESS_MIGRATED,
    CONF_PROVIDERS,
    HOMEASSISTANT_SYSTEM_USER,
)
from music_assistant.helpers.json import json_loads

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

# the connected-player plugins collapsed their instances into a single instance keyed by
# the bare domain; a filter entry naming a collapsed instance follows it instead of being
# pruned, which would lift the user's restriction
COLLAPSED_PLUGIN_DOMAINS = ("spotify_connect", "airplay_receiver")


@dataclass
class _StoredUser:
    """A user as the conversion reads it, with the filter it is about to lose."""

    # the music sources the user is restricted to, an empty set meaning unrestricted
    sources: set[str]
    # a guest and the Home Assistant system user may use a source but never own one
    can_own: bool


async def migrate_provider_access(mass: MusicAssistant) -> None:
    """
    Give every music source the access record that matches the user filters it replaces.

    Runs at most once per install and never raises: an install where the conversion fails
    keeps its filters and is retried on the next startup.

    :param mass: The MusicAssistant instance, with its webserver set up.
    """
    if mass.config.get(CONF_PROVIDER_ACCESS_MIGRATED, False):
        return
    try:
        users = await _stored_users(mass)
        for instance_id, raw_conf in mass.config.get(CONF_PROVIDERS, {}).items():
            try:
                access = _access_for_source(mass, instance_id, raw_conf, users)
            except Exception as err:
                # fail closed: the source stays hidden until an admin sets its access
                LOGGER.warning(
                    "Unable to convert the access of music source %s, it is hidden until an "
                    "admin sets its access - %s: %s",
                    instance_id,
                    type(err).__name__,
                    err,
                )
                access = ProviderAccess(sharing=ProviderSharing.PRIVATE)
            if access is None:
                continue
            mass.config.set(f"{CONF_PROVIDERS}/{instance_id}/access", access.to_dict())
        # the filters are the input of the conversion, so they only go once the records are
        # on disk; a source that already carries one is left alone on a retry
        await mass.config.async_save()
        await mass.webserver.auth.database.execute_write("UPDATE users SET provider_filter = '[]'")
    except Exception:
        # an escape here would abort the boot. The flag stays unset, so the next startup
        # retries the (idempotent) conversion
        LOGGER.exception("Unable to convert the music source restrictions of this install")
        return
    mass.config.set(CONF_PROVIDER_ACCESS_MIGRATED, True, immediate=True)


def _access_for_source(
    mass: MusicAssistant,
    instance_id: str,
    raw_conf: Mapping[str, Any],
    users: dict[str, _StoredUser],
) -> ProviderAccess | None:
    """
    Return the access record for the given provider config, or None to leave it alone.

    :param mass: The MusicAssistant instance to read the provider manifests of.
    :param instance_id: The instance id of the provider config.
    :param raw_conf: The raw (stored) provider config.
    :param users: The users of this install, by user id.
    """
    if raw_conf.get("type") != ProviderType.MUSIC or raw_conf.get("access") is not None:
        return None
    if mass.get_provider_manifest(raw_conf["domain"]).builtin:
        # the builtin provider serves the entire household and carries no access record
        return None
    listed_by = [user_id for user_id, user in users.items() if instance_id in user.sources]
    unrestricted = {user_id for user_id, user in users.items() if not user.sources}
    # a source that exactly one restricted user was given is taken to be theirs, unless
    # that user can not own one - it then stays ownerless and the user keeps its access
    owner = listed_by[0] if len(listed_by) == 1 and users[listed_by[0]].can_own else None
    allowed = unrestricted.union(listed_by)
    if allowed == set(users):
        # nobody was kept out of this source, so it becomes a household source
        return ProviderAccess(owner=owner, sharing=ProviderSharing.EVERYONE)
    return ProviderAccess(
        owner=owner,
        sharing=ProviderSharing.SELECTED,
        shared_users=sorted(user_id for user_id in allowed if user_id != owner),
    )


async def _stored_users(mass: MusicAssistant) -> dict[str, _StoredUser]:
    """
    Return every user of this install by user id.

    :param mass: The MusicAssistant instance to read the users and provider configs of.
    """
    known_sources = set(mass.config.get(CONF_PROVIDERS, {}))
    rows = await mass.webserver.auth.database.get_rows("users", limit=0)
    return {
        str(row["user_id"]): _StoredUser(
            sources=_normalized_filter(row, known_sources), can_own=_can_own_a_source(row)
        )
        for row in rows
    }


def _can_own_a_source(row: Mapping[str, Any]) -> bool:
    """Return whether a music source may be given to the user of the given row."""
    return bool(row["role"] != UserRole.GUEST and row["username"] != HOMEASSISTANT_SYSTEM_USER)


def _normalized_filter(row: Mapping[str, Any], known_sources: set[str]) -> set[str]:
    """
    Return the stored filter of a user row, mapped onto the sources that still exist.

    :param row: The user row to read the filter of.
    :param known_sources: The instance ids of every configured provider.
    """
    try:
        stored = json_loads(row["provider_filter"])
    except KeyError, IndexError, TypeError, ValueError:
        return set()
    if not isinstance(stored, list):
        return set()
    # a filter that names only sources that were removed leaves the user unrestricted, the
    # alternative being an account that can see nothing at all
    return {
        _mapped_source(entry, known_sources) for entry in stored if isinstance(entry, str)
    } & known_sources


def _mapped_source(entry: str, known_sources: set[str]) -> str:
    """
    Return the source a filter entry names, following the collapsed plugin instances.

    :param entry: The filter entry to resolve.
    :param known_sources: The instance ids of every configured provider.
    """
    for domain in COLLAPSED_PLUGIN_DOMAINS:
        if entry.startswith(f"{domain}--") and domain in known_sources:
            return domain
    return entry
