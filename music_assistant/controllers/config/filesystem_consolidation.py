"""
Conversion of the SMB and NFS music sources into Local files sources.

The SMB and NFS providers mounted their network share themselves. A network share is a storage
location now, mounted by the Home Assistant Supervisor or by the server itself, and a Local
files source reads a folder in a storage location. So each SMB and NFS source gets the storage
location of its share and becomes a Local files source on the same folder. It keeps its
instance id, and with that its library, its access record, its options and a name of its own.
Without a name of its own it shows the default name of a Local files source. The playlists of
the builtin provider name the domain of an entry's provider, so their entries of a converted
source get the domain of Local files.

It runs at every start, as such a source can come back with a downgrade or a restored backup,
and does something only when one is there. Unlike the `settings.json` migrations in
`migrations.py`, it needs the library database and, under a Supervisor, the mounts the
Supervisor has, so it runs from `MusicAssistant.start()` once the core controllers are up. It
never mounts anything itself: a share whose server is off at this point is still converted,
and its source loads once the storage controller mounted it.

TODO: remove after 2.13 release
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.errors import InvalidDataError, SetupFailedError

from music_assistant.constants import (
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
from music_assistant.helpers.playlists import (
    generate_m3u,
    parse_m3u,
    parse_m3u_playlist_image,
    parse_m3u_playlist_name,
)
from music_assistant.helpers.security import is_safe_path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant.controllers.storage.backends.base import ShareMounter
    from music_assistant.helpers.playlists import PlaylistItem
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

SMB_DOMAIN: Final[str] = "filesystem_smb"
NFS_DOMAIN: Final[str] = "filesystem_nfs"
LOCAL_FILES_DOMAIN: Final[str] = "filesystem_local"
REMOVED_PROVIDER_DOMAINS: Final[frozenset[str]] = frozenset({SMB_DOMAIN, NFS_DOMAIN})
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
# the folder in the storage path that the builtin provider keeps its playlists in
PLAYLISTS_FOLDER: Final[str] = "playlists"


async def consolidate_filesystem_sources(mass: MusicAssistant) -> None:
    """
    Turn every SMB and NFS music source into a Local files source on a storage location.

    Changes nothing when there is no such source. Never raises: a source that can not be
    converted now, e.g. because the Supervisor can not say which shares it has mounted, stays
    as it is and is tried again at the next start.

    :param mass: The MusicAssistant instance, with its core controllers set up.
    """
    raw_configs: dict[str, Any] = mass.config.get(CONF_PROVIDERS, {})
    if not (sources := _read_removed_sources(mass, raw_configs)):
        return
    try:
        async with asyncio.timeout(SUPERVISOR_TIMEOUT):
            conversions = await _plan_conversions(mass, raw_configs, sources)
    except (BackendUnavailable, SetupFailedError, TimeoutError) as err:
        LOGGER.warning(
            "Leaving music sources %s as they are, the Supervisor can not tell which network "
            "shares it has mounted (%s). The next start tries again.",
            ", ".join(sources),
            str(err) or type(err).__name__,
        )
        return
    except Exception:
        LOGGER.exception(
            "Unable to convert music sources %s, the next start tries again", ", ".join(sources)
        )
        return
    instance_ids = [conversion.instance_id for conversion in conversions]
    # the playlists and the library go before the settings: both updates can be repeated as
    # they are, and only a start that still finds the sources converts, so the next start
    # finishes an update that was cut short
    await _update_playlists(mass, raw_configs, set(instance_ids))
    try:
        await _update_library(mass, instance_ids)
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
    except Exception:
        # an escape here would abort the boot; what did not reach the disk is converted again
        # at the next start
        LOGGER.exception(
            "Unable to convert music sources %s, the next start tries again", ", ".join(sources)
        )
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


class _Unconvertible(Exception):
    """A source that can not be converted; the message says why, naming none of its settings."""


def _read_removed_sources(
    mass: MusicAssistant, raw_configs: Mapping[str, Any]
) -> dict[str, _RemovedSource]:
    """
    Return what each SMB and NFS music source that can be converted reads, by instance id.

    A source that can not be converted is logged with the reason and left out: its config stays
    as it is, neither listed nor loaded now that its provider is gone, and its library stays.

    :param mass: The MusicAssistant instance.
    :param raw_configs: The raw (stored) provider configs.
    """
    sources: dict[str, _RemovedSource] = {}
    for instance_id, raw_conf in raw_configs.items():
        if not isinstance(raw_conf, dict) or raw_conf.get("domain") not in REMOVED_PROVIDER_DOMAINS:
            continue
        try:
            sources[instance_id] = _read_removed_source(mass, raw_conf)
        except Exception as err:
            LOGGER.warning(
                "Leaving music source %s as it is, %s. The next start tries again.",
                instance_id,
                _reason(err),
            )
    return sources


def _reason(err: Exception) -> str:
    """
    Return why a source can not be converted, naming none of its settings.

    :param err: What reading the source raised.
    """
    if isinstance(err, _Unconvertible):
        return str(err)
    if isinstance(err, InvalidDataError):
        return "its settings can not be decrypted"
    return f"its settings can not be read ({type(err).__name__})"


async def _plan_conversions(
    mass: MusicAssistant, raw_configs: Mapping[str, Any], sources: dict[str, _RemovedSource]
) -> list[_Conversion]:
    """
    Return the conversion of each source. Changes nothing.

    :param mass: The MusicAssistant instance.
    :param raw_configs: The raw (stored) provider configs.
    :param sources: What each source to convert reads, by instance id.
    :raises BackendUnavailable: When the Supervisor does not let this server manage its mounts.
    :raises SetupFailedError: When the Supervisor does not list its mounts.
    """
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
        path = os.path.normpath(os.path.join(share_path, source.subfolder))
        conversions.append(
            _Conversion(
                instance_id=instance_id,
                config=_converted_config(
                    mass,
                    raw_configs[instance_id],
                    source,
                    path,
                    _default_name(mass, raw_configs, sources, path),
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


def _read_removed_source(mass: MusicAssistant, raw_conf: Mapping[str, Any]) -> _RemovedSource:
    """
    Return what an SMB or NFS music source read.

    :param mass: The MusicAssistant instance.
    :param raw_conf: The raw (stored) provider config of the source.
    :raises InvalidDataError: When a setup value can not be decrypted.
    :raises _Unconvertible: When its settings could not mount a share.
    """
    setup_data = raw_conf.get("setup_data")
    if not isinstance(setup_data, dict):
        raise _Unconvertible("it has no settings")
    values = {
        key: mass.config.decrypt_string(value) if isinstance(value, str) else value
        for key, value in setup_data.items()
    }
    server = str(values.get(CONF_HOST) or "").strip()
    stored_subfolder = str(values.get(CONF_SUBFOLDER) or "")
    content_type = str(values.get(CONF_CONTENT_TYPE) or DEFAULT_CONTENT_TYPE)
    if raw_conf["domain"] == SMB_DOMAIN:
        username = str(values.get(CONF_USERNAME) or "").strip()
        password = str(values.get(CONF_PASSWORD) or "")
        # the SMB provider mounted as guest without a user, or with the user named guest; the
        # mount backends take a user only together with its password
        has_credentials = bool(username) and username.lower() != "guest" and bool(password)
        source = _RemovedSource(
            share_type=ShareType.CIFS,
            server=server,
            share=str(values.get(CONF_SHARE) or "").strip(),
            username=username if has_credentials else None,
            password=password if has_credentials else None,
            version=str(values.get(CONF_SMB_VERSION) or "") or None,
            subfolder=stored_subfolder.replace("\\", "/").strip("/"),
            content_type=content_type,
        )
        # the SMB provider refused such a share
        if not source.share or any(char in source.share for char in "/\\"):
            raise _Unconvertible("its share name is not valid")
    else:
        export_path = str(values.get(CONF_EXPORT_PATH) or "").strip()
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
        )
        # the NFS provider refused such an export
        if not export_path.startswith("/") or not is_safe_path(export_path):
            raise _Unconvertible("its export path is not valid")
    if not server:
        raise _Unconvertible("it names no server")
    if _steps_up(source.subfolder):
        raise _Unconvertible("its subfolder goes up a folder")
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
    mass: MusicAssistant,
    raw_conf: Mapping[str, Any],
    source: _RemovedSource,
    path: str,
    default_name: str,
) -> dict[str, Any]:
    """
    Return the raw provider config of the Local files source that a source becomes.

    :param mass: The MusicAssistant instance.
    :param raw_conf: The raw (stored) provider config of the source.
    :param source: The source.
    :param path: The folder the Local files source reads.
    :param default_name: The default name Local files gives the source.
    """
    config = {
        **raw_conf,
        "domain": LOCAL_FILES_DOMAIN,
        # without a name of its own it holds none, as a new Local files source does
        "name": raw_conf.get("name") or None,
        "default_name": default_name,
        "last_error": None,
        "setup_data": {
            CONF_CONTENT_TYPE: mass.config.encrypt_string(source.content_type),
            CONF_PATH: mass.config.encrypt_string(path),
        },
    }
    if isinstance(values := raw_conf.get("values"), dict):
        config["values"] = {key: value for key, value in values.items() if key != CONF_CACHE_MODE}
    return config


def _default_name(
    mass: MusicAssistant,
    raw_configs: Mapping[str, Any],
    sources: Mapping[str, _RemovedSource],
    path: str,
) -> str:
    """
    Return the default name Local files gives a converted source when it loads.

    :param mass: The MusicAssistant instance.
    :param raw_configs: The raw (stored) provider configs, before any conversion.
    :param sources: What each source to convert reads, by instance id.
    :param path: The folder the converted source reads.
    """
    name = mass.get_provider_manifest(LOCAL_FILES_DOMAIN).name
    # as Provider.default_name derives it with the postfix of Local files, the name of the folder
    # it reads, which is never empty for a folder in a share; written now, so a source that can
    # not load yet is told apart from the others
    local_files = [
        instance_id
        for instance_id, raw_conf in raw_configs.items()
        if instance_id in sources
        or (isinstance(raw_conf, dict) and raw_conf.get("domain") == LOCAL_FILES_DOMAIN)
    ]
    if len(local_files) <= 1:
        return name
    return f"{name} [{PurePosixPath(path).name}]"


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


async def _update_playlists(
    mass: MusicAssistant, raw_configs: Mapping[str, Any], instance_ids: set[str]
) -> None:
    """
    Give the converted sources' entries in the builtin provider's playlists the Local files domain.

    Never raises: a playlist that can not be read or written is left as it is.

    :param mass: The MusicAssistant instance.
    :param raw_configs: The raw (stored) provider configs, before any conversion.
    :param instance_ids: The instance ids of the sources this start converts.
    """
    # an entry that names only the domain of its source can be of any source of that domain,
    # so it is converted only when no source of the domain is left as it is
    left = {
        raw_conf["domain"]
        for instance_id, raw_conf in raw_configs.items()
        if isinstance(raw_conf, dict)
        and raw_conf.get("domain") in REMOVED_PROVIDER_DOMAINS
        and instance_id not in instance_ids
    }
    domains = {raw_configs[instance_id]["domain"] for instance_id in instance_ids} - left
    folder = Path(mass.storage_path, PLAYLISTS_FOLDER)
    try:
        filenames = sorted(await asyncio.to_thread(os.listdir, folder))
    except FileNotFoundError:
        return
    except OSError as err:
        LOGGER.warning(
            "Leaving the playlists as they are, their folder can not be read (%s)",
            type(err).__name__,
        )
        return
    for filename in filenames:
        if filename.endswith(".m3u"):
            await asyncio.to_thread(_update_playlist, folder / filename, instance_ids, domains)


def _update_playlist(path: Path, instance_ids: set[str], domains: set[str]) -> None:
    """
    Give the entries of converted sources in a playlist file the Local files domain (blocking).

    Leaves a file alone that holds no such entry, or that can not be read or written.

    :param path: The playlist file.
    :param instance_ids: The instance ids of the sources this start converts.
    :param domains: The domains whose entries that name no source are converted too.
    """
    filename = path.name
    try:
        with path.open(encoding="utf-8", newline="") as file:
            data = file.read()
        # the writer ends its lines with \n, a file with \r\n gets those back
        text = data.replace("\r\n", "\n")
        name = parse_m3u_playlist_name(text) or path.stem
        image = parse_m3u_playlist_image(text)
        entries = parse_m3u(text)
        as_read = generate_m3u(name, entries, image)
        changed = False
        for entry in entries:
            changed |= _point_at_local_files(entry, instance_ids, domains)
    except Exception as err:
        LOGGER.warning(
            "Leaving playlist %s as it is, it can not be read (%s)", filename, type(err).__name__
        )
        return
    if not changed:
        return
    if as_read != text:
        # the parser skips what it does not understand, so writing this file back would change
        # more than the domains
        LOGGER.warning(
            "Leaving playlist %s as it is, it is not in the form Music Assistant writes it",
            filename,
        )
        return
    content = generate_m3u(name, entries, image)
    try:
        _replace_file(path, content if text == data else content.replace("\n", "\r\n"))
    except Exception as err:
        LOGGER.warning(
            "Leaving playlist %s as it is, it can not be written (%s)",
            filename,
            type(err).__name__,
        )
        return
    LOGGER.info("Updated the entries of converted music sources in playlist %s", filename)


def _point_at_local_files(entry: PlaylistItem, instance_ids: set[str], domains: set[str]) -> bool:
    """
    Give each part of a playlist entry that names a converted source the domain of Local files.

    Returns whether the entry changed.

    :param entry: The playlist entry, changed in place.
    :param instance_ids: The instance ids of the sources this start converts.
    :param domains: The domains whose parts that name no source are converted too.
    """

    def _converts(domain: str, instance_id: str) -> bool:
        if domain not in REMOVED_PROVIDER_DOMAINS:
            return False
        # a part names only the domain with no instance, or with the domain in its place
        return instance_id in instance_ids or (instance_id in ("", domain) and domain in domains)

    changed = False
    # the path is the URI of the entry's mapping on the provider with that domain and item id
    scheme, _, rest = entry.path.partition("://")
    item_id = rest.partition("/")[2]
    path_instance = next(
        (
            mapping.instance_id
            for mapping in entry.providers
            if (mapping.domain, mapping.item_id) == (scheme, item_id)
        ),
        "",
    )
    if _converts(scheme, path_instance):
        entry.path = f"{LOCAL_FILES_DOMAIN}://{rest}"
        changed = True
    for mapping in entry.providers:
        if _converts(mapping.domain, mapping.instance_id):
            mapping.domain = LOCAL_FILES_DOMAIN
            changed = True
    for reference in (*entry.artists, entry.album, entry.podcast):
        if reference is not None and _converts(
            reference.provider_domain, reference.provider_instance
        ):
            reference.provider_domain = LOCAL_FILES_DOMAIN
            changed = True
    return changed


def _replace_file(path: Path, content: str) -> None:
    """
    Replace the content of a file in one step, keeping its permissions (blocking).

    :param path: The file.
    :param content: The new content.
    """
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(content)
        shutil.copymode(path, temp_path)
        temp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
