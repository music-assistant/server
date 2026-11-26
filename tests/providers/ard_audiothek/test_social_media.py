"""Test the social media links of the ARD Audiothek provider."""

from music_assistant_models.enums import LinkType

from music_assistant.providers.ard_audiothek import _parse_social_media


def test_show_without_social_media_accounts_keeps_homepage() -> None:
    """A show the API returns without social media accounts still gets its homepage link."""
    links = _parse_social_media("https://www.zdf.de/", None)
    assert [link.type for link in links] == [LinkType.WEBSITE]
