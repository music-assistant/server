"""Focused coverage for setup-data account selection."""

from __future__ import annotations

from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.providers.yandex_station.constants import CONF_YM_INSTANCE

from .test_provider_cascade import _make_provider


async def test_borrow_source_comes_from_setup_data() -> None:
    """A linked account selected during setup drives the credential source."""
    provider = _make_provider({CONF_YM_INSTANCE: BORROW_SOURCE_OWN})
    provider._update_setup_data(CONF_YM_INSTANCE, "ym-1")

    source = provider._build_borrow_source()

    assert source is not None
    assert source.instance_id == "ym-1"
