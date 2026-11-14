"""
Tests for the announcements the players controller plays.

Every player of these tests has a native protocol with an id of its own (``proto_<player id>``);
an announcement is handed to the protocol that plays it, the player's own or one of its
audio protocols.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
from unittest.mock import ANY, AsyncMock, MagicMock, PropertyMock, call, patch

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import MusicAssistantError, PlayerCommandFailed
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    ATTR_IDLE_SINCE,
    ATTR_MUTE_LOCK,
    ATTR_PREVIOUS_VOLUME,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_MUTE_CONTROL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.announcements import ANNOUNCEMENT_TTS_TIMEOUT
from music_assistant.helpers.tts import TTS_QUERY_TIMEOUT_SECONDS, TTSLanguageNotSupportedError
from music_assistant.models.player import Player
from music_assistant.models.player_protocol import AnnouncementFeature, PlayerProtocol
from tests.common import MockProtocol, MockProvider, attach_mock, use_real_create_task


def _proto(player_id: str) -> str:
    """Return the id of the native protocol of a player in these tests."""
    return f"proto_{player_id}"


def _attach(
    provider: MockProvider,
    player_id: str,
    name: str | None = None,
    *,
    player_type: PlayerType = PlayerType.PLAYER,
    protocol_class: type[MockProtocol] = MockProtocol,
) -> tuple[Player, MockProtocol]:
    """
    Return a player with the given id and its native protocol, which has an id of its own.

    :param provider: The (mock) provider of the protocol; its mass holds the controller.
    :param player_id: Id of the player.
    :param name: The name the device reports.
    :param player_type: The type of player the protocol reports.
    :param protocol_class: The class of the protocol.
    """
    return attach_mock(
        provider,
        _proto(player_id),
        name,
        # a group is a virtual player
        role=ProtocolRole.VIRTUAL if player_type == PlayerType.GROUP else ProtocolRole.NATIVE,
        player_id=player_id,
        player_type=player_type,
        protocol_class=protocol_class,
    )


def _attach_audio(
    provider: MockProvider, player: Player, protocol_id: str, name: str | None = None
) -> MockProtocol:
    """
    Return an audio protocol attached to a player, next to its native protocol.

    :param provider: The (mock) provider of the audio protocol.
    :param player: The player it belongs to.
    :param protocol_id: Id of the audio protocol.
    :param name: The name the device reports through it.
    """
    _, protocol = attach_mock(provider, protocol_id, name, role=ProtocolRole.AUDIO, player=player)
    return protocol


def _own(player: Player) -> MockProtocol:
    """Return the native protocol of a player."""
    return cast("MockProtocol", player.own_protocol)


def _controller(mock_mass: MagicMock) -> PlayerController:
    """Return a players controller that is the one of the mocked mass."""
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    return controller


def _refresh(*players: Player) -> None:
    """Recalculate the state of the given players, in order, without publishing it."""
    for player in players:
        player._cache.clear()
        for protocol in player.attached_protocols.values():
            protocol._cache.clear()
        player.update_state(signal_event=False, force_update=True)


def _player_config_stub(
    values: dict[str, object] | None = None,
    *,
    min_volume: int = 0,
    max_volume: int = 100,
) -> Callable[..., object]:
    """
    Build a ``get_player_value`` side effect.

    :param values: Extra config keys to answer, e.g. ``{CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE}``.
    :param min_volume: Value returned for the ``min_volume`` key.
    :param max_volume: Value returned for the ``max_volume`` key.
    """
    config: dict[str, object] = {
        CONF_MIN_VOLUME: min_volume,
        CONF_MAX_VOLUME: max_volume,
        **(values or {}),
    }

    def _conf(_player_id: str, key: str, default: object = None) -> object:
        if key in config:
            return config[key]
        return default

    return _conf


def _announcement() -> PlayerMedia:
    """Return the announcement to play."""
    return PlayerMedia(
        uri="http://ma/announcement/player_1.mp3",
        media_type=MediaType.ANNOUNCEMENT,
        title="Announcement",
        duration=3,
    )


def _mute_natively(player: Player) -> AsyncMock:
    """Give the player a native mute control, mute it and return the mute handler of its device."""
    protocol = _own(player)
    protocol._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
    protocol._attr_volume_muted = True
    mute_mock = AsyncMock(side_effect=lambda muted: setattr(protocol, "_attr_volume_muted", muted))
    protocol.volume_mute = mute_mock  # type: ignore[method-assign]
    _refresh(player)
    assert player.volume_muted is True
    return mute_mock


@pytest.fixture
def running_background_tasks(mock_mass: MagicMock) -> Iterator[None]:
    """
    Really run the tasks that ``mass.create_task`` is handed, and raise what they raise.

    The real implementation only logs the exception of a background task, which would
    leave a test that dispatches its work through the TaskManager passing regardless.
    """
    tasks: list[asyncio.Task[Any]] = []
    use_real_create_task(mock_mass)
    real_create_task = mock_mass.create_task

    def _create_task(target: Any, *args: Any, **kwargs: Any) -> Any:
        task = real_create_task(target, *args, **kwargs)
        if isinstance(task, asyncio.Task):
            tasks.append(task)
        return task

    mock_mass.create_task = MagicMock(side_effect=_create_task)
    yield
    errors = [
        err
        for task in tasks
        if task.done() and not task.cancelled() and (err := task.exception()) is not None
    ]
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("background tasks failed", errors)


@pytest.fixture
def mock_mass() -> Iterator[MagicMock]:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    # kept so teardown still sees its calls when a test swaps mass.create_task
    create_task = mass.create_task = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_player_value = MagicMock(side_effect=_player_config_stub())
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    yield mass
    # a mocked create_task never runs what it is handed, so close what no test started
    for scheduled in create_task.call_args_list:
        target = scheduled.args[0] if scheduled.args else None
        if inspect.iscoroutine(target) and inspect.getcoroutinestate(target) == "CORO_CREATED":
            target.close()


class _AnnouncingProtocol(MockProtocol):
    """A protocol whose announcement behaviour a test sets."""

    reported_announcement_features: set[AnnouncementFeature] = set()  # noqa: RUF012

    @property
    def announcement_features(self) -> set[AnnouncementFeature]:
        """Return the announcement behaviour the test set."""
        return self.reported_announcement_features


class TestGetAnnouncementVolume:
    """Test how the per-player announcement volume is resolved."""

    @staticmethod
    def _player(
        mock_mass: MagicMock, features: set[AnnouncementFeature], volume_level: int | None
    ) -> tuple[PlayerController, Player]:
        """Build a player that announces natively, with the given features and volume."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "p1", "Player 1", protocol_class=_AnnouncingProtocol)
        cast("_AnnouncingProtocol", protocol).reported_announcement_features = features
        protocol._attr_supported_features = {
            PlayerFeature.PLAY_ANNOUNCEMENT,
            PlayerFeature.VOLUME_SET,
        }
        protocol._attr_volume_level = volume_level
        _refresh(player)
        return controller, player

    def test_native_volume_absent_defaults_to_no_adjustment(self, mock_mass: MagicMock) -> None:
        """A player whose native route ignores the level gets no bump by default."""
        controller, _player = self._player(mock_mass, set(), 40)
        assert controller.get_announcement_volume("p1", None) is None

    def test_explicit_override_wins_over_the_none_default(self, mock_mass: MagicMock) -> None:
        """An explicit level is honoured even when the strategy resolves to none."""
        controller, _player = self._player(mock_mass, set(), 40)
        assert controller.get_announcement_volume("p1", 60) == 60

    def test_explicit_override_is_clamped_to_the_max(self, mock_mass: MagicMock) -> None:
        """An explicit level is still bounded by the configured maximum (default 75)."""
        controller, _player = self._player(mock_mass, set(), 40)
        assert controller.get_announcement_volume("p1", 90) == 75

    def test_native_volume_present_defaults_to_percentual(self, mock_mass: MagicMock) -> None:
        """A player whose native route honours the level keeps the percentual default."""
        controller, _player = self._player(mock_mass, {AnnouncementFeature.SUPPORTS_VOLUME}, 40)
        # percentual: 40 + (40/100) * 85 = 74, within the default 15..75 bounds
        assert controller.get_announcement_volume("p1", None) == 74

    def test_default_follows_the_resolved_announce_output(self, mock_mass: MagicMock) -> None:
        """The default reflects the output that announces, not the player's own hint."""
        controller, player = self._player(mock_mass, {AnnouncementFeature.SUPPORTS_VOLUME}, 40)
        output = SimpleNamespace(protocol_id="out", announcement_features=set())
        with patch.object(controller, "_resolve_announce_player", return_value=output):
            assert controller.announce_output_supports_volume(player) is False
            assert controller.get_announcement_volume("p1", None) is None

    def test_generic_fallback_route_supports_volume(self, mock_mass: MagicMock) -> None:
        """With no native announce output, the builtin fallback applies the level."""
        controller, player = self._player(mock_mass, set(), 40)
        with patch.object(controller, "_resolve_announce_player", return_value=None):
            assert controller.announce_output_supports_volume(player) is True
            # percentual default applies: 40 + (40/100) * 85 = 74
            assert controller.get_announcement_volume("p1", None) == 74


class TestNativeRouteIgnoresWantedVolume:
    """Test the decision to leave the native announce route for the builtin path."""

    def _controller(self, mock_mass: MagicMock) -> PlayerController:
        """Return a controller with get_announcement_volume stubbed to a fixed level."""
        controller = PlayerController(mock_mass)
        controller.get_announcement_volume = MagicMock(return_value=60)  # type: ignore[method-assign]
        return controller

    @staticmethod
    def _player(volume_control: str, volume_level: int | None) -> SimpleNamespace:
        """Build a fake target player with the given volume control and level."""
        return SimpleNamespace(
            player_id="p1",
            state=SimpleNamespace(volume_control=volume_control, volume_level=volume_level),
        )

    @staticmethod
    def _output(supports_volume: bool) -> SimpleNamespace:
        """Build a fake announce output that does or does not honour the volume."""
        features = {AnnouncementFeature.SUPPORTS_VOLUME} if supports_volume else set()
        return SimpleNamespace(announcement_features=features)

    def _decide(
        self, controller: PlayerController, player: SimpleNamespace, output: SimpleNamespace
    ) -> bool:
        """Run the divert decision with the fakes cast to the expected type."""
        return controller._native_route_ignores_wanted_volume(
            cast("Player", player), cast("PlayerProtocol", output), None
        )

    def test_diverts_when_the_target_volume_can_be_set(self, mock_mass: MagicMock) -> None:
        """A wanted level the native route ignores diverts to the builtin path."""
        controller = self._controller(mock_mass)
        player = self._player(PLAYER_CONTROL_NATIVE, 40)
        assert self._decide(controller, player, self._output(False))

    def test_stays_native_when_the_output_applies_the_volume(self, mock_mass: MagicMock) -> None:
        """An output that honours the level keeps the native route."""
        controller = self._controller(mock_mass)
        player = self._player(PLAYER_CONTROL_NATIVE, 40)
        assert not self._decide(controller, player, self._output(True))

    def test_stays_native_without_a_volume_control(self, mock_mass: MagicMock) -> None:
        """No volume control means the builtin path cannot apply the level, so stay native."""
        controller = self._controller(mock_mass)
        player = self._player(PLAYER_CONTROL_NONE, 40)
        assert not self._decide(controller, player, self._output(False))

    def test_stays_native_with_an_unknown_level(self, mock_mass: MagicMock) -> None:
        """An unknown current level cannot be restored, so stay native."""
        controller = self._controller(mock_mass)
        player = self._player(PLAYER_CONTROL_NATIVE, None)
        assert not self._decide(controller, player, self._output(False))


def _announcing_player(
    mock_mass: MagicMock, announcements: dict[str, object]
) -> tuple[PlayerController, Player, MagicMock]:
    """Create a controller and a player with native announcement support."""
    controller = _controller(mock_mass)
    provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
    player, protocol = _attach(provider, "player_1", "Player 1")
    protocol._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
    render = MagicMock()
    render.wait_ready = AsyncMock(return_value=True)
    render.wait_finished = AsyncMock(return_value=3.0)

    # mimic the real renderer: it owns which announcement each player is playing
    def _register(player_id: str, announce_data: object) -> MagicMock:
        announcements[player_id] = announce_data
        return render

    async def _unregister(player_id: str, _render: object) -> None:
        announcements.pop(player_id, None)

    renderer = mock_mass.streams.announcement_renderer
    renderer.register = MagicMock(side_effect=_register)
    renderer.unregister = AsyncMock(side_effect=_unregister)
    mock_mass.streams.get_announcement_url = MagicMock(
        side_effect=lambda player_id, **_kwargs: f"http://ma/announcement/{player_id}.mp3"
    )
    _refresh(player)
    return controller, player, render


class TestPlayAnnouncementCleanup:
    """Test announcement data cleanup after play_announcement."""

    async def test_announcement_data_removed_after_playback(self, mock_mass: MagicMock) -> None:
        """The registered announcement data is released once playback finished."""
        announcements: dict[str, object] = {}
        controller, player, _render = _announcing_player(mock_mass, announcements)

        async def _play_announcement(*_args: object, **_kwargs: object) -> None:
            # entry must exist while the announcement is being played/served
            assert "player_1" in announcements

        announce = AsyncMock(side_effect=_play_announcement)
        _own(player).play_announcement = announce  # type: ignore[method-assign]

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        announce.assert_awaited_once()
        assert announcements == {}
        mock_mass.streams.announcement_renderer.unregister.assert_awaited_once()

    async def test_announcement_starts_the_idle_count_over(self, mock_mass: MagicMock) -> None:
        """A native announcement that leaves the playback state as it was still counts."""
        controller, player, _render = _announcing_player(mock_mass, {})
        announce = AsyncMock()
        _own(player).play_announcement = announce  # type: ignore[method-assign]
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        announce.assert_awaited_once()
        assert player.playback_state == PlaybackState.IDLE
        assert ATTR_IDLE_SINCE not in player.extra_data

    async def test_announcement_data_removed_on_error(self, mock_mass: MagicMock) -> None:
        """The registered announcement data is released even when playback fails."""
        announcements: dict[str, object] = {}
        controller, player, _render = _announcing_player(mock_mass, announcements)
        _own(player).play_announcement = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom")
        )

        with pytest.raises(PlayerCommandFailed):
            await controller.play_announcement("player_1", "http://test/announcement.mp3")

        assert announcements == {}
        mock_mass.streams.announcement_renderer.unregister.assert_awaited_once()

    async def test_native_announcement_starts_on_first_audio(self, mock_mass: MagicMock) -> None:
        """A native implementation is handed the url as soon as there is audio to serve."""
        announcements: dict[str, object] = {}
        controller, player, render = _announcing_player(mock_mass, announcements)
        announce = AsyncMock()
        _own(player).play_announcement = announce  # type: ignore[method-assign]

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        announce.assert_awaited_once()
        # waiting for the whole clip here would delay the player for a slow source;
        # the length is resolved downstream while it plays
        render.wait_ready.assert_awaited_once()
        render.wait_finished.assert_not_awaited()

    async def test_feature_still_offered_after_the_render_keeps_the_native_path(
        self, mock_mass: MagicMock
    ) -> None:
        """A player still offering the feature once its audio is ready announces natively."""
        announcements: dict[str, object] = {}
        controller, _player, render = _announcing_player(mock_mass, announcements)
        order: list[str] = []

        async def _wait_ready() -> bool:
            order.append("render")
            return True

        async def _native(*_args: object, **_kwargs: object) -> None:
            order.append("native")

        render.wait_ready = AsyncMock(side_effect=_wait_ready)

        with (
            patch.object(controller, "_play_native_announcement", side_effect=_native) as native,
            patch.object(controller, "_play_announcement") as fallback,
        ):
            await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native.assert_awaited_once()
        fallback.assert_not_awaited()
        assert order == ["render", "native"]

    async def test_feature_lost_during_the_render_falls_back_to_the_default(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A player that stopped offering the feature while its audio rendered is not handed it.

        Rendering speech takes seconds, and an output that announces by mixing the clip
        into what it is already playing stops offering the feature the moment that
        playback ends - so the default implementation has to take over.
        """
        announcements: dict[str, object] = {}
        controller, player, render = _announcing_player(mock_mass, announcements)

        async def _wait_ready() -> bool:
            _own(player)._attr_supported_features.discard(PlayerFeature.PLAY_ANNOUNCEMENT)
            return True

        render.wait_ready = AsyncMock(side_effect=_wait_ready)

        with (
            patch.object(controller, "_play_native_announcement") as native,
            patch.object(controller, "_play_announcement") as fallback,
        ):
            await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native.assert_not_awaited()
        fallback.assert_awaited_once()
        # nothing renders the clip natively, so the announcement is not tagged for it
        registered = mock_mass.streams.announcement_renderer.register.call_args.args[1]
        assert registered["announce_player_id"] is None

    async def test_native_route_without_volume_support_falls_back_when_a_level_is_wanted(
        self, mock_mass: MagicMock
    ) -> None:
        """A native route that ignores the level uses the builtin path once one is requested."""
        announcements: dict[str, object] = {}
        controller, player, _render = _announcing_player(mock_mass, announcements)
        protocol = _own(player)
        # the builtin path can only apply the level when the target has a settable volume
        protocol._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        protocol._attr_volume_level = 40
        _refresh(player)

        with (
            patch.object(
                type(protocol),
                "announcement_features",
                new_callable=PropertyMock,
                return_value=set(),
            ),
            patch.object(controller, "_play_native_announcement") as native,
            patch.object(controller, "_play_announcement") as fallback,
        ):
            await controller.play_announcement(
                "player_1", "http://test/announcement.mp3", volume_level=60
            )

        native.assert_not_awaited()
        fallback.assert_awaited_once()

    async def test_native_route_without_volume_support_stays_native_by_default(
        self, mock_mass: MagicMock
    ) -> None:
        """With no level requested it defaults to no adjustment and keeps the native route."""
        announcements: dict[str, object] = {}
        controller, player, _render = _announcing_player(mock_mass, announcements)

        with (
            patch.object(
                type(_own(player)),
                "announcement_features",
                new_callable=PropertyMock,
                return_value=set(),
            ),
            patch.object(controller, "_play_native_announcement") as native,
            patch.object(controller, "_play_announcement") as fallback,
        ):
            await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native.assert_awaited_once()
        fallback.assert_not_awaited()


class _AnnounceSetup(NamedTuple):
    """A player announcing through one of its audio protocols, with the calls it makes mocked."""

    controller: PlayerController
    player: Player
    output: MockProtocol
    volume_set: AsyncMock
    play_announcement: AsyncMock


@pytest.mark.usefixtures("running_background_tasks")
class TestNativeAnnouncementVolumeRouting:
    """The announcement volume is applied through the control that owns it."""

    ANNOUNCE_VOLUME = 45

    def _make_setup(self, mock_mass: MagicMock, volume_control: str) -> _AnnounceSetup:
        """
        Create a player announcing through one of its audio protocols.

        The player has another audio protocol and a bridge riding on the announcing output,
        so any of them can be named as its volume control.

        :param volume_control: Value of the player's volume control config entry.
        """
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_VOLUME_CONTROL: volume_control})
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, _ = _attach(provider, "parent", "Parent")
        output = _attach_audio(
            MockProvider("airplay", instance_id="airplay", mass=mock_mass), player, "output"
        )
        output._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        output._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        sibling = _attach_audio(
            MockProvider("dlna", instance_id="dlna", mass=mock_mass), player, "sibling"
        )
        sibling._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        sibling._attr_volume_level = 20
        bridge = _attach_audio(
            MockProvider("sendspin", instance_id="sendspin", mass=mock_mass), player, "bridge"
        )
        bridge._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        bridge._attr_underlying_protocol_id = "output"
        bridge._attr_volume_level = 20
        # an external control (e.g. a Home Assistant volume entity) is not a protocol
        controller._controls = {
            "ha_volume": PlayerControl(
                id="ha_volume",
                provider="hass",
                name="Amplifier volume",
                supports_volume=True,
                volume_level=20,
            )
        }
        _refresh(player)
        play_announcement = AsyncMock()
        volume_set = AsyncMock()
        output.play_announcement = play_announcement  # type: ignore[method-assign]
        controller._handle_cmd_volume_set = volume_set  # type: ignore[method-assign]
        return _AnnounceSetup(controller, player, output, volume_set, play_announcement)

    async def _announce(self, setup: _AnnounceSetup) -> None:
        """Play an announcement on the player, rendered by its announcing output."""
        await setup.controller._play_native_announcement(
            setup.player, setup.output, _announcement(), self.ANNOUNCE_VOLUME
        )

    async def test_external_control_applies_the_announcement_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """Another audio protocol owning the volume gets the announcement volume, not the output."""
        setup = self._make_setup(mock_mass, "sibling")

        await self._announce(setup)

        # the output cannot attenuate what another control is already attenuating,
        # so the level goes through that control and the output announces at unity
        assert setup.volume_set.await_args_list == [
            call("parent", self.ANNOUNCE_VOLUME),
            call("parent", 20),
        ]
        setup.play_announcement.assert_awaited_once_with(ANY, None)

    async def test_output_that_applies_the_volume_itself_gets_it_handed_down(
        self, mock_mass: MagicMock
    ) -> None:
        """
        An output that applies the announcement volume itself is left to do so.

        A player that mixes the clip into audio it is already playing knows when the
        clip becomes audible; setting the level up front would raise the music that
        is still playing instead. Any other output has it applied before it starts.
        """
        setup = self._make_setup(mock_mass, "sibling")

        with patch.object(
            type(setup.output),
            "announcement_features",
            new_callable=PropertyMock,
            return_value={
                AnnouncementFeature.SUPPORTS_VOLUME,
                AnnouncementFeature.APPLIES_VOLUME,
            },
        ):
            await self._announce(setup)

        setup.volume_set.assert_not_awaited()
        setup.play_announcement.assert_awaited_once_with(ANY, self.ANNOUNCE_VOLUME)
        setup.play_announcement.reset_mock()

        await self._announce(setup)

        assert setup.volume_set.await_args_list == [
            call("parent", self.ANNOUNCE_VOLUME),
            call("parent", 20),
        ]
        setup.play_announcement.assert_awaited_once_with(ANY, None)

    async def test_player_control_applies_the_announcement_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """A player control owning the volume (e.g. an HA entity) gets the announcement volume."""
        setup = self._make_setup(mock_mass, "ha_volume")

        await self._announce(setup)

        assert setup.volume_set.await_args_list == [
            call("parent", self.ANNOUNCE_VOLUME),
            call("parent", 20),
        ]
        setup.play_announcement.assert_awaited_once_with(ANY, None)

    async def test_external_control_volume_restored_when_the_announcement_fails(
        self, mock_mass: MagicMock
    ) -> None:
        """The temporary volume is restored even when the announcement itself fails."""
        setup = self._make_setup(mock_mass, "sibling")
        setup.play_announcement.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await self._announce(setup)

        assert setup.volume_set.await_args_list[-1] == call("parent", 20)

    async def test_announcing_output_keeps_the_announcement_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """An output that owns the volume applies the announcement volume itself."""
        setup = self._make_setup(mock_mass, "output")

        await self._announce(setup)

        setup.volume_set.assert_not_awaited()
        setup.play_announcement.assert_awaited_once_with(ANY, self.ANNOUNCE_VOLUME)

    async def test_bridge_on_the_announcing_output_keeps_the_announcement_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """A bridge riding on the announcing output forwards the volume to it."""
        setup = self._make_setup(mock_mass, "bridge")

        await self._announce(setup)

        setup.volume_set.assert_not_awaited()
        setup.play_announcement.assert_awaited_once_with(ANY, self.ANNOUNCE_VOLUME)

    async def test_native_volume_is_applied_through_the_player(self, mock_mass: MagicMock) -> None:
        """
        A native volume lives on the player's own protocol, so the player applies and restores it.

        The rendering output has no way to reach the native volume, and its own idea of the
        level can be stale, so routing through the player keeps both the announcement level
        and the restore on the control that actually knows it.
        """
        setup = self._make_setup(mock_mass, PLAYER_CONTROL_NATIVE)
        _own(setup.player)._attr_volume_level = 20
        _refresh(setup.player)

        await self._announce(setup)

        assert setup.volume_set.await_args_list == [
            call("parent", self.ANNOUNCE_VOLUME),
            call("parent", 20),
        ]
        setup.play_announcement.assert_awaited_once_with(ANY, None)

    async def test_native_volume_on_its_own_output_is_kept_by_the_player(
        self, mock_mass: MagicMock
    ) -> None:
        """A player announcing on its own output can apply its native volume itself."""
        setup = self._make_setup(mock_mass, PLAYER_CONTROL_NATIVE)
        own = _own(setup.player)
        play_announcement = AsyncMock()
        own.play_announcement = play_announcement  # type: ignore[method-assign]

        await setup.controller._play_native_announcement(
            setup.player, own, _announcement(), self.ANNOUNCE_VOLUME
        )

        setup.volume_set.assert_not_awaited()
        play_announcement.assert_awaited_once_with(ANY, self.ANNOUNCE_VOLUME)

    async def test_without_volume_control_no_volume_is_applied(self, mock_mass: MagicMock) -> None:
        """Nothing in the signal path can set a volume, so the announcement plays as-is."""
        setup = self._make_setup(mock_mass, PLAYER_CONTROL_NONE)

        await self._announce(setup)

        setup.volume_set.assert_not_awaited()
        setup.play_announcement.assert_awaited_once_with(ANY, None)


class TestPlayAnnouncementMessage:
    """A spoken message is rendered by a TTS engine and then announced like any other audio."""

    ANNOUNCE_MODULE = "music_assistant.controllers.players.announcements"

    def _make_engine(self, path: str = "http://speech/spoken.mp3") -> MagicMock:
        """Create a TTS engine that renders every message to the given path."""
        engine = MagicMock()
        engine.uid = "tts_plugin/voice"
        engine.id = "voice"
        engine.provider.get_tts_message = AsyncMock(return_value=SimpleNamespace(path=path))
        return engine

    def _make_player(
        self, mock_mass: MagicMock, announcements: dict[str, object]
    ) -> tuple[PlayerController, AsyncMock]:
        """Create a controller and a player with native announcement support."""
        controller, player, _render = _announcing_player(mock_mass, announcements)
        announce = AsyncMock()
        _own(player).play_announcement = announce  # type: ignore[method-assign]
        return controller, announce

    def _add_group(self, mock_mass: MagicMock, controller: PlayerController) -> Player:
        """Register a group player that holds the controller's player as its only member."""
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        group, group_protocol = _attach(
            provider, "group_1", "Group 1", player_type=PlayerType.GROUP
        )
        group_protocol._attr_group_members = ["player_1"]
        _refresh(group)
        return group

    def _members_coordinate(self) -> Any:
        """Let every mock protocol report that it lines up an announcement start."""
        return patch.object(
            MockProtocol,
            "announcement_features",
            new_callable=PropertyMock,
            return_value={
                AnnouncementFeature.SUPPORTS_VOLUME,
                AnnouncementFeature.COORDINATES_START,
            },
        )

    async def test_message_is_spoken_by_the_configured_engine(self, mock_mass: MagicMock) -> None:
        """A message is rendered by the default engine and announced as the rendered audio."""
        announcements: dict[str, object] = {}
        controller, announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()

        with patch(
            f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)
        ):
            await controller.play_announcement("player_1", message="dinner is ready")

        # no language is sent, so the engine speaks in the language it is configured for
        engine.provider.get_tts_message.assert_awaited_once_with(
            "dinner is ready", language=None, engine_id="voice", options=None
        )
        registered = mock_mass.streams.announcement_renderer.register.call_args.args[1]
        assert registered["announcement_url"] == "http://speech/spoken.mp3"
        announce.assert_awaited_once()

    async def test_an_explicit_language_reaches_the_engine(self, mock_mass: MagicMock) -> None:
        """A message can name the language to speak it in."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()

        with patch(
            f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)
        ):
            await controller.play_announcement(
                "player_1", message="het eten is klaar", language="nl-NL"
            )

        engine.provider.get_tts_message.assert_awaited_once_with(
            "het eten is klaar", language="nl-NL", engine_id="voice", options=None
        )

    async def test_a_rejected_language_is_retried_without_it(self, mock_mass: MagicMock) -> None:
        """An engine that rejects the language speaks the message in its default voice."""
        announcements: dict[str, object] = {}
        controller, announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()
        engine.provider.get_tts_message = AsyncMock(
            side_effect=[
                TTSLanguageNotSupportedError(
                    "TTS engine 'voice' does not support language 'en-US'"
                ),
                SimpleNamespace(path="http://speech/spoken.mp3"),
            ]
        )

        with patch(
            f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)
        ):
            await controller.play_announcement(
                "player_1", message="dinner is ready", language="en-US"
            )

        first_call, second_call = engine.provider.get_tts_message.await_args_list
        assert first_call.kwargs["language"] == "en-US"
        assert second_call.kwargs["language"] is None
        registered = mock_mass.streams.announcement_renderer.register.call_args.args[1]
        assert registered["announcement_url"] == "http://speech/spoken.mp3"
        announce.assert_awaited_once()

    @pytest.mark.parametrize(
        "error", [TimeoutError(), MusicAssistantError("engine did not respond within 30s")]
    )
    async def test_a_failure_that_is_not_a_language_rejection_is_not_retried(
        self, mock_mass: MagicMock, error: Exception
    ) -> None:
        """A timeout or a structured failure is no language rejection, so it is not retried."""
        announcements: dict[str, object] = {}
        controller, announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()
        engine.provider.get_tts_message = AsyncMock(side_effect=error)

        with (
            patch(f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)),
            pytest.raises(MusicAssistantError),
        ):
            await controller.play_announcement("player_1", message="dinner is ready")

        engine.provider.get_tts_message.assert_awaited_once()
        announce.assert_not_awaited()

    async def test_an_explicit_engine_is_used(self, mock_mass: MagicMock) -> None:
        """A message names the engine to speak it, overriding the configured default."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()

        with (
            patch(
                f"{self.ANNOUNCE_MODULE}.resolve_tts_engine", AsyncMock(return_value=engine)
            ) as resolve,
            patch(f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock()) as select,
        ):
            await controller.play_announcement(
                "player_1", message="hello", tts_engine="tts_plugin/voice"
            )

        resolve.assert_awaited_once_with(mock_mass, "tts_plugin/voice")
        select.assert_not_awaited()

    async def test_pre_announce_follows_the_player_config(self, mock_mass: MagicMock) -> None:
        """A spoken message uses the player's pre-announce setting without sniffing the url."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda _player_id, key, default=None: (
                True if key == CONF_ENTRY_TTS_PRE_ANNOUNCE.key else default
            )
        )

        with patch(
            f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)
        ):
            await controller.play_announcement("player_1", message="dinner is ready")

        registered = mock_mass.streams.announcement_renderer.register.call_args.args[1]
        assert registered["pre_announce"] is True

    async def test_the_engine_gets_the_shorter_announcement_timeout(
        self, mock_mass: MagicMock
    ) -> None:
        """The engine is capped well below the background default, it holds the player lock."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()
        query = AsyncMock(return_value=SimpleNamespace(path="http://speech/spoken.mp3"))

        with (
            patch(f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)),
            patch(f"{self.ANNOUNCE_MODULE}.query_tts_engine_with_language_fallback", query),
        ):
            await controller.play_announcement("player_1", message="hello")

        assert query.call_args.kwargs["timeout"] == ANNOUNCEMENT_TTS_TIMEOUT
        assert ANNOUNCEMENT_TTS_TIMEOUT < TTS_QUERY_TIMEOUT_SECONDS

    async def test_an_engine_without_a_message_is_rejected(self, mock_mass: MagicMock) -> None:
        """Naming an engine for a url announcement is rejected instead of silently ignored."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)

        with pytest.raises(PlayerCommandFailed, match="only be used to speak a message"):
            await controller.play_announcement(
                "player_1", url="http://test/clip.mp3", tts_engine="tts_plugin/voice"
            )

    async def test_a_language_without_a_message_is_rejected(self, mock_mass: MagicMock) -> None:
        """Naming a language for a url announcement is rejected instead of silently ignored."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)

        with pytest.raises(PlayerCommandFailed, match="A language can only be used"):
            await controller.play_announcement(
                "player_1", url="http://test/clip.mp3", language="nl-NL"
            )

    async def test_a_failing_engine_surfaces_its_error(self, mock_mass: MagicMock) -> None:
        """An engine that fails to speak the message fails the announcement."""
        announcements: dict[str, object] = {}
        controller, announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine()
        engine.provider.get_tts_message = AsyncMock(side_effect=RuntimeError("engine down"))

        with (
            patch(f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)),
            pytest.raises(PlayerCommandFailed),
        ):
            await controller.play_announcement("player_1", message="hello")

        announce.assert_not_awaited()
        mock_mass.streams.announcement_renderer.register.assert_not_called()

    async def test_a_url_or_a_message_is_required(self, mock_mass: MagicMock) -> None:
        """An announcement with neither a url nor a message is rejected."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)

        with pytest.raises(PlayerCommandFailed, match="Either a url or a message"):
            await controller.play_announcement("player_1")

    async def test_a_url_and_a_message_are_mutually_exclusive(self, mock_mass: MagicMock) -> None:
        """An announcement carrying both a url and a message is rejected."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)

        with pytest.raises(PlayerCommandFailed, match="not both"):
            await controller.play_announcement(
                "player_1", url="http://test/clip.mp3", message="hello"
            )

    async def test_unknown_engine_is_rejected(self, mock_mass: MagicMock) -> None:
        """A message naming an engine that does not exist fails instead of using another."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)

        with (
            patch(f"{self.ANNOUNCE_MODULE}.resolve_tts_engine", AsyncMock(return_value=None)),
            pytest.raises(PlayerCommandFailed, match="is not available"),
        ):
            await controller.play_announcement("player_1", message="hello", tts_engine="gone")

    async def test_no_engine_available_is_rejected(self, mock_mass: MagicMock) -> None:
        """A message fails clearly when no TTS engine is set up at all."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)

        with (
            patch(f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=None)),
            pytest.raises(PlayerCommandFailed, match="No text-to-speech engine"),
        ):
            await controller.play_announcement("player_1", message="hello")

    async def test_audio_that_can_not_be_fetched_is_rejected(self, mock_mass: MagicMock) -> None:
        """An engine that only rendered to disk fails, since an announcement is fetched by url."""
        announcements: dict[str, object] = {}
        controller, _announce = self._make_player(mock_mass, announcements)
        engine = self._make_engine(path=str(ANNOUNCE_ALERT_FILE))

        with (
            patch(f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)),
            pytest.raises(PlayerCommandFailed, match="rendered the message to a local file"),
        ):
            await controller.play_announcement("player_1", message="hello")

    async def test_group_members_play_the_rendered_audio(self, mock_mass: MagicMock) -> None:
        """The message is spoken once and every group member announces the resulting audio."""
        announcements: dict[str, object] = {}
        use_real_create_task(mock_mass)
        controller, _announce = self._make_player(mock_mass, announcements)
        self._add_group(mock_mass, controller)
        engine = self._make_engine()

        with (
            patch(f"{self.ANNOUNCE_MODULE}.select_core_tts_engine", AsyncMock(return_value=engine)),
            self._members_coordinate(),
        ):
            await controller.play_announcement("group_1", message="dinner is ready")

        # rendered once for the group, then handed to the member as plain audio
        engine.provider.get_tts_message.assert_awaited_once()
        member_call = next(
            call_args
            for call_args in mock_mass.streams.announcement_renderer.register.call_args_list
            if call_args.args[0] == "player_1"
        )
        assert member_call.args[1]["announcement_url"] == "http://speech/spoken.mp3"

    async def test_members_that_start_together_are_each_handed_the_announcement(
        self, mock_mass: MagicMock
    ) -> None:
        """Members that announce natively and line up their start get the announcement each."""
        announcements: dict[str, object] = {}
        use_real_create_task(mock_mass)
        controller, announce = self._make_player(mock_mass, announcements)
        self._add_group(mock_mass, controller)
        fallback = AsyncMock()
        controller._play_announcement = fallback  # type: ignore[method-assign]

        with self._members_coordinate():
            await controller.play_announcement("group_1", url="http://test/clip.mp3")

        announce.assert_awaited_once()
        fallback.assert_not_awaited()

    async def test_members_that_can_not_start_together_leave_it_to_the_group(
        self, mock_mass: MagicMock
    ) -> None:
        """Members that announce natively but can not line up would be heard out of step."""
        announcements: dict[str, object] = {}
        use_real_create_task(mock_mass)
        controller, announce = self._make_player(mock_mass, announcements)
        group = self._add_group(mock_mass, controller)
        fallback = AsyncMock()
        controller._play_announcement = fallback  # type: ignore[method-assign]

        await controller.play_announcement("group_1", url="http://test/clip.mp3")

        # the group renders the clip through its own (synchronized) stream instead
        assert fallback.await_args is not None
        assert fallback.await_args.args[0] is group
        announce.assert_not_awaited()

    async def test_members_announcing_through_different_providers_leave_it_to_the_group(
        self, mock_mass: MagicMock
    ) -> None:
        """Members that only line up within their own provider are not fanned out together."""
        announcements: dict[str, object] = {}
        use_real_create_task(mock_mass)
        controller, announce = self._make_player(mock_mass, announcements)
        other_provider = MockProvider("other_provider", instance_id="other", mass=mock_mass)
        other_member, other_protocol = _attach(other_provider, "player_2", "Player 2")
        other_protocol._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        other_announce = AsyncMock()
        other_protocol.play_announcement = other_announce  # type: ignore[method-assign]
        _refresh(other_member)
        group = self._add_group(mock_mass, controller)
        _own(group)._attr_group_members = ["player_1", "player_2"]
        _refresh(group)
        fallback = AsyncMock()
        controller._play_announcement = fallback  # type: ignore[method-assign]

        with self._members_coordinate():
            await controller.play_announcement("group_1", url="http://test/clip.mp3")

        # the group renders the clip through its own (synchronized) stream instead
        assert fallback.await_args is not None
        assert fallback.await_args.args[0] is group
        announce.assert_not_awaited()
        other_announce.assert_not_awaited()

    async def test_group_decides_on_the_outputs_that_announce_once_the_audio_is_ready(
        self, mock_mass: MagicMock
    ) -> None:
        """A member that loses its native support while the audio renders falls back as a group."""
        announcements: dict[str, object] = {}
        use_real_create_task(mock_mass)
        controller, player, render = _announcing_player(mock_mass, announcements)
        announce = AsyncMock()
        _own(player).play_announcement = announce  # type: ignore[method-assign]
        group = self._add_group(mock_mass, controller)
        fallback = AsyncMock()
        controller._play_announcement = fallback  # type: ignore[method-assign]

        async def _stop_announcing_natively() -> bool:
            _own(player)._attr_supported_features.discard(PlayerFeature.PLAY_ANNOUNCEMENT)
            _refresh(player)
            return True

        render.wait_ready = AsyncMock(side_effect=_stop_announcing_natively)

        with self._members_coordinate():
            await controller.play_announcement("group_1", url="http://test/clip.mp3")

        assert controller._resolve_announce_player(player) is None
        # the group renders the clip through its own (synchronized) stream instead
        assert fallback.await_args is not None
        assert fallback.await_args.args[0] is group
        announce.assert_not_awaited()


class TestNativeAnnouncementRouting:
    """Announcement routing respects the player's own support and its active output."""

    def _make_player_with_audio_protocol(
        self,
        mock_mass: MagicMock,
        playback_state: PlaybackState,
        *,
        own_support: bool = False,
        active_protocol: str | None = None,
    ) -> tuple[PlayerController, Player, MockProtocol, AsyncMock, AsyncMock]:
        """Create a controller, a player, its audio protocol and the two path mocks."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_playback_state = playback_state
        if own_support:
            protocol._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        audio_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        audio = _attach_audio(audio_provider, player, "audio_1", "AirPlay")
        audio._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if active_protocol is not None:
            player.set_active_output_protocol(active_protocol)
        render = MagicMock()
        render.wait_ready = AsyncMock(return_value=True)
        renderer = mock_mass.streams.announcement_renderer
        renderer.register = MagicMock(return_value=render)
        renderer.unregister = AsyncMock()
        mock_mass.streams.get_announcement_url = MagicMock(
            side_effect=lambda player_id, **_kwargs: f"http://ma/announcement/{player_id}.mp3"
        )
        _refresh(player)
        native_path = AsyncMock()
        generic_path = AsyncMock()
        controller._play_native_announcement = native_path  # type: ignore[method-assign]
        controller._play_announcement = generic_path  # type: ignore[method-assign]
        return controller, player, audio, native_path, generic_path

    def _add_group(self, mock_mass: MagicMock) -> Player:
        """Register a group player holding the player as its only member."""
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        group, group_protocol = _attach(
            provider, "group_1", "Group 1", player_type=PlayerType.GROUP
        )
        group_protocol._attr_group_members = ["player_1"]
        _refresh(group)
        return group

    async def test_playing_player_does_not_route_to_an_idle_audio_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A player rendering through one output must not announce through another.

        E.g. a WiiM playing natively with an idle AirPlay protocol: routing the
        announcement to that protocol would seize the device from the native
        output, with nothing restoring that playback afterwards.
        """
        controller, _player, _audio, native_path, generic_path = (
            self._make_player_with_audio_protocol(
                mock_mass, PlaybackState.PLAYING, active_protocol="native"
            )
        )

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native_path.assert_not_awaited()
        generic_path.assert_awaited_once()

    async def test_idle_player_routes_to_its_audio_protocol(self, mock_mass: MagicMock) -> None:
        """An idle player announces natively through any capable audio protocol."""
        controller, _player, audio, native_path, _generic_path = (
            self._make_player_with_audio_protocol(mock_mass, PlaybackState.IDLE)
        )

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native_path.assert_awaited_once()
        assert native_path.call_args.args[1] is audio

    async def test_own_native_support_beats_the_active_audio_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """
        The player's own announcement handler wins over the output rendering the audio.

        E.g. a Sonos playing through its AirPlay protocol announces with audioClip,
        which overlays the clip on that stream.
        """
        controller, player, _audio, native_path, _generic_path = (
            self._make_player_with_audio_protocol(
                mock_mass,
                PlaybackState.PLAYING,
                own_support=True,
                active_protocol="audio_1",
            )
        )

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native_path.assert_awaited_once()
        assert native_path.call_args.args[1] is _own(player)

    async def test_active_audio_protocol_announces_without_own_support(
        self, mock_mass: MagicMock
    ) -> None:
        """A player that cannot announce itself announces through its rendering output."""
        controller, _player, audio, native_path, _generic_path = (
            self._make_player_with_audio_protocol(
                mock_mass,
                PlaybackState.PLAYING,
                active_protocol="audio_1",
            )
        )

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native_path.assert_awaited_once()
        assert native_path.call_args.args[1] is audio

    async def test_own_native_support_wins_when_playing_natively(
        self, mock_mass: MagicMock
    ) -> None:
        """A player rendering through its own native output announces natively."""
        controller, player, _audio, native_path, _generic_path = (
            self._make_player_with_audio_protocol(
                mock_mass,
                PlaybackState.PLAYING,
                own_support=True,
                active_protocol="native",
            )
        )

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native_path.assert_awaited_once()
        assert native_path.call_args.args[1] is _own(player)

    async def test_idle_player_prefers_its_own_support_over_its_audio_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """Without active playback the player's own announcement support wins."""
        controller, player, _audio, native_path, _generic_path = (
            self._make_player_with_audio_protocol(mock_mass, PlaybackState.IDLE, own_support=True)
        )

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        native_path.assert_awaited_once()
        assert native_path.call_args.args[1] is _own(player)

    async def test_group_member_announcing_through_its_output_is_handed_the_announcement(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A member announcing through its rendering output is fanned out.

        The output that would announce for the member lines up the start, so the group
        hands the clip to its members instead of playing it through its own stream.
        """
        use_real_create_task(mock_mass)
        controller, _player, audio, native_path, generic_path = (
            self._make_player_with_audio_protocol(
                mock_mass,
                PlaybackState.PLAYING,
                active_protocol="audio_1",
            )
        )
        self._add_group(mock_mass)

        # only the audio protocol lines up its start, the member's own protocol does not
        with patch.object(
            MockProtocol,
            "announcement_features",
            property(
                lambda self: (
                    {
                        AnnouncementFeature.SUPPORTS_VOLUME,
                        AnnouncementFeature.COORDINATES_START,
                    }
                    if self.protocol_id == "audio_1"
                    else {AnnouncementFeature.SUPPORTS_VOLUME}
                )
            ),
        ):
            await controller.play_announcement("group_1", "http://test/announcement.mp3")

        native_path.assert_awaited_once()
        assert native_path.call_args.args[1] is audio
        generic_path.assert_not_awaited()

    async def test_group_plays_itself_when_a_member_would_drop_the_wanted_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A member whose native route ignores the announcement volume is not fanned out.

        Such a member would announce through the default implementation, which frees it
        from the group - and that needs the group's lock, held by the announcement that
        fans out. So the group plays the clip through its own stream instead.
        """
        use_real_create_task(mock_mass)
        controller, player, _audio, native_path, generic_path = (
            self._make_player_with_audio_protocol(mock_mass, PlaybackState.IDLE, own_support=True)
        )
        _own(player)._attr_volume_level = 40
        _refresh(player)
        controller.get_announcement_volume = MagicMock(return_value=60)  # type: ignore[method-assign]
        group = self._add_group(mock_mass)

        # the member lines up its start but drops a wanted volume
        with patch.object(
            MockProtocol,
            "announcement_features",
            new_callable=PropertyMock,
            return_value={AnnouncementFeature.COORDINATES_START},
        ):
            await controller.play_announcement("group_1", "http://test/announcement.mp3")

        native_path.assert_not_awaited()
        generic_path.assert_awaited_once()
        assert generic_path.call_args.args[0] is group

    async def test_group_member_that_lost_its_native_route_is_skipped(
        self, mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A fanned-out member that no longer announces natively is left out, not handed the default.

        The default implementation would free the member from its group, which needs the
        group's lock that the announcement fanning out holds for its whole duration.
        """
        use_real_create_task(mock_mass)
        controller, player, _audio, native_path, generic_path = (
            self._make_player_with_audio_protocol(mock_mass, PlaybackState.IDLE, own_support=True)
        )
        self._add_group(mock_mass)
        ready_waits: list[None] = []

        async def _wait_ready() -> bool:
            # the group waits for the audio before it decides on the members; the member
            # waits again under its own lock, by which time its output stopped announcing
            ready_waits.append(None)
            if len(ready_waits) > 1:
                _own(player)._attr_supported_features.discard(PlayerFeature.PLAY_ANNOUNCEMENT)
            return True

        renderer = mock_mass.streams.announcement_renderer
        renderer.register.return_value.wait_ready = AsyncMock(side_effect=_wait_ready)

        with patch.object(
            MockProtocol,
            "announcement_features",
            new_callable=PropertyMock,
            return_value={
                AnnouncementFeature.SUPPORTS_VOLUME,
                AnnouncementFeature.COORDINATES_START,
            },
        ):
            await controller.play_announcement("group_1", "http://test/announcement.mp3")

        native_path.assert_not_awaited()
        generic_path.assert_not_awaited()
        assert "no longer announces natively" in caplog.text


@pytest.mark.usefixtures("running_background_tasks")
class TestPlayAnnouncementRestore:
    """Test the state restore of the default (fallback) announcement implementation."""

    def _make_player(
        self, mock_mass: MagicMock, prev_media: PlayerMedia
    ) -> tuple[PlayerController, Player, AsyncMock]:
        """Create a controller and a playing player, returning the patched resume handler."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_playback_state = PlaybackState.PLAYING
        protocol._attr_current_media = prev_media
        mock_mass.player_queues.get = MagicMock(return_value=None)
        resume_mock = AsyncMock()
        controller._handle_cmd_resume = resume_mock  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        controller._wait_for_playback_state = AsyncMock()  # type: ignore[method-assign]
        controller.get_announcement_volume = MagicMock(return_value=None)  # type: ignore[method-assign]
        _refresh(player)
        return controller, player, resume_mock

    @staticmethod
    def _add_group(player: Player, *, supports_set_members: bool) -> tuple[Player, AsyncMock]:
        """Register a powered group player that holds the given player as its member."""
        group, group_protocol = _attach(
            cast("MockProvider", _own(player).provider),
            "group_1",
            "Group 1",
            player_type=PlayerType.GROUP,
        )
        group_protocol._attr_powered = True
        # a group names its members by player id
        group_protocol._attr_group_members = [player.player_id]
        # a group offers its own members as candidates to (re)join it
        group_protocol._attr_can_group_with = {player.player_id}
        if supports_set_members:
            group_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        _refresh(group)
        # the member has no own input change to trigger a recalculation of its
        # (group derived) state, so force it here - just like an attach does
        _refresh(player)
        assert player.active_group == group.player_id
        real_set_members = group_protocol.set_members

        async def _set_members(**kwargs: list[str]) -> None:
            # let the membership really change, so the player is ungrouped while the
            # announcement plays - just like it is in production. neither player picks
            # the new membership up on its own here, so publish it on both.
            await real_set_members(**kwargs)
            _refresh(group, player)

        set_members = AsyncMock(side_effect=_set_members)
        group_protocol.set_members = set_members  # type: ignore[method-assign]
        return group, set_members

    async def test_previous_playback_is_restored(self, mock_mass: MagicMock) -> None:
        """Content that was playing before the announcement is resumed afterwards."""
        controller, player, resume_mock = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )

        await controller._play_announcement(player, _announcement())

        resume_mock.assert_awaited_once()

    async def test_active_queue_is_stopped_to_park_resume_position(
        self, mock_mass: MagicMock
    ) -> None:
        """Active MA queue is stopped via the queue controller, not the device alone."""
        controller, player, _resume_mock = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        active_queue = MagicMock()
        active_queue.queue_id = "player_1"
        controller.get_active_queue = MagicMock(return_value=active_queue)  # type: ignore[method-assign]
        queue_stop = AsyncMock()
        mock_mass.player_queues._handle_stop = queue_stop
        device_stop = cast("AsyncMock", controller._handle_cmd_stop)

        await controller._play_announcement(player, _announcement())

        queue_stop.assert_awaited_once_with("player_1")
        device_stop.assert_not_awaited()

    async def test_previous_announcement_is_not_restored(self, mock_mass: MagicMock) -> None:
        """A player still busy with an earlier announcement has no playback to restore."""
        controller, player, resume_mock = self._make_player(
            mock_mass,
            PlayerMedia(uri="http://ma/announcement/x.mp3", media_type=MediaType.ANNOUNCEMENT),
        )

        await controller._play_announcement(player, _announcement())

        resume_mock.assert_not_awaited()

    async def test_volume_is_restored_when_playback_fails(self, mock_mass: MagicMock) -> None:
        """A failing announcement never leaves the player at the raised volume."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        _own(player)._attr_volume_level = 20
        _refresh(player)
        controller.get_announcement_volume = MagicMock(return_value=80)  # type: ignore[method-assign]
        volume_mock = AsyncMock()
        controller._handle_cmd_volume_set = volume_mock  # type: ignore[method-assign]
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed):
            await controller._play_announcement(player, _announcement())

        assert volume_mock.call_args_list == [call("player_1", 80), call("player_1", 20)]

    async def test_zero_announcement_volume_is_applied_and_restored(
        self, mock_mass: MagicMock
    ) -> None:
        """An announcement volume of 0 is a real volume, not an 'unset' fallback."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        _own(player)._attr_volume_level = 20
        _refresh(player)
        controller.get_announcement_volume = MagicMock(return_value=0)  # type: ignore[method-assign]
        volume_mock = AsyncMock()
        controller._handle_cmd_volume_set = volume_mock  # type: ignore[method-assign]

        await controller._play_announcement(player, _announcement())

        assert volume_mock.call_args_list == [call("player_1", 0), call("player_1", 20)]

    async def test_playback_is_restored_when_duration_is_unknown(
        self, mock_mass: MagicMock
    ) -> None:
        """An announcement of unknown length still hands the player back to its content."""
        controller, player, resume_mock = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        announcement = _announcement()
        announcement.duration = None

        # an unknown length waits for the player to report it finished instead of failing
        await controller._play_announcement(player, announcement)

        resume_mock.assert_awaited_once()

    async def test_group_membership_is_restored_when_playback_fails(
        self, mock_mass: MagicMock
    ) -> None:
        """A failing announcement never leaves the player out of its group player."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        group, set_members = self._add_group(player, supports_set_members=True)
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed):
            await controller._play_announcement(player, _announcement())

        # the member changes reach the group through the controller's set_members handler
        assert set_members.await_args_list == [
            call(player_ids_to_add=[], player_ids_to_remove=["player_1"]),
            call(player_ids_to_add=["player_1"], player_ids_to_remove=[]),
        ]
        assert player.active_group == group.player_id

    async def test_restore_failure_does_not_mask_the_announcement_error(
        self, mock_mass: MagicMock
    ) -> None:
        """A provider blowing up during the restore must not hide why the announcement failed."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        group, _set_members = self._add_group(player, supports_set_members=True)
        # set_members is a raw provider call: whatever its client library raises comes
        # through unwrapped, so the ungroup succeeds and the regroup times out
        _own(group).set_members = AsyncMock(  # type: ignore[method-assign]
            side_effect=[None, TimeoutError("provider timeout")]
        )
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed, match="player went away"):
            await controller._play_announcement(player, _announcement())

    async def test_group_without_set_members_is_the_one_powered_off(
        self, mock_mass: MagicMock
    ) -> None:
        """A group that can not release members is powered off, not the announcement target."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        self._add_group(player, supports_set_members=False)
        power_mock = AsyncMock()
        controller._handle_cmd_power = power_mock  # type: ignore[method-assign]
        play_mock = AsyncMock()
        controller.cmd_play = play_mock  # type: ignore[method-assign]

        await controller._play_announcement(player, _announcement())

        # the group is switched off for the announcement and restarted afterwards
        power_mock.assert_awaited_once_with("group_1", False)
        play_mock.assert_awaited_once_with("group_1")

    async def test_idle_player_without_power_control_is_regrouped(
        self, mock_mass: MagicMock
    ) -> None:
        """An idle player that has no power state to restore is still put back in its group."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        protocol = _own(player)
        protocol._attr_playback_state = PlaybackState.IDLE
        protocol._attr_powered = None
        group, set_members = self._add_group(player, supports_set_members=True)
        assert player.power_control == PLAYER_CONTROL_NONE

        await controller._play_announcement(player, _announcement())

        assert set_members.await_args_list == [
            call(player_ids_to_add=[], player_ids_to_remove=["player_1"]),
            call(player_ids_to_add=["player_1"], player_ids_to_remove=[]),
        ]
        assert player.active_group == group.player_id

    async def test_muted_player_is_unmuted_and_muted_back(self, mock_mass: MagicMock) -> None:
        """A muted player hears the announcement and is muted again afterwards."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = _mute_natively(player)

        await controller._play_announcement(player, _announcement())

        assert mute_mock.await_args_list == [call(False), call(True)]

    async def test_player_without_volume_control_is_still_unmuted(
        self, mock_mass: MagicMock
    ) -> None:
        """A player that can only be muted is unmuted for the announcement all the same."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_VOLUME_CONTROL: PLAYER_CONTROL_NONE})
        )
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = _mute_natively(player)
        assert player.volume_control == PLAYER_CONTROL_NONE

        await controller._play_announcement(player, _announcement())

        assert mute_mock.await_args_list == [call(False), call(True)]

    async def test_mute_is_restored_before_the_player_is_regrouped(
        self, mock_mass: MagicMock
    ) -> None:
        """A player is handed back to its group already muted, holding on to its mute lock."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        _group, set_members = self._add_group(player, supports_set_members=True)
        player.extra_data[ATTR_MUTE_LOCK] = True
        recorder = MagicMock()
        recorder.attach_mock(_mute_natively(player), "mute")
        recorder.attach_mock(set_members, "set_members")

        await controller._play_announcement(player, _announcement())

        assert recorder.mock_calls == [
            call.set_members(player_ids_to_add=[], player_ids_to_remove=["player_1"]),
            call.mute(False),
            call.mute(True),
            call.set_members(player_ids_to_add=["player_1"], player_ids_to_remove=[]),
        ]
        # the lock survives the announcement, so the regroup does not unmute the player
        assert player.extra_data[ATTR_MUTE_LOCK] is True

    async def test_unmuted_player_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that was not muted is never sent a mute command."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = _mute_natively(player)
        _own(player)._attr_volume_muted = False
        _refresh(player)
        mute_mock.reset_mock()

        await controller._play_announcement(player, _announcement())

        mute_mock.assert_not_awaited()

    async def test_mute_is_restored_when_playback_fails(self, mock_mass: MagicMock) -> None:
        """A failing announcement never leaves the player unmuted."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = _mute_natively(player)
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed):
            await controller._play_announcement(player, _announcement())

        assert mute_mock.await_args_list == [call(False), call(True)]

    async def test_muted_sync_group_members_all_hear_the_announcement(
        self, mock_mass: MagicMock
    ) -> None:
        """Every member of a muted sync group is unmuted, keeping its mute lock."""
        controller, leader, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        member, _ = _attach(cast("MockProvider", _own(leader).provider), "player_2", "Player 2")
        _own(leader)._attr_group_members = [_proto("player_1"), _proto("player_2")]
        _refresh(leader, member)
        assert leader.group_members == ["player_1", "player_2"]
        mute_mocks = {player.player_id: _mute_natively(player) for player in (leader, member)}
        # both members were muted while grouped, so both hold a mute lock
        for player in (leader, member):
            player.extra_data[ATTR_MUTE_LOCK] = True

        await controller._play_announcement(leader, _announcement())

        for player_id, mute_mock in mute_mocks.items():
            assert mute_mock.await_args_list == [call(False), call(True)], player_id
            assert controller._players[player_id].extra_data[ATTR_MUTE_LOCK] is True

    async def test_fake_muted_player_announces_at_its_real_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """A fake muted player announces at its real volume, not at the zero it is parked on."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})
        )
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        protocol = _own(player)

        def _apply_volume(volume: int) -> None:
            protocol._attr_volume_level = volume
            player.update_state(signal_event=False)

        protocol._attr_volume_level = 40
        volume_set = AsyncMock(side_effect=_apply_volume)
        protocol.volume_set = volume_set  # type: ignore[method-assign]
        _refresh(player)
        await controller.cmd_volume_mute("player_1", True)
        assert player.volume_muted is True
        controller.get_announcement_volume = MagicMock(return_value=80)  # type: ignore[method-assign]

        await controller._play_announcement(player, _announcement())

        # unmute to 40, announce at 80, restore 40 and park back on 0 for the fake mute
        assert volume_set.await_args_list == [
            call(0),
            call(40),
            call(80),
            call(40),
            call(0),
        ]
        assert player.volume_muted is True
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] == 40


@pytest.mark.usefixtures("running_background_tasks")
class TestPlayNativeAnnouncement:
    """Test the mute handling around an announcement that a player plays natively."""

    def _make_player(self, mock_mass: MagicMock) -> tuple[PlayerController, Player, AsyncMock]:
        """Create a controller and a player with native announcement support."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        mock_mass.player_queues.get = MagicMock(return_value=None)
        controller.get_announcement_volume = MagicMock(return_value=None)  # type: ignore[method-assign]
        announce_mock = AsyncMock()
        protocol.play_announcement = announce_mock  # type: ignore[method-assign]
        _refresh(player)
        return controller, player, announce_mock

    async def test_muted_player_is_unmuted_and_muted_back(self, mock_mass: MagicMock) -> None:
        """A muted player hears the announcement and is muted again afterwards."""
        controller, player, announce_mock = self._make_player(mock_mass)
        recorder = MagicMock()
        recorder.attach_mock(_mute_natively(player), "mute")
        recorder.attach_mock(announce_mock, "announce")

        await controller._play_native_announcement(player, _own(player), _announcement(), None)

        assert recorder.mock_calls == [
            call.mute(False),
            call.announce(ANY, None),
            call.mute(True),
        ]

    async def test_unmuted_player_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that was not muted is never sent a mute command."""
        controller, player, _ = self._make_player(mock_mass)
        mute_mock = _mute_natively(player)
        _own(player)._attr_volume_muted = False
        _refresh(player)
        mute_mock.reset_mock()

        await controller._play_native_announcement(player, _own(player), _announcement(), None)

        mute_mock.assert_not_awaited()

    async def test_mute_is_restored_when_the_provider_fails(self, mock_mass: MagicMock) -> None:
        """A failing announcement never leaves the player unmuted."""
        controller, player, announce_mock = self._make_player(mock_mass)
        mute_mock = _mute_natively(player)
        announce_mock.side_effect = PlayerCommandFailed("player went away")

        with pytest.raises(PlayerCommandFailed):
            await controller._play_native_announcement(player, _own(player), _announcement(), None)

        assert mute_mock.await_args_list == [call(False), call(True)]

    async def test_muted_sync_group_members_all_hear_the_announcement(
        self, mock_mass: MagicMock
    ) -> None:
        """Every member of a muted sync group is unmuted, keeping its mute lock."""
        controller, leader, _ = self._make_player(mock_mass)
        member, _ = _attach(cast("MockProvider", _own(leader).provider), "player_2", "Player 2")
        _own(leader)._attr_group_members = [_proto("player_1"), _proto("player_2")]
        _refresh(leader, member)
        assert leader.group_members == ["player_1", "player_2"]
        mute_mocks = {player.player_id: _mute_natively(player) for player in (leader, member)}
        # both members were muted while grouped, so both hold a mute lock
        for player in (leader, member):
            player.extra_data[ATTR_MUTE_LOCK] = True

        await controller._play_native_announcement(leader, _own(leader), _announcement(), None)

        for player_id, mute_mock in mute_mocks.items():
            assert mute_mock.await_args_list == [call(False), call(True)], player_id
            assert controller._players[player_id].extra_data[ATTR_MUTE_LOCK] is True

    async def test_fake_muted_player_announces_at_its_real_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """The announcement volume is resolved after the unmute, not from the parked zero."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})
        )
        controller, player, announce_mock = self._make_player(mock_mass)
        protocol = _own(player)

        def _apply_volume(volume: int) -> None:
            protocol._attr_volume_level = volume
            player.update_state(signal_event=False)

        protocol._attr_volume_level = 40
        volume_set = AsyncMock(side_effect=_apply_volume)
        protocol.volume_set = volume_set  # type: ignore[method-assign]
        _refresh(player)
        await controller.cmd_volume_mute("player_1", True)
        assert player.volume_muted is True
        # stand in for the configured strategy, which reads the volume of the player
        controller.get_announcement_volume = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda _player_id, _volume_level: player.volume_level
        )

        await controller._play_native_announcement(player, protocol, _announcement(), None)

        assert announce_mock.await_args == call(ANY, 40)
        # unmute to 40 for the announcement, park back on 0 for the fake mute
        assert volume_set.await_args_list == [call(0), call(40), call(0)]
        assert player.volume_muted is True
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] == 40
