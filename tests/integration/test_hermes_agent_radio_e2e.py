"""
End-to-end test: Hermes Agent Radio provider inside a hermetic MusicAssistant.

Boots a real MusicAssistant (via the shared e2e fixture: fake demo players, no LAN
discovery), configures the hermes_agent_radio provider, and plays a LIVE station from
hermesagentradio.com through the normal queue -> stream -> player pipeline. This
exercises item resolution, browse, search, and - most importantly - that the live
stream the provider hands back is consumable by MA's real ffmpeg audio pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.mass import MusicAssistant
from tests.integration.conftest import demo_players, wait_for

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider


async def _setup_provider(mass: MusicAssistant) -> MusicProvider:
    await mass.config._create_provider_instance("hermes_agent_radio", {})
    prov = cast("MusicProvider", mass.get_provider("hermes_agent_radio"))
    assert prov is not None
    return prov


async def test_provider_setup_and_features(e2e_mass: MusicAssistant) -> None:
    """Provider boots inside a real MA instance and declares its features."""
    prov = await _setup_provider(e2e_mass)
    assert ProviderFeature.BROWSE in prov.supported_features
    assert ProviderFeature.SEARCH in prov.supported_features
    assert prov.is_streaming_provider


async def test_browse_and_search_live(e2e_mass: MusicAssistant) -> None:
    """Browse lists the live stations; search finds the hip-hop mount."""
    prov = await _setup_provider(e2e_mass)
    radios = list(await prov.browse("hermes_agent_radio://"))
    names = {r.name for r in radios if r.media_type == MediaType.RADIO}
    assert "Hermes Agent Radio" in names
    assert len(radios) >= 6

    results = await prov.search("hip", [MediaType.RADIO], limit=5)
    assert results.radio
    assert "Hip-Hop" in results.radio[0].name


async def test_play_live_station_on_demo_player(e2e_mass: MusicAssistant) -> None:
    """Play the main live station through the full queue/player pipeline."""
    prov = await _setup_provider(e2e_mass)
    player = demo_players(e2e_mass)[0]
    radio = await prov.get_radio("main")
    await e2e_mass.player_queues.play_media(player.player_id, radio)
    assert await wait_for(lambda: player.state.current_media is not None, timeout=30), (
        f"player never picked up the station: {player.state}"
    )
    current = player.state.current_media
    assert current is not None
    assert "hermes_agent_radio" in current.uri
    await e2e_mass.player_queues.stop(player.player_id)


async def test_live_stream_flows_through_ffmpeg_pipeline(e2e_mass: MusicAssistant) -> None:
    """MA's streaming controller can pull real PCM audio from the live broadcast."""
    prov = await _setup_provider(e2e_mass)
    sd = await prov.get_stream_details("main", MediaType.RADIO)
    pcm = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )
    gen = e2e_mass.streams.audio.get_media_stream(sd, pcm_format=pcm, chunk_seconds=0.5)
    total = 0
    async for chunk in gen:
        total += len(chunk)
        if total >= 44100 * 4:  # ~1 second of 44.1k stereo 16-bit PCM proves decode
            break
    await gen.aclose()
    assert total >= 44100 * 4, f"only received {total} bytes of PCM from the live stream"
