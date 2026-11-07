"""Tests for the shairport-sync metadata decoder."""

from __future__ import annotations

import logging
import plistlib
from typing import Any

import pytest

from music_assistant.helpers.shairport_metadata import ShairportMetadataDecoder

NPI = "kMRMediaRemoteNowPlayingInfo"


def _decoder() -> tuple[ShairportMetadataDecoder, list[dict[str, Any]]]:
    """Return a decoder and the list it emits its updates into."""
    updates: list[dict[str, Any]] = []
    return ShairportMetadataDecoder(updates.append, logging.getLogger("test")), updates


def _now_playing(info: dict[str, Any], merge_policy: str = "merge") -> bytes:
    """Return a copl updateMRNowPlayingInfo command plist."""
    command = {
        "type": "updateMRNowPlayingInfo",
        "params": {"type": "npi-text", "mergePolicy": merge_policy, "params": info},
    }
    return plistlib.dumps(command, fmt=plistlib.FMT_BINARY)


def _playback_state(state: int) -> bytes:
    """Return a copl updateMRPlaybackState command plist."""
    command = {"type": "updateMRPlaybackState", "params": {"mrPlaybackState": state}}
    return plistlib.dumps(command, fmt=plistlib.FMT_BINARY)


@pytest.mark.parametrize(
    ("code", "key"),
    [
        ("pbeg", "play_begin"),
        ("pend", "play_end"),
        ("paus", "paused"),
        ("prsm", "resumed"),
        ("pres", "resumed"),  # codespell:ignore pres
        ("abeg", "active_begin"),
        ("aend", "active_end"),
    ],
)
def test_payloadless_ssnc_codes_emit_events(code: str, key: str) -> None:
    """Session events carry no payload and are still emitted right away."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", code, b"")
    assert updates == [{key: True}]


def test_flush_ignores_its_payload() -> None:
    """A pfls carries an RTP timestamp, which is not part of the flush event."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "pfls", b"1234567")
    assert updates == [{"flush": True}]


def test_sender_identity() -> None:
    """Sender identity items decode text, the hex DACP id and the decimal DACP port."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "snam", "Küche iPhone".encode())
    decoder.process_item("ssnc", "clip", b"192.168.1.20")
    decoder.process_item("ssnc", "daid", b"D63E8A4B5C7F12A0")
    decoder.process_item("ssnc", "acre", b"1986535575")
    decoder.process_item("ssnc", "dapo", b"3689")
    decoder.process_item("ssnc", "styp", b"Buffered")
    assert updates == [
        {"sender_name": "Küche iPhone"},
        {"client_ip": "192.168.1.20"},
        {"dacp_id": 0xD63E8A4B5C7F12A0},
        {"active_remote": "1986535575"},
        {"dacp_port": 3689},
        {"stream_type": "Buffered"},
    ]


@pytest.mark.parametrize(("code", "data"), [("daid", b"not-hex"), ("dapo", b""), ("pvol", b"x")])
def test_malformed_numeric_payload_emits_nothing(code: str, data: bytes) -> None:
    """A payload that does not parse is dropped instead of raising."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", code, data)
    assert updates == []


def test_mper_is_an_integer() -> None:
    """The persistent track id is eight big-endian bytes."""
    decoder, updates = _decoder()
    decoder.process_item("core", "mper", (0x0123456789ABCDEF).to_bytes(8, "big"))
    assert updates == [{"track_id": 0x0123456789ABCDEF}]


def test_astm_duration_in_seconds() -> None:
    """The astm track length in milliseconds becomes whole seconds."""
    decoder, updates = _decoder()
    decoder.process_item("core", "astm", (215_999).to_bytes(4, "big"))
    decoder.process_item("core", "astm", b"\x00\x01")
    assert updates == [{"duration": 215}]


def test_progress_elapsed_time_and_duration() -> None:
    """A prgr reports elapsed time and duration in seconds at 44.1 kHz."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "prgr", b"1000/442000/8821000")
    assert updates == [{"elapsed_time": 10, "duration": 200}]


def test_progress_across_rtp_wraparound() -> None:
    """RTP timestamps that wrap past 2**32 still give the right elapsed time and duration."""
    decoder, updates = _decoder()
    start = 2**32 - 44100
    decoder.process_item("ssnc", "prgr", f"{start}/{44100 * 2}/{44100 * 9}".encode())
    assert updates == [{"elapsed_time": 3, "duration": 10}]


def test_progress_before_start_is_zero() -> None:
    """A current timestamp just before the start is zero elapsed, not a wrapped day."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "prgr", b"441000/440000/882000")
    assert updates == [{"elapsed_time": 0, "duration": 10}]


@pytest.mark.parametrize(
    ("data", "volume", "airplay_volume"),
    [
        (b"0.00,100.00,-48.00,0.00", 100, 0.0),
        (b"-15.00,50.00,-48.00,0.00", 50, -15.0),
        (b"-30.00,0.00,-48.00,0.00", 0, -30.0),
        (b"-144.00,0.00,-48.00,0.00", 0, -144.0),
    ],
)
def test_volume_mapping(data: bytes, volume: int, airplay_volume: float) -> None:
    """A pvol maps -30..0 dB onto 0..100, mute to 0, and keeps the raw AirPlay volume."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "pvol", data)
    assert updates == [{"volume": volume, "airplay_volume": airplay_volume}]


def test_core_items_batched_inside_block() -> None:
    """Core items between mdst and mden are emitted together when the block closes."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "mdst", b"1234")
    decoder.process_item("core", "minm", "Café".encode())
    decoder.process_item("core", "asar", b"Artist")
    decoder.process_item("ssnc", "pvol", b"-15.00,50.00,-48.00,0.00")
    decoder.process_item("core", "asal", b"Album")
    assert updates == [
        {"metadata_start": True},
        {"volume": 50, "airplay_volume": -15.0},
    ]
    decoder.process_item("ssnc", "mden", b"1234")
    assert updates[2:] == [{"title": "Café", "artist": "Artist", "album": "Album"}]


def test_core_items_outside_block_emitted_right_away() -> None:
    """Core items without block markers, or after a block closed, go out one by one."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "mdst", b"")
    decoder.process_item("ssnc", "mden", b"")
    decoder.process_item("core", "minm", b"Title")
    decoder.process_item("core", "asar", b"Artist")
    assert updates == [{"metadata_start": True}, {"title": "Title"}, {"artist": "Artist"}]


@pytest.mark.parametrize(
    ("block_rtptime", "expected"),
    [
        (b"66150", {"title": "Title", "duration": 240}),
        (b"99999", {"title": "Title"}),
    ],
)
def test_progress_before_block_gives_that_track_its_duration(
    block_rtptime: bytes, expected: dict[str, Any]
) -> None:
    """A prgr just before the block of the same track (same RTP start) lends it the duration."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "prgr", b"66150/66150/10650150")
    decoder.process_item("ssnc", "mdst", block_rtptime)
    decoder.process_item("core", "minm", b"Title")
    decoder.process_item("ssnc", "mden", block_rtptime)
    assert updates[-1] == expected


@pytest.mark.parametrize("item_type", ["core", "ssnc"])
def test_artwork_emitted_inside_block(item_type: str) -> None:
    """Cover art goes out right away, even inside an open block."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "mdst", b"")
    decoder.process_item(item_type, "PICT", b"\xff\xd8\xff")
    assert updates == [{"metadata_start": True}, {"artwork": b"\xff\xd8\xff"}]


def test_copl_replace_resets_now_playing() -> None:
    """A replace command starts a new track and drops fields of the previous one."""
    decoder, updates = _decoder()
    decoder.process_item(
        "ssnc",
        "copl",
        _now_playing({f"{NPI}Title": "Old", f"{NPI}Album": "Old Album"}, "replace"),
    )
    decoder.process_item(
        "ssnc",
        "copl",
        _now_playing(
            {
                f"{NPI}Title": "New",
                f"{NPI}Artist": "Artist",
                f"{NPI}Duration": 245.7,
                f"{NPI}ElapsedTime": 3.9,
                f"{NPI}UniqueIdentifier": 42,
                f"{NPI}PlaybackRate": 1.0,
            },
            "replace",
        ),
    )
    assert updates[2:] == [
        {"metadata_start": True},
        {
            "title": "New",
            "artist": "Artist",
            "duration": 245,
            "track_id": 42,
            "elapsed_time": 3,
            "resumed": True,
        },
    ]


def test_copl_merge_keeps_fields_but_not_elapsed_time() -> None:
    """A merge re-reports stored fields, while a stored elapsed time is not repeated."""
    decoder, updates = _decoder()
    decoder.process_item(
        "ssnc",
        "copl",
        _now_playing(
            {f"{NPI}Title": "Title", f"{NPI}ElapsedTime": 10.0, f"{NPI}PlaybackRate": 1.0},
            "replace",
        ),
    )
    decoder.process_item("ssnc", "copl", _now_playing({f"{NPI}PlaybackRate": 0.0}))
    assert updates[2:] == [{"title": "Title", "paused": True}]


def test_copl_missing_playback_rate_means_paused() -> None:
    """Now-playing info without a playback rate reports paused."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "copl", _now_playing({f"{NPI}Title": "Title"}))
    assert updates == [{"title": "Title", "paused": True}]


def test_copl_artwork_not_repeated() -> None:
    """Artwork is emitted with the command that carries it and not with later merges."""
    decoder, updates = _decoder()
    decoder.process_item(
        "ssnc", "copl", _now_playing({f"{NPI}ArtworkData": b"\x89PNG", f"{NPI}PlaybackRate": 1})
    )
    decoder.process_item("ssnc", "copl", _now_playing({f"{NPI}Title": "Title"}))
    assert updates == [
        {"artwork": b"\x89PNG", "resumed": True},
        {"title": "Title", "resumed": True},
    ]


@pytest.mark.parametrize(
    ("state", "expected"),
    [(1, [{"resumed": True}]), (2, [{"paused": True}]), (3, [{"play_end": True}]), (4, [])],
)
def test_copl_playback_state(state: int, expected: list[dict[str, Any]]) -> None:
    """Playback states 1, 2 and 3 map to resumed, paused and play_end; 4 is ignored."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "copl", _playback_state(state))
    assert updates == expected


def test_copl_merge_after_a_playback_state_keeps_that_state() -> None:
    """A merge without a rate after a pause reports paused, not the rate stored before it."""
    decoder, updates = _decoder()
    decoder.process_item(
        "ssnc", "copl", _now_playing({f"{NPI}Title": "Title", f"{NPI}PlaybackRate": 1.0})
    )
    decoder.process_item("ssnc", "copl", _playback_state(2))
    decoder.process_item("ssnc", "copl", _now_playing({f"{NPI}ArtworkData": b"\x89PNG"}))
    assert updates[1:] == [
        {"paused": True},
        {"title": "Title", "artwork": b"\x89PNG", "paused": True},
    ]


@pytest.mark.parametrize(
    "data",
    [
        b"bplist00garbage",
        b"",
        plistlib.dumps(["not", "a", "dict"], fmt=plistlib.FMT_BINARY),
        plistlib.dumps(
            {"type": "updateMRNowPlayingInfo", "params": {"type": "npi-text", "params": "x"}},
            fmt=plistlib.FMT_BINARY,
        ),
        plistlib.dumps(
            {"type": "updateMRPlaybackState", "params": {"mrPlaybackState": "1"}},
            fmt=plistlib.FMT_BINARY,
        ),
        plistlib.dumps(
            {"type": "updateMRSupportedCommands", "params": {}}, fmt=plistlib.FMT_BINARY
        ),
    ],
)
def test_copl_invalid_or_ignored_emits_nothing(data: bytes) -> None:
    """Invalid plists, wrong shapes and unhandled command types emit nothing."""
    decoder, updates = _decoder()
    decoder.process_item("ssnc", "copl", data)
    assert updates == []
