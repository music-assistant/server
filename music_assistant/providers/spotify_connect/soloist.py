"""
Shared helpers for Spotify Soloist, Spotify's official headless Connect client for Linux.

Owned by the Spotify Connect provider and deliberately provider-neutral: the
Spotify music provider reuses these helpers instead of shipping a second
implementation. It manages a single shared install of the ``soloist`` binary
under the server's storage dir and offers a typed client for the daemon's local
WebSocket API (see https://developer.spotify.com/documentation/soloist).

SECURITY NOTE: the soloist daemon takes the user's personal API key on its
command line. This module never sees or logs that key, and callers that manage
the daemon process must equally never log its argv.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import platform
import re
import shutil
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

import aiofiles
from aiohttp import ClientError, ClientTimeout, ClientWebSocketResponse, WSMsgType
from mashumaro import DataClassDictMixin
from mashumaro.exceptions import InvalidFieldValue, MissingField
from music_assistant_models.errors import MusicAssistantError
from yarl import URL

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.providers.spotify_connect.soloist")

# Official per-architecture release archives (docs: reference/downloads-and-updates).
CDN_URL_TEMPLATE: Final[str] = "https://soloist-builds.spotifycdn.com/soloist_release_{arch}.tar.gz"

# The daemon publishes its WebSocket endpoint as two small files in its data dir.
WS_ADDR_FILE: Final[str] = "ws.addr"
WS_PORT_FILE: Final[str] = "ws.port"

# soloist exits with this code once its build passed the 90-day expiry.
EXIT_CODE_BUILD_EXPIRED: Final[int] = 10

# platform.machine() values mapped to the CDN artifact architecture;
# anything else has no official build and is rejected.
_MACHINE_TO_ARCH: Final[dict[str, str]] = {
    "aarch64": "arm64",
    "arm64": "arm64",
    # the official arm32 build targets ARMv7; older ARM cores are unsupported
    "armv7l": "arm32",
    "armv8l": "arm32",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}

# A download may redirect, but only within Spotify's own infrastructure.
_TRUSTED_DOWNLOAD_DOMAINS: Final[tuple[str, ...]] = ("spotifycdn.com", "spotify.com")
_MAX_REDIRECTS: Final[int] = 5
_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

# Conservative caps so a misbehaving CDN response cannot fill the disk:
# real release archives are only a few tens of MB.
_MAX_ARCHIVE_SIZE: Final[int] = 64 * 1024 * 1024
_MAX_EXTRACTED_SIZE: Final[int] = 128 * 1024 * 1024
_DOWNLOAD_CHUNK_SIZE: Final[int] = 64 * 1024

# Builds expire 90 days after their build date; start looking for a replacement
# 14 days ahead of that so there is a comfortable update window.
_BUILD_EXPIRY_SECONDS: Final[int] = 90 * 24 * 3600
_REFRESH_AGE_SECONDS: Final[int] = 76 * 24 * 3600

# ELF identity per architecture: (EI_CLASS, e_machine).
_ELF_IDENT: Final[dict[str, tuple[int, int]]] = {
    "arm64": (2, 0xB7),  # 64-bit, EM_AARCH64
    "arm32": (1, 0x28),  # 32-bit, EM_ARM
    "x86_64": (2, 0x3E),  # 64-bit, EM_X86_64
}

# Every SoloistBinaryManager instance manages the same shared install paths, so
# the single-flight install lock is shared process-wide as well.
_INSTALL_LOCK: Final = asyncio.Lock()

_VERSION_CMD_TIMEOUT: Final[float] = 10.0
_DOWNLOAD_TIMEOUT: Final[float] = 300.0
_HEAD_TIMEOUT: Final[float] = 30.0

# --version output is free-form text (the docs give no schema); fish out a
# version token and an ISO-like timestamp defensively.
_VERSION_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)*)\b")
_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?)\b"
)

# The docs do not guarantee when the endpoint files appear, so poll for them.
_ENDPOINT_POLL_INTERVAL: Final[float] = 0.25
_WS_HEARTBEAT: Final[float] = 30.0
_COMMAND_RESULT_TIMEOUT: Final[float] = 10.0


class SoloistError(MusicAssistantError):
    """Base error for all soloist helper failures."""


class ConsentRequiredError(SoloistError):
    """A download from Spotify's CDN is needed but the user did not consent (yet)."""


class UnsupportedPlatformError(SoloistError):
    """No official soloist build exists for this platform/architecture."""


class DownloadFailedError(SoloistError):
    """The soloist release archive could not be downloaded."""


class InvalidArchiveError(SoloistError):
    """The downloaded release archive or its binary failed validation."""


class BuildExpiredError(SoloistError):
    """The soloist build passed its 90-day expiry and no replacement is available."""


@dataclass
class SoloistEntity(DataClassDictMixin):
    """A Spotify entity (track/album/playlist/...) as used in item/context fields."""

    uri: str
    entity_type: str
    # decorations is an extensible bag (identity.name, playback.duration_ms, ...)
    decorations: dict[str, Any] = field(default_factory=dict)


@dataclass
class SoloistPosition(DataClassDictMixin):
    """Playback position reference point, to be interpolated with speed over time."""

    position_ms: int
    timestamp_ms: int
    speed: float = 1.0


@dataclass
class SoloistPlaybackOptions(DataClassDictMixin):
    """Playback options (shuffle/repeat/speed)."""

    shuffle: bool = False
    repeat: str = "off"
    playback_speed: float = 1.0
    modes: dict[str, Any] = field(default_factory=dict)


@dataclass
class SoloistQueueEntry(DataClassDictMixin):
    """One entry in the previous/upcoming queue listing."""

    uid: str
    source: str
    item: SoloistEntity | None = None


@dataclass
class SoloistAuthState(DataClassDictMixin):
    """Payload of the ``auth_state`` event."""

    logged_in: bool
    is_active: bool
    device_name: str | None = None


@dataclass
class SoloistPlaybackState(DataClassDictMixin):
    """Payload of the ``playback_state`` snapshot and the ``playback_changed`` delta."""

    status: str
    item: SoloistEntity | None = None
    context: SoloistEntity | None = None
    position: SoloistPosition | None = None
    volume: int | None = None
    is_active: bool | None = None
    options: SoloistPlaybackOptions | None = None
    available_actions: dict[str, Any] = field(default_factory=dict)


@dataclass
class SoloistTrackChanged(DataClassDictMixin):
    """Payload of the ``track_changed`` event."""

    item: SoloistEntity | None = None


@dataclass
class SoloistPositionSync(DataClassDictMixin):
    """Payload of the ``position_sync`` event."""

    position: SoloistPosition


@dataclass
class SoloistVolumeChanged(DataClassDictMixin):
    """Payload of the ``volume_changed`` event."""

    volume: int


@dataclass
class SoloistDeviceChanged(DataClassDictMixin):
    """Payload of the ``device_changed`` event."""

    is_active: bool


@dataclass
class SoloistContextChanged(DataClassDictMixin):
    """Payload of the ``context_changed`` event."""

    context: SoloistEntity | None = None


@dataclass
class SoloistOptionsChanged(DataClassDictMixin):
    """Payload of the ``options_changed`` event."""

    options: SoloistPlaybackOptions


@dataclass
class SoloistQueueChanged(DataClassDictMixin):
    """Payload of the ``queue_changed`` event."""

    previous: list[SoloistQueueEntry] = field(default_factory=list)
    upcoming: list[SoloistQueueEntry] = field(default_factory=list)


@dataclass
class SoloistCommandResult(DataClassDictMixin):
    """Payload of the ``command_result`` acknowledgement event."""

    command: str


@dataclass
class SoloistErrorMessage(DataClassDictMixin):
    """Payload of the ``error`` event."""

    message: str


SoloistEventData = (
    SoloistAuthState
    | SoloistPlaybackState
    | SoloistTrackChanged
    | SoloistPositionSync
    | SoloistVolumeChanged
    | SoloistDeviceChanged
    | SoloistContextChanged
    | SoloistOptionsChanged
    | SoloistQueueChanged
    | SoloistCommandResult
    | SoloistErrorMessage
)

# Maps documented event types to their payload model;
# unrecognized types are passed through as generic raw events.
_EVENT_MODELS: Final[dict[str, type[SoloistEventData]]] = {
    "auth_state": SoloistAuthState,
    "playback_state": SoloistPlaybackState,
    "playback_changed": SoloistPlaybackState,
    "track_changed": SoloistTrackChanged,
    "position_sync": SoloistPositionSync,
    "volume_changed": SoloistVolumeChanged,
    "device_changed": SoloistDeviceChanged,
    "context_changed": SoloistContextChanged,
    "options_changed": SoloistOptionsChanged,
    "queue_changed": SoloistQueueChanged,
    "command_result": SoloistCommandResult,
    "error": SoloistErrorMessage,
}


@dataclass
class SoloistEvent:
    """A single event received from the daemon, with its decoded payload when recognized."""

    type: str
    data: SoloistEventData | None
    raw: dict[str, Any]


# Called with the decoded event for every WebSocket event.
EventCallback = Callable[[SoloistEvent], Awaitable[None]]


class SoloistBinaryManager:
    """
    Manages the single shared soloist binary install for all consumers.

    The install lives under ``<storage_path>/soloist``; concurrent callers
    share one download.
    """

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the binary manager.

        :param mass: The MusicAssistant instance (for its HTTP session and storage path).
        """
        self.mass = mass
        self._install_dir = Path(mass.storage_path) / "soloist"
        self._binary_path = self._install_dir / "soloist"
        self._previous_path = self._install_dir / "soloist.prev"
        self._metadata_path = self._install_dir / "soloist.meta.json"

    @property
    def binary_path(self) -> Path:
        """Path where the soloist binary is (or will be) installed."""
        return self._binary_path

    async def ensure_binary(self, consent: bool) -> Path:
        """
        Return the path to a validated soloist binary, downloading it when needed.

        :param consent: Whether the user consented to downloading the binary from
            Spotify's CDN. An already-installed valid binary is returned without
            any network access, regardless of this flag.
        :raises ConsentRequiredError: A download is needed but consent was not given.
        :raises UnsupportedPlatformError: No soloist build exists for this platform.
        :raises DownloadFailedError: The release archive could not be downloaded.
        :raises InvalidArchiveError: The downloaded archive or binary failed validation.
        :raises BuildExpiredError: The freshly downloaded build has already expired.
        """
        arch = _resolve_architecture()
        async with _INSTALL_LOCK:
            if await self._installed_returncode() == 0 and not self._installed_expired():
                return self._binary_path
            await self._install_with_consent(consent, arch)
            return self._binary_path

    async def ensure_fresh(self, consent: bool) -> Path:
        """
        Return a validated soloist binary, refreshing it when it is (close to) expiry.

        Builds expire 90 days after their build date: an installed build that
        already expired is replaced immediately, one nearing expiry only when
        the CDN offers a different build. When the refresh fails (e.g. offline)
        a still-valid binary is returned with a warning instead.

        :param consent: Whether the user consented to downloading from Spotify's
            CDN. Without consent a still-valid installed binary is returned
            as-is (no proactive refresh).
        :raises ConsentRequiredError: A download is needed but consent was not given.
        :raises UnsupportedPlatformError: No soloist build exists for this platform.
        :raises DownloadFailedError: No usable binary is installed and the download failed.
        :raises InvalidArchiveError: The downloaded archive or binary failed validation.
        :raises BuildExpiredError: The installed build expired and no valid
            replacement could be obtained.
        """
        arch = _resolve_architecture()
        async with _INSTALL_LOCK:
            returncode = await self._installed_returncode()
            if returncode not in (0, EXIT_CODE_BUILD_EXPIRED):
                # missing or broken install: plain (re)install
                await self._install_with_consent(consent, arch)
                return self._binary_path
            expired = returncode == EXIT_CODE_BUILD_EXPIRED or self._installed_expired()
            if not expired and (not consent or not self._due_for_update()):
                return self._binary_path
            if expired and not consent:
                raise ConsentRequiredError(
                    "Updating the expired soloist binary requires user consent"
                )
            if not expired and not await self._update_available(arch):
                return self._binary_path
            try:
                await self._download_and_install(arch)
            except SoloistError as err:
                if expired:
                    if isinstance(err, DownloadFailedError):
                        raise BuildExpiredError(
                            "soloist build expired and no replacement could be downloaded"
                        ) from err
                    raise
                LOGGER.warning("soloist update failed, keeping the current binary: %s", err)
            return self._binary_path

    def diagnostics(self) -> dict[str, Any]:
        """
        Return diagnostic details about the installed binary.

        Contains install/build metadata only; never any key or session material.
        """
        metadata = self._read_metadata()
        if metadata is None:
            return {"installed": False}
        return {
            "installed": True,
            "sha256": metadata.sha256,
            "etag": metadata.etag,
            "version": metadata.version,
            "version_raw": metadata.version_raw,
            "installed_at": metadata.installed_at,
            "build_timestamp": metadata.build_timestamp,
            # upper-bound estimate when the build timestamp could not be parsed
            "expires_at": (metadata.build_timestamp or metadata.installed_at)
            + _BUILD_EXPIRY_SECONDS,
        }

    async def _install_with_consent(self, consent: bool, arch: str) -> None:
        """Download and install the binary, requiring user consent first."""
        if not consent:
            raise ConsentRequiredError("Downloading the soloist binary requires user consent")
        await self._download_and_install(arch)

    async def _installed_returncode(self) -> int | None:
        """Return the ``--version`` exit code of the installed binary, or None if unrunnable."""
        if not self._binary_path.is_file():
            return None
        try:
            returncode, _ = await check_output(
                str(self._binary_path), "--version", timeout=_VERSION_CMD_TIMEOUT
            )
        except OSError, TimeoutError:
            return None
        return returncode

    def _installed_expired(self) -> bool:
        """
        Return whether the installed build's known build timestamp has expired.

        Defense in depth next to the exit-code check: whether ``--version``
        itself reports expiry is not documented, so an expired-by-timestamp
        build is refused even when the binary still runs.
        """
        metadata = self._read_metadata()
        return metadata is not None and _build_expired(metadata.build_timestamp)

    def _due_for_update(self) -> bool:
        """Return whether the installed build is old enough to look for an update."""
        metadata = self._read_metadata()
        if metadata is None:
            return True
        anchor = metadata.build_timestamp or metadata.installed_at
        return (time.time() - anchor) >= _REFRESH_AGE_SECONDS

    async def _update_available(self, arch: str) -> bool:
        """Return whether the CDN offers a different build than the installed one."""
        metadata = self._read_metadata()
        try:
            remote_etag = await self._fetch_remote_etag(arch)
        except (ClientError, TimeoutError, OSError) as err:
            LOGGER.warning("Unable to check for a soloist update: %s", err)
            return False
        return remote_etag is not None and (metadata is None or metadata.etag != remote_etag)

    async def _fetch_remote_etag(self, arch: str) -> str | None:
        """Return the ETag the CDN currently serves for the given architecture, if any."""
        current_url = CDN_URL_TEMPLATE.format(arch=arch)
        # follow redirects manually so every hop passes the same host allowlist
        # the download path enforces
        for _ in range(_MAX_REDIRECTS + 1):
            _validate_download_url(current_url)
            async with self.mass.http_session.head(
                current_url, allow_redirects=False, timeout=ClientTimeout(total=_HEAD_TIMEOUT)
            ) as resp:
                if resp.status in _REDIRECT_STATUSES:
                    if not (location := resp.headers.get("Location")):
                        return None
                    current_url = str(URL(current_url).join(URL(location)))
                    continue
                if resp.status != HTTPStatus.OK:
                    return None
                return resp.headers.get("ETag")
        return None

    async def _download_and_install(self, arch: str) -> None:
        """Download, validate and atomically install the binary for the given architecture."""
        url = CDN_URL_TEMPLATE.format(arch=arch)
        try:
            await asyncio.to_thread(self._install_dir.mkdir, parents=True, exist_ok=True)
            temp_dir = Path(
                await asyncio.to_thread(tempfile.mkdtemp, prefix=".soloist-", dir=self._install_dir)
            )
        except OSError as err:
            raise DownloadFailedError(f"cannot prepare the soloist install dir: {err}") from err
        try:
            archive_path = temp_dir / "soloist.tar.gz"
            new_binary = temp_dir / "soloist"
            sha256, etag = await self._download_archive(url, archive_path)
            await asyncio.to_thread(_extract_binary_from_archive, archive_path, new_binary)
            await asyncio.to_thread(_validate_elf_header, new_binary, arch)
            version_raw = await self._swap_in_and_validate(new_binary)
            metadata = _BinaryMetadata(
                sha256=sha256,
                version_raw=version_raw,
                installed_at=time.time(),
                etag=etag,
                version=_parse_version_token(version_raw),
                build_timestamp=_parse_build_timestamp(version_raw),
            )
            # metadata is advisory (diagnostics + refresh hints): the binary is
            # already validated and installed, so never fail the install over it
            try:
                await asyncio.to_thread(self._write_metadata, metadata)
            except OSError as err:
                LOGGER.warning("Unable to persist soloist install metadata: %s", err)
            LOGGER.info("Installed soloist binary %s (%s)", metadata.version or "unknown", arch)
        finally:
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)

    async def _download_archive(self, url: str, dest: Path) -> tuple[str, str | None]:
        """
        Stream the release archive to a local file, following (validated) redirects.

        :return: Tuple of the archive's sha256 hexdigest and its ETag header (if any).
        """
        hasher = hashlib.sha256()
        current_url = url
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                _validate_download_url(current_url)
                async with self.mass.http_session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=ClientTimeout(total=_DOWNLOAD_TIMEOUT),
                ) as resp:
                    if resp.status in _REDIRECT_STATUSES:
                        location = resp.headers.get("Location")
                        if not location:
                            raise DownloadFailedError("soloist download redirect has no location")
                        current_url = str(URL(current_url).join(URL(location)))
                        continue
                    if resp.status != HTTPStatus.OK:
                        raise DownloadFailedError(
                            f"soloist download failed with HTTP {resp.status}"
                        )
                    etag = resp.headers.get("ETag")
                    size = 0
                    async with aiofiles.open(dest, "wb") as _file:
                        async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                            size += len(chunk)
                            if size > _MAX_ARCHIVE_SIZE:
                                raise DownloadFailedError("soloist archive exceeds the size limit")
                            hasher.update(chunk)
                            await _file.write(chunk)
                    return (hasher.hexdigest(), etag)
            raise DownloadFailedError("too many redirects while downloading soloist")
        except (ClientError, TimeoutError, OSError) as err:
            raise DownloadFailedError(f"soloist download failed: {err}") from err

    async def _swap_in_and_validate(self, new_binary: Path) -> str:
        """
        Atomically install the new binary and verify it actually runs.

        Shielded from cancellation so the install always reaches a consistent
        end state (committed or rolled back) even when the caller goes away.

        :return: The raw ``--version`` output of the installed binary.
        """
        inner = asyncio.ensure_future(self._swap_in_and_validate_inner(new_binary))
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            # the inner install continues to a consistent end state (committed
            # or rolled back); consume its late result so nothing goes unlogged
            inner.add_done_callback(_consume_install_result)
            raise

    async def _swap_in_and_validate_inner(self, new_binary: Path) -> str:
        """Perform the swap/validate/rollback sequence (see _swap_in_and_validate)."""

        def _swap_in() -> None:
            new_binary.chmod(0o755)
            if self._binary_path.exists():
                self._binary_path.replace(self._previous_path)
            new_binary.replace(self._binary_path)

        def _rollback() -> None:
            if self._previous_path.exists():
                self._previous_path.replace(self._binary_path)
            else:
                self._binary_path.unlink(missing_ok=True)

        def _commit() -> None:
            self._previous_path.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(_swap_in)
        except OSError as err:
            # keep SoloistError semantics so ensure_fresh's keep-the-old-binary
            # fallback also covers filesystem failures during the swap
            with suppress(OSError):
                await asyncio.to_thread(_rollback)
            raise InvalidArchiveError(f"failed to install the soloist binary: {err}") from err
        try:
            returncode, output = await check_output(
                str(self._binary_path), "--version", timeout=_VERSION_CMD_TIMEOUT
            )
        except (OSError, TimeoutError) as err:
            with suppress(OSError):
                await asyncio.to_thread(_rollback)
            raise InvalidArchiveError(f"soloist binary failed to run: {err}") from err
        if returncode == EXIT_CODE_BUILD_EXPIRED:
            with suppress(OSError):
                await asyncio.to_thread(_rollback)
            raise BuildExpiredError("the downloaded soloist build has already expired")
        if returncode != 0:
            with suppress(OSError):
                await asyncio.to_thread(_rollback)
            raise InvalidArchiveError(f"soloist --version exited with code {returncode}")
        version_raw = output.decode("utf-8", errors="replace").strip()
        if _build_expired(_parse_build_timestamp(version_raw)):
            with suppress(OSError):
                await asyncio.to_thread(_rollback)
            raise BuildExpiredError("the downloaded soloist build has already expired")
        await asyncio.to_thread(_commit)
        return version_raw

    def _read_metadata(self) -> _BinaryMetadata | None:
        """Return the persisted install metadata, if present and readable."""
        try:
            raw = json_loads(self._metadata_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            return _BinaryMetadata.from_dict(raw)
        except OSError, ValueError, TypeError, MissingField, InvalidFieldValue:
            return None

    def _write_metadata(self, metadata: _BinaryMetadata) -> None:
        """Persist install metadata next to the binary."""
        self._metadata_path.write_text(json_dumps(metadata.to_dict()), encoding="utf-8")


class SoloistClient:
    """
    Client for the local WebSocket API of a running soloist daemon.

    The daemon publishes its (loopback) endpoint as ``ws.addr``/``ws.port``
    files in its data directory; this client discovers it from there. Commands
    are sent over the events connection, so :meth:`listen_events` must be
    running for the command senders to work.
    """

    def __init__(self, mass: MusicAssistant, data_dir: Path, logger: logging.Logger) -> None:
        """
        Initialize the client.

        :param mass: The MusicAssistant instance (for its shared HTTP session).
        :param data_dir: The daemon's data directory (holds the endpoint files).
        :param logger: Logger to use for diagnostics.
        """
        self.mass = mass
        self.data_dir = data_dir
        self.logger = logger
        self._ws: ClientWebSocketResponse | None = None
        self._pending_results: dict[str, deque[asyncio.Future[None]]] = {}

    @property
    def connected(self) -> bool:
        """Whether the events WebSocket is currently connected."""
        return self._ws is not None and not self._ws.closed

    async def wait_until_ready(self, timeout: float = 30.0) -> bool:
        """
        Poll the daemon's data directory until the WebSocket endpoint is published.

        :param timeout: Maximum seconds to wait for the endpoint files.
        :return: True once the endpoint is known, False if the timeout elapses.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if await asyncio.to_thread(self._read_endpoint) is not None:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_ENDPOINT_POLL_INTERVAL)

    async def listen_events(self, on_event: EventCallback) -> None:
        """
        Connect to the daemon's WebSocket API and dispatch events until it closes.

        Returns normally when the connection is closed by either side; raises on
        connection errors so the caller can implement a reconnect loop.
        Malformed or unrecognized events are tolerated and never interrupt the
        stream.

        :param on_event: Coroutine called with a :class:`SoloistEvent` per event.
        :raises SoloistError: The daemon has not published its endpoint (yet).
        """
        if (endpoint := await asyncio.to_thread(self._read_endpoint)) is None:
            raise SoloistError("soloist has not published its WebSocket endpoint (yet)")
        addr, port = endpoint
        host = f"[{addr}]" if ":" in addr else addr
        ws: ClientWebSocketResponse | None = None
        try:
            async with self.mass.http_session.ws_connect(
                f"ws://{host}:{port}/", heartbeat=_WS_HEARTBEAT
            ) as ws:
                self._ws = ws
                self.logger.debug("Connected to the soloist websocket at %s:%s", addr, port)
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        await self._handle_message(msg.data, on_event)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                        break
                    elif msg.type == WSMsgType.ERROR:
                        raise ws.exception() or ClientError("websocket error")
        finally:
            # only tear down our own connection state: a reconnecting caller may
            # already have a newer listen_events connection registered
            if ws is not None and self._ws is ws:
                self._ws = None
                self._fail_pending_results()

    async def play(self, uri: str | None = None, *, await_result: bool = False) -> None:
        """
        Start playback of a Spotify URI/context, or resume the current one.

        :param uri: Spotify URI (track/album/playlist/...); omit to resume.
        :param await_result: Wait for the daemon's command acknowledgement.
        """
        fields: dict[str, Any] = {"uri": uri} if uri is not None else {}
        await self._send_command("play", await_result=await_result, **fields)

    async def resume(self, *, await_result: bool = False) -> None:
        """Resume playback of the current context."""
        await self._send_command("play", await_result=await_result)

    async def pause(self, *, await_result: bool = False) -> None:
        """Pause playback."""
        await self._send_command("pause", await_result=await_result)

    async def skip_next(self, *, await_result: bool = False) -> None:
        """Skip to the next track."""
        await self._send_command("skip_next", await_result=await_result)

    async def skip_prev(self, *, await_result: bool = False) -> None:
        """Skip to the previous track (or rewind the current one)."""
        await self._send_command("skip_prev", await_result=await_result)

    async def seek(self, position_ms: int, *, await_result: bool = False) -> None:
        """
        Seek to an absolute position in the current track.

        :param position_ms: Target position in milliseconds.
        """
        await self._send_command("seek", await_result=await_result, position_ms=max(0, position_ms))

    async def set_volume(self, volume: int, *, await_result: bool = False) -> None:
        """
        Set the absolute playback volume.

        :param volume: Volume from 0 to 100.
        """
        await self._send_command(
            "set_volume", await_result=await_result, volume=max(0, min(100, volume))
        )

    async def activate(self, *, await_result: bool = False) -> None:
        """Make this daemon the active Spotify Connect device."""
        await self._send_command("activate", await_result=await_result)

    async def deactivate(self, *, await_result: bool = False) -> None:
        """Release this daemon as the active Spotify Connect device."""
        await self._send_command("deactivate", await_result=await_result)

    async def set_shuffle(self, enabled: bool, *, await_result: bool = False) -> None:
        """Enable or disable shuffle."""
        await self._send_command("set_shuffle", await_result=await_result, enabled=enabled)

    async def set_repeat_context(self, enabled: bool, *, await_result: bool = False) -> None:
        """Enable or disable repeating the current context."""
        await self._send_command("set_repeat_context", await_result=await_result, enabled=enabled)

    async def set_repeat_track(self, enabled: bool, *, await_result: bool = False) -> None:
        """Enable or disable repeating the current track."""
        await self._send_command("set_repeat_track", await_result=await_result, enabled=enabled)

    async def add_to_queue(self, uri: str, *, await_result: bool = False) -> None:
        """
        Add a track to the play queue.

        :param uri: Spotify track URI.
        """
        await self._send_command("add_to_queue", await_result=await_result, uri=uri)

    async def get_state(self) -> None:
        """Request a full ``playback_state`` snapshot (answered as an event)."""
        await self._send_command("get_state")

    async def get_auth_state(self) -> None:
        """Request the current ``auth_state`` (answered as an event)."""
        await self._send_command("get_auth_state")

    async def get_queue(self, limit: int = 10) -> None:
        """
        Request the play queue (answered as a ``queue_changed`` event).

        :param limit: Maximum number of upcoming tracks to return.
        """
        await self._send_command("get_queue", limit=limit)

    async def _send_command(
        self,
        command: str,
        *,
        await_result: bool = False,
        timeout: float = _COMMAND_RESULT_TIMEOUT,
        **fields: Any,
    ) -> None:
        """
        Send a command frame, optionally waiting for its ``command_result`` ack.

        Acks carry only the command name (no request id), so concurrent calls of
        the same command resolve in FIFO order with no per-call correlation, and
        an ack for a fire-and-forget send of the same command can resolve an
        awaited call early — avoid mixing both styles for one command.
        Never wait for an ack from inside an ``on_event`` callback: the ack is
        delivered by the same event loop, so the wait can only time out.

        :raises SoloistError: The WebSocket is not connected (or closed while waiting).
        :raises TimeoutError: The acknowledgement did not arrive within the timeout.
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise SoloistError("soloist websocket is not connected")
        future: asyncio.Future[None] | None = None
        if await_result:
            future = asyncio.get_running_loop().create_future()
            self._pending_results.setdefault(command, deque()).append(future)
        try:
            await ws.send_json({"type": "command", "command": command, **fields})
            if future is not None:
                await asyncio.wait_for(future, timeout)
        finally:
            if future is not None:
                self._discard_pending_result(command, future)

    async def _handle_message(self, raw: str, on_event: EventCallback) -> None:
        """Decode a single WebSocket text frame and dispatch it to the callback."""
        try:
            payload = json_loads(raw)
        except ValueError:
            self.logger.debug("Ignoring non-JSON websocket message: %s", raw)
            return
        if not isinstance(payload, dict) or not isinstance(event_type := payload.get("type"), str):
            self.logger.debug("Ignoring websocket message without an event type: %s", raw)
            return
        data: SoloistEventData | None = None
        if (model := _EVENT_MODELS.get(event_type)) is not None:
            try:
                data = model.from_dict(payload)
            except Exception as err:
                # tolerance to malformed events is a hard requirement: whatever
                # the decode error, log it and keep the stream alive
                self.logger.debug("Ignoring malformed %s event: %s (%s)", event_type, raw, err)
                return
            if event_type == "command_result" and isinstance(data, SoloistCommandResult):
                self._resolve_pending_result(data.command)
        await on_event(SoloistEvent(type=event_type, data=data, raw=payload))

    def _read_endpoint(self) -> tuple[str, int] | None:
        """Read the WebSocket endpoint published in the daemon's data directory."""
        try:
            addr = (self.data_dir / WS_ADDR_FILE).read_text(encoding="utf-8").strip()
            port = int((self.data_dir / WS_PORT_FILE).read_text(encoding="utf-8").strip())
        except OSError, ValueError:
            return None
        if not addr or not 0 < port <= 65535:
            return None
        return (addr, port)

    def _resolve_pending_result(self, command: str) -> None:
        """Resolve the oldest result waiter for the given command, if any."""
        if (queue := self._pending_results.get(command)) is None:
            return
        while queue:
            future = queue.popleft()
            if not future.done():
                future.set_result(None)
                break
        if not queue:
            self._pending_results.pop(command, None)

    def _discard_pending_result(self, command: str, future: asyncio.Future[None]) -> None:
        """Drop a result waiter that is no longer interested in its result."""
        if (queue := self._pending_results.get(command)) is None:
            return
        with suppress(ValueError):
            queue.remove(future)
        if not queue:
            self._pending_results.pop(command, None)

    def _fail_pending_results(self) -> None:
        """Fail all outstanding result waiters (connection lost)."""
        for queue in self._pending_results.values():
            for future in queue:
                if not future.done():
                    future.set_exception(SoloistError("websocket connection closed"))
        self._pending_results.clear()


@dataclass
class _BinaryMetadata(DataClassDictMixin):
    """Install metadata persisted next to the soloist binary."""

    sha256: str
    version_raw: str
    installed_at: float
    etag: str | None = None
    version: str | None = None
    build_timestamp: float | None = None


def _resolve_architecture() -> str:
    """Return the CDN artifact architecture for the current platform."""
    system = platform.system()
    if system != "Linux":
        raise UnsupportedPlatformError(f"soloist is only available for Linux, not {system}")
    machine = platform.machine().lower()
    if (arch := _MACHINE_TO_ARCH.get(machine)) is None:
        raise UnsupportedPlatformError(f"no soloist build is available for {machine}")
    return arch


def _validate_download_url(url: str) -> None:
    """Reject download URLs that are insecure or outside Spotify's infrastructure."""
    parsed = URL(url)
    host = (parsed.host or "").lower()
    if parsed.scheme != "https" or not host:
        raise DownloadFailedError(f"refusing insecure soloist download url: {url}")
    if not any(
        host == domain or host.endswith(f".{domain}") for domain in _TRUSTED_DOWNLOAD_DOMAINS
    ):
        raise DownloadFailedError(f"refusing soloist download from untrusted host: {host}")


def _extract_binary_from_archive(archive_path: Path, dest_path: Path) -> None:
    """
    Validate the release archive and extract its single soloist binary (blocking).

    :raises InvalidArchiveError: The archive is corrupt, unsafe, or does not
        contain exactly one regular ``soloist`` file.
    """
    try:
        with tarfile.open(archive_path, mode="r:gz") as tar:
            binary_member: tarfile.TarInfo | None = None
            total_size = 0
            for member in tar:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise InvalidArchiveError(f"unsafe path in soloist archive: {member.name}")
                if member.isdir():
                    continue
                if not member.isreg():
                    raise InvalidArchiveError(
                        f"unsupported member type in soloist archive: {member.name}"
                    )
                total_size += member.size
                if total_size > _MAX_EXTRACTED_SIZE:
                    raise InvalidArchiveError("soloist archive exceeds the extracted size limit")
                if name.name != "soloist":
                    raise InvalidArchiveError(f"unexpected file in soloist archive: {member.name}")
                if binary_member is not None:
                    raise InvalidArchiveError("soloist archive contains multiple binaries")
                binary_member = member
            if binary_member is None:
                raise InvalidArchiveError("soloist archive does not contain the soloist binary")
            source = tar.extractfile(binary_member)
            if source is None:
                raise InvalidArchiveError("soloist archive member is not extractable")
            with source, dest_path.open("wb") as dest:
                shutil.copyfileobj(source, dest, _DOWNLOAD_CHUNK_SIZE)
    except (tarfile.TarError, EOFError, OSError) as err:
        raise InvalidArchiveError(f"invalid soloist archive: {err}") from err


def _validate_elf_header(binary_path: Path, arch: str) -> None:
    """
    Verify the extracted binary is a little-endian ELF for the expected architecture.

    :raises InvalidArchiveError: The file is not an ELF binary for the given arch.
    """
    with binary_path.open("rb") as _file:
        header = _file.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise InvalidArchiveError("soloist binary is not an ELF executable")
    # all supported targets are little-endian (EI_DATA == 1)
    if header[5] != 1:
        raise InvalidArchiveError("soloist binary has an unexpected ELF byte order")
    ei_class, e_machine = _ELF_IDENT[arch]
    if header[4] != ei_class or int.from_bytes(header[18:20], "little") != e_machine:
        raise InvalidArchiveError(f"soloist binary does not match architecture {arch}")


def _consume_install_result(fut: asyncio.Future[str]) -> None:
    """Log the outcome of an install that finished after its caller was cancelled."""
    if (exc := fut.exception()) is not None and not isinstance(exc, asyncio.CancelledError):
        LOGGER.warning("soloist install finished with an error after cancellation: %s", exc)


def _build_expired(build_timestamp: float | None) -> bool:
    """Return whether a build timestamp (when known) has passed the 90-day expiry."""
    return build_timestamp is not None and build_timestamp + _BUILD_EXPIRY_SECONDS <= time.time()


def _parse_version_token(version_output: str) -> str | None:
    """Best-effort extraction of a version number from free-form ``--version`` output."""
    match = _VERSION_TOKEN_RE.search(version_output)
    return match.group(1) if match else None


def _parse_build_timestamp(version_output: str) -> float | None:
    """Best-effort extraction of the build timestamp from free-form ``--version`` output."""
    for match in _TIMESTAMP_RE.finditer(version_output):
        candidate = match.group(1).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None
