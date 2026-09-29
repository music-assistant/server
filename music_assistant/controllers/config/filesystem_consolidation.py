"""
One-shot conversion of the SMB and NFS music sources into Local files sources.

The SMB and NFS providers mounted their network share themselves. A network share is a storage
location now, mounted by the Home Assistant Supervisor or by the server itself, and a Local
files source reads a folder in a storage location. So each SMB and NFS source gets the storage
location of its share and becomes a Local files source on the same folder. It keeps its
instance id, and with that its library, its access record and its options, and it keeps the
name it was shown with.

Unlike the `settings.json` migrations in `migrations.py`, this needs the library database and,
under a Supervisor, the mounts the Supervisor has, so it runs from `MusicAssistant.start()` once
the core controllers are up. It never mounts anything itself: a share whose server is off at
this point is still converted, and its source loads once the storage controller mounted it.

TODO: remove after 2.13 release
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.errors import InvalidDataError, SetupFailedError

from music_assistant.constants import (
    CONF_FILESYSTEM_SOURCES_CONSOLIDATED,
    CONF_PASSWORD,
    CONF_PATH,
    CONF_PROVIDERS,
    CONF_STORAGE_SHARES,
    CONF_USERNAME,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.storage.backends.base import BackendUnavailable
from music_assistant.controllers.storage.backends.local_mount import LOCAL_VERSIONS, LocalMounter
from music_assistant.controllers.storage.backends.supervisor import create_supervisor_mounter
from music_assistant.controllers.storage.helpers import share_key
from music_assistant.controllers.storage.models import NetworkShareSpec, ShareType
from music_assistant.helpers.security import is_safe_path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant.controllers.storage.backends.base import ShareMounter
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

SMB_DOMAIN: Final[str] = "filesystem_smb"
NFS_DOMAIN: Final[str] = "filesystem_nfs"
LOCAL_FILES_DOMAIN: Final[str] = "filesystem_local"
# the names of the removed providers, which a source without a name of its own was shown with
REMOVED_PROVIDER_NAMES: Final[dict[str, str]] = {
    SMB_DOMAIN: "Filesystem (remote share)",
    NFS_DOMAIN: "Filesystem (NFS share)",
}
# the setup values of the removed providers; the content type is a setup value of Local files too
CONF_CONTENT_TYPE: Final[str] = "content_type"
DEFAULT_CONTENT_TYPE: Final[str] = "music"
CONF_HOST: Final[str] = "host"
CONF_SHARE: Final[str] = "share"
CONF_SMB_VERSION: Final[str] = "smb_version"
CONF_EXPORT_PATH: Final[str] = "export_path"
CONF_NFS_VERSION: Final[str] = "nfs_version"
CONF_SUBFOLDER: Final[str] = "subfolder"
# the one option of the SMB provider that Local files does not have
CONF_CACHE_MODE: Final[str] = "cache_mode"
# how long the conversion waits for the Supervisor at most, in seconds
SUPERVISOR_TIMEOUT: Final[float] = 30


async def consolidate_filesystem_sources(mass: MusicAssistant) -> None:
    """
    Turn every SMB and NFS music source into a Local files source on a storage location.

    Runs at most once per install and never raises. A source whose settings can not be read is
    left as it is. When there is a source to convert but the Supervisor can not say which shares
    it has mounted, nothing is converted and the next start tries again.

    :param mass: The MusicAssistant instance, with its core controllers set up.
    """
    if mass.config.get(CONF_FILESYSTEM_SOURCES_CONSOLIDATED, False):
        return
    try:
        async with asyncio.timeout(SUPERVISOR_TIMEOUT):
            conversions = await _plan_conversions(mass)
    except (BackendUnavailable, SetupFailedError, TimeoutError) as err:
        LOGGER.warning(
            "Unable to convert the SMB and NFS music sources, the Supervisor can not tell which "
            "network shares it has mounted (%s). The next start tries again.",
            str(err) or type(err).__name__,
        )
        return
    except Exception:
        LOGGER.exception("Unable to convert the SMB and NFS music sources")
        return
    try:
        if conversions:
            # the library goes first: its update can be repeated as it is, so when the settings
            # below do not reach the disk the next start simply converts again
            await _update_library(mass, [conversion.instance_id for conversion in conversions])
            for conversion in conversions:
                if conversion.share is not None:
                    mass.config.set(
                        f"{CONF_STORAGE_SHARES}/{conversion.share.name}", conversion.share.to_dict()
                    )
                mass.config.set(f"{CONF_PROVIDERS}/{conversion.instance_id}", conversion.config)
                LOGGER.info(
                    "Converted music source %s into a Local files source", conversion.instance_id
                )
            await mass.config.async_save()
        mass.config.set(CONF_FILESYSTEM_SOURCES_CONSOLIDATED, True, immediate=True)
    except Exception:
        # an escape here would abort the boot; the marker is not stored, so the next start
        # converts what is left
        LOGGER.exception("Unable to convert the SMB and NFS music sources")
        return
    if any(conversion.share is not None for conversion in conversions):
        try:
            # list the new shares right away, so a source on one reads as not available until
            # the storage controller mounted it
            await mass.storage.refresh()
        except Exception:
            LOGGER.exception("Unable to list the network shares of the converted music sources")
        mass.create_task(mass.storage.reconcile())


@dataclass
class _Conversion:
    """The conversion of one SMB or NFS music source."""

    instance_id: str
    # the raw provider config of the Local files source it becomes
    config: dict[str, Any]
    # the network share to store for it, None when its share is a storage location already
    share: NetworkShareSpec | None


@dataclass
class _RemovedSource:
    """What an SMB or NFS music source read, taken from its setup values."""

    share_type: ShareType
    server: str
    share: str
    username: str | None
    password: str | None
    version: str | None
    # the folder inside the share the source read, relative to the share
    subfolder: str
    content_type: str
    # what the removed provider appended to its name when it had more than one source
    name_postfix: str | None


async def _plan_conversions(mass: MusicAssistant) -> list[_Conversion]:
    """
    Return the conversion of every SMB and NFS music source that can be converted.

    Changes nothing.

    :param mass: The MusicAssistant instance.
    :raises BackendUnavailable: When the Supervisor does not let this server manage its mounts.
    :raises SetupFailedError: When the Supervisor does not list its mounts.
    """
    raw_configs: dict[str, Any] = mass.config.get(CONF_PROVIDERS, {})
    sources: dict[str, _RemovedSource] = {}
    for instance_id, raw_conf in raw_configs.items():
        if not isinstance(raw_conf, dict) or raw_conf.get("domain") not in REMOVED_PROVIDER_NAMES:
            continue
        try:
            source = _read_removed_source(mass, raw_conf)
        except InvalidDataError:
            source = None
        if source is None:
            # the removed provider could not mount its share either; its config stays as it is,
            # neither listed nor loaded now that its provider is gone, and its library stays
            LOGGER.warning(
                "Leaving music source %s as it is: its settings can not be converted", instance_id
            )
            continue
        sources[instance_id] = source
    if not sources:
        return []
    mounter = await _get_mounter(mass)
    raw_shares: dict[str, Any] = mass.config.get(CONF_STORAGE_SHARES, {})
    shares: dict[str, NetworkShareSpec] = {}
    for name, record in raw_shares.items():
        try:
            shares[name] = NetworkShareSpec.from_dict(record)
        except LookupError, ValueError, TypeError:
            # its name stays taken, it just can not be reused
            continue
    taken = set(raw_shares)
    # the source whose credentials each new network share carries, by name of the share
    credentials_of: dict[str, str] = {}
    conversions: list[_Conversion] = []
    for instance_id, source in sources.items():
        new_share: NetworkShareSpec | None = None
        # a share mounted without Music Assistant, e.g. in Home Assistant, is a location as it is
        share_path = await mounter.find_mount(source.share_type, source.server, source.share)
        if share_path is None:
            if (spec := _find_share(shares, source)) is None:
                spec = new_share = await _new_share(mass, mounter, source, taken)
                shares[spec.name] = spec
                taken.add(spec.name)
                credentials_of[spec.name] = instance_id
            elif (holder_id := credentials_of.get(spec.name)) is not None:
                credentials_of[spec.name] = _share_credentials(
                    mass, spec, (holder_id, sources[holder_id]), (instance_id, source)
                )
            share_path = spec.path
        raw_conf = raw_configs[instance_id]
        conversions.append(
            _Conversion(
                instance_id=instance_id,
                config=_converted_config(
                    mass,
                    raw_conf,
                    source,
                    os.path.normpath(os.path.join(share_path, source.subfolder)),
                    _shown_name(raw_configs, instance_id, raw_conf["domain"], source),
                ),
                share=new_share,
            )
        )
    return conversions


async def _get_mounter(mass: MusicAssistant) -> ShareMounter:
    """
    Return the mount backend that mounts the network shares of this server.

    Under a Supervisor only the Supervisor mounts a share. Elsewhere the server mounts it
    itself, also when it lacks the rights to do so right now: the share is then listed with
    the reason, until the server may mount.

    :param mass: The MusicAssistant instance.
    :raises BackendUnavailable: When the Supervisor does not let this server manage its mounts.
    """
    if mass.running_as_hass_addon:
        return await create_supervisor_mounter(mass)
    return LocalMounter(
        {share_type: list(versions) for share_type, versions in LOCAL_VERSIONS.items()}, LOGGER
    )


def _read_removed_source(
    mass: MusicAssistant, raw_conf: Mapping[str, Any]
) -> _RemovedSource | None:
    """
    Return what an SMB or NFS music source read, None when its settings could not mount a share.

    :param mass: The MusicAssistant instance.
    :param raw_conf: The raw (stored) provider config of the source.
    :raises InvalidDataError: When a setup value can not be decrypted.
    """
    setup_data = raw_conf.get("setup_data")
    if not isinstance(setup_data, dict):
        return None
    values = {
        key: mass.config.decrypt_string(value) if isinstance(value, str) else value
        for key, value in setup_data.items()
    }
    server = str(values.get(CONF_HOST) or "").strip()
    stored_subfolder = str(values.get(CONF_SUBFOLDER) or "")
    content_type = str(values.get(CONF_CONTENT_TYPE) or DEFAULT_CONTENT_TYPE)
    if raw_conf["domain"] == SMB_DOMAIN:
        stored_share = str(values.get(CONF_SHARE) or "")
        username = str(values.get(CONF_USERNAME) or "").strip()
        password = str(values.get(CONF_PASSWORD) or "")
        # the SMB provider mounted as guest without a user, or with the user named guest; the
        # mount backends take a user only together with its password
        has_credentials = bool(username) and username.lower() != "guest" and bool(password)
        source = _RemovedSource(
            share_type=ShareType.CIFS,
            server=server,
            share=stored_share.strip(),
            username=username if has_credentials else None,
            password=password if has_credentials else None,
            version=str(values.get(CONF_SMB_VERSION) or "") or None,
            subfolder=stored_subfolder.replace("\\", "/").strip("/"),
            content_type=content_type,
            name_postfix=stored_subfolder or stored_share or None,
        )
        # the SMB provider refused such a share
        valid_share = bool(source.share) and not any(char in source.share for char in "/\\")
    else:
        stored_export_path = str(values.get(CONF_EXPORT_PATH) or "")
        export_path = stored_export_path.strip()
        source = _RemovedSource(
            share_type=ShareType.NFS,
            server=server,
            # compared and stored the way the storage controller keeps an export: /a/ is /a
            share=str(PurePosixPath(export_path)),
            username=None,
            password=None,
            version=str(values.get(CONF_NFS_VERSION) or "") or None,
            subfolder=stored_subfolder.strip().lstrip("/"),
            content_type=content_type,
            name_postfix=stored_subfolder or PurePosixPath(stored_export_path).name or None,
        )
        # the NFS provider refused such an export
        valid_share = export_path.startswith("/") and is_safe_path(export_path)
    if not server or not valid_share or _steps_up(source.subfolder):
        return None
    return source


def _steps_up(subfolder: str) -> bool:
    """
    Return whether a relative path goes up a folder anywhere, which may lead out of the share.

    A name that starts with two dots is a plain name.

    :param subfolder: A relative path with forward slashes.
    """
    return ".." in subfolder.split("/")


def _find_share(
    shares: dict[str, NetworkShareSpec], source: _RemovedSource
) -> NetworkShareSpec | None:
    """
    Return the network share Music Assistant manages that a source reads, if there is one.

    :param shares: The network shares Music Assistant manages, including the new ones.
    :param source: The source.
    """
    key = share_key(source.share_type, source.server, source.share)
    return next(
        (
            spec
            for spec in shares.values()
            if share_key(spec.share_type, spec.server, spec.share) == key
        ),
        None,
    )


async def _new_share(
    mass: MusicAssistant, mounter: ShareMounter, source: _RemovedSource, taken: set[str]
) -> NetworkShareSpec:
    """
    Return a new network share for the share of a source, with a free name and its path.

    :param mass: The MusicAssistant instance.
    :param mounter: The mount backend of this server.
    :param source: The source.
    :param taken: The names of the network shares Music Assistant manages.
    """
    versions = mounter.supported_versions.get(source.share_type) or []
    return await mounter.assign_name(
        NetworkShareSpec(
            name="",
            share_type=source.share_type,
            server=source.server,
            share=source.share,
            backend=mounter.backend,
            path="",
            username=source.username,
            password=mass.config.encrypt_string(source.password) if source.password else None,
            # a version the backend can not pin is negotiated
            version=source.version if source.version in versions else None,
        ),
        taken,
    )


def _share_credentials(
    mass: MusicAssistant,
    spec: NetworkShareSpec,
    holder: tuple[str, _RemovedSource],
    other: tuple[str, _RemovedSource],
) -> str:
    """
    Give a new network share that two sources read the credentials to mount it with.

    The share keeps the credentials it has, unless it has none and the other source has some.
    Returns the instance id of the source whose credentials the share carries.

    :param mass: The MusicAssistant instance.
    :param spec: The new network share, changed in place.
    :param holder: The instance id and the source whose credentials the share carries.
    :param other: The instance id and the other source that reads the share.
    """
    (holder_id, holder_source), (other_id, other_source) = holder, other
    if (other_source.username, other_source.password) == (
        holder_source.username,
        holder_source.password,
    ):
        return holder_id
    chosen = holder_id
    if holder_source.password is None and other_source.password is not None:
        spec.username = other_source.username
        spec.password = mass.config.encrypt_string(other_source.password)
        chosen = other_id
    LOGGER.warning(
        "Music sources %s and %s read the same network share with different credentials; it "
        "is mounted with those of %s",
        holder_id,
        other_id,
        chosen,
    )
    return chosen


def _converted_config(
    mass: MusicAssistant, raw_conf: Mapping[str, Any], source: _RemovedSource, path: str, name: str
) -> dict[str, Any]:
    """
    Return the raw provider config of the Local files source that a source becomes.

    :param mass: The MusicAssistant instance.
    :param raw_conf: The raw (stored) provider config of the source.
    :param source: The source.
    :param path: The folder the Local files source reads.
    :param name: The name the source was shown with.
    """
    config = {
        **raw_conf,
        "domain": LOCAL_FILES_DOMAIN,
        "name": name,
        "last_error": None,
        "setup_data": {
            CONF_CONTENT_TYPE: mass.config.encrypt_string(source.content_type),
            CONF_PATH: mass.config.encrypt_string(path),
        },
    }
    if isinstance(values := raw_conf.get("values"), dict):
        config["values"] = {key: value for key, value in values.items() if key != CONF_CACHE_MODE}
    return config


def _shown_name(
    raw_configs: Mapping[str, Any], instance_id: str, domain: str, source: _RemovedSource
) -> str:
    """
    Return the name a source was shown with: its own, else the one its provider gave it.

    :param raw_configs: The raw (stored) provider configs, before any conversion.
    :param instance_id: The instance id of the source.
    :param domain: The domain of the removed provider of the source.
    :param source: The source.
    """
    if name := raw_configs[instance_id].get("name"):
        return str(name)
    provider_name = REMOVED_PROVIDER_NAMES[domain]
    instances = [
        key
        for key, raw_conf in raw_configs.items()
        if isinstance(raw_conf, dict) and raw_conf.get("domain") == domain
    ]
    if len(instances) <= 1:
        return provider_name
    postfix = source.name_postfix or str(instances.index(instance_id) + 1)
    return f"{provider_name} [{postfix}]"


async def _update_library(mass: MusicAssistant, instance_ids: list[str]) -> None:
    """
    Give the library rows of converted sources the domain of Local files.

    :param mass: The MusicAssistant instance.
    :param instance_ids: The instance ids of the converted sources.
    """
    # bound one by one: execute_write has no list parameter support
    params = {f"instance_{index}": instance_id for index, instance_id in enumerate(instance_ids)}
    placeholders = ", ".join(f":{name}" for name in params)
    await mass.music.database.execute_write(
        f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET provider_domain = :domain "
        f"WHERE provider_instance IN ({placeholders})",
        {"domain": LOCAL_FILES_DOMAIN, **params},
    )
