"""Unit tests for AI Radio RSS/Atom feed handling."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import AsyncMock

from music_assistant.providers.ai_radio.constants import (
    NO_RSS_DATA_INSTRUCTION,
    RSS_MAX_ARTICLE_CHARS,
    RSS_MAX_FEED_BYTES,
    RSS_MAX_FEEDS_PER_SECTION,
    RSS_MAX_MAX_ARTICLES,
    RSS_MAX_TOTAL_CHARS,
)
from music_assistant.providers.ai_radio.rendering import (
    AIRadioRenderMixin,
    _clip_to_budget,
    _decode_feed_bytes,
    _is_public_ip,
    _parse_rss_articles,
    _rss_tokens_in,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

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
        self,
        status: int = 200,
        charset: str | None = "utf-8",
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.charset = charset
        self.content = _FakeContent(chunks or [])
        self.headers = headers or {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class RssRenderer(AIRadioRenderMixin):
    """Harness exposing the RSS fetch/decode path with a mocked ``mass``."""

    instance_id = "ai_radio--test"

    def __init__(
        self,
        response: _FakeResponse | None = None,
        *,
        responses: list[_FakeResponse] | None = None,
        public_ok: bool = True,
    ) -> None:
        """
        Wire up stub logger/cache/http_session state.

        ``response`` returns the same fake response for every request; ``responses`` returns each
        queued response in turn (used to exercise redirect chains). ``public_ok`` short-circuits the
        SSRF host check so download tests need no real DNS; SSRF behaviour is tested separately.
        """
        self.logger = logging.getLogger("tests.ai_radio.rss")
        self._response = response
        self._responses = list(responses) if responses is not None else None
        self._public_ok = public_ok
        self.requested_urls: list[str] = []
        self.cache_store: dict[str, Any] = {}

        async def _cache_get(key: str, **_kw: Any) -> Any:
            return self.cache_store.get(key)

        async def _cache_set(key: str, data: Any, **_kw: Any) -> None:
            self.cache_store[key] = data

        def _session_get(url: str, **_kw: Any) -> _FakeResponse:
            self.requested_urls.append(url)
            if self._responses is not None:
                assert self._responses, f"no queued response left for {url}"
                return self._responses.pop(0)
            assert self._response is not None
            return self._response

        # keep typed references so the awaited-assertions are visible to the type checker
        self.cache_get: AsyncMock = AsyncMock(side_effect=_cache_get)
        self.cache_set: AsyncMock = AsyncMock(side_effect=_cache_set)

        self.mass = cast(
            "MusicAssistant",
            SimpleNamespace(
                cache=SimpleNamespace(get=self.cache_get, set=self.cache_set),
                http_session=SimpleNamespace(get=_session_get),
            ),
        )

    async def _is_public_http_url(self, url: str) -> bool:
        """Override the real DNS-backed SSRF check so download tests need no network."""
        return self._public_ok


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
    renderer.cache_set.assert_awaited_once()

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
    renderer.cache_set.assert_not_awaited()


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


# --- _is_public_ip ---------------------------------------------------------------


def test_is_public_ip_accepts_routable_addresses() -> None:
    """Ordinary public IPv4/IPv6 addresses are allowed."""
    assert _is_public_ip("8.8.8.8") is True
    assert _is_public_ip("1.1.1.1") is True
    assert _is_public_ip("2606:4700:4700::1111") is True


def test_is_public_ip_rejects_internal_addresses() -> None:
    """Loopback, private, link-local and metadata addresses are refused."""
    for blocked in (
        "127.0.0.1",  # loopback
        "10.0.0.1",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "169.254.169.254",  # cloud metadata (link-local)
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "::1",  # IPv6 loopback
        "::ffff:127.0.0.1",  # IPv4 loopback mapped into IPv6
    ):
        assert _is_public_ip(blocked) is False, blocked


def test_is_public_ip_rejects_garbage() -> None:
    """A value that is not an IP address is refused rather than raising."""
    assert _is_public_ip("not-an-ip") is False
    assert _is_public_ip("") is False


# --- _decode_feed_bytes ----------------------------------------------------------


def test_decode_feed_bytes_honors_prolog_encoding() -> None:
    """A feed declaring ISO-8859-1 in its prolog is decoded with that charset, not forced utf-8."""
    raw = '<?xml version="1.0" encoding="ISO-8859-1"?><rss><t>café</t></rss>'.encode("iso-8859-1")
    # no HTTP charset -> the prolog wins and the accented text survives
    assert "café" in _decode_feed_bytes(raw, None)
    # forcing utf-8 on the same bytes would corrupt it, proving the prolog path matters
    assert "café" not in raw.decode("utf-8", errors="replace")


def test_decode_feed_bytes_http_charset_takes_priority() -> None:
    """The HTTP Content-Type charset is preferred over the prolog (RFC 3023)."""
    raw = '<?xml version="1.0" encoding="utf-8"?><rss><t>café</t></rss>'.encode("iso-8859-1")
    assert "café" in _decode_feed_bytes(raw, "iso-8859-1")


def test_decode_feed_bytes_plain_utf8() -> None:
    """A plain utf-8 document with no declaration decodes cleanly."""
    raw = "<rss><t>café</t></rss>".encode()
    assert "café" in _decode_feed_bytes(raw, None)


def test_decode_feed_bytes_utf8_bom_wins() -> None:
    """A utf-8 BOM is honored and stripped regardless of any declared charset."""
    raw = b"\xef\xbb\xbf" + "<rss><t>café</t></rss>".encode()
    decoded = _decode_feed_bytes(raw, "iso-8859-1")
    assert decoded.startswith("<rss>")
    assert "café" in decoded


def test_decode_feed_bytes_unknown_codec_falls_back_to_utf8() -> None:
    """An unknown/garbage charset name falls back to utf-8 instead of raising."""
    raw = "<rss><t>café</t></rss>".encode()
    assert "café" in _decode_feed_bytes(raw, "totally-bogus-codec")


# --- _clip_to_budget -------------------------------------------------------------


def test_clip_to_budget_passthrough_under_budget() -> None:
    """Text within the budget is returned unchanged."""
    assert _clip_to_budget("- a\n- b", 100) == "- a\n- b"


def test_clip_to_budget_cuts_on_newline_boundary() -> None:
    """Over-budget text is trimmed at the last article boundary when possible."""
    assert _clip_to_budget("- aaaa\n- bbbb\n- cccc", 10) == "- aaaa"


def test_clip_to_budget_zero_or_negative_budget() -> None:
    """A non-positive budget yields nothing."""
    assert _clip_to_budget("abc", 0) == ""
    assert _clip_to_budget("abc", -5) == ""


# --- _parse_rss_articles: defused XML --------------------------------------------


def test_parse_rss_rejects_dtd_entity_document() -> None:
    """A document using DTD entity definitions is defused and yields an empty string."""
    doc = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE lolz [ <!ENTITY lol "lol"> ]>\n'
        '<rss version="2.0"><channel><item><title>&lol;</title></item></channel></rss>'
    )
    assert _parse_rss_articles(doc, max_articles=5) == ""


# --- render-time max_articles re-clamp -------------------------------------------


def test_fetch_rss_content_reclamps_negative_max_articles() -> None:
    """A negative max_articles (which would slice from the tail) is re-clamped at render time."""
    renderer = RssRenderer(response=_FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")]))
    # -1 would, without clamping, slice items[:-1] and drop the last article instead of limiting to 1
    result = asyncio.run(
        renderer._fetch_rss_content([{"url": "https://a.example/f", "max_articles": -1}])
    )
    assert len(result.splitlines()) == 1
    assert "First headline" in result


def test_fetch_rss_content_reclamps_oversized_max_articles() -> None:
    """A huge max_articles is capped to the supported maximum before parsing."""
    renderer = RssRenderer(response=_FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")]))
    result = asyncio.run(
        renderer._fetch_rss_content(
            [{"url": "https://a.example/f", "max_articles": RSS_MAX_MAX_ARTICLES + 10_000}]
        )
    )
    # the sample only has 3 items, so the clamp is not directly observable in the count, but the call
    # must succeed and return every available article rather than erroring on the absurd value
    assert len(result.splitlines()) == 3


# --- _download_feed: redirects & SSRF --------------------------------------------


def test_download_feed_follows_validated_redirect() -> None:
    """A redirect is followed manually and the final 200 body is returned."""
    redirect = _FakeResponse(status=302, headers={"Location": "https://b.example/final"})
    final = _FakeResponse(status=200, chunks=[RSS_SAMPLE.encode("utf-8")])
    renderer = RssRenderer(responses=[redirect, final])

    result = asyncio.run(renderer._download_feed("https://a.example/start"))
    assert "First headline" in _parse_rss_articles(result, max_articles=5)
    assert renderer.requested_urls == ["https://a.example/start", "https://b.example/final"]


def test_download_feed_rejects_non_public_host() -> None:
    """A URL whose host fails the SSRF check is never fetched."""
    renderer = RssRenderer(
        response=_FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")]), public_ok=False
    )
    result = asyncio.run(renderer._download_feed("http://169.254.169.254/latest/meta-data/"))
    assert result == ""
    assert renderer.requested_urls == []  # request was blocked before any HTTP call


def test_download_feed_revalidates_each_redirect_hop() -> None:
    """A redirect pointing at an internal address is refused before it is fetched."""

    class _SelectiveRenderer(RssRenderer):
        async def _is_public_http_url(self, url: str) -> bool:
            return "169.254" not in url

    redirect = _FakeResponse(status=302, headers={"Location": "http://169.254.169.254/"})
    renderer = _SelectiveRenderer(responses=[redirect])
    result = asyncio.run(renderer._download_feed("https://a.example/start"))
    assert result == ""
    # only the first hop was fetched; the redirect target was blocked by re-validation
    assert renderer.requested_urls == ["https://a.example/start"]


def test_download_feed_stops_at_redirect_limit() -> None:
    """An endless redirect loop is abandoned once the hop budget is exhausted."""
    loops = [
        _FakeResponse(status=302, headers={"Location": f"https://a.example/{i}"}) for i in range(20)
    ]
    renderer = RssRenderer(responses=loops)
    result = asyncio.run(renderer._download_feed("https://a.example/start"))
    assert result == ""


# --- _resolve_rss_tokens: shared budget & concurrency ----------------------------


def test_resolve_rss_tokens_enforces_total_budget() -> None:
    """The combined RSS text across all tokens is capped at RSS_MAX_TOTAL_CHARS."""
    renderer = RssRenderer(response=_FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")]))

    # each token resolves to a large block of text so the running budget is exhausted
    big_text = "x" * (RSS_MAX_TOTAL_CHARS - 100)

    async def _fake_fetch(_feeds: list[dict[str, Any]], _semaphore: Any = None) -> str:
        return big_text

    renderer._fetch_rss_content = _fake_fetch  # type: ignore[method-assign]

    feeds_by_token = {
        "<rss_feed_0>": [{"url": "https://a.example/0"}],
        "<rss_feed_1>": [{"url": "https://a.example/1"}],
    }
    resolved = asyncio.run(
        renderer._resolve_rss_tokens(["<rss_feed_0>", "<rss_feed_1>"], feeds_by_token)
    )
    total = sum(len(v) for v in resolved.values() if v != NO_RSS_DATA_INSTRUCTION)
    assert total <= RSS_MAX_TOTAL_CHARS
    # first token gets most of the budget; the second is squeezed to the placeholder
    assert resolved["<rss_feed_0>"] == big_text
    assert resolved["<rss_feed_1>"] == NO_RSS_DATA_INSTRUCTION


def test_resolve_rss_tokens_empty_feeds_get_placeholder() -> None:
    """A token with no feeds resolves to the no-data instruction."""
    renderer = RssRenderer()
    resolved = asyncio.run(renderer._resolve_rss_tokens(["<rss_feed>"], {"<rss_feed>": []}))
    assert resolved == {"<rss_feed>": NO_RSS_DATA_INSTRUCTION}


def test_resolve_rss_tokens_shares_one_semaphore_across_tokens() -> None:
    """All tokens in a clip share a single concurrency limiter passed into each fetch."""
    renderer = RssRenderer(response=_FakeResponse(chunks=[RSS_SAMPLE.encode("utf-8")]))
    seen_semaphores: list[Any] = []

    async def _capturing_fetch(_feeds: list[dict[str, Any]], semaphore: Any = None) -> str:
        seen_semaphores.append(semaphore)
        return "- headline"

    renderer._fetch_rss_content = _capturing_fetch  # type: ignore[method-assign]
    feeds_by_token = {
        "<rss_feed_0>": [{"url": "https://a.example/0"}],
        "<rss_feed_1>": [{"url": "https://a.example/1"}],
    }
    asyncio.run(renderer._resolve_rss_tokens(["<rss_feed_0>", "<rss_feed_1>"], feeds_by_token))
    assert len(seen_semaphores) == 2
    assert seen_semaphores[0] is seen_semaphores[1] is not None


# --- encoding integration through _fetch_feed_document ---------------------------


def test_fetch_feed_document_decodes_declared_encoding() -> None:
    """An ISO-8859-1 feed round-trips through the download path without corruption."""
    body = '<?xml version="1.0" encoding="ISO-8859-1"?><rss version="2.0"><channel>'
    body += "<item><title>Caf\u00e9 news</title><description>Se\u00f1or</description></item>"
    body += "</channel></rss>"
    raw = body.encode("iso-8859-1")
    # server does not advertise a charset, so the prolog must drive decoding
    renderer = RssRenderer(response=_FakeResponse(charset=None, chunks=[raw]))
    result = asyncio.run(renderer._fetch_feed_document("https://a.example/feed"))
    assert "Café news" in result
    assert "Señor" in result
