"""Tests for the VRT MAX provider's pure browse-id helper functions."""

from __future__ import annotations

from music_assistant.providers.vrt_max.parsers import _image_url
from music_assistant.providers.vrt_max.provider import (
    _decode,
    _encode,
    _has_tracklist,
    _program_id_from_episode,
)


def test_program_id_from_radio_episode() -> None:
    """A radio-archive episode id trims one trailing (episode) segment."""
    episode_id = (
        "/vrtmax/luister/radio/s/sweet-summer-sundays~11-207/sweet-summer-sundays~11-38720-0/"
    )
    assert (
        _program_id_from_episode(episode_id)
        == "/vrtmax/luister/radio/s/sweet-summer-sundays~11-207/"
    )


def test_program_id_from_podcast_episode() -> None:
    """A podcast episode id trims two trailing (season + episode) segments."""
    episode_id = "/vrtmax/podcasts/radio-1/h/het-fortuin-carlier/1/1--een-begrafenis/"
    assert _program_id_from_episode(episode_id) == "/vrtmax/podcasts/radio-1/h/het-fortuin-carlier/"


def test_has_tracklist_true_for_radio_archive() -> None:
    """Only radio-archive episode ids expose a played-songs tracklist."""
    assert _has_tracklist("/vrtmax/luister/radio/s/show~1-2/show~1-3-0/") is True


def test_has_tracklist_false_for_podcast() -> None:
    """Podcast episodes never expose a played-songs tracklist."""
    assert _has_tracklist("/vrtmax/podcasts/radio-1/h/pod/1/1--ep/") is False


def test_encode_decode_round_trip() -> None:
    """_encode/_decode round-trip, and the encoded form is URL/path safe."""
    # This value's standard base64 encoding contains '+', '/' and '=' padding.
    value = "Ga~_r29l?J"
    encoded = _encode(value)
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded
    assert _decode(encoded) == value


def test_image_url_requests_a_rendition_not_the_original() -> None:
    """A VRT image url is rewritten to a sized rendition instead of the full original."""
    image = {"templateUrl": "https://images.vrt.be/orig/2026/06/30/abc.jpg"}

    # 'orig' is several times the size of anything rendered, and a browse list of them
    # is enough concurrent traffic to earn a 429 from VRT's image CDN.
    assert _image_url(image) == "https://images.vrt.be/w1280hx/2026/06/30/abc.jpg"


def test_image_url_strips_sizing_placeholders() -> None:
    """Any leftover template placeholder is removed."""
    image = {"templateUrl": "https://images.vrt.be/orig/2026/06/30/abc.jpg{?width}"}

    assert _image_url(image) == "https://images.vrt.be/w1280hx/2026/06/30/abc.jpg"


def test_image_url_leaves_other_hosts_untouched() -> None:
    """A url that is not a VRT image-CDN original is passed through unchanged."""
    image = {"templateUrl": "https://example.com/orig/pic.jpg"}

    assert _image_url(image) == "https://example.com/orig/pic.jpg"


def test_image_url_rejects_non_urls() -> None:
    """A missing or non-http template url yields None."""
    assert _image_url({}) is None
    assert _image_url({"templateUrl": "not-a-url"}) is None
    assert _image_url(None) is None
