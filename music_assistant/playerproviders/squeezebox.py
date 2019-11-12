#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
import struct
from collections import OrderedDict
import time
import decimal
from typing import List
import random
import sys
import socket
from ..utils import run_periodic, LOGGER, try_parse_int, get_ip, get_hostname
from ..models import PlayerProvider, Player, PlayerState, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from ..constants import CONF_ENABLED


PROV_ID = 'squeezebox'
PROV_NAME = 'Squeezebox'
PROV_CLASS = 'PySqueezeProvider'

CONFIG_ENTRIES = [
    (CONF_ENABLED, True, CONF_ENABLED),
    ]


class PySqueezeProvider(PlayerProvider):
    ''' Python implementation of SlimProto server '''

     ### Provider specific implementation #####

    async def setup(self, conf):
        ''' async initialize of module '''
        # start slimproto server
        self.mass.event_loop.create_task(
                asyncio.start_server(self.__handle_socket_client, '0.0.0.0', 3483))
        # setup discovery
        self.mass.event_loop.create_task(self.start_discovery())

    async def start_discovery(self):
        transport, protocol = await self.mass.event_loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(self.mass.web.http_port),
        local_addr=('0.0.0.0', 3483))
        try:
            while True:
                await asyncio.sleep(60)  # serve forever
        finally:
            transport.close()

    async def __handle_socket_client(self, reader, writer):
        ''' handle a client connection on the socket'''
        buffer = b''
        player = None
        try:            
            # keep reading bytes from the socket
            while True:
                data = await reader.read(64)
                if not data:
                    # connection lost with client
                    break
                # handle incoming data from socket
                buffer = buffer + data
                del data
                if len(buffer) > 8:
                    operation, length = buffer[:4], buffer[4:8]
                    plen = struct.unpack('!I', length)[0] + 8
                    if len(buffer) >= plen:
                        packet, buffer = buffer[8:plen], buffer[plen:]
                        operation = operation.strip(b"!").strip().decode()
                        if operation == 'HELO':
                            # player connected
                            (dev_id, rev, mac) = struct.unpack('BB6s', packet[:8])
                            device_mac = ':'.join("%02x" % x for x in mac)
                            player_id = str(device_mac).lower()
                            device_type = devices.get(dev_id, 'unknown device')
                            player = PySqueezePlayer(self.mass, player_id, self.prov_id, device_type, writer)
                            await self.mass.players.add_player(player)
                        elif player != None:
                            await player.process_msg(operation, packet)
        except Exception as exc:
            # connection lost ?
            LOGGER.debug(exc)
        finally:
            # disconnect and cleanup
            if player:
                if player._heartbeat_task:
                    player._heartbeat_task.cancel()
                await self.mass.players.remove_player(player.player_id)
                self.mass.config.save()

class PySqueezePlayer(Player):
    ''' Squeezebox socket client '''

    def __init__(self, mass, player_id, prov_id, dev_type, writer):
        super().__init__(mass, player_id, prov_id)
        self.supports_queue = True
        self.supports_gapless = True
        self.supports_crossfade = True
        self._writer = writer
        self.buffer = b''
        self.name = "%s - %s" %(dev_type, player_id)
        self._volume = PySqueezeVolume()
        self._last_volume = 0
        self._last_heartbeat = 0
        self._cur_time_milliseconds = 0
        # initialize player
        self.mass.event_loop.create_task(self.initialize_player())
        self._heartbeat_task = self.mass.event_loop.create_task(self.__send_heartbeat())

    async def initialize_player(self):
        ''' set some startup settings for the player '''
        # send version
        await self.__send_frame(b'vers', b'7.8')
        await self.__send_frame(b"setd", struct.pack("B", 0))
        await self.__send_frame(b"setd", struct.pack("B", 4))
        # TODO: handle display stuff
        #await self.setBrightness()
        # restore last volume and power state
        if self.settings.get("last_volume"):
            await self.volume_set(self.settings["last_volume"])
        else:
            await self.volume_set(40)
        if self.settings.get("last_power"):
            await self.power(self.settings["last_power"])
        else:
            await self.power_off()

    async def cmd_stop(self):
        ''' send stop command to player '''
        data = await self.__pack_stream(b"q", autostart=b"0", flags=0)
        await self.__send_frame(b"strm", data)

    async def cmd_play(self):
        ''' send play (unpause) command to player '''
        data = await self.__pack_stream(b"u", autostart=b"0", flags=0)
        await self.__send_frame(b"strm", data)

    async def cmd_pause(self):
        ''' send pause command to player '''
        data = await self.__pack_stream(b"p", autostart=b"0", flags=0)
        await self.__send_frame(b"strm", data)
    
    async def cmd_power_on(self):
        ''' send power ON command to player '''
        await self.__send_frame(b"aude", struct.pack("2B", 1, 1))
        self.settings["last_power"] = True
        self.powered = True

    async def cmd_power_off(self):
        ''' send power TOGGLE command to player '''
        await self.cmd_stop()
        await self.__send_frame(b"aude", struct.pack("2B", 0, 0))
        self.settings["last_power"] = False
        self.powered = False

    async def cmd_volume_set(self, volume_level):
        ''' send new volume level command to player '''
        self._volume.volume = volume_level
        og = self._volume.old_gain()
        ng = self._volume.new_gain()
        await self.__send_frame(b"audg", struct.pack("!LLBBLL", og, og, 1, 255, ng, ng))
        self.settings["last_volume"] = volume_level
        self.volume_level = volume_level
    
    async def cmd_volume_mute(self, is_muted=False):
        ''' send mute command to player '''
        if is_muted:
            await self.__send_frame(b"aude", struct.pack("2B", 0, 0))
        else:
            await self.__send_frame(b"aude", struct.pack("2B", 1, 1))
        self.muted = is_muted

    async def cmd_queue_play_index(self, index:int):
        '''
            play item at index X on player's queue
            :param index: (int) index of the queue item that should start playing
        '''
        new_track = await self.queue.get_item(index)
        if new_track:
            await self.__send_flush()
            await self.__send_play(new_track.uri)

    async def cmd_queue_load(self, queue_items):
        ''' 
            load/overwrite given items in the player's own queue implementation
            :param queue_items: a list of QueueItems
        '''
        await self.__send_flush()
        if queue_items:
            await self.__send_play(queue_items[0].uri)

    async def cmd_queue_insert(self, queue_items, insert_at_index):
        # queue handled by built-in queue controller
        # we only check the start index
        if insert_at_index == self.queue.cur_index:
            return await self.cmd_queue_play_index(insert_at_index)

    async def cmd_queue_append(self, queue_items):
        pass # automagically handled by built-in queue controller

    async def cmd_play_uri(self, uri:str):
        '''
            [MUST OVERRIDE]
            tell player to start playing a single uri
        '''
        await self.__send_flush()
        await self.__send_play(uri)

    async def __send_flush(self):
        data = await self.__pack_stream(b"f", autostart=b"0", flags=0)
        await self.__send_frame(b"strm", data)
    
    async def __send_play(self, uri):
        ''' play uri '''
        self.cur_uri = uri
        self.powered = True
        enable_crossfade = self.settings["crossfade_duration"] > 0
        command = b's'
        autostart = b'3' # we use direct stream for now so let the player do the messy work with buffers
        transType= b'1' if enable_crossfade else b'0'
        transDuration = self.settings["crossfade_duration"]
        formatbyte = b'f' # fixed to flac
        uri = '/stream' + uri.split('/stream')[1]
        data = await self.__pack_stream(command, autostart=autostart, flags=0x00, 
            formatbyte=formatbyte, transType=transType, 
            transDuration=transDuration)
        headers = "Connection: close\r\nAccept: */*\r\nHost: %s:%s\r\n" %(self.mass.web.local_ip, self.mass.web.http_port)
        request = "GET %s HTTP/1.0\r\n%s\r\n" % (uri, headers)
        data = data + request.encode("utf-8")
        await self.__send_frame(b'strm', data)
        LOGGER.info("Requesting play from squeezebox" )

    def __delete__(self, instance):
        ''' make sure the heartbeat task is deleted '''
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

    @run_periodic(5)
    async def __send_heartbeat(self):
        ''' send periodic heartbeat message to player '''
        timestamp = int(time.time())
        data = await self.__pack_stream(b"t", replayGain=timestamp, flags=0)
        await self.__send_frame(b"strm", data)

    async def __send_frame(self, command, data):
        ''' send command to Squeeze player'''
        packet = struct.pack('!H', len(data) + 4) + command + data
        self._writer.write(packet)
        await self._writer.drain()

    async def __pack_stream(self, command, autostart=b"1", formatbyte = b'o', 
                pcmargs = (b'?',b'?',b'?',b'?'), threshold = 200,
                spdif = b'0', transDuration = 0, transType = b'0', 
                flags = 0x40, outputThreshold = 0,
                replayGain=0, serverPort = 8095, serverIp = 0):
        return struct.pack("!cccccccBcBcBBBLHL",
                           command, autostart, formatbyte, *pcmargs,
                           threshold, spdif, transDuration, transType,
                           flags, outputThreshold, 0, replayGain, serverPort, serverIp)
    
    async def displayTrack(self, track):
        await self.render("%s by %s" % (track.title, track.artist))
        
    async def setBrightness(self, level=4):
        assert 0 <= level <= 4
        await self.__send_frame(b"grfb", struct.pack("!H", level))

    async def set_visualisation(self, visualisation):
        await self.__send_frame(b"visu", visualisation.pack())

    async def render(self, text):
        #self.display.clear()
        #self.display.renderText(text, "DejaVu-Sans", 16, (0,0))
        #self.updateDisplay(self.display.frame())
        pass

    async def updateDisplay(self, bitmap, transition = 'c', offset=0, param=0):
        frame = struct.pack("!Hcb", offset, transition, param) + bitmap
        await self.__send_frame(b"grfe", frame)

    async def process_msg(self, operation, packet):
        handler = getattr(self, "process_%s" % operation, None)
        if handler is None:
            LOGGER.error("No handler for %s" % operation)
        else:
            await handler(packet)

    async def process_STAT(self, data):
        '''process incoming event from player'''
        event = data[:4].decode()
        event_data = data[4:]
        if event == b'\x00\x00\x00\x00':
            # Presumed informational stat message
            return
        event_handler = getattr(self, 'stat_%s' %event, None)
        if event_handler is None:
            LOGGER.debug("Got event %s - event_data: %s" %(event, event_data))
        else:
            await event_handler(data[4:])

    async def stat_aude(self, data):
        (spdif_enable, dac_enable) = struct.unpack("2B", data[:4])
        powered = spdif_enable or dac_enable
        self.powered = powered
        self.muted = not powered
        LOGGER.debug("ACK aude - Received player power: %s" % powered)

    async def stat_audg(self, data):
        # TODO: process volume level
        LOGGER.info("Received volume_level from player %s" % data)
        self.volume_level = self._volume.volume

    async def stat_STMd(self, data):
        LOGGER.debug("Decoder Ready for next track")
        next_item = self.queue.next_item
        if next_item:
            await self.__send_play(next_item.uri)

    async def stat_STMf(self, data):
        LOGGER.debug("Status Message: Connection closed")
        self.state = PlayerState.Stopped

    async def stat_STMo(self, data):
        ''' No more decoded (uncompressed) data to play; triggers rebuffering. '''
        LOGGER.debug("Output Underrun")
        
    async def stat_STMp(self, data):
        '''Pause confirmed'''
        self.state = PlayerState.Paused

    async def stat_STMr(self, data):
        '''Resume confirmed'''
        self.state = PlayerState.Playing

    async def stat_STMs(self, data):
        '''Playback of new track has started'''
        self.state = PlayerState.Playing

    async def stat_STMt(self, data):
        """ heartbeat from client """
        timestamp = time.time()
        self._last_heartbeat = timestamp
        (num_crlf, mas_initialized, mas_mode, rptr, wptr, 
            bytes_received_h, bytes_received_l, signal_strength, 
            jiffies, output_buffer_size, output_buffer_fullness, 
            elapsed_seconds, voltage, cur_time_milliseconds, 
            server_timestamp, error_code) = struct.unpack("!BBBLLLLHLLLLHLLH", data)
        if self.state == PlayerState.Playing and elapsed_seconds != self.cur_time:
            self.cur_time = elapsed_seconds
        self._cur_time_milliseconds = cur_time_milliseconds

    async def stat_STMu(self, data):
        ''' Buffer underrun: Normal end of playback'''
        self.state = PlayerState.Stopped

    async def process_RESP(self, data):
        ''' response received at player, send continue '''
        LOGGER.debug("RESP received")
        await self.__send_frame(b"cont", b"0")

    async def process_IR(self, data):
        """ Slightly involved codepath here. This raises an event, which may
        be picked up by the service and then the process_remote_* function in
        this player will be called. This is mostly relevant for volume changes
        - most other button presses will require some context to operate. """
        (time, code) = struct.unpack("!IxxI", data)
        LOGGER.info("IR code %s" % code)
        # command = Remote.codes.get(code, None)
        # if command is not None:
        #     LOGGER.info("IR received: %r, %r" % (code, command))
        #     #self.service.evreactor.fireEvent(RemoteButtonPressed(self, command))
        # else:
        #     LOGGER.info("Unknown IR received: %r, %r" % (time, code))

    async def process_SETD(self, data):
        ''' Get/set player firmware settings '''
        LOGGER.debug("SETD received %s" % data)
        cmd_id = data[0]
        if cmd_id == 0:
            # received player name
            data = data[1:].decode()
            self.name = data


# from http://wiki.slimdevices.com/index.php/SlimProtoTCPProtocol#HELO
devices = {
    2: 'squeezebox',
    3: 'softsqueeze',
    4: 'squeezebox2',
    5: 'transporter',
    6: 'softsqueeze3',
    7: 'receiver',
    8: 'squeezeslave',
    9: 'controller',
    10: 'boom',
    11: 'softboom',
    12: 'squeezeplay',
    }


class PySqueezeVolume(object):

    """ Represents a sound volume. This is an awful lot more complex than it
    sounds. """

    minimum = 0
    maximum = 100
    step = 1

    # this map is taken from Slim::Player::Squeezebox2 in the squeezecenter source
    # i don't know how much magic it contains, or any way I can test it
    old_map = [
        0, 1, 1, 1, 2, 2, 2, 3,  3,  4,
        5, 5, 6, 6, 7, 8, 9, 9, 10, 11,
        12, 13, 14, 15, 16, 16, 17, 18, 19, 20,
        22, 23, 24, 25, 26, 27, 28, 29, 30, 32,
        33, 34, 35, 37, 38, 39, 40, 42, 43, 44,
        46, 47, 48, 50, 51, 53, 54, 56, 57, 59,
        60, 61, 63, 65, 66, 68, 69, 71, 72, 74,
        75, 77, 79, 80, 82, 84, 85, 87, 89, 90,
        92, 94, 96, 97, 99, 101, 103, 104, 106, 108, 110,
        112, 113, 115, 117, 119, 121, 123, 125, 127, 128
        ];

    # new gain parameters, from the same place
    total_volume_range = -50 # dB
    step_point = -1           # Number of steps, up from the bottom, where a 2nd volume ramp kicks in.
    step_fraction = 1         # fraction of totalVolumeRange where alternate volume ramp kicks in.

    def __init__(self):
        self.volume = 50

    def increment(self):
        """ Increment the volume """
        self.volume += self.step
        if self.volume > self.maximum:
            self.volume = self.maximum

    def decrement(self):
        """ Decrement the volume """
        self.volume -= self.step
        if self.volume < self.minimum:
            self.volume = self.minimum

    def old_gain(self):
        """ Return the "Old" gain value as required by the squeezebox """
        return self.old_map[self.volume]

    def decibels(self):
        """ Return the "new" gain value. """

        step_db = self.total_volume_range * self.step_fraction
        max_volume_db = 0 # different on the boom?

        # Equation for a line:
        # y = mx+b
        # y1 = mx1+b, y2 = mx2+b.
        # y2-y1 = m(x2 - x1)
        # y2 = m(x2 - x1) + y1
        slope_high = max_volume_db - step_db / (100.0 - self.step_point)
        slope_low = step_db - self.total_volume_range / (self.step_point - 0.0)
        x2 = self.volume
        if (x2 > self.step_point):
            m = slope_high
            x1 = 100
            y1 = max_volume_db
        else:
            m = slope_low
            x1 = 0
            y1 = self.total_volume_range
        return m * (x2 - x1) + y1

    def new_gain(self):
        db = self.decibels()
        floatmult = 10 ** (db/20.0)
        # avoid rounding errors somehow
        if -30 <= db <= 0:
            return int(floatmult * (1 << 8) + 0.5) * (1<<8)
        else:
            return int((floatmult * (1<<16)) + 0.5)


##### UDP DISCOVERY STUFF #############

class Datagram(object):

    @classmethod
    def decode(self, data):
        if data[0] == 'e':
            return TLVDiscoveryRequestDatagram(data)
        elif data[0] == 'E':
            return TLVDiscoveryResponseDatagram(data)
        elif data[0] == 'd':
            return ClientDiscoveryDatagram(data)
        elif data[0] == 'h':
            pass # Hello!
        elif data[0] == 'i':
            pass # IR
        elif data[0] == '2':
            pass # i2c?
        elif data[0] == 'a':
            pass # ack!

class ClientDiscoveryDatagram(Datagram):

    device = None
    firmware = None
    client = None

    def __init__(self, data):
        s = struct.unpack('!cxBB8x6B', data.encode())
        assert  s[0] == 'd'
        self.device = s[1]
        self.firmware = hex(s[2])
        self.client = ":".join(["%02x" % (x,) for x in s[3:]])

    def __repr__(self):
        return "<%s device=%r firmware=%r client=%r>" % (self.__class__.__name__, self.device, self.firmware, self.client)

class DiscoveryResponseDatagram(Datagram):

    def __init__(self, hostname, port):
        hostname = hostname[:16].encode("UTF-8")
        hostname += (16 - len(hostname)) * '\x00'
        self.packet = struct.pack('!c16s', 'D', hostname).decode()

class TLVDiscoveryRequestDatagram(Datagram):
    
    def __init__(self, data):
        requestdata = OrderedDict()
        assert data[0] == 'e'
        idx = 1
        length = len(data)-5
        while idx <= length:
            typ, l = struct.unpack_from("4sB", data.encode(), idx)
            if l:
                val = data[idx+5:idx+5+l]
                idx += 5+l
            else:
                val = None
                idx += 5
            typ = typ.decode()
            requestdata[typ] = val
        self.data = requestdata
            
    def __repr__(self):
        return "<%s data=%r>" % (self.__class__.__name__, self.data.items())

class TLVDiscoveryResponseDatagram(Datagram):

    def __init__(self, responsedata):
        parts = ['E'] # new discovery format
        for typ, value in responsedata.items():
            if value is None:
                value = ''
            elif len(value) > 255:
                # Response too long, truncating to 255 bytes
                value = value[:255]
            parts.extend((typ, chr(len(value)), value))
        self.packet = ''.join(parts)

class DiscoveryProtocol():

    def __init__(self, web_port):
        self.web_port = web_port
    
    def connection_made(self, transport):
        self.transport = transport
        # Allow receiving multicast broadcasts
        sock = self.transport.get_extra_info('socket')
        group = socket.inet_aton('239.255.255.250')
        mreq = struct.pack('4sL', group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    def error_received(self, exc):
        LOGGER.error(exc)

    def connection_lost(self, *args, **kwargs):
        LOGGER.debug("Connection lost to discovery")
    
    def build_TLV_response(self, requestdata):
        responsedata = OrderedDict()
        for typ, value in requestdata.items():
            if typ == 'NAME':
                # send full host name - no truncation
                value = get_hostname()
            elif typ == 'IPAD':
                # send ipaddress as a string only if it is set
                value = get_ip()
                # :todo: IPv6
                if value == '0.0.0.0':
                    # do not send back an ip address
                    typ = None
            elif typ == 'JSON':
                # send port as a string
                json_port = self.web_port
                value = str(json_port)
            elif typ == 'VERS':
                # send server version
                 value = '7.9'
            elif typ == 'UUID':
                # send server uuid
                value = 'musicassistant'
            else:
                LOGGER.debug('Unexpected information request: %r', typ)
                typ = None
            if typ:
                responsedata[typ] = value
        return responsedata

    def datagram_received(self, data, addr):
        try:
            data = data.decode()
            dgram = Datagram.decode(data)
            if isinstance(dgram, ClientDiscoveryDatagram):
                self.sendDiscoveryResponse(addr)
            elif isinstance(dgram, TLVDiscoveryRequestDatagram):
                resonsedata = self.build_TLV_response(dgram.data)
                self.sendTLVDiscoveryResponse(resonsedata, addr)
        except Exception as exc:
            LOGGER.exception(exc)

    def sendDiscoveryResponse(self, addr):
        dgram = DiscoveryResponseDatagram(get_hostname(), 3483)
        self.transport.sendto(dgram.packet.encode(), addr)

    def sendTLVDiscoveryResponse(self, resonsedata, addr):
        dgram = TLVDiscoveryResponseDatagram(resonsedata)
        self.transport.sendto(dgram.packet.encode(), addr)

