"""Provider reads retain playlists when optional library fields are omitted."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ya_passport_auth import SecretStr
from yandex_music import ClientAsync, Subscription

from music_assistant.providers.yandex_music.api_client import YandexMusicClient


def test_subscription_without_renewal_remainder_is_valid() -> None:
    """An account's subscription need not include an optional renewal reminder."""
    subscription = Subscription.de_json(
        {"autoRenewable": [], "familyAutoRenewable": []}, ClientAsync(strict=True)
    )
    assert subscription is not None
    assert subscription.non_auto_renewable_remainder is None


async def test_tag_playlists_without_tag_or_personal_playlist_fields_are_available() -> None:
    """Missing tag metadata and optional playlist fields do not hide curated content."""
    playlist = json.loads((Path(__file__).parent / "fixtures/playlists/minimal.json").read_text())
    playlist["cover"] = {"type": "pic", "uri": "cdn.example/%%"}
    raw = ClientAsync(strict=True)
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    with (
        patch.object(
            raw.request, "get", AsyncMock(return_value={"ids": [{"uid": 12345, "kind": 3}]})
        ),
        patch.object(raw.request, "post", AsyncMock(return_value=[playlist])),
    ):
        result = await client.get_tag_playlists("chill")
    assert len(result) == 1
    assert result[0].title == "My Playlist"
    assert result[0].made_for is None
    assert result[0].play_counter is None
    assert result[0].playlist_absence is None
