"""SMB filesystem provider for Music Assistant."""

import json
import tempfile
from typing import AsyncGenerator, Optional, Tuple

from smb.base import SharedFile
from smb.SMBConnection import SMBConnection

from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.enums import ProviderType
from music_assistant.models.errors import InvalidDataError
from music_assistant.models.media_items import MediaType, Track
from music_assistant.music_providers.filesystem import FileSystemProvider

from ...helpers.tags import AudioTags
from .filesystem import (
    PLAYLIST_EXTENSIONS,
    SCHEMA_VERSION,
    SUPPORTED_EXTENSIONS,
    TRACK_EXTENSIONS,
)

SERVICE_NAME = "music_assistant"


async def scantree(
    share: str, smb_connection: SMBConnection, path: str = "/"
) -> AsyncGenerator[SharedFile, None]:
    """Recursively yield DirEntry objects for given directory."""
    path_result = smb_connection.listPath(share, path)
    for entry in path_result:
        if entry.filename.startswith("."):
            continue
        if entry.isDirectory:
            sub_path = f"{path}{entry.filename}/"
            async for sub_entry in scantree(
                share=share, smb_connection=smb_connection, path=sub_path
            ):
                yield sub_entry
        else:
            full_path = f"{path}{entry.filename}"
            setattr(entry, "full_path", full_path)
            yield entry


class SMBFileSystemProvider(FileSystemProvider):
    """Implementation of an SMB File System Provider."""

    _attr_name = "smb"
    _attr_type = ProviderType.FILESYSTEM_SMB
    _smb_connection = None

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        await self._setup_smb_connection()
        return True

    async def sync_library(
        self, media_types: Optional[Tuple[MediaType]] = None
    ) -> None:
        """Run library sync for this provider."""
        cache_key = f"{self.id}.checksums"
        prev_checksums = await self.mass.cache.get(cache_key, SCHEMA_VERSION)
        save_checksum_interval = 0
        if prev_checksums is None:
            prev_checksums = {}

        # find all music files in the music directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        cur_checksums = {}
        async for entry in scantree(
            share=self.config.path, smb_connection=self._smb_connection
        ):

            if "." not in entry.filename or entry.filename.startswith("."):
                # skip system files and files without extension
                continue

            _, ext = entry.filename.rsplit(".", 1)
            if ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            try:
                # mtime is used as file checksum
                stat = entry.last_attr_change_time
                checksum = int(stat)
                cur_checksums[entry.full_path] = checksum
                if checksum == prev_checksums.get(entry.full_path):
                    continue

                if ext in TRACK_EXTENSIONS:
                    # add/update track to db
                    track = await self._parse_track(entry.full_path)
                    # if the track was edited on disk, always overwrite existing db details
                    overwrite_existing = entry.path in prev_checksums
                    await self.mass.music.tracks.add_db_item(
                        track, overwrite_existing=overwrite_existing
                    )
                elif ext in PLAYLIST_EXTENSIONS:
                    playlist = await self._parse_playlist(entry.full_path)
                    # add/update] playlist to db
                    playlist.metadata.checksum = checksum
                    # playlist is always in-library
                    playlist.in_library = True
                    await self.mass.music.playlists.add_db_item(playlist)
            except Exception as err:  # pylint: disable=broad-except
                # we don't want the whole sync to crash on one file so we catch all exceptions here
                self.logger.exception(
                    "Error processing %s - %s", entry.full_path, str(err)
                )

            # save checksums every 100 processed items
            # this allows us to pickup where we leftoff when initial scan gets interrupted
            if save_checksum_interval == 100:
                await self.mass.cache.set(cache_key, cur_checksums, SCHEMA_VERSION)
                save_checksum_interval = 0
            else:
                save_checksum_interval += 1

        # store (final) checksums in cache
        await self.mass.cache.set(cache_key, cur_checksums, SCHEMA_VERSION)
        # work out deletions
        deleted_files = set(prev_checksums.keys()) - set(cur_checksums.keys())
        await self._process_deletions(deleted_files)

    async def _parse_track(self, track_path: str) -> Track:
        """Try to parse a track from a filename by reading its tags."""

        # if not await self.exists(track_path):
        #     raise MediaNotFoundError(f"Track path does not exist: {track_path}")

        # track_item_id = self._get_item_id(track_path)

        # parse tags
        tags = await self.parse_tags(track_path)
        print(f"pre commit doesn't like an used {tags}")

    async def parse_tags(self, file_path: str) -> AudioTags:
        """Parse tags from a media file."""

        with tempfile.NamedTemporaryFile() as file_obj:
            _, _ = self._smb_connection.retrieveFile(
                self.config.path, file_path, file_obj
            )

            args = (
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "fatal",
                "-show_error",
                "-show_format",
                "-show_streams",
                "-print_format",
                "json",
                "-i",
                "-",
            )

            async with AsyncProcess(
                args, enable_stdin=True, enable_stdout=True, enable_stderr=False
            ) as proc:

                try:
                    file_obj.seek(0, 0)
                    await proc.write(file_obj.read())
                    res, _ = await proc.communicate()
                    data = json.loads(res)
                    if error := data.get("error"):
                        raise InvalidDataError(error["string"])
                    return AudioTags.parse(data)
                except (
                    KeyError,
                    ValueError,
                    json.JSONDecodeError,
                    InvalidDataError,
                ) as err:
                    raise InvalidDataError(
                        f"Unable to retrieve info for {file_path}: {str(err)}"
                    ) from err

    async def _setup_smb_connection(self):
        self._smb_connection = SMBConnection(
            self.config.username,
            self.config.password,
            "music_assistant",
            self.config.target_name,
            use_ntlm_v2=True,
        )
        assert self._smb_connection.connect(self.config.target_ip)
