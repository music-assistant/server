"""Tests for stripping HTML tags from strings."""

from music_assistant.helpers.util import strip_html_tags


def test_strip_html_tags() -> None:
    """Test that HTML tags and entities are stripped to plain text."""
    # regression for audiobook/podcast descriptions leaking HTML into the player OSD
    line = "<p>In 1940, 18-year old Ursula Todd is born.</p>"
    assert strip_html_tags(line) == "In 1940, 18-year old Ursula Todd is born."

    # nested/inline tags
    line = "A <b>bold</b> and <i>italic</i> tale"
    assert strip_html_tags(line) == "A bold and italic tale"

    # html entities are unescaped
    assert strip_html_tags("Cause &amp; Effect") == "Cause & Effect"

    # multiple paragraphs do not run words together
    line = "<p>First.</p><p>Second.</p>"
    assert strip_html_tags(line) == "First. Second."

    # plain text without markup is returned unchanged
    assert strip_html_tags("Just plain text") == "Just plain text"
