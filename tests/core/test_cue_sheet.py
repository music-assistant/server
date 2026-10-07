"""Tests for CUE sheet parser."""

from music_assistant.helpers.cue_sheet import parse_cue_sheet

SAMPLE_CUE = """\
REM GENRE Rock
REM DATE 1995
REM MUSICBRAINZ_ALBUMID 4591f427-4632-474c-9f2d-b4f009e53096
PERFORMER "The Artist"
TITLE "The Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First Track"
    PERFORMER "The Artist"
    ISRC USRC17607839
    REM MUSICBRAINZ_TRACKID d5c4a1b0-1234-5678-9abc-def012345678
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Track"
    PERFORMER "Featured Artist"
    INDEX 00 03:45:50
    INDEX 01 03:46:00
  TRACK 03 AUDIO
    TITLE "Third Track"
    INDEX 01 07:30:25
"""


def test_parse_basic_cue_sheet() -> None:
    """Test parsing a standard CUE sheet."""
    result = parse_cue_sheet(SAMPLE_CUE)

    assert result.title == "The Album"
    assert result.performers == ["The Artist"]
    assert result.file_path == "album.flac"
    assert result.date == "1995"
    assert result.genres == ["Rock"]
    assert result.musicbrainz_albumid == "4591f427-4632-474c-9f2d-b4f009e53096"
    assert len(result.tracks) == 3


def test_parse_track_metadata() -> None:
    """Test track-level metadata parsing."""
    result = parse_cue_sheet(SAMPLE_CUE)

    track1 = result.tracks[0]
    assert track1.number == 1
    assert track1.title == "First Track"
    assert track1.performers == ["The Artist"]
    assert track1.isrcs == ["USRC17607839"]
    # REM MUSICBRAINZ_TRACKID follows Picard's %musicbrainz_trackid% (release-track MBID)
    assert track1.musicbrainz_releasetrackid == "d5c4a1b0-1234-5678-9abc-def012345678"

    track2 = result.tracks[1]
    assert track2.number == 2
    assert track2.title == "Second Track"
    assert track2.performers == ["Featured Artist"]


def test_parse_timestamps() -> None:
    """Test CUE timestamp to seconds conversion."""
    result = parse_cue_sheet(SAMPLE_CUE)

    # Track 1: INDEX 01 00:00:00 = 0.0 seconds
    assert result.tracks[0].start_position == 0.0

    # Track 2: INDEX 01 03:46:00 = 3*60 + 46 + 0/75 = 226.0 seconds
    # (INDEX 00 at 03:45:50 should be ignored, INDEX 01 is used)
    assert result.tracks[1].start_position == 226.0

    # Track 3: INDEX 01 07:30:25 = 7*60 + 30 + 25/75 = 450.333... seconds
    assert abs(result.tracks[2].start_position - (7 * 60 + 30 + 25 / 75)) < 0.001


def test_parse_empty_cue() -> None:
    """Test parsing an empty CUE sheet."""
    result = parse_cue_sheet("")
    assert result.tracks == []
    assert result.title is None
    assert result.file_path is None


def test_parse_minimal_cue() -> None:
    """Test parsing a minimal CUE sheet with just tracks."""
    cue = """\
FILE "music.mp3" MP3
  TRACK 01 AUDIO
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    INDEX 01 05:00:00
"""
    result = parse_cue_sheet(cue)

    assert result.file_path == "music.mp3"
    assert len(result.tracks) == 2
    assert result.tracks[0].number == 1
    assert result.tracks[0].title is None
    assert result.tracks[0].start_position == 0.0
    assert result.tracks[1].number == 2
    assert result.tracks[1].start_position == 300.0  # 5 minutes


def test_parse_unquoted_filename() -> None:
    """Test parsing FILE command with unquoted filename."""
    cue = """\
FILE music.flac WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.file_path == "music.flac"


def test_parse_no_performer() -> None:
    """Test parsing CUE sheet without PERFORMER tags."""
    cue = """\
TITLE "My Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Song One"
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.performers == []
    assert result.tracks[0].performers == []
    assert result.tracks[0].title == "Song One"


def test_track_without_title_defaults_none() -> None:
    """Test that tracks without TITLE have None as title."""
    cue = """\
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.tracks[0].title is None


def test_rem_lines_at_track_level() -> None:
    """REM MBIDs land in their distinct fields (Picard variable naming)."""
    cue = """\
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Track One"
    REM MUSICBRAINZ_TRACKID rtrk-123
    REM MUSICBRAINZ_RECORDINGID rec-456
    ISRC GBAYE0000351
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    # MUSICBRAINZ_TRACKID follows %musicbrainz_trackid% = release-track MBID;
    # MUSICBRAINZ_RECORDINGID = recording MBID
    assert result.tracks[0].musicbrainz_releasetrackid == "rtrk-123"
    assert result.tracks[0].musicbrainz_recordingid == "rec-456"
    assert result.tracks[0].isrcs == ["GBAYE0000351"]


def test_multi_line_vorbis_style_fields() -> None:
    """Repeated PERFORMER / ISRC / REM lines accumulate into lists."""
    cue = """\
PERFORMER "Label Artist A"
PERFORMER "Label Artist B"
REM GENRE "Rock"
REM GENRE "Pop"
REM MUSICBRAINZ_ALBUMARTISTID aa-1
REM MUSICBRAINZ_ALBUMARTISTID aa-2
REM ALBUMARTISTSORT "Artist A, Label"
REM ALBUMARTISTSORT "Artist B, Label"
REM RELEASETYPE album
REM RELEASETYPE compilation
TITLE "Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "T1"
    PERFORMER "Duo Member One"
    PERFORMER "Duo Member Two"
    ISRC USRC0000001
    ISRC USRC0000002
    REM ARTISTSORT "Member One, Duo"
    REM ARTISTSORT "Member Two, Duo"
    REM MUSICBRAINZ_ARTISTID mb-1
    REM MUSICBRAINZ_ARTISTID mb-2
    REM GENRE "Folk"
    REM GENRE "Blues"
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.performers == ["Label Artist A", "Label Artist B"]
    assert result.genres == ["Rock", "Pop"]
    assert result.musicbrainz_albumartistids == ["aa-1", "aa-2"]
    assert result.album_artist_sort_names == ["Artist A, Label", "Artist B, Label"]
    assert result.album_types == ["album", "compilation"]

    track = result.tracks[0]
    assert track.performers == ["Duo Member One", "Duo Member Two"]
    assert track.isrcs == ["USRC0000001", "USRC0000002"]
    assert track.artist_sort_names == ["Member One, Duo", "Member Two, Duo"]
    assert track.musicbrainz_artistids == ["mb-1", "mb-2"]
    assert track.genres == ["Folk", "Blues"]


def test_additional_single_value_rem_fields() -> None:
    """REM TITLESORT / COPYRIGHT / GROUPING / COMMENT / ITUNESADVISORY + CATALOG."""
    cue = """\
CATALOG 0123456789012
TITLE "Album"
REM ALBUMSORT "Album, The"
REM MUSICBRAINZ_RELEASEGROUPID rg-uuid
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Song, The"
    REM TITLESORT "Song, The"
    REM COPYRIGHT "(c) 2024 Label"
    REM GROUPING "Movement I"
    REM COMMENT "Live at Wembley"
    REM ITUNESADVISORY 1
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.sort_title == "Album, The"
    assert result.barcode == "0123456789012"
    assert result.musicbrainz_releasegroupid == "rg-uuid"

    track = result.tracks[0]
    assert track.sort_name == "Song, The"
    assert track.copyright == "(c) 2024 Label"
    assert track.grouping == "Movement I"
    assert track.comment == "Live at Wembley"
    assert track.explicit is True


def test_parse_top_level_genre() -> None:
    """Top-level GENRE lands in the same bucket as REM GENRE at both scopes."""
    cue = """\
GENRE "Jazz"
TITLE "Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Song"
    GENRE "Fusion"
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.genres == ["Jazz"]
    assert result.tracks[0].genres == ["Fusion"]


def test_parse_mixed_top_level_and_rem_genre() -> None:
    """Top-level GENRE and REM GENRE both append to the same list."""
    cue = """\
GENRE "Rock"
REM GENRE "Progressive"
TITLE "Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Song"
    INDEX 01 00:00:00
"""
    result = parse_cue_sheet(cue)
    assert result.genres == ["Rock", "Progressive"]
