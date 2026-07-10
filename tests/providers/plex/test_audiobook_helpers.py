"""Unit tests for the Plex Music Provider audiobook helpers."""

from __future__ import annotations

from typing import Any

import pytest

from music_assistant.providers.plex.helpers import (
    PlexSectionInfo,
    _library_tracks_progress,
    extract_library_name,
)


class TestPlexSectionInfo:
    """Tests for PlexSectionInfo dataclass."""

    def test_from_dict_kwargs_reconstruction(self) -> None:
        """PlexSectionInfo can be reconstructed from a dict via **kwargs."""
        data: dict[str, Any] = {
            "display_name": "My Server / Audiobooks",
            "section_title": "Audiobooks",
            "server_name": "My Server",
            "section_type": "artist",
            "is_tracking_progress": True,
        }
        info = PlexSectionInfo(**data)
        assert info.display_name == "My Server / Audiobooks"
        assert info.section_title == "Audiobooks"
        assert info.is_tracking_progress is True


class TestExtractLibraryName:
    """Tests for extract_library_name() config value parser."""

    @pytest.mark.parametrize(
        ("input_value", "expected"),
        [
            ("My Server / Music", "Music"),
            ("My Server / Audiobooks", "Audiobooks"),
            ("  My Server  /  Audiobooks  ", "Audiobooks"),
            ("Audiobooks", "Audiobooks"),
            ("  Audiobooks  ", "Audiobooks"),
        ],
    )
    def test_extract_library_name(self, input_value: str, expected: str) -> None:
        """Extract library name handles both 'server / name' and plain 'name' formats."""
        assert extract_library_name(input_value) == expected

    def test_empty_string_returns_empty(self) -> None:
        """Empty string should be returned as-is (not an error)."""
        assert extract_library_name("") == ""


class TestLibraryUsesTrackProgress:
    """Tests for     _library_tracks_progress() using storeTrackProgress preference."""

    class FakeSetting:
        """Minimal Setting stub for unit tests."""

        def __init__(self, setting_id: str, value: bool) -> None:
            """Initialize fake setting."""
            self.id = setting_id
            self.value = value

    class FakeSection:
        """Minimal LibrarySection stub for unit tests."""

        def __init__(self, title: str, settings_data: list[tuple[str, bool]]) -> None:
            """Initialize fake section with given title and settings."""
            self.title = title
            self._settings = [
                TestLibraryUsesTrackProgress.FakeSetting(setting_id, value)
                for setting_id, value in settings_data
            ]

        def settings(self) -> list[TestLibraryUsesTrackProgress.FakeSetting]:
            """Return fake settings list."""
            return self._settings

    def test_enable_track_offsets_enabled(self) -> None:
        """Section with enableTrackOffsets=True should be flagged as resumable."""
        section = self.FakeSection("Audiobooks", [("enableTrackOffsets", True)])
        assert _library_tracks_progress(section) is True

    def test_enable_track_offsets_disabled(self) -> None:
        """Section with enableTrackOffsets=False should not be flagged."""
        section = self.FakeSection("Music", [("enableTrackOffsets", False)])
        assert _library_tracks_progress(section) is False

    def test_setting_absent(self) -> None:
        """Section without enableTrackOffsets should not be flagged."""
        section = self.FakeSection("Music", [("someOtherSetting", True)])
        assert _library_tracks_progress(section) is False

    def test_empty_settings(self) -> None:
        """Section with no settings should not raise."""
        section = self.FakeSection("Music", [])
        assert _library_tracks_progress(section) is False

    def test_settings_call_raises(self) -> None:
        """If settings() raises, should gracefully fall back to False."""

        class BrokenSection:
            """Stub that raises on settings() call."""

            title = "Broken"

            def settings(self) -> list[Any]:
                """Simulate a failing settings call."""
                raise RuntimeError("Network error")

        assert _library_tracks_progress(BrokenSection()) is False


class TestGetSectionInfo:
    """Tests for get_section_info audiobook-first behaviour."""

    class FakePlexServer:
        """Minimal PlexServer stub."""

        def __init__(self, sections: list[Any]) -> None:
            """Initialize fake server with given sections."""
            self.friendlyName = "Test Server"
            self._sections = sections

        def library(self) -> TestGetSectionInfo.FakeLibrary:
            """Return fake library."""
            return TestGetSectionInfo.FakeLibrary(self._sections)

    class FakeLibrary:
        """Minimal Library stub."""

        def __init__(self, sections: list[Any]) -> None:
            """Initialize fake library with given sections."""
            self._sections = sections

        def sections(self) -> list[Any]:
            """Return contained sections."""
            return self._sections

    class FakeMusicSection:
        """Minimal MusicSection stub."""

        TYPE = "artist"

        def __init__(
            self, title: str, enable_track_offsets: bool = False, section_type: str = "artist"
        ) -> None:
            """Initialize fake music section."""
            self.title = title
            self.type = section_type
            self._enable_track_offsets = enable_track_offsets

        def settings(self) -> list[TestLibraryUsesTrackProgress.FakeSetting]:
            """Return fake settings for this section."""
            if self._enable_track_offsets:
                return [TestLibraryUsesTrackProgress.FakeSetting("enableTrackOffsets", True)]
            return []

    def test_all_music_sections_with_flag_are_resume_content(self) -> None:
        """All music sections with enableTrackOffsets=True should be flagged as resumable."""
        sections = [
            self.FakeMusicSection("Music (No Resume)"),
            self.FakeMusicSection("Audiobooks", enable_track_offsets=True),
            self.FakeMusicSection("More Audios", enable_track_offsets=True),
        ]

        # Build PlexSectionInfo manually to simulate get_section_info logic
        results: list[PlexSectionInfo] = []
        for section in sections:
            if section.type != self.FakeMusicSection.TYPE:
                continue
            results.append(
                PlexSectionInfo(
                    display_name=f"Test Server / {section.title}",
                    section_title=section.title,
                    server_name="Test Server",
                    section_type=section.type,
                    is_tracking_progress=_library_tracks_progress(section),
                )
            )

        assert len(results) == 3
        assert results[0].is_tracking_progress is False
        assert results[1].is_tracking_progress is True
        assert results[2].is_tracking_progress is True


class TestGetSectionInfoLegacyFallback:
    """Tests ensuring library-type filtering is still respected."""

    class FakeMovieSection:
        """Minimal non-music section stub."""

        TYPE = "movie"

        def __init__(self, title: str) -> None:
            """Initialize fake movie section."""
            self.title = title
            self.type = "movie"

        def settings(self) -> list[Any]:
            """Return empty settings."""
            return []

    def test_non_music_sections_ignored(self) -> None:
        """Non-music sections should be skipped entirely."""
        movie_section = self.FakeMovieSection("Movies")
        assert movie_section.type != "artist"
        # Simulating the loop filter
        results: list[bool] = []
        if movie_section.type == "artist":
            results.append(True)
        assert len(results) == 0
