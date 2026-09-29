"""Unit tests for AI Radio RSS/Atom feed handling."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock

from music_assistant.providers.ai_radio.constants import (
    RSS_MAX_ARTICLE_CHARS,
    RSS_MAX_FEED_BYTES,
    RSS_MAX_FEEDS_PER_SECTION,
)
from music_assistant.providers.ai_radio.rendering import (
    AIRadioRenderMixin,
    _parse_rss_articles,
    _rss_tokens_in,
)

# --- fixtures / sample documents -------------------------------------------------

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example News</title>
    <item>
      <title>First headline</title>
      <description>First body &lt;b&gt;bold&lt;/b&gt;</description>
    </item>
    <item>
      <title>Second headline</title>
      <description>Second body</description>
    </item>
    <item>
      <title>Third headline</title>
      <description>Third body</description>
    </item>
  </channel>
</rss>
"""

ATOM_XHTML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Nested <b>title</b></div></title>
    <content type="xhtml">
      <div xmlns="http://www.w3.org/1999/xhtml"><p>Nested body paragraph</p></div>
    </content>
  </entry>
</feed>
"""


# --- _rss_tokens_in --------------------------------------------------------------


def test_rss_tokens_in_returns_bare_token() -> None:
    """The standalone placeholder is detected."""
    assert _rss_tokens_in("Read this: <rss_feed> now") == ["<rss_feed>"]


def test_rss_tokens_in_detects_indexed_tokens_deduped_in_order() -> None:
    """Indexed tokens from a merged plan are returned once each, in first-seen order."""
    prompt = "A <rss_feed_2> then B <rss_feed_1> then again <rss_feed_2>"
    assert _rss_tokens_in(prompt) == ["<rss_feed_2>", "<rss_feed_1>"]


def test_rss_tokens_in_returns_empty_without_tokens() -> None:
    """A prompt without any placeholder yields no tokens."""
    assert _rss_tokens_in("no placeholders here") == []


# --- _parse_rss_articles ---------------------------------------------------------


def test_parse_rss_success_formats_articles() -> None:
    """RSS 2.0 items are formatted and HTML in bodies is stripped."""
    result = _parse_rss_articles(RSS_SAMPLE, max_articles=5)
    lines = result.splitlines()
    assert lines[0] == "- First headline: First body bold"
    assert lines[1] == "- Second headline: Second body"
    assert len(lines) == 3


def test_parse_rss_respects_max_articles_limit() -> None:
    """No more than max_articles items are returned."""
    result = _parse_rss_articles(RSS_SAMPLE, max_articles=2)
    assert len(result.splitlines()) == 2


def test_parse_atom_uses_itertext_for_xhtml_children() -> None:
    """Atom XHTML text constructs keep the text held in child elements."""
    result = _parse_rss_articles(ATOM_XHTML_SAMPLE, max_articles=5)
    assert "Nested title" in result
    assert "Nested body paragraph" in result


def test_parse_rss_malformed_returns_empty() -> None:
    """Malformed XML is swallowed and yields an empty string."""
    assert _parse_rss_articles("<rss><channel><item>", max_articles=5) == ""


def test_parse_rss_missing_fields_are_tolerated() -> None:
    """Items missing a title or description do not raise and are skipped when empty."""
    xml = (
        '<rss version="2.0"><channel>'
        "<item><title>Only title</title></item>"
        "<item><description>Only body</description></item>"
        "<item></item>"
        "</channel></rss>"
    )
    result = _parse_rss_articles(xml, max_articles=5)
    lines = result.splitlines()
    assert lines == ["- Only title", "- Only body"]


def test_parse_rss_caps_article_length() -> None:
    """A verbose article body is trimmed to the per-article character cap."""
    long_body = "a" * (RSS_MAX_ARTICLE_CHARS + 200)
    xml = (
        '<rss version="2.0"><channel>'
        f"<item><title>T</title><description>{long_body}</description></item>"
        "</channel></rss>"
    )
    result = _parse_rss_articles(xml, max_articles=5)
    body = result.split(": ", 1)[1]
    assert body.endswith("…")
    assert len(body) == RSS_MAX_ARTICLE_CHARS + 1  # capped chars + ellipsis


# --- harness for the async fetch/decoding methods --------------------------------


class _FakeContent:
    """Minimal stand-in for aiohttp's streaming response body."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int) -> Any:
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    """Async context manager mimicking an aiohttp response."""

    def __init__(
        self, status: int = 200, charset: str = "utf-8", chunks: list[bytes] | None = None
    ):
        self.status = status
        self.charset = charset
        self.content = _FakeContent(chunks or [])

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class RssRenderer(AIRadioRenderMixin):
    """Harness exposing the RSS fetch/decode path with a mocked ``mass``."""

    instance_id = "ai_radio--test"

    def __init__(self, response: _FakeResponse | None = None) -> None:
        """Wire up stub logger/cache/http_session state."""
        self.logger = logging.getLogger("tests.ai_radio.rss")
        self._response = response
        self.cache_store: dict[str, Any] = {}

        async def _cache_get(key: str, **_kw: Any) -> Any:
            return self.cache_store.get(key)

        async def _cache_set(key: str, data: Any, **_kw: Any) -> None:
            self.cache_store[key] = data

        def _session_get(_url: str, **_kw: Any) -> _FakeResponse:
            assert self._response is not None
            return self._response

        self.mass = SimpleNamespace(
            cache=SimpleNamespace(
                get=AsyncMock(side_effect=_cache_get), set=AsyncMock(side_effect=_cache_set)
            ),
            http_session=SimpleNamespace(get=_session_get),
        )


# --- _decode_rss_feed_map --------------------------------------------------------


def test_decode_rss_feed_map_valid() -> None:
    """A well-formed JSON map is decoded into per-token feed lists."""
    renderer = RssRenderer()
    raw = '{"<rss_feed>": [{"url": "https://a.example/feed", "max_articles": 3}]}'
    assert renderer._decode_rss_feed_map(raw) == {
        "<rss_feed>": [{"url": "https://a.example/feed", "max_articles": 3}]
    }


def test_decode_rss_feed_map_malformed_json_returns_empty() -> None:
    """Malformed JSON is swallowed and yields an empty map."""
    renderer = RssRenderer()
    assert renderer._decode_rss_feed_map("{not json") == {}


def test_decode_rss_feed_map_wrong_shape_returns_empty() -> None:
    """A non-dict payload is rejected."""
    renderer = RssRenderer()
    assert renderer._decode_rss_feed_map("[1, 2, 3]") == {}


def test_decode_rss_feed_map_filters_non_dict_feeds() -> None:
    """Non-dict feed entries are dropped from an otherwise valid map."""
    renderer = RssRenderer()
    raw = '{"<rss_feed>": [{"url": "https://a.example/feed"}, "junk", 5]}'
    assert renderer._decode_rss_feed_map(raw) == {"<rss_feed>": [{"url": "https://a.example/feed"}]}


def test_decode_rss_feed_map_empty_input() -> None:
    """Empty/None input yields an empty map."""
    renderer = RssRenderer()
    assert renderer._decode_rss_feed_map(None) == {}
    assert renderer._decode_rss_feed_map("") == {}


# --- _fetch_feed_document --------------------------------------------------------


def test_fetch_feed_document_caches_and_reuses() -> None:
    """A successful download is cached and served from cache on the next call."""
    response = _FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")])
    renderer = RssRenderer(response=response)

    first = asyncio.run(renderer._fetch_feed_document("https://a.example/feed"))
    assert "<rss" in first
    renderer.mass.cache.set.assert_awaited_once()

    # a second call should hit the cache (get returns the stored value) without erroring even if
    # the http layer would fail
    renderer._response = None
    second = asyncio.run(renderer._fetch_feed_document("https://a.example/feed"))
    assert second == first


def test_fetch_feed_document_non_200_returns_empty() -> None:
    """A non-200 response yields an empty string and is not cached."""
    response = _FakeResponse(status=404, chunks=[b"nope"])
    renderer = RssRenderer(response=response)

    result = asyncio.run(renderer._fetch_feed_document("https://a.example/feed"))
    assert result == ""
    renderer.mass.cache.set.assert_not_awaited()


def test_fetch_feed_document_truncates_oversized_body() -> None:
    """A body larger than the cap is truncated to the byte limit."""
    oversized = b"x" * (RSS_MAX_FEED_BYTES + 100_000)
    response = _FakeResponse(chunks=[oversized])
    renderer = RssRenderer(response=response)

    result = asyncio.run(renderer._fetch_feed_document("https://a.example/feed"))
    assert len(result.encode("utf-8")) <= RSS_MAX_FEED_BYTES


# --- _fetch_rss_content ----------------------------------------------------------


def test_fetch_rss_content_combines_and_caps_feed_count() -> None:
    """Only the first RSS_MAX_FEEDS_PER_SECTION feeds are fetched."""
    response = _FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")])
    renderer = RssRenderer(response=response)

    calls: list[str] = []
    original = renderer._fetch_feed_document

    async def _tracking(url: str) -> str:
        calls.append(url)
        return await original(url)

    renderer._fetch_feed_document = _tracking  # type: ignore[method-assign]

    feeds = [
        {"url": f"https://example.com/feed{i}.xml", "max_articles": 1}
        for i in range(RSS_MAX_FEEDS_PER_SECTION + 3)
    ]
    result = asyncio.run(renderer._fetch_rss_content(feeds))

    assert len(calls) == RSS_MAX_FEEDS_PER_SECTION
    assert "First headline" in result


def test_fetch_rss_content_skips_feeds_without_url() -> None:
    """A feed entry without a url contributes nothing."""
    renderer = RssRenderer(response=_FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")]))
    result = asyncio.run(renderer._fetch_rss_content([{"url": "  "}]))
    assert result == ""
