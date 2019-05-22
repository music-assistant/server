#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
import struct
import time
import decimal
from typing import List
import random
import sys
from netaddr import EUI
from utils import run_periodic, LOGGER, parse_track_title
from models import PlayerProvider, MusicPlayer, PlayerState, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from constants import CONF_ENABLED


def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["playerproviders"]['pylms'].get(CONF_ENABLED)
    if enabled:
        provider = PyLMSProvider(mass)
        return provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, False, CONF_ENABLED)
        ]

class PyLMSProvider(PlayerProvider):
    ''' Python implementation of SlimProto '''

    def __init__(self, mass):
        self.prov_id = 'pylms'
        self.name = 'Logitech Media Server Emulation'
        self.icon = ''
        self.mass = mass
        self._players = {}
        self._players = {}
        self.buffer = b''
        self.last_msg_received = 0
        self.supported_musicproviders = ['http']
        mass.event_loop.create_task(asyncio.start_server(self.__handle_client, 'localhost', 3483))       


    async def __handle_client(self, reader, writer):
        request = None
        lms_player = PyLMSPlayer()
        
        def send_frame(command, data):
            packet = struct.pack('!H', len(data) + 4) + command + data
            print("Sending packet %r" % packet)
            writer.write(packet)
        lms_player.send_frame = send_frame
        asyncio.create_task(self.send_play(lms_player))
        
        while request != 'quit':
            data = await reader.read(100)
            if not data:
                break
            #data = data.decode('latin-1')
            print(data)
            lms_player.dataReceived(data)
            
            #response = str(eval(request)) + '\n'
            #writer.write(response.encode('utf8'))
        LOGGER.info('client disconnected')

    async def send_play(self, lms_player):
        await asyncio.sleep(5)
        lms_player.play()
        lms_player.unpause()






    ### Provider specific implementation #####

class PyLMSPlayer(object):
    ''' Python implementation of SlimProto '''

    # these numbers are also in a dict in Collection.  This should obviously be refactored.
    typeMap = {
        0: b'o', # ogg
        1: b'm', # mp3
        2: b'f', # flac
        3: b'p', # pcm (wav etc.)
    }

    def __init__(self):
        self.buffer = b''
        #self.display = Display()
        self.volume = PyLMSVolume()
        self.device_type = None
        self.mac_address = None
        self.send_frame = None

    def connectionEstablished(self):
        """ Called when a connection has been successfully established with
        the player. """
        #self.service.evreactor.fireEvent(StateChanged(self, StateChanged.State.ESTABLISHED))
        LOGGER.info("Connected to squeezebox")
        

    def connectionLost(self, reason):
        #self.service.evreactor.fireEvent(StateChanged(self, StateChanged.State.DISCONNECTED))
        #self.service.players.remove(self)
        pass

    def dataReceived(self, data):
        self.buffer = self.buffer + data
        if len(self.buffer) > 8:
            operation, length = self.buffer[:4], self.buffer[4:8]
            length = struct.unpack('!I', length)[0]
            plen = length + 8
            if len(self.buffer) >= plen:
                packet, self.buffer = self.buffer[8:plen], self.buffer[plen:]
                operation = operation.strip(b"!").strip().decode()
                LOGGER.info("operation: %s" % operation)
                handler = getattr(self, "process_%s" % operation, None)
                if handler is None:
                    raise NotImplementedError
                handler(packet)

    

    def send_version(self):
        self.send_frame(b'vers', b'7.0')

    def pack_stream(self, command, autostart=b"1", formatbyte = b'o', pcmargs = b'1321', threshold = 255,
                    spdif = b'0', transDuration = 0, transType = b'0', flags = 0x40, outputThreshold = 0,
                    replayGainHigh = 0, replayGainLow = 0, serverPort = 8095, serverIp = 0):
        return struct.pack("!ccc4sBcBcBBBHHHL",
                           command, autostart, formatbyte, pcmargs,
                           threshold, spdif, transDuration, transType,
                           flags, outputThreshold, 0, replayGainHigh, replayGainLow, serverPort, serverIp)

    def stop_streaming(self):
        data = self.pack_stream(b"q", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)

    def pause(self):
        data = self.pack_stream(b"bp", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)
        LOGGER.info("Sending pause request")

    def unpause(self):
        data = self.pack_stream(b"u", autostart=b"0", flags=0)
        self.send_frame(b"strm", data)
        LOGGER.info("Sending unpause request")

    def stop(self):
        self.stop_streaming()

    def play(self):
        command = b's'
        autostart = b'1'
        formatbyte = self.typeMap[2]
        uri = "/stream?provider=spotify&track_id=56z8UyE4foPVnSrER7lVR5"
        data = self.pack_stream(command, autostart=autostart, flags=0x00, formatbyte=formatbyte)
        request = "GET %s HTTP/1.0\r\n\r\n" % uri
        data = data + request.encode("utf-8")
        self.send_frame(b'strm', data)
        LOGGER.info("Requesting play from squeezebox %s" % (id(self),))
        #self.displayTrack(track)

    # def play(self, track):
    #     command = b's'
    #     autostart = b'1'
    #     formatbyte = self.typeMap[track.type]
    #     data = self.pack_stream(command, autostart=autostart, flags=0x00, formatbyte=formatbyte)
    #     request = "GET %s HTTP/1.0\r\n\r\n" % (track.player_uri(id(self)),)
    #     data = data + request.encode("utf-8")
    #     self.send_frame(b'strm', data)
    #     LOGGER.info("Requesting play from squeezebox %s" % (id(self),))
    #     self.displayTrack(track)

    def displayTrack(self, track):
        self.render("%s by %s" % (track.title, track.artist))

    def process_HELO(self, data):
        #(devId, rev, mac, wlan, bytes) = struct.unpack('BB6sHL', data[:16])
        (devId, rev, mac) = struct.unpack('BB6s', data[:8])
        (mac,) = struct.unpack(">q", b'00'+mac)
        mac = EUI(mac)
        self.device_type = devices.get(devId, 'unknown device')
        self.mac_address = str(mac)
        LOGGER.info("HELO received from %s %s" % (self.mac_address, self.device_type))
        self.init_client()

    def init_client(self):
        self.send_version()
        self.stop_streaming()
        self.setBrightness()
        #self.set_visualisation(SpectrumAnalyser())
        self.send_frame(b"setd", struct.pack("B", 0))
        self.send_frame(b"setd", struct.pack("B", 4))
        self.enableAudio()
        self.send_volume()
        self.send_frame(b"strm", self.pack_stream(b't', autostart=b"1", flags=0, replayGainHigh=0))
        self.connectionEstablished()

    def enableAudio(self):
        self.send_frame(b"aude", struct.pack("2B", 1, 1))

    def send_volume(self):
        og = self.volume.old_gain()
        ng = self.volume.new_gain()
        LOGGER.info("Volume set to %d (%d/%d)" % (self.volume.volume, og, ng))
        d = self.send_frame(b"audg", struct.pack("!LLBBLL", og, og, 1, 255, ng, ng))
        #self.service.evreactor.fireEvent(VolumeChanged(self, self.volume))
        return d

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
        #print "STAT received: %r" % data
        ev = data[:4]
        if ev == b'\x00\x00\x00\x00':
            LOGGER.info("Presumed informational stat message")
        else:
            handler = getattr(self, 'stat_%s' % ev.decode(), None)
            if handler is None:
                raise NotImplementedError("Stat message %r not known" % ev)
            handler(data[4:])

    def stat_aude(self, data):
        LOGGER.info("ACK aude")

    def stat_audg(self, data):
        LOGGER.info("ACK audg")

    def stat_strm(self, data):
        LOGGER.info("ACK strm")

    def stat_STMc(self, data):
        LOGGER.info("Status Message: Connect")

    def stat_STMd(self, data):
        LOGGER.info("Decoder Ready")
        #self.service.evreactor.fireEvent(StateChanged(self, StateChanged.State.READY))

    def stat_STMe(self, data):
        LOGGER.info("Connection established")

    def stat_STMf(self, data):
        LOGGER.info("Status Message: Connection closed")

    def stat_STMh(self, data):
        LOGGER.info("Status Message: End of headers")

    def stat_STMn(self, data):
        LOGGER.info("Decoder does not support file format")

    def stat_STMo(self, data):
        LOGGER.info("Output Underrun")

    def stat_STMp(self, data):
        LOGGER.info("Pause confirmed")
        #self.service.evreactor.fireEvent(StateChanged(self, StateChanged.State.PAUSED))

    def stat_STMr(self, data):
        LOGGER.info("Resume confirmed")
        #self.service.evreactor.fireEvent(StateChanged(self, StateChanged.State.PLAYING))

    def stat_STMs(self, data):
        LOGGER.info("Player status message: playback of new track has started")
        #self.service.evreactor.fireEvent(StateChanged(self, StateChanged.State.PLAYING))

    def stat_STMt(self, data):
        """ Timer heartbeat """
        self.last_heartbeat = time.time()

    def stat_STMu(self, data):
        LOGGER.info("End of playback")
        #self.service.evreactor.fireEvent(StateChanged(self, StateChanged.State.UNDERRUN))

    def process_BYE(self, data):
        LOGGER.info("BYE received")

    def process_RESP(self, data):
        LOGGER.info("RESP received")

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
        command = Remote.codes.get(code, None)
        if command is not None:
            LOGGER.info("IR received: %r, %r" % (code, command))
            #self.service.evreactor.fireEvent(RemoteButtonPressed(self, command))
        else:
            LOGGER.info("Unknown IR received: %r, %r" % (time, code))

    def process_RAWI(self, data):
        LOGGER.info("RAWI received")

    def process_ANIC(self, data):
        LOGGER.info("ANIC received")

    def process_BUTN(self, data):
        LOGGER.info("BUTN received")

    def process_KNOB(self, data):
        LOGGER.info("KNOB received")

    def process_SETD(self, data):
        LOGGER.info("SETD received")

    def process_UREQ(self, data):
        LOGGER.info("UREQ received")

    def process_remote_volumeup(self):
        self.volume.increment()
        self.send_volume()

    def process_remote_volumedown(self):
        self.volume.decrement()
        self.send_volume()



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