"""Tests for utility/helper functions."""

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers import uri, util


def test_version_extract() -> None:
    """Test the extraction of version from title."""
    test_str = "Bam Bam (feat. Ed Sheeran)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Bam Bam"
    assert version == ""
    test_str = "Bam Bam (feat. Ed Sheeran) - Karaoke Version"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Bam Bam"
    assert version == "Karaoke Version"
    test_str = "Bam Bam (feat. Ed Sheeran) [Karaoke Version]"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Bam Bam"
    assert version == "Karaoke Version"
    test_str = "SuperSong (2011 Remaster)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == "2011 Remaster"
    test_str = "SuperSong (Live at Wembley)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == "Live at Wembley"
    test_str = "SuperSong (Instrumental)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == "Instrumental"
    test_str = "SuperSong (Explicit)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "SuperSong"
    assert version == ""
    # Version keywords in main title should NOT be stripped (only in parentheses)
    test_str = "Great live unplugged song"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Great live unplugged song"
    assert version == ""
    test_str = "I Do (featuring Sonny of P.O.D.) (Album Version)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "I Do"
    assert version == "Album Version"
    test_str = "Get Up Stand Up (Phunk Investigation instrumental club mix)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Get Up Stand Up"
    assert version == "Phunk Investigation instrumental club mix"
    # Complex case: non-version part + version part with 'mix' keyword
    test_str = "Lovin' You More (That Big Track) (Mosquito Chillout mix)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Lovin' You More (That Big Track)"
    assert version == "Mosquito Chillout mix"


def test_with_handling_in_titles() -> None:
    """Test 'with' handling - preserved in title, stripped as featuring credit."""
    # 'with you' (preserved as title word)
    test_str = "CCF (I'm Gonna Stay with You)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "CCF (I'm Gonna Stay with You)"
    assert version == ""
    # 'with someone' (preserved as title word)
    test_str = "Ever Fallen in Love (With Someone You Shouldn't've)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Ever Fallen in Love (With Someone You Shouldn't've)"
    assert version == ""
    # 'with u' (preserved as title word)
    test_str = "Dance (With U)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Dance (With U)"
    assert version == ""
    # 'with the' (preserved as title word)
    test_str = "Girl (With the Patent Leather Face)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Girl (With the Patent Leather Face)"
    assert version == ""
    # 'with you' - different phrasing (preserved as title word)
    test_str = "Rockin' Around (With You)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Rockin' Around (With You)"
    assert version == ""
    # 'with no' (preserved as title word)
    test_str = "Ain't Gonna Bump No More (With No Big Fat Woman)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Ain't Gonna Bump No More (With No Big Fat Woman)"
    assert version == ""
    # 'with that' - not in WITH_TITLE_WORDS but not stripped because it doesn't start with "with "
    test_str = "The Catastrophe (Good Luck with That Man)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "The Catastrophe (Good Luck with That Man)"
    assert version == ""
    # 'with [artist name]' - should still be stripped (not a title word)
    test_str = "Great Song (with John Smith)"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Great Song"
    assert version == ""
    # 'with [artist name]' in brackets - should still be stripped
    test_str = "Great Song [with Jane Doe]"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Great Song"
    assert version == ""
    # Title word preserved + version extracted from dash notation
    test_str = "CCF (I'm Gonna Stay with You) - Live Version"
    title, version = util.parse_title_and_version(test_str)
    assert title == "CCF (I'm Gonna Stay with You)"
    assert version == "Live Version"
    # Title word preserved + version extracted from brackets
    test_str = "Dance (With U) [Remix]"
    title, version = util.parse_title_and_version(test_str)
    assert title == "Dance (With U)"
    assert version == "Remix"


async def test_uri_parsing() -> None:
    """Test parsing of URI."""
    # test regular uri
    test_uri = "spotify://track/123456789"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.TRACK
    assert provider == "spotify"
    assert item_id == "123456789"
    # test spotify uri
    test_uri = "spotify:track:123456789"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.TRACK
    assert provider == "spotify"
    assert item_id == "123456789"
    # test public play/open url
    test_uri = "https://open.spotify.com/playlist/5lH9NjOeJvctAO92ZrKQNB?si=04a63c8234ac413e"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.PLAYLIST
    assert provider == "spotify"
    assert item_id == "5lH9NjOeJvctAO92ZrKQNB"
    # test filename with slashes as item_id
    test_uri = "filesystem://track/Artist/Album/Track.flac"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.TRACK
    assert provider == "filesystem"
    assert item_id == "Artist/Album/Track.flac"
    # test regular url to builtin provider
    test_uri = "http://radiostream.io/stream.mp3"
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.UNKNOWN
    assert provider == "builtin"
    assert item_id == "http://radiostream.io/stream.mp3"
    # test local file to builtin provider
    test_uri = __file__
    media_type, provider, item_id = await uri.parse_uri(test_uri)
    assert media_type == MediaType.UNKNOWN
    assert provider == "builtin"
    assert item_id == __file__
    # test invalid uri
    with pytest.raises(MusicAssistantError):
        await uri.parse_uri("invalid://blah")
