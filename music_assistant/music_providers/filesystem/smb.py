"""SMB filesystem provider for Music Assistant."""

import tempfile
from typing import AsyncGenerator, Optional, Tuple

from smb.base import SharedFile
from smb.SMBConnection import SMBConnection

from music_assistant.models.enums import ProviderType
from music_assistant.models.media_items import MediaType
from music_assistant.music_providers.filesystem import FileSystemProvider

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
        # TODO: Include scantree in class so we can override it and reuse sync_library()?
        async for entry in scantree(
            share=self.config.share_name,
            smb_connection=self._smb_connection,
            path=self.config.path or "/",
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
                    track_path = self._get_smb_url(entry.full_path)
                    track = await self._parse_track(track_path)
                    # if the track was edited on disk, always overwrite existing db details
                    overwrite_existing = entry.full_path in prev_checksums
                    await self.mass.music.tracks.add_db_item(
                        track, overwrite_existing=overwrite_existing
                    )
                elif ext in PLAYLIST_EXTENSIONS:
                    with tempfile.NamedTemporaryFile() as file_obj:
                        _, _ = self._smb_connection.retrieveFile(
                            self.config.share_name, entry.full_path, file_obj
                        )
                        playlist = await self._parse_playlist(file_obj.name)
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

    # async def _parse_track(self, file_path: str) -> Track:
    #     """Try to parse a track from a filename by reading its tags."""
    #     # Retrieve file from smb share
    #     with tempfile.NamedTemporaryFile() as file_obj:
    #         _, _ = self._smb_connection.retrieveFile(
    #             self.config.share_name, file_path, file_obj
    #         )
    #         track = await super()._parse_track(file_obj.name)
    #         # set the id to the relative path on the share
    #         track.item_id = file_path
    #         # update the uri
    #         track.uri = create_uri(MediaType.TRACK, self._attr_type, file_path)
    #         return track

    # async def get_stream_details(self, item_id: str) -> StreamDetails:
    #     """Return the content details for the given track when it will be streamed."""
    #     itempath = self._get_smb_url(item_id)

    #     metadata = await parse_tags(itempath)
    #     stat = await self.mass.loop.run_in_executor(None, os.stat, itempath)

    #     return StreamDetails(
    #         provider=self.type,
    #         item_id=item_id,
    #         content_type=ContentType.try_parse(metadata.format),
    #         media_type=MediaType.TRACK,
    #         duration=metadata.duration,
    #         size=stat.st_size,
    #         sample_rate=metadata.sample_rate,
    #         bit_depth=metadata.bits_per_sample,
    #         direct=itempath,
    #     )

    def _get_smb_url(self, prov_track_id):
        return f"smb://{self.config.username}:{self.config.password}@{self.config.target_ip}/{self.config.share_name}{prov_track_id}"

    async def _setup_smb_connection(self):
        self._smb_connection = SMBConnection(
            self.config.username,
            self.config.password,
            "music_assistant",
            self.config.target_name,
            use_ntlm_v2=True,
        )
        assert self._smb_connection.connect(self.config.target_ip)
