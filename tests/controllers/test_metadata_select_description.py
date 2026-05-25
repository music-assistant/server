"""Tests for MetaDataController._select_description bio-selection policy."""

from unittest.mock import MagicMock, patch

from music_assistant.controllers.metadata import MetaDataController

# candidate tuples are (tier, language, text); tier is "metadata" or "music"


def _controller(language: str = "nl") -> MetaDataController:
    """Create a bare MetaDataController whose preferred language is ``language``."""
    with patch.object(MetaDataController, "__init__", lambda *_a, **_kw: None):
        ctrl = MetaDataController.__new__(MetaDataController)
    ctrl.mass = MagicMock()
    ctrl.mass.config.get_raw_core_config_value = MagicMock(return_value=f"{language}_XX")
    return ctrl


class TestSelectDescription:
    """Latest highest-priority metadata bio in the user's language, English as fallback."""

    def test_preferred_language_highest_priority_wins(self) -> None:
        """Both metadata providers have the preferred language: the highest-priority wins."""
        ctrl = _controller("nl")
        candidates = [("metadata", "nl", "TADB nl"), ("metadata", "nl", "Wikipedia nl")]
        assert ctrl._select_description(candidates, None, None) == ("TADB nl", "nl")

    def test_preferred_language_beats_higher_priority_english(self) -> None:
        """A lower-priority provider with the preferred language beats a higher-priority English one."""
        ctrl = _controller("nl")
        candidates = [("metadata", "en", "TADB en"), ("metadata", "nl", "Wikipedia nl")]
        assert ctrl._select_description(candidates, None, None) == ("Wikipedia nl", "nl")

    def test_higher_priority_preferred_beats_lower_english(self) -> None:
        """Highest-priority provider already has the preferred language."""
        ctrl = _controller("nl")
        candidates = [("metadata", "nl", "TADB nl"), ("metadata", "en", "Wikipedia en")]
        assert ctrl._select_description(candidates, None, None) == ("TADB nl", "nl")

    def test_english_fallback_when_no_preferred(self) -> None:
        """No provider has the preferred language: fall back to highest-priority English."""
        ctrl = _controller("nl")
        candidates = [("metadata", "en", "TADB en"), ("metadata", "en", "Wikipedia en")]
        assert ctrl._select_description(candidates, None, None) == ("TADB en", "en")

    def test_stored_preferred_not_downgraded_to_english(self) -> None:
        """A stored preferred-language bio is kept rather than downgraded to English."""
        ctrl = _controller("nl")
        candidates = [("metadata", "en", "TADB en")]
        assert ctrl._select_description(candidates, "stored nl", "nl") == ("stored nl", "nl")

    def test_fresh_preferred_replaces_stored_preferred(self) -> None:
        """A fresh preferred-language bio replaces the stored one (content updates on refresh)."""
        ctrl = _controller("nl")
        candidates = [("metadata", "nl", "fresh nl")]
        assert ctrl._select_description(candidates, "old nl", "nl") == ("fresh nl", "nl")

    def test_music_used_only_when_no_metadata(self) -> None:
        """With no metadata provider, the music-provider bio is the fallback."""
        ctrl = _controller("nl")
        candidates = [("music", None, "qobuz bio")]
        assert ctrl._select_description(candidates, None, None) == ("qobuz bio", None)

    def test_metadata_english_beats_music(self) -> None:
        """An English metadata bio is preferred over a music-provider bio."""
        ctrl = _controller("nl")
        candidates = [("music", None, "qobuz bio"), ("metadata", "en", "TADB en")]
        assert ctrl._select_description(candidates, None, None) == ("TADB en", "en")

    def test_stored_preferred_beats_music(self) -> None:
        """A stored preferred-language bio is not downgraded to a music-provider fallback."""
        ctrl = _controller("nl")
        candidates = [("music", None, "qobuz bio")]
        assert ctrl._select_description(candidates, "stored nl", "nl") == ("stored nl", "nl")

    def test_empty_pass_keeps_previous(self) -> None:
        """No candidates this refresh: keep whatever was stored."""
        ctrl = _controller("nl")
        assert ctrl._select_description([], "old en", "en") == ("old en", "en")

    def test_nothing_at_all_returns_none(self) -> None:
        """No candidates and no stored bio: nothing to show."""
        ctrl = _controller("nl")
        assert ctrl._select_description([], None, None) == (None, None)

    def test_english_user_gets_english_as_preferred(self) -> None:
        """For an English user, English is the preferred language and wins by priority."""
        ctrl = _controller("en")
        candidates = [("metadata", "en", "TADB en"), ("metadata", "en", "Wikipedia en")]
        assert ctrl._select_description(candidates, None, None) == ("TADB en", "en")

    def test_non_english_metadata_used_as_last_resort(self) -> None:
        """A non-preferred, non-English metadata bio is still used when nothing better exists."""
        ctrl = _controller("nl")
        candidates = [("metadata", "de", "TADB de")]
        assert ctrl._select_description(candidates, None, None) == ("TADB de", "de")
