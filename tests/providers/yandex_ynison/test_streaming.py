"""Tests for provider/streaming.py — PCM helpers."""

from __future__ import annotations

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.yandex_ynison.streaming import (
    PCM_LOSSLESS_PARAMS,
    PCM_LOSSY_PARAMS,
    make_pcm_format,
)

# ---------------------------------------------------------------
# make_pcm_format
# ---------------------------------------------------------------


class TestMakePcmFormat:
    """Tests for the AudioFormat factory."""

    def test_lossless_format(self) -> None:
        """
        Lossless params produce s24le/44.1kHz/24bit/stereo.

        The no-hint floor is 44.1 kHz (spec 0006): the bulk of the Yandex
        lossless catalogue is CD-rate FLAC, so a missing format hint must
        preserve 44.1 kHz instead of upsampling to 48 kHz inside the single
        passthrough-era decode. Bit depth stays 24 (lossless expansion).
        """
        fmt = make_pcm_format(PCM_LOSSLESS_PARAMS)
        assert isinstance(fmt, AudioFormat)
        assert fmt.content_type == ContentType.PCM_S24LE
        assert fmt.sample_rate == 44100
        assert fmt.bit_depth == 24
        assert fmt.channels == 2

    def test_lossy_format(self) -> None:
        """Lossy params produce s16le/44.1kHz/16bit/stereo."""
        fmt = make_pcm_format(PCM_LOSSY_PARAMS)
        assert isinstance(fmt, AudioFormat)
        assert fmt.content_type == ContentType.PCM_S16LE
        assert fmt.sample_rate == 44100
        assert fmt.bit_depth == 16
        assert fmt.channels == 2

    def test_returns_fresh_instances(self) -> None:
        """Each call must return a NEW AudioFormat to prevent mutation leaks."""
        fmt1 = make_pcm_format(PCM_LOSSY_PARAMS)
        fmt2 = make_pcm_format(PCM_LOSSY_PARAMS)
        assert fmt1 is not fmt2

    def test_custom_params(self) -> None:
        """Custom params (22050Hz, mono) create matching format."""
        params = {
            "content_type": ContentType.PCM_S16LE,
            "sample_rate": 22050,
            "bit_depth": 16,
            "channels": 1,
        }
        fmt = make_pcm_format(params)
        assert fmt.sample_rate == 22050
        assert fmt.channels == 1


# ---------------------------------------------------------------
# Constants
# ---------------------------------------------------------------


class TestConstants:
    """Verify PCM param dicts."""

    def test_pcm_lossless_keys(self) -> None:
        """Lossless dict has all required keys."""
        assert set(PCM_LOSSLESS_PARAMS.keys()) == {
            "content_type",
            "sample_rate",
            "bit_depth",
            "channels",
        }

    def test_pcm_lossy_keys(self) -> None:
        """Lossy dict has all required keys."""
        assert set(PCM_LOSSY_PARAMS.keys()) == {
            "content_type",
            "sample_rate",
            "bit_depth",
            "channels",
        }
