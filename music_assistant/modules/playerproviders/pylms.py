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
from utils import run_periodic, LOGGER, parse_track_title, try_parse_int, get_ip
from models import PlayerProvider, MusicPlayer, PlayerState, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from constants import CONF_ENABLED


def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["playerproviders"]['pylms'].get(CONF_ENABLED)
    if enabled:
        provider = PyLMSServer(mass)
        return provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, True, CONF_ENABLED)
        ]

class PyLMSServer(PlayerProvider):
    ''' Python implementation of SlimProto server '''

    def __init__(self, mass):
        self.prov_id = 'pylms'
        self.name = 'Logitech Media Server Emulation'
        self.icon = ''
        self.mass = mass
        self._players = {}
        self._lmsplayers = {}
        self._player_queue = {}
        self._player_queue_index = {}
        self.buffer = b''
        self.last_msg_received = 0
        self.supported_musicproviders = ['http']
        # start slimproto server
        mass.event_loop.create_task(asyncio.start_server(self.__handle_socket_client, '0.0.0.0', 3483))
        # setup discovery
        listen = mass.event_loop.create_datagram_endpoint(
                DiscoveryProtocol, local_addr=('0.0.0.0', 3483), 
                family=socket.AF_INET, reuse_address=True, reuse_port=True,
            allow_broadcast=True)
        mass.event_loop.create_task(listen)
        

     ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        if cmd == 'play':
            if self._players[player_id].state == PlayerState.Stopped:
                await self.__queue_play(player_id, None)
            else:
                self._lmsplayers[player_id].unpause()
        elif cmd == 'pause':
            self._lmsplayers[player_id].pause()
        elif cmd == 'stop':
            self._lmsplayers[player_id].stop()
        elif cmd == 'next':
            self._lmsplayers[player_id].next()
        elif cmd == 'previous':
             await self.__queue_previous(player_id)
        elif cmd == 'power' and cmd_args == 'off':
            self._lmsplayers[player_id].power_off()
        elif cmd == 'power':
            self._lmsplayers[player_id].power_on()
        elif cmd == 'volume':
            self._lmsplayers[player_id].volume_set(try_parse_int(cmd_args))
        elif cmd == 'mute' and cmd_args == 'off':
            self._lmsplayers[player_id].unmute()
        elif cmd == 'mute':
            self._lmsplayers[player_id].mute()

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the current items in the player's queue '''
        return self._player_queue[player_id][offset:limit]
    
    async def play_media(self, player_id, media_items, queue_opt='play'):
        ''' 
            play media on a player
        '''
        cur_queue_index = self._player_queue_index.get(player_id, 0)

        if queue_opt == 'replace' or not self._player_queue[player_id]:
            # overwrite queue with new items
            self._player_queue[player_id] = media_items
            await self.__queue_play(player_id, 0)
        elif queue_opt == 'play':
            # replace current item with new item(s)
            self._player_queue[player_id] = self._player_queue[player_id][:cur_queue_index] + media_items + self._player_queue[player_id][cur_queue_index+1:]
            await self.__queue_play(player_id, cur_queue_index)
        elif queue_opt == 'next':
            # insert new items at current index +1
            self._player_queue[player_id] = self._player_queue[player_id][:cur_queue_index+1] + media_items + self._player_queue[player_id][cur_queue_index+1:]
        elif queue_opt == 'add':
            # add new items at end of queue
            self._player_queue[player_id] = self._player_queue[player_id] + media_items

    ### Provider specific (helper) methods #####

    async def __queue_play(self, player_id, index):
        ''' send play command to player '''
        if not index:
            index = self._player_queue_index[player_id]
        if len(self._player_queue[player_id]) >= index-1:
            track = self._player_queue[player_id][index]
            self._lmsplayers[player_id].stop()
            self._lmsplayers[player_id].play(track.uri)
            self._player_queue_index[player_id] = index

    async def __queue_next(self, player_id):
        ''' request next track from queue '''
        if not player_id in self._player_queue or not player_id in self._player_queue:
            return
        cur_queue_index = self._player_queue_index[player_id]
        if len(self._player_queue[player_id]) > cur_queue_index:
            new_queue_index = cur_queue_index + 1
        elif self._players[player_id].repeat_enabled:
            new_queue_index = 0
        else:
            LOGGER.warning("next track requested but no more tracks in queue")
            return
        return await self.__queue_play(player_id, new_queue_index)

    async def __queue_previous(self, player_id):
        ''' request previous track from queue '''
        if not player_id in self._player_queue:
            return
        cur_queue_index = self._player_queue_index[player_id]
        if cur_queue_index == 0 and len(self._player_queue[player_id]) > 1:
            new_queue_index = len(self._player_queue[player_id]) -1
        elif cur_queue_index == 0:
            new_queue_index = cur_queue_index
        else:
            new_queue_index -= 1
            self._player_queue_index[player_id] = new_queue_index
        return await self.__queue_play(player_id, new_queue_index)

    async def __handle_player_event(self, player_id, event, event_data=None):
        ''' handle event from player '''
        if not player_id:
            return
        LOGGER.debug("Event from player %s: %s - event_data: %s" %(player_id, event, str(event_data)))
        lms_player = self._lmsplayers[player_id]
        if event == "next_track":
            return await self.__queue_next(player_id)
        if not player_id in self._players:
            player = MusicPlayer()
            player.player_id = player_id
            player.player_provider = self.prov_id
            self._players[player_id] = player
            if not player_id in self._player_queue:
                self._player_queue[player_id] = []
            if not player_id in self._player_queue_index:
                self._player_queue_index[player_id] = 0
        else:
            player = self._players[player_id]
        # update player properties
        player.name = lms_player.player_name
        player.volume_level = lms_player.volume_level
        player.cur_item_time = lms_player._elapsed_seconds
        if event == "disconnected":
            player.enabled = False
        elif event == "power":
            player.powered = event_data
        elif event == "state":
            player.state = event_data
        if self._player_queue[player_id]:
            cur_queue_index = self._player_queue_index[player_id]
            player.cur_item = self._player_queue[player_id][cur_queue_index]
        # update player details
        await self.mass.player.update_player(player)

    async def __handle_socket_client(self, reader, writer):
        ''' handle a client connection on the socket'''
        LOGGER.debug("new socket client connected")
        stream_host = get_ip()
        stream_port = self.mass.config['base']['web']['http_port']
        lms_player = PyLMSPlayer(stream_host, stream_port)

        def send_frame(command, data):
            ''' send command to lms player'''
            packet = struct.pack('!H', len(data) + 4) + command + data
            writer.write(packet)
        
        def handle_event(event, event_data=None):
            ''' handle events from player'''
            if event == "connected":
                self._lmsplayers[lms_player.player_id] = lms_player
            asyncio.create_task(self.__handle_player_event(lms_player.player_id, event, event_data))

        lms_player.send_frame = send_frame
        lms_player.send_event = handle_event
        heartbeat_task = asyncio.create_task(self.send_heartbeat(lms_player))
        
        # keep reading bytes from the socket
        while True:
            data = await reader.read(64)
            if data:
                lms_player.dataReceived(data)
            else:
                break
        # disconnect
        heartbeat_task.cancel()
        asyncio.create_task(self.__handle_player_event(lms_player.player_id, 'disconnected'))

    @run_periodic(5)
    async def send_heartbeat(self, lms_player):
        timestamp = int(time.time())
        data = lms_player.pack_stream(b"t", replayGain=timestamp, flags=0)
        lms_player.send_frame(b"strm", data)

    ### Provider specific implementation #####

class PyLMSPlayer(object):
    ''' very basic Python implementation of SlimProto '''

    def __init__(self, stream_host, stream_port):
        self.buffer = b''
        #self.display = Display()
        self.send_frame = None
        self.send_event = None
        self.stream_host = stream_host
        self.stream_port = stream_port
        self.playback_millis = 0
        self._volume = PyLMSVolume()
        self._device_type = None
        self._mac_address = None
        self._player_name = None
        self._last_volume = 0
        self._last_heartbeat = 0
        self._elapsed_seconds = 0
        self._elapsed_milliseconds = 0

    @property
    def player_name(self):
        if self._player_name:
            return self._player_name
        return "%s - %s" %(self._device_type, self._mac_address)

    @property
    def player_id(self):
        return self._mac_address

    @property
    def volume_level(self):
        return self._volume.volume
    
    def dataReceived(self, data):
        self.buffer = self.buffer + data
        if len(self.buffer) > 8:
            operation, length = self.buffer[:4], self.buffer[4:8]
            length = struct.unpack('!I', length)[0]
            plen = length + 8
            if len(self.buffer) >= plen:
                packet, self.buffer = self.buffer[8:plen], self.buffer[plen:]
                operation = operation.strip(b"!").strip().decode()
                #LOGGER.info("operation: %s" % operation)
                handler = getattr(self, "process_%s" % operation, None)
                if handler is None:
                    raise NotImplementedError
                handler(packet)

    def send_version(self):
        self.send_frame(b'vers', b'7.8')

    def pack_stream(self, command, autostart=b"1", formatbyte = b'o', pcmargs = (b'?',b'?',b'?',b'?'), threshold = 200,
                    spdif = b'0', transDuration = 0, transType = b'0', flags = 0x40, outputThreshold = 0,
                    replayGain=0, serverPort = 8095, serverIp = 0):
        return struct.pack("!cccccccBcBcBBBLHL",
                           command, autostart, formatbyte, *pcmargs,
                           threshold, spdif, transDuration, transType,
                           flags, outputThreshold, 0, replayGain, serverPort, serverIp)

    def stop(self):
        data = self.pack_stream(b"q", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)

    def pause(self):
        data = self.pack_stream(b"p", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)
        LOGGER.info("Sending pause request")

    def unpause(self):
        data = self.pack_stream(b"u", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)
        LOGGER.info("Sending unpause request")

    def next(self):
        data = self.pack_stream(b"f", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)
        self.send_event("next_track")

    def previous(self):
        data = self.pack_stream(b"f", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)
        self.send_event("previous_track")

    def power_on(self):
        self.send_frame(b"aude", struct.pack("2B", 1, 1))
        self.send_event("power", True)

    def power_off(self):
        self.stop()
        self.send_frame(b"aude", struct.pack("2B", 0, 0))
        self.send_event("power", False)

    def mute_on(self):
        self.send_frame(b"aude", struct.pack("2B", 0, 0))
        self.send_event("mute", True)

    def mute_off(self):
        self.send_frame(b"aude", struct.pack("2B", 1, 1))
        self.send_event("mute", False)

    def volume_up(self):
        self._volume.increment()
        self.send_volume()

    def volume_down(self):
        self._volume.decrement()
        self.send_volume()

    def volume_set(self, new_vol):
        self._volume.volume = new_vol
        self.send_volume()
    
    def play(self, uri, crossfade=False):
        command = b's'
        autostart = b'3' # we use direct stream for now so let the player do the messy work with buffers
        transType= b'1' if crossfade else b'0'
        transDuration = 10 if crossfade else 0
        formatbyte = b'f' # fixed to flac
        uri = '/stream' + uri.split('/stream')[1]
        data = self.pack_stream(command, autostart=autostart, flags=0x00, formatbyte=formatbyte, transType=transType, transDuration=transDuration)
        headers = "Connection: close\r\nAccept: */*\r\nHost: %s:%s\r\n" %(self.stream_host, self.stream_port)
        request = "GET %s HTTP/1.0\r\n%s\r\n" % (uri, headers)
        data = data + request.encode("utf-8")
        self.send_frame(b'strm', data)
        LOGGER.info("Requesting play from squeezebox" )

    def displayTrack(self, track):
        self.render("%s by %s" % (track.title, track.artist))

    def process_HELO(self, data):
        (devId, rev, mac) = struct.unpack('BB6s', data[:8])
        device_mac = ':'.join("%02x" % x for x in mac)
        self._device_type = devices.get(devId, 'unknown device')
        self._mac_address = str(device_mac).lower()
        LOGGER.debug("HELO received from %s %s" % (self._mac_address, self._device_type))
        self.init_client()

    def init_client(self):
        ''' initialize a new connected client '''
        self.send_event("connected")
        self.send_version()
        self.stop()
        self.setBrightness()
        #self.set_visualisation(SpectrumAnalyser())
        self.send_frame(b"setd", struct.pack("B", 0))
        self.send_frame(b"setd", struct.pack("B", 4))
        self.power_on()
        self.volume_set(40) # TODO: remember last volume
        
    def send_volume(self):
        og = self._volume.old_gain()
        ng = self._volume.new_gain()
        LOGGER.info("Volume set to %d (%d/%d)" % (self._volume.volume, og, ng))
        d = self.send_frame(b"audg", struct.pack("!LLBBLL", og, og, 1, 255, ng, ng))
        self.send_event("volume", self._volume.volume)

    def setBrightness(self, level=4):
        assert 0 <= level <= 4
        self.send_frame(b"grfb", struct.pack("!H", level))

    def set_visualisation(self, visualisation):
        self.send_frame(b"visu", visualisation.pack())

    def render(self, text):
        #self.display.clear()
        #self.display.renderText(text, "DejaVu-Sans", 16, (0,0))
        #self.updateDisplay(self.display.frame())
        pass

    def updateDisplay(self, bitmap, transition = 'c', offset=0, param=0):
        frame = struct.pack("!Hcb", offset, transition, param) + bitmap
        self.send_frame(b"grfe", frame)

    def process_STAT(self, data):
        ev = data[:4]
        if ev == b'\x00\x00\x00\x00':
            LOGGER.info("Presumed informational stat message")
        else:
            handler = getattr(self, 'stat_%s' % ev.decode(), None)
            if handler is None:
                raise NotImplementedError("Stat message %r not known" % ev)
            handler(data[4:])

    def stat_aude(self, data):
        (spdif_enable, dac_enable) = struct.unpack("2B", data[:4])
        powered = spdif_enable or dac_enable
        self.send_event("power", powered)
        LOGGER.debug("ACK aude - Received player power: %s" % powered)

    def stat_audg(self, data):
        LOGGER.info("Received volume_level from player %s" % data)
        self.send_event("volume", self._volume.volume)

    def stat_strm(self, data):
        LOGGER.debug("ACK strm")
        #self.send_frame(b"cont", b"0")

    def stat_STMc(self, data):
        LOGGER.debug("Status Message: Connect")

    def stat_STMd(self, data):
        LOGGER.debug("Decoder Ready for next track")
        self.send_event("next_track")

    def stat_STMe(self, data):
        LOGGER.info("Connection established")

    def stat_STMf(self, data):
        LOGGER.info("Status Message: Connection closed")
        self.send_event("state", PlayerState.Stopped)

    def stat_STMh(self, data):
        LOGGER.info("Status Message: End of headers")

    def stat_STMn(self, data):
        LOGGER.error("Decoder does not support file format")

    def stat_STMo(self, data):
        ''' No more decoded (uncompressed) data to play; triggers rebuffering. '''
        LOGGER.debug("Output Underrun")
        
    def stat_STMp(self, data):
        '''Pause confirmed'''
        self.send_event("state", PlayerState.Paused)

    def stat_STMr(self, data):
        '''Resume confirmed'''
        self.send_event("state", PlayerState.Playing)

    def stat_STMs(self, data):
        '''Playback of new track has started'''
        self.send_event("state", PlayerState.Playing)

    def stat_STMt(self, data):
        """ heartbeat from client """
        timestamp = time.time()
        self._last_heartbeat = timestamp
        (num_crlf, mas_initialized, mas_mode, rptr, wptr, 
        bytes_received_h, bytes_received_l, signal_strength, 
        jiffies, output_buffer_size, output_buffer_fullness, 
        elapsed_seconds, voltage, elapsed_milliseconds, 
        server_timestamp, error_code) = struct.unpack("!BBBLLLLHLLLLHLLH", data)
        if elapsed_seconds != self._elapsed_seconds:
            self.send_event("progress")
        self._elapsed_seconds = elapsed_seconds
        self._elapsed_milliseconds = elapsed_milliseconds

    def stat_STMu(self, data):
        '''Normal end of playback'''
        LOGGER.info("End of playback - Underrun")
        self.send_event("state", PlayerState.Stopped)

    def process_BYE(self, data):
        LOGGER.info("BYE received")
        self.send_event("disconnected")

    def process_RESP(self, data):
        LOGGER.info("RESP received")
        self.send_frame(b"cont", b"0")

    def process_BODY(self, data):
        LOGGER.info("BODY received")

    def process_META(self, data):
        LOGGER.info("META received")

    def process_DSCO(self, data):
        LOGGER.info("Data Stream Disconnected")

    def process_DBUG(self, data):
        LOGGER.info("DBUG received")

    def process_IR(self, data):
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

    def process_RAWI(self, data):
        LOGGER.info("RAWI received")

    def process_ANIC(self, data):
        LOGGER.info("ANIC received")

    def process_BUTN(self, data):
        LOGGER.info("BUTN received")

    def process_KNOB(self, data):
        ''' Transporter only, knob-related '''
        LOGGER.info("KNOB received")

    def process_SETD(self, data):
        ''' Get/set player firmware settings '''
        LOGGER.debug("SETD received %s" % data)
        cmd_id = data[0]
        if cmd_id == 0:
            # received player name
            data = data[1:].decode()
            self._player_name = data
            self.send_event("name")

    def process_UREQ(self, data):
        LOGGER.info("UREQ received")


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
        s = struct.unpack('!cxBB8x6B', data)
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
        self.packet = struct.pack('!c16s', 'D', hostname)

class TLVDiscoveryRequestDatagram(Datagram):
    
    def __init__(self, data):
        requestdata = OrderedDict()
        assert data[0] == 'e'
        idx = 1
        length = len(data)-5
        while idx <= length:
            typ, l = struct.unpack_from("4sB", data, idx)
            if l:
                val = data[idx+5:idx+5+l]
                idx += 5+l
            else:
                val = None
                idx += 5
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
                LOGGER.warning("Response %s too long, truncating to 255 bytes" % typ)
                value = value[:255]
            parts.extend((typ, chr(len(value)), value))
        self.packet = ''.join(parts)

class DiscoveryProtocol():

    def connection_made(self, transport):
        self.transport = transport
        # Allow receiving multicast broadcasts
        sock = self.transport.get_extra_info('socket')
        group = socket.inet_aton('239.255.255.250')
        mreq = struct.pack('4sL', group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    
    def build_TLV_response(self, requestdata):
        responsedata = OrderedDict()
        for typ, value in requestdata.items():
            if typ == 'NAME':
                # send full host name - no truncation
                value = 'macbook-marcel' # TODO
            elif typ == 'IPAD':
                # send ipaddress as a string only if it is set
                value = '192.168.1.145' # TODO
                # :todo: IPv6
                if value == '0.0.0.0':
                    # do not send back an ip address
                    typ = None
            elif typ == 'JSON':
                # send port as a string
                json_port = 9000 # todo: web.service.port
                value = str(json_port)
            elif typ == 'VERS':
                # send server version
                 value = '7.9'
            elif typ == 'UUID':
                # send server uuid
                value = 'test'
            # elif typ == 'JVID':
            #     # not handle, just log the information
            #     typ = None
            #     log.msg("Jive: %x:%x:%x:%x:%x:%x:" % struct.unpack('>6B', value),
            #             logLevel=logging.INFO)
            else:
                LOGGER.error('Unexpected information request: %r', typ)
                typ = None
            if typ:
                responsedata[typ] = value
        return responsedata

    def datagram_received(self, data, addr):
        try:
            data = data.decode()
            LOGGER.info('Received %r from %s' % (data, addr))
            dgram = Datagram.decode(data)
            LOGGER.info("Data received from %s: %s" % (addr, dgram))
            if isinstance(dgram, ClientDiscoveryDatagram):
                self.sendDiscoveryResponse(addr)
            elif isinstance(dgram, TLVDiscoveryRequestDatagram):
                resonsedata = self.build_TLV_response(dgram.data)
                self.sendTLVDiscoveryResponse(resonsedata, addr)
        except Exception as exc:
            LOGGER.exception(exc)

    def sendDiscoveryResponse(self, addr):
        dgram = DiscoveryResponseDatagram('macbook-marcel', 3483)
        LOGGER.info("Sending discovery response %r" % (dgram.packet,))
        self.transport.sendto(dgram.packet.encode(), addr)

    def sendTLVDiscoveryResponse(self, resonsedata, addr):
        dgram = TLVDiscoveryResponseDatagram(resonsedata)
        LOGGER.info("Sending discovery response %r" % (dgram.packet,))
        self.transport.sendto(dgram.packet.encode(), addr)







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


class PyLMSVolume(object):

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