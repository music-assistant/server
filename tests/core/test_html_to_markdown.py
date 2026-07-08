"""Tests for converting HTML descriptions to markdown."""

from music_assistant.helpers.util import html_to_markdown


def test_html_to_markdown() -> None:
    """Test that safe HTML is converted to markdown and the rest is stripped."""
    # regression for audiobook/podcast descriptions leaking HTML into the player OSD
    line = "<p>In 1940, 18-year old Ursula Todd is born.</p>"
    assert html_to_markdown(line) == "In 1940, 18-year old Ursula Todd is born."

    # inline formatting is converted to markdown
    assert html_to_markdown("A <b>bold</b> tale") == "A **bold** tale"
    assert html_to_markdown("An <i>italic</i> tale") == "An *italic* tale"

    # html entities are unescaped
    assert html_to_markdown("Cause &amp; Effect") == "Cause & Effect"

    # entity-escaped markup (e.g. from podcast RSS feeds) is decoded first, then converted
    assert html_to_markdown("&lt;p&gt;In 1940.&lt;/p&gt;") == "In 1940."
    assert html_to_markdown("A &lt;b&gt;bold&lt;/b&gt; tale") == "A **bold** tale"

    # multiple paragraphs become separate markdown paragraphs
    assert html_to_markdown("<p>First.</p><p>Second.</p>") == "First.\n\nSecond."

    # tags outside the safe set are stripped while their text content is kept
    assert html_to_markdown("A <u>underlined</u> word") == "A underlined word"

    # plain text without markup is returned unchanged
    assert html_to_markdown("Just plain text") == "Just plain text"
