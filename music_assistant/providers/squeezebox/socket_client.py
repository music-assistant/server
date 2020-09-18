"""Socketclient implementation for Squeezebox emulated player provider."""

import asyncio
import logging
import re
import struct
import time
from enum import Enum
from typing import Awaitable

from music_assistant.models.player import PlayerState
from music_assistant.utils import callback, run_periodic

from .constants import PROV_ID

LOGGER = logging.getLogger(PROV_ID)

# from http://wiki.slimdevices.com/index.php/SlimProtoTCPProtocol#HELO
DEVICE_TYPE = {
    2: "squeezebox",
    3: "softsqueeze",
    4: "squeezebox2",
    5: "transporter",
    6: "softsqueeze3",
    7: "receiver",
    8: "squeezeslave",
    9: "controller",
    10: "boom",
    11: "softboom",
    12: "squeezeplay",
}


class Event(Enum):
    """Enum with the various events the socket client fires."""

    EVENT_CONNECTED = "connected"
    EVENT_DISCONNECTED = "disconnected"
    EVENT_UPDATED = "updated"
    EVENT_DECODER_READY = "decoder_ready"


class SqueezeSocketClient:
    """Squeezebox socket client."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        event_callback: Awaitable = None,
    ):
        """Initialize the socket client."""
        self._reader = reader
        self._writer = writer
        self._event_callback = event_callback
        self._player_id = ""
        self._device_type = ""
        self._device_name = ""
        self._last_volume = 0
        self._last_heartbeat = 0
        self._cur_time_milliseconds = 0
        self._volume_control = PySqueezeVolume()
        self._volume_level = 0
        self._powered = False
        self._muted = False
        self._state = PlayerState.Stopped
        self._elapsed_time = 0
        self._current_uri = ""
        self._tasks = [
            asyncio.create_task(self.__async_socket_reader()),
            asyncio.create_task(self.__async_send_heartbeat()),
        ]

    def close(self):
        """Cleanup when the socket client needs to closed."""
        for task in self._tasks:
            if not task.cancelled():
                task.cancel()
        asyncio.create_task(self._event_callback(Event.EVENT_DISCONNECTED, self))

    @property
    def player_id(self) -> str:
        """Return player id (=mac address) of the player."""
        return self._player_id

    @property
    def device_type(self) -> str:
        """Return device type of the player."""
        return self._device_type

    @property
    def device_address(self) -> str:
        """Return device IP address of the player."""
        dev_address = self._writer.get_extra_info("peername")
        return dev_address[0] if dev_address else ""

    @property
    def name(self) -> str:
        """Return name of the player."""
        if self._device_name:
            return self._device_name
        return f"{self.device_type}: {self.player_id}"

    @property
    def volume_level(self):
        """Return current volume level of player."""
        return self._volume_level

    @property
    def powered(self):
        """Return current power state of player."""
        return self._powered

    @property
    def muted(self):
        """Return current mute state of player."""
        return self._muted

    @property
    def state(self):
        """Return current state of player."""
        return self._state

    @property
    def elapsed_time(self):
        """Return elapsed_time of current playing track."""
        return self._elapsed_time

    @property
    def current_uri(self):
        """Return uri of currently loaded track."""
        return self._current_uri

    async def __async_initialize_player(self):
        """Set some startup settings for the player."""
        # send version
        await self.__async_send_frame(b"vers", b"7.8")
        await self.__async_send_frame(b"setd", struct.pack("B", 0))
        await self.__async_send_frame(b"setd", struct.pack("B", 4))

    async def async_cmd_stop(self):
        """Send stop command to player."""
        data = self.__pack_stream(b"q", autostart=b"0", flags=0)
        await self.__async_send_frame(b"strm", data)

    async def async_cmd_play(self):
        """Send play (unpause) command to player."""
        data = self.__pack_stream(b"u", autostart=b"0", flags=0)
        await self.__async_send_frame(b"strm", data)

    async def async_cmd_pause(self):
        """Send pause command to player."""
        data = self.__pack_stream(b"p", autostart=b"0", flags=0)
        await self.__async_send_frame(b"strm", data)

    async def async_cmd_power(self, powered: bool = True):
        """Send power command to player."""
        # power is not supported so abuse mute instead
        power_int = 1 if powered else 0
        await self.__async_send_frame(b"aude", struct.pack("2B", power_int, 1))
        self._powered = powered
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    async def async_cmd_volume_set(self, volume_level: int):
        """Send new volume level command to player."""
        self._volume_control.volume = volume_level
        old_gain = self._volume_control.old_gain()
        new_gain = self._volume_control.new_gain()
        await self.__async_send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self._volume_level = volume_level

    async def async_cmd_mute(self, muted: bool = False):
        """Send mute command to player."""
        muted_int = 0 if muted else 1
        await self.__async_send_frame(b"aude", struct.pack("2B", muted_int, 0))
        self.muted = muted

    async def async_cmd_play_uri(
        self, uri: str, send_flush: bool = True, crossfade_duration: int = 0
    ):
        """Request player to start playing a single uri."""
        if send_flush:
            data = self.__pack_stream(b"f", autostart=b"0", flags=0)
            await self.__async_send_frame(b"strm", data)
        self._current_uri = uri
        self._powered = True
        enable_crossfade = crossfade_duration > 0
        command = b"s"
        # we use direct stream for now so let the player do the messy work with buffers
        autostart = b"3"
        trans_type = b"1" if enable_crossfade else b"0"
        formatbyte = b"f"  # fixed to flac
        uri = "/stream" + uri.split("/stream")[1]
        data = self.__pack_stream(
            command,
            autostart=autostart,
            flags=0x00,
            formatbyte=formatbyte,
            trans_type=trans_type,
            trans_duration=crossfade_duration,
        )
        # extract host and port from uri
        regex = "(?:http.*://)?(?P<host>[^:/ ]+).?(?P<port>[0-9]*).*"
        regex_result = re.search(regex, uri)
        host = regex_result.group("host")  # 'www.abc.com'
        port = regex_result.group("port")  # '123'
        if not port and uri.startswith("https"):
            port = 443
        elif not port:
            port = 80
        headers = f"Connection: close\r\nAccept: */*\r\nHost: {host}:{port}\r\n"
        request = "GET %s HTTP/1.0\r\n%s\r\n" % (uri, headers)
        data = data + request.encode("utf-8")
        await self.__async_send_frame(b"strm", data)

    @run_periodic(5)
    async def __async_send_heartbeat(self):
        """Send periodic heartbeat message to player."""
        timestamp = int(time.time())
        data = self.__pack_stream(b"t", replay_gain=timestamp, flags=0)
        await self.__async_send_frame(b"strm", data)

    async def __async_send_frame(self, command, data):
        """Send command to Squeeze player."""
        if self._reader.at_eof() or self._writer.is_closing():
            LOGGER.debug("Socket is disconnected.")
            return self.close()
        packet = struct.pack("!H", len(data) + 4) + command + data
        self._writer.write(packet)
        await self._writer.drain()

    async def __async_socket_reader(self):
        """Handle incoming data from socket."""
        buffer = b""
        # keep reading bytes from the socket
        while not (self._reader.at_eof() or self._writer.is_closing()):
            data = await self._reader.read(64)
            # handle incoming data from socket
            buffer = buffer + data
            del data
            if len(buffer) > 8:
                # construct operation and
                operation, length = buffer[:4], buffer[4:8]
                plen = struct.unpack("!I", length)[0] + 8
                if len(buffer) >= plen:
                    packet, buffer = buffer[8:plen], buffer[plen:]
                    operation = operation.strip(b"!").strip().decode().lower()
                    handler = getattr(self, f"_process_{operation}", None)
                    if handler is None:
                        LOGGER.warning("No handler for %s", operation)
                    else:
                        handler(packet)
        # EOF reached: socket is disconnected
        LOGGER.info("Socket disconnected: %s", self._writer.get_extra_info("peername"))
        self.close()

    @callback
    @staticmethod
    def __pack_stream(
        command,
        autostart=b"1",
        formatbyte=b"o",
        pcmargs=(b"?", b"?", b"?", b"?"),
        threshold=200,
        spdif=b"0",
        trans_duration=0,
        trans_type=b"0",
        flags=0x40,
        output_threshold=0,
        replay_gain=0,
        server_port=8095,
        server_ip=0,
    ):
        """Create stream request message based on given arguments."""
        return struct.pack(
            "!cccccccBcBcBBBLHL",
            command,
            autostart,
            formatbyte,
            *pcmargs,
            threshold,
            spdif,
            trans_duration,
            trans_type,
            flags,
            output_threshold,
            0,
            replay_gain,
            server_port,
            server_ip,
        )

    @callback
    def _process_helo(self, data):
        """Process incoming HELO event from player (player connected)."""
        # pylint: disable=unused-variable
        # player connected
        (dev_id, rev, mac) = struct.unpack("BB6s", data[:8])
        device_mac = ":".join("%02x" % x for x in mac)
        self._player_id = str(device_mac).lower()
        self._device_type = DEVICE_TYPE.get(dev_id, "unknown device")
        LOGGER.info("Player connected: %s", self.name)
        asyncio.create_task(self.__async_initialize_player())
        asyncio.create_task(self._event_callback(Event.EVENT_CONNECTED, self))

    @callback
    def _process_stat(self, data):
        """Redirect incoming STAT event from player to correct method."""
        event = data[:4].decode()
        event_data = data[4:]
        if event == b"\x00\x00\x00\x00":
            # Presumed informational stat message
            return
        event_handler = getattr(self, "_process_stat_%s" % event.lower(), None)
        if event_handler is None:
            LOGGER.debug("Unhandled event: %s - event_data: %s", event, event_data)
        else:
            event_handler(data[4:])

    @callback
    def _process_stat_aude(self, data):
        """Process incoming stat AUDe message (power level and mute)."""
        (spdif_enable, dac_enable) = struct.unpack("2B", data[:4])
        powered = spdif_enable or dac_enable
        self._powered = powered
        self._muted = not powered
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    def _process_stat_audg(self, data):
        """Process incoming stat AUDg message (volume level)."""
        # TODO: process volume level
        LOGGER.debug("AUDg received - Volume level: %s", data)
        self._volume_level = self._volume_control.volume
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    def _process_stat_stmd(self, data):
        """Process incoming stat STMd message (decoder ready)."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMu received - Decoder Ready for next track.")
        asyncio.create_task(self._event_callback(Event.EVENT_DECODER_READY, self))

    @callback
    def _process_stat_stmf(self, data):
        """Process incoming stat STMf message (connection closed)."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMf received - connection closed.")
        self._state = PlayerState.Stopped
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    @classmethod
    def _process_stat_stmo(cls, data):
        """
        Process incoming stat STMo message.

        No more decoded (uncompressed) data to play; triggers rebuffering.
        """
        # pylint: disable=unused-argument
        LOGGER.debug("STMo received - output underrun.")
        LOGGER.debug("Output Underrun")

    @callback
    def _process_stat_stmp(self, data):
        """Process incoming stat STMp message: Pause confirmed."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMp received - pause confirmed.")
        self._state = PlayerState.Paused
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    def _process_stat_stmr(self, data):
        """Process incoming stat STMr message: Resume confirmed."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMr received - resume confirmed.")
        self._state = PlayerState.Playing
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    def _process_stat_stms(self, data):
        # pylint: disable=unused-argument
        """Process incoming stat STMs message: Playback of new track has started."""
        LOGGER.debug("STMs received - playback of new track has started.")
        self._state = PlayerState.Playing
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    def _process_stat_stmt(self, data):
        """Process incoming stat STMt message: heartbeat from client."""
        # pylint: disable=unused-variable
        timestamp = time.time()
        self._last_heartbeat = timestamp
        (
            num_crlf,
            mas_initialized,
            mas_mode,
            rptr,
            wptr,
            bytes_received_h,
            bytes_received_l,
            signal_strength,
            jiffies,
            output_buffer_size,
            output_buffer_fullness,
            elapsed_seconds,
            voltage,
            elapsed_milliseconds,
            server_timestamp,
            error_code,
        ) = struct.unpack("!BBBLLLLHLLLLHLLH", data)
        if self._state == PlayerState.Playing and elapsed_seconds != self._elapsed_time:
            self._elapsed_time = elapsed_seconds
            asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    def _process_stat_stmu(self, data):
        """Process incoming stat STMu message: Buffer underrun: Normal end of playback."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMu received - end of playback.")
        self.state = PlayerState.Stopped
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))

    @callback
    def _process_resp(self, data):
        """Process incoming RESP message: Response received at player."""
        # pylint: disable=unused-argument
        # send continue
        asyncio.create_task(self.__async_send_frame(b"cont", b"0"))

    @callback
    def _process_setd(self, data):
        """Process incoming SETD message: Get/set player firmware settings."""
        cmd_id = data[0]
        if cmd_id == 0:
            # received player name
            data = data[1:].decode()
            self._device_name = data
        asyncio.create_task(self._event_callback(Event.EVENT_UPDATED, self))


class PySqueezeVolume:
    """Represents a sound volume. This is an awful lot more complex than it sounds."""

    minimum = 0
    maximum = 100
    step = 1

    # this map is taken from Slim::Player::Squeezebox2 in the squeezecenter source
    # i don't know how much magic it contains, or any way I can test it
    old_map = [
        0,
        1,
        1,
        1,
        2,
        2,
        2,
        3,
        3,
        4,
        5,
        5,
        6,
        6,
        7,
        8,
        9,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        16,
        17,
        18,
        19,
        20,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        32,
        33,
        34,
        35,
        37,
        38,
        39,
        40,
        42,
        43,
        44,
        46,
        47,
        48,
        50,
        51,
        53,
        54,
        56,
        57,
        59,
        60,
        61,
        63,
        65,
        66,
        68,
        69,
        71,
        72,
        74,
        75,
        77,
        79,
        80,
        82,
        84,
        85,
        87,
        89,
        90,
        92,
        94,
        96,
        97,
        99,
        101,
        103,
        104,
        106,
        108,
        110,
        112,
        113,
        115,
        117,
        119,
        121,
        123,
        125,
        127,
        128,
    ]

    # new gain parameters, from the same place
    total_volume_range = -50  # dB
    step_point = (
        -1
    )  # Number of steps, up from the bottom, where a 2nd volume ramp kicks in.
    step_fraction = (
        1  # fraction of totalVolumeRange where alternate volume ramp kicks in.
    )

    def __init__(self):
        """Initialize class."""
        self.volume = 50

    def increment(self):
        """Increment the volume."""
        self.volume += self.step
        if self.volume > self.maximum:
            self.volume = self.maximum

    def decrement(self):
        """Decrement the volume."""
        self.volume -= self.step
        if self.volume < self.minimum:
            self.volume = self.minimum

    def old_gain(self):
        """Return the "Old" gain value as required by the squeezebox."""
        return self.old_map[self.volume]

    def decibels(self):
        """Return the "new" gain value."""
        # pylint: disable=invalid-name

        step_db = self.total_volume_range * self.step_fraction
        max_volume_db = 0  # different on the boom?

        # Equation for a line:
        # y = mx+b
        # y1 = mx1+b, y2 = mx2+b.
        # y2-y1 = m(x2 - x1)
        # y2 = m(x2 - x1) + y1
        slope_high = max_volume_db - step_db / (100.0 - self.step_point)
        slope_low = step_db - self.total_volume_range / (self.step_point - 0.0)
        x2 = self.volume
        if x2 > self.step_point:
            m = slope_high
            x1 = 100
            y1 = max_volume_db
        else:
            m = slope_low
            x1 = 0
            y1 = self.total_volume_range
        return m * (x2 - x1) + y1

    def new_gain(self):
        """Return new gainvalue of the volume control."""
        decibel = self.decibels()
        floatmult = 10 ** (decibel / 20.0)
        # avoid rounding errors somehow
        if -30 <= decibel <= 0:
            return int(floatmult * (1 << 8) + 0.5) * (1 << 8)
        return int((floatmult * (1 << 16)) + 0.5)
