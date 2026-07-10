"""Integration tests for CUE sheet support in the filesystem provider."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, ExternalID
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, AudioFormat, Track
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.helpers.tags import AudioTags
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.cue import (
    CUE_TRACK_ID_DELIMITER,
    CueSheetHandler,
    make_cue_track_id,
    parse_cue_track_id,
)
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

SAMPLE_CUE = """\
REM GENRE "Classic Rock"
REM DATE 1995
PERFORMER "Dire Straits"
TITLE "Live at the BBC"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Down to the Waterline"
    PERFORMER "Dire Straits"
    ISRC GBAMU7800001
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Six Blade Knife"
    PERFORMER "Dire Straits"
    ISRC GBAMU7800002
    INDEX 01 04:10:40
  TRACK 03 AUDIO
    TITLE "Water of Love"
    PERFORMER "Dire Straits"
    ISRC GBAMU7800003
    INDEX 01 07:58:05
"""


def _make_audio_tags(
    duration: float = 900.0,
    album: str | None = None,
    albumartist: str | None = None,
    genre: str | None = None,
    disc: str | None = None,
    has_cover_image: bool = False,
    **extra_tags: str,
) -> AudioTags:
    """Build a minimal AudioTags object for tests."""
    tag_dict: dict[str, str] = {}
    if album is not None:
        tag_dict["album"] = album
    if albumartist is not None:
        tag_dict["albumartist"] = albumartist
    if genre is not None:
        tag_dict["genre"] = genre
    if disc is not None:
        tag_dict["disc"] = disc
    tag_dict.update(extra_tags)
    return AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="flac",
        bit_rate=1000,
        duration=duration,
        tags=tag_dict,
        has_cover_image=has_cover_image,
        filename="album.flac",
    )


def _make_provider(base_path: str = "/music") -> LocalFileSystemProvider:
    """Build a LocalFileSystemProvider with dependencies mocked."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.media_content_type = "music"
    provider.base_path = base_path
    # instance_id and domain are read-only properties sourced from config/manifest
    provider.config = MagicMock(instance_id="filesystem_local--test")
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    # cache is used by load_cue_sheet; default to miss so tests exercise the parse path
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock(return_value=None)
    provider.cache = MagicMock()
    provider._sync_tracks = True
    provider._cue = CueSheetHandler(provider)
    return provider


def _stub_library_track(
    provider: LocalFileSystemProvider, item_id: str, duration: int = 180
) -> None:
    """Configure the provider's mass to return a library track for item_id."""
    prov_mapping = MagicMock(
        item_id=item_id,
        audio_format=AudioFormat(
            content_type=ContentType.FLAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            bit_rate=1000,
        ),
    )
    library_track = MagicMock(provider_mappings=[prov_mapping], duration=duration)
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=library_track)  # type: ignore[method-assign]


def _make_cue_item(tmp_path: Path, cue_text: str, name: str = "album.cue") -> FileSystemItem:
    """Write a CUE file under tmp_path and return a FileSystemItem for it."""
    cue_file = tmp_path / name
    cue_file.write_text(cue_text, encoding="utf-8")
    return FileSystemItem(
        filename=name,
        relative_path=name,
        absolute_path=str(cue_file),
        is_dir=False,
        checksum="1",
        file_size=cue_file.stat().st_size,
        created_at=1700000000,
    )


class TestCueTrackIdHelpers:
    """Tests for CUE track id construction and parsing."""

    def test_make_format(self) -> None:
        """Make format."""
        assert make_cue_track_id("album.cue", 3) == f"album.cue{CUE_TRACK_ID_DELIMITER}03"

    def test_make_pads_single_digits(self) -> None:
        """Make pads single digits."""
        assert make_cue_track_id("a.cue", 1).endswith("01")

    def test_make_handles_large_track_numbers(self) -> None:
        """Make handles large track numbers."""
        assert make_cue_track_id("a.cue", 123).endswith("123")

    def test_parse_roundtrip(self) -> None:
        """Parse roundtrip."""
        for track_num in (1, 9, 10, 99):
            item_id = make_cue_track_id("artist/album.cue", track_num)
            parsed = parse_cue_track_id(item_id)
            assert parsed == ("artist/album.cue", track_num)

    def test_parse_non_cue_id_returns_none(self) -> None:
        """Parse non cue id returns none."""
        assert parse_cue_track_id("regular/path/track.flac") is None
        assert parse_cue_track_id("") is None


class TestReadCueFile:
    """Tests for CUE file encoding handling."""

    @pytest.mark.asyncio
    async def test_reads_utf8(self, tmp_path: Path) -> None:
        """Reads utf8."""
        (tmp_path / "a.cue").write_text('TITLE "Café"\n', encoding="utf-8")
        provider = _make_provider(base_path=str(tmp_path))
        cue_item = _make_cue_item(tmp_path, 'TITLE "Café"\n', name="a.cue")
        content = await provider._cue.read_cue_file(cue_item)
        assert "Café" in content

    @pytest.mark.asyncio
    async def test_reads_utf8_bom(self, tmp_path: Path) -> None:
        """Reads utf8 bom."""
        (tmp_path / "a.cue").write_text('TITLE "Café"\n', encoding="utf-8-sig")
        provider = _make_provider(base_path=str(tmp_path))
        cue_item = FileSystemItem(
            filename="a.cue",
            relative_path="a.cue",
            absolute_path=str(tmp_path / "a.cue"),
            is_dir=False,
            checksum="1",
            file_size=(tmp_path / "a.cue").stat().st_size,
        )
        content = await provider._cue.read_cue_file(cue_item)
        assert "Café" in content
        assert not content.startswith("\ufeff")

    @pytest.mark.asyncio
    async def test_decodes_non_utf8_tolerantly(self, tmp_path: Path) -> None:
        """Non-UTF-8 bytes are decoded without raising."""
        # 0xFC is "ü" in Latin-1 but invalid as a UTF-8 continuation byte
        (tmp_path / "a.cue").write_bytes(b'TITLE "M\xfcller"\n')
        provider = _make_provider(base_path=str(tmp_path))
        cue_item = FileSystemItem(
            filename="a.cue",
            relative_path="a.cue",
            absolute_path=str(tmp_path / "a.cue"),
            is_dir=False,
            checksum="1",
            file_size=(tmp_path / "a.cue").stat().st_size,
        )
        content = await provider._cue.read_cue_file(cue_item)
        # ASCII surroundings are preserved; the non-UTF-8 byte may be replaced
        # or decoded depending on what chardet detects
        assert "TITLE" in content
        assert "ller" in content


class TestFindCueAudioFile:
    """Tests for audio file resolution from a CUE sheet."""

    @pytest.mark.asyncio
    async def test_matches_file_command(self, tmp_path: Path) -> None:
        """Matches file command."""
        (tmp_path / "album.flac").write_bytes(b"")
        (tmp_path / "other.flac").write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, 'FILE "album.flac" WAVE\n')
        provider = _make_provider(base_path=str(tmp_path))
        cue_sheet = MagicMock(file_path="album.flac")
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result == "album.flac"

    @pytest.mark.asyncio
    async def test_same_stem_fallback(self, tmp_path: Path) -> None:
        """Same stem fallback."""
        (tmp_path / "album.flac").write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, "")
        provider = _make_provider(base_path=str(tmp_path))
        cue_sheet = MagicMock(file_path=None)
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result == "album.flac"

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing_and_stem_mismatch(self, tmp_path: Path) -> None:
        """Returns None when neither FILE nor same-stem match locates the audio file."""
        (tmp_path / "onlyone.flac").write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, "", name="different.cue")
        provider = _make_provider(base_path=str(tmp_path))
        cue_sheet = MagicMock(file_path="missing.flac")
        result = await provider._cue.find_audio_file(cue_item, cue_sheet)
        assert result is None


class TestParseCueTracks:
    """Tests for _parse_cue_tracks end-to-end (with mocked parse_tags/parse_album)."""

    @staticmethod
    def _wire_provider_for_parse(
        provider: LocalFileSystemProvider, album: Album | None = None
    ) -> None:
        """Install common mocks for _parse_cue_tracks."""
        provider._parse_album = AsyncMock(return_value=album)  # type: ignore[method-assign]
        provider._parse_artist = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda name, **_k: Artist(
                item_id=name,
                provider=provider.instance_id,
                name=name,
                provider_mappings=set(),
            )
        )

    @pytest.mark.asyncio
    async def test_raises_when_no_tracks(self, tmp_path: Path) -> None:
        """Raises when CUE has no TRACK entries."""
        cue_item = _make_cue_item(tmp_path, 'TITLE "Empty"\n')
        provider = _make_provider(base_path=str(tmp_path))
        provider._parse_album = AsyncMock(return_value=None)  # type: ignore[method-assign]
        with pytest.raises(InvalidDataError):
            await provider._cue.parse_tracks(cue_item)

    @pytest.mark.asyncio
    async def test_raises_when_audio_missing(self, tmp_path: Path) -> None:
        """Raises when the CUE-referenced audio file cannot be located."""
        cue_item = _make_cue_item(
            tmp_path,
            'FILE "missing.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "x"\n    INDEX 01 00:00:00\n',
        )
        provider = _make_provider(base_path=str(tmp_path))
        provider._parse_album = AsyncMock(return_value=None)  # type: ignore[method-assign]
        with pytest.raises(MediaNotFoundError):
            await provider._cue.parse_tracks(cue_item)

    @pytest.mark.asyncio
    async def test_raises_when_audio_has_no_duration(self, tmp_path: Path) -> None:
        """Raises when the referenced audio file has no usable duration."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider._parse_album = AsyncMock(return_value=None)  # type: ignore[method-assign]
        with (
            patch(
                "music_assistant.providers.filesystem_local.cue.async_parse_tags",
                AsyncMock(return_value=_make_audio_tags(duration=0.0)),
            ),
            pytest.raises(InvalidDataError),
        ):
            await provider._cue.parse_tracks(cue_item)

    @pytest.mark.asyncio
    async def test_builds_tracks_with_cue_metadata(self, tmp_path: Path) -> None:
        """Builds tracks with cue metadata."""
        # CUE with 3 tracks, audio file exists, audio tags have a different album/artist
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        # audio has different album+albumartist+year; CUE should override
        tags = _make_audio_tags(
            duration=900.0,
            album="Different Album From Tag",
            albumartist="Different Artist From Tag",
        )
        album = Album(
            item_id="a1",
            provider=provider.instance_id,
            name="Live at the BBC",
            provider_mappings=set(),
        )
        self._wire_provider_for_parse(provider, album)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 3
        # CUE TITLE overrode audio tag for album name
        assert tags.tags["album"] == "Live at the BBC"
        # CUE top-level PERFORMER overrode audio albumartist, stored as the plural
        # multi-value form (list at runtime, though tags is typed str-valued)
        albumartists_value: object = tags.tags["albumartists"]
        assert albumartists_value == ["Dire Straits"]
        assert "albumartist" not in tags.tags
        # per-track names from CUE
        assert tracks[0].name == "Down to the Waterline"
        assert tracks[1].name == "Six Blade Knife"
        assert tracks[2].name == "Water of Love"
        # track numbers preserved
        assert [t.track_number for t in tracks] == [1, 2, 3]
        # item_ids are synthetic and distinct
        ids = [t.item_id for t in tracks]
        assert len(set(ids)) == 3
        for track, num in zip(tracks, [1, 2, 3], strict=True):
            assert track.item_id == make_cue_track_id(cue_item.relative_path, num)
        # ISRC from CUE propagates to each track
        for track in tracks:
            isrcs = [v for k, v in track.external_ids if k == ExternalID.ISRC]
            assert len(isrcs) == 1
            assert isrcs[0].startswith("GBAMU78")

    @pytest.mark.asyncio
    async def test_track_durations(self, tmp_path: Path) -> None:
        """Track durations."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        # total_duration = 900s (15min)
        tags = _make_audio_tags(duration=900.0, album="Live at the BBC")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        # Track 1: 00:00:00 to 04:10:40 = ~250.53s
        # Track 2: 04:10:40 to 07:58:05 = ~227.4s
        # Track 3: 07:58:05 to 900 = ~421.93s
        assert tracks[0].duration == round(4 * 60 + 10 + 40 / 75)
        assert tracks[1].duration == round((7 * 60 + 58 + 5 / 75) - (4 * 60 + 10 + 40 / 75))
        assert tracks[2].duration == round(900.0 - (7 * 60 + 58 + 5 / 75))

    @pytest.mark.asyncio
    async def test_honors_disc_number_tag(self, tmp_path: Path) -> None:
        """Honors disc number tag."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=900.0, album="Live at the BBC", disc="2")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert all(t.disc_number == 2 for t in tracks)

    @pytest.mark.asyncio
    async def test_skips_track_missing_title(self, tmp_path: Path) -> None:
        """Skips track missing title."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_text = (
            'PERFORMER "X"\n'
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "Real Track"\n'
            "    INDEX 01 00:00:00\n"
            "  TRACK 02 AUDIO\n"
            "    INDEX 01 02:00:00\n"  # no TITLE
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        assert tracks[0].name == "Real Track"
        # a warning should have been emitted for the skipped track
        warning_msgs = [str(c) for c in provider.logger.warning.call_args_list]  # type: ignore[attr-defined]
        assert any("TITLE" in msg for msg in warning_msgs)

    @pytest.mark.asyncio
    async def test_track_artist_falls_back_to_album_performer(self, tmp_path: Path) -> None:
        """Track artist falls back to album performer."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        # no per-track PERFORMER, only top-level
        cue_text = (
            'PERFORMER "Band"\n'
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "T1"\n'
            "    INDEX 01 00:00:00\n"
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        assert [a.name for a in tracks[0].artists] == ["Band"]

    @pytest.mark.asyncio
    async def test_track_artist_falls_back_to_unknown_when_no_performer(
        self, tmp_path: Path
    ) -> None:
        """No PERFORMER at sheet or track level falls back to the [unknown] artist."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_text = (
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "T1"\n'
            "    INDEX 01 00:00:00\n"
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        assert [a.name for a in tracks[0].artists] == [UNKNOWN_ARTIST]

    @pytest.mark.asyncio
    async def test_multi_line_performer_yields_multiple_artists(self, tmp_path: Path) -> None:
        """Repeated PERFORMER lines produce one Artist each (Vorbis multi-field style)."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_text = (
            'TITLE "Split"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "T1"\n'
            '    PERFORMER "AC/DC"\n'
            '    PERFORMER "Queen"\n'
            "    INDEX 01 00:00:00\n"
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Split")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        # "AC/DC" is preserved intact, not split on the slash
        assert [a.name for a in tracks[0].artists] == ["AC/DC", "Queen"]

    @pytest.mark.asyncio
    async def test_recording_and_releasetrack_mbids_mapped_distinctly(self, tmp_path: Path) -> None:
        """REM MUSICBRAINZ_RECORDINGID → MB_RECORDING / .mbid; REM MUSICBRAINZ_TRACKID → MB_TRACK."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        recording_mbid = "11111111-1111-1111-1111-111111111111"
        releasetrack_mbid = "22222222-2222-2222-2222-222222222222"
        cue_text = (
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "T1"\n'
            f"    REM MUSICBRAINZ_RECORDINGID {recording_mbid}\n"
            f"    REM MUSICBRAINZ_TRACKID {releasetrack_mbid}\n"
            "    INDEX 01 00:00:00\n"
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        assert (ExternalID.MB_RECORDING, recording_mbid) in tracks[0].external_ids
        assert (ExternalID.MB_TRACK, releasetrack_mbid) in tracks[0].external_ids
        assert tracks[0].mbid == recording_mbid

    @pytest.mark.asyncio
    async def test_aligned_track_artist_metadata(self, tmp_path: Path) -> None:
        """REM ARTISTSORT / REM MUSICBRAINZ_ARTISTID align by index with PERFORMER."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_text = (
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "T1"\n'
            '    PERFORMER "First Artist"\n'
            '    PERFORMER "Second Artist"\n'
            '    REM ARTISTSORT "Artist, First"\n'
            '    REM ARTISTSORT "Artist, Second"\n'
            "    REM MUSICBRAINZ_ARTISTID 11111111-1111-1111-1111-111111111111\n"
            "    REM MUSICBRAINZ_ARTISTID 22222222-2222-2222-2222-222222222222\n"
            "    INDEX 01 00:00:00\n"
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)
        # override _parse_artist to capture the sort_name/mbid args passed per artist
        captured: list[dict[str, str | None]] = []

        async def _capture(
            name: str, sort_name: str | None = None, mbid: str | None = None, **_k: object
        ) -> Artist:
            captured.append({"name": name, "sort_name": sort_name, "mbid": mbid})
            return Artist(
                item_id=name,
                provider=provider.instance_id,
                name=name,
                sort_name=sort_name,
                provider_mappings=set(),
            )

        provider._parse_artist = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        assert captured == [
            {
                "name": "First Artist",
                "sort_name": "Artist, First",
                "mbid": "11111111-1111-1111-1111-111111111111",
            },
            {
                "name": "Second Artist",
                "sort_name": "Artist, Second",
                "mbid": "22222222-2222-2222-2222-222222222222",
            },
        ]

    @pytest.mark.asyncio
    async def test_track_level_descriptive_fields(self, tmp_path: Path) -> None:
        """REM COPYRIGHT / GROUPING / COMMENT / ITUNESADVISORY / TITLESORT populate track metadata."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_text = (
            'TITLE "Album"\n'
            'FILE "album.flac" WAVE\n'
            "  TRACK 01 AUDIO\n"
            '    TITLE "Song, The"\n'
            '    REM TITLESORT "Song, The"\n'
            '    REM COPYRIGHT "(c) 2024 Label"\n'
            '    REM GROUPING "Movement I"\n'
            '    REM COMMENT "Live at Wembley"\n'
            "    REM ITUNESADVISORY 1\n"
            "    INDEX 01 00:00:00\n"
        )
        cue_item = _make_cue_item(tmp_path, cue_text)
        provider = _make_provider(base_path=str(tmp_path))
        tags = _make_audio_tags(duration=300.0, album="Album")
        self._wire_provider_for_parse(provider)

        with patch(
            "music_assistant.providers.filesystem_local.cue.async_parse_tags",
            AsyncMock(return_value=tags),
        ):
            tracks = await provider._cue.parse_tracks(cue_item)

        assert len(tracks) == 1
        track = tracks[0]
        assert track.sort_name == "Song, The"
        assert track.metadata.copyright == "(c) 2024 Label"
        assert track.metadata.grouping == "Movement I"
        assert track.metadata.description == "Live at Wembley"
        assert track.metadata.explicit is True


class TestGetStreamDetailsForCueTrack:
    """Tests for _get_stream_details_for_cue_track."""

    @pytest.mark.asyncio
    async def test_invalid_id_raises(self) -> None:
        """Invalid id raises."""
        provider = _make_provider()
        with pytest.raises(InvalidDataError):
            await provider._cue.get_stream_details("not_a_cue_id.flac")

    @pytest.mark.asyncio
    async def test_not_in_library_raises(self, tmp_path: Path) -> None:
        """Track not in library raises."""
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
        item_id = make_cue_track_id(cue_item.relative_path, 1)
        with pytest.raises(MediaNotFoundError):
            await provider._cue.get_stream_details(item_id)

    @pytest.mark.asyncio
    async def test_missing_audio_raises(self, tmp_path: Path) -> None:
        """Missing audio raises."""
        cue_item = _make_cue_item(
            tmp_path,
            'FILE "missing.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "x"\n    INDEX 01 00:00:00\n',
        )
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        item_id = make_cue_track_id(cue_item.relative_path, 1)
        _stub_library_track(provider, item_id)
        with pytest.raises(MediaNotFoundError):
            await provider._cue.get_stream_details(item_id)

    @pytest.mark.asyncio
    async def test_unknown_track_number_raises(self, tmp_path: Path) -> None:
        """Unknown track number raises."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        # request track 99 which isn't in the CUE
        item_id = make_cue_track_id(cue_item.relative_path, 99)
        _stub_library_track(provider, item_id)
        with pytest.raises(MediaNotFoundError):
            await provider._cue.get_stream_details(item_id)

    @pytest.mark.asyncio
    async def test_builds_streamdetails_with_offset_and_duration(self, tmp_path: Path) -> None:
        """Builds streamdetails with offset and duration."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        item_id = make_cue_track_id(cue_item.relative_path, 2)
        _stub_library_track(provider, item_id, duration=228)

        details = await provider._cue.get_stream_details(item_id)

        assert isinstance(details, StreamDetails)
        assert details.item_id == item_id
        assert details.can_seek is True
        assert details.allow_seek is True
        assert details.audio_format.content_type == ContentType.PCM_F32LE
        assert details.duration == 228
        assert details.data is not None
        assert details.data["audio_relative_path"] == "album.flac"
        # Track 2 starts at 04:10:40 = 250.533...
        expected_start = 4 * 60 + 10 + 40 / 75
        assert abs(details.data["start_seconds"] - expected_start) < 0.001


class TestProcessDeletionsCueBranch:
    """Tests for _process_deletions routing of CUE-derived track ids."""

    @pytest.mark.asyncio
    async def test_cue_track_id_routed_to_track_controller(self) -> None:
        """Cue track id routed to track controller."""
        provider = _make_provider()
        controller = MagicMock()
        controller.get_library_item_by_prov_id = AsyncMock(return_value=None)
        provider.mass.music.get_controller = MagicMock(return_value=controller)  # type: ignore[method-assign]

        cue_track_id = make_cue_track_id("artist/album.cue", 3)
        await provider._process_deletions({cue_track_id})

        # must have consulted a controller (the track controller)
        assert provider.mass.music.get_controller.called


class TestGetTrackCueBranch:
    """Test get_track's CUE-id branch."""

    @pytest.mark.asyncio
    async def test_returns_matching_cue_track(self, tmp_path: Path) -> None:
        """Returns matching cue track."""
        audio_file = tmp_path / "album.flac"
        audio_file.write_bytes(b"")
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]

        # mock _parse_cue_tracks to return three synthetic tracks
        def fake_track(num: int) -> Track:
            return Track(
                item_id=make_cue_track_id(cue_item.relative_path, num),
                provider=provider.instance_id,
                name=f"Track {num}",
                provider_mappings=set(),
            )

        provider._cue.parse_tracks = AsyncMock(  # type: ignore[method-assign]
            return_value=[fake_track(1), fake_track(2), fake_track(3)]
        )
        item_id = make_cue_track_id(cue_item.relative_path, 2)
        track = await provider.get_track(item_id)
        assert track.item_id == item_id
        assert track.name == "Track 2"

    @pytest.mark.asyncio
    async def test_missing_cue_track_raises(self, tmp_path: Path) -> None:
        """Missing cue track raises."""
        cue_item = _make_cue_item(tmp_path, SAMPLE_CUE)
        provider = _make_provider(base_path=str(tmp_path))
        provider.resolve = AsyncMock(return_value=cue_item)  # type: ignore[method-assign]
        provider._cue.parse_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
        item_id = make_cue_track_id(cue_item.relative_path, 1)
        with pytest.raises(MediaNotFoundError):
            await provider.get_track(item_id)


class TestClassifyScanItemCue:
    """
    Sync-walker classification for CUE files.

    Guards the edit-resync path: a CUE's previous checksum lives under synthetic
    per-track ids in provider_mappings, never under the CUE path itself. The
    scan reverse-derives a path-keyed map so an unchanged CUE is recognised and
    an edited CUE forwards its prior checksum, which in turn makes the library
    write use overwrite_existing=True.
    """

    @staticmethod
    def _cue_item(checksum: str) -> FileSystemItem:
        return FileSystemItem(
            filename="album.cue",
            relative_path="album.cue",
            absolute_path="/music/album.cue",
            is_dir=False,
            checksum=checksum,
            file_size=100,
        )

    @staticmethod
    def _classify(
        provider: LocalFileSystemProvider,
        item: FileSystemItem,
        *,
        cue_file_checksums: dict[str, str] | None = None,
    ) -> tuple[
        list[tuple[FileSystemItem, str | None]],
        list[FileSystemItem],
        set[str],
        set[str],
    ]:
        items_to_process: list[tuple[FileSystemItem, str | None]] = []
        unchanged_cue_items: list[FileSystemItem] = []
        cur_filenames: set[str] = set()
        cue_stems: set[str] = set()
        provider._classify_scan_item(
            item,
            file_checksums={},
            cue_file_checksums=cue_file_checksums or {},
            cur_filenames=cur_filenames,
            items_to_process=items_to_process,
            unchanged_cue_items=unchanged_cue_items,
            cue_stems=cue_stems,
            ignore_album_playlists=False,
        )
        return items_to_process, unchanged_cue_items, cur_filenames, cue_stems

    def test_unchanged_cue_routes_to_unchanged_bucket(self) -> None:
        """Matching checksum: CUE is marked present and not re-processed."""
        provider = _make_provider()
        cue_item = self._cue_item("checksum-v1")
        items, unchanged, cur, stems = self._classify(
            provider,
            cue_item,
            cue_file_checksums={"album.cue": "checksum-v1"},
        )
        assert items == []
        assert unchanged == [cue_item]
        assert cur == {"album.cue"}
        assert stems == {"/music/album"}

    def test_edited_cue_forwards_prior_checksum(self) -> None:
        """Changed checksum: prior value is forwarded so downstream overwrite=True."""
        provider = _make_provider()
        cue_item = self._cue_item("checksum-v2")
        items, unchanged, _, _ = self._classify(
            provider,
            cue_item,
            cue_file_checksums={"album.cue": "checksum-v1"},
        )
        assert items == [(cue_item, "checksum-v1")]
        assert unchanged == []

    def test_new_cue_has_no_prior_checksum(self) -> None:
        """First-time ingest: prev_checksum is None, item queued for processing."""
        provider = _make_provider()
        cue_item = self._cue_item("checksum-v1")
        items, unchanged, _, _ = self._classify(
            provider,
            cue_item,
            cue_file_checksums={},
        )
        assert items == [(cue_item, None)]
        assert unchanged == []
