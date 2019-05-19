#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import run_periodic, LOGGER, try_parse_int, try_parse_float, get_ip, run_async_background_task
import aiohttp
from difflib import SequenceMatcher as Matcher
from models import MediaType, PlayerState, MusicPlayer
from typing import List
import toolz
import operator
import socket
import random
from copy import deepcopy
import functools
import time
import shutil
import xml.etree.ElementTree as ET
import concurrent
import aiohttp
import random
import urllib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "playerproviders" )
AUDIO_TEMP_DIR = "/tmp/audio_tmp"
AUDIO_CACHE_DIR = "/tmp/audio_cache"

class Player():
    ''' several helpers to handle playback through player providers '''
    
    def __init__(self, mass):
        self.mass = mass
        self.providers = {}
        self._players = {}
        self.create_config_entries()
        self.local_ip = get_ip()
        # create needed temp/cache dirs
        if self.mass.config['base']['http_streamer']['enable_cache'] and not os.path.isdir(AUDIO_CACHE_DIR):
            os.makedirs(AUDIO_CACHE_DIR)
        if not os.path.isdir(AUDIO_TEMP_DIR):
            os.makedirs(AUDIO_TEMP_DIR)
        # dynamically load provider modules
        self.load_providers()

    def create_config_entries(self):
        ''' sets the config entries for this module (list with key/value pairs)'''
        # player specific settings
        self.mass.config['player_settings']['__desc__'] = [
            ("enabled", False, "player_enabled"),
            ("name", "", "player_name"),
            ("group_parent", "<player>", "player_group_with"),
            ("mute_as_power", False, "player_mute_power"),
            ("disable_volume", False, "player_disable_vol"),
            ("apply_group_volume", False, "player_group_vol"),
            ("apply_group_power", False, "player_group_pow"),
            ("play_power_on", False, "player_power_play"),
            ("sox_effects", '', "http_streamer_sox_effects"),
            ("force_http_streamer", False, "force_http_streamer")
        ]
        # config for the http streamer
        config_entries = [
            ('volume_normalisation', True, 'enable_r128_volume_normalisation'), 
            ('target_volume', '-23', 'target_volume_lufs'),
            ('fallback_gain_correct', '-12', 'fallback_gain_correct'),
            ('enable_cache', True, 'enable_audio_cache'),
            ('trim_silence', True, 'trim_silence')
            ]
        if not self.mass.config['base'].get('http_streamer'):
            self.mass.config['base']['http_streamer'] = {}
        self.mass.config['base']['http_streamer']['__desc__'] = config_entries
        for key, def_value, desc in config_entries:
            if not key in self.mass.config['base']['http_streamer']:
                self.mass.config['base']['http_streamer'][key] = def_value
    
    async def players(self):
        ''' return all players '''
        items = list(self._players.values())
        items.sort(key=lambda x: x.name, reverse=False)
        return items

    async def player(self, player_id):
        ''' return players by id '''
        return self._players[player_id]

    async def player_command(self, player_id, cmd, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume, mute) '''
        if player_id not in self._players:
            return
        player = self._players[player_id]
        # handle some common workarounds
        if cmd in ['pause', 'play'] and cmd_args == 'toggle':
            cmd = 'pause' if player.state == PlayerState.Playing else 'play'
        if cmd == 'power' and cmd_args == 'toggle':
            cmd_args = 'off' if player.powered else 'on'
        if cmd == 'volume' and cmd_args == 'up':
            cmd_args = player.volume_level + 2
        elif cmd == 'volume' and cmd_args == 'down':
            cmd_args = player.volume_level - 2
        # redirect playlist related commands to parent player
        if player.group_parent and cmd not in ['power', 'volume', 'mute']:
            return await self.player_command(player.group_parent, cmd, cmd_args)
        # handle hass integration
        await self.__player_command_hass_integration(player, cmd, cmd_args)
        # handle mute as power
        if cmd == 'power' and player.settings['mute_as_power']:
            cmd = 'mute'
            cmd_args = 'on' if cmd_args == 'off' else 'off' # invert logic (power ON is mute OFF)
        # handle group volume for group players
        player_childs = [item for item in self._players.values() if item.group_parent == player_id]
        if player.is_group and cmd == 'volume' and player.settings['apply_group_volume']:
            return await self.__player_command_group_volume(player, player_childs, cmd_args)
        if player.is_group and cmd == 'power' and cmd_args == 'off':
            for item in player_childs:
                asyncio.create_task(self.player_command(item.player_id, cmd, cmd_args))
        # normal execution of command on player
        prov_id = self._players[player_id].player_provider
        prov = self.providers[prov_id]
        await prov.player_command(player_id, cmd, cmd_args)
        # handle play on power on
        if cmd == 'power' and cmd_args == 'on' and player.settings['play_power_on']:
            LOGGER.info('play_power_on %s' % player.name)
            await prov.player_command(player_id, 'play')

    async def __player_command_hass_integration(self, player, cmd, cmd_args):
        ''' handle hass integration in player command '''
        if not self.mass.hass:
            return
        if cmd == 'power' and player.settings.get('hass_power_entity') and player.settings.get('hass_power_entity_source'):
            cur_source = await self.mass.hass.get_state(player.settings['hass_power_entity'], attribute='source')
            if cmd_args == 'on' and not cur_source:
                service_data = { 'entity_id': player.settings['hass_power_entity'], 'source':player.settings['hass_power_entity_source'] }
                await self.mass.hass.call_service('media_player', 'select_source', service_data)
            elif cmd_args == 'off' and cur_source == player.settings['hass_power_entity_source']:
                service_data = { 'entity_id': player.settings['hass_power_entity'] }
                await self.mass.hass.call_service('media_player', 'turn_off', service_data)
            else:
                LOGGER.warning('Ignoring power command as required source is not active')
        elif cmd == 'power' and player.settings.get('hass_power_entity'):
            domain = player.settings['hass_power_entity'].split('.')[0]
            service_data = { 'entity_id': player.settings['hass_power_entity']}
            await self.mass.hass.call_service(domain, 'turn_%s' % cmd_args, service_data)
        if cmd == 'volume' and player.settings.get('hass_volume_entity'):
            service_data = { 'entity_id': player.settings['hass_power_entity'], 'volume_level': int(cmd_args)/100}
            await self.mass.hass.call_service('media_player', 'volume_set', service_data)
            cmd_args = 100 # just force full volume on actual player if volume is outsourced to hass
            
    async def __player_command_group_volume(self, player, player_childs, cmd_args):
        ''' handle group volume if needed'''
        cur_volume = player.volume_level
        new_volume = try_parse_int(cmd_args)
        volume_dif = new_volume - cur_volume
        volume_dif_percent = volume_dif/cur_volume
        for child_player in player_childs:
            if child_player.enabled and child_player.powered:
                cur_child_volume = child_player.volume_level
                new_child_volume = cur_child_volume + (cur_child_volume * volume_dif_percent)
                child_player.volume_level = new_child_volume
                await self.player_command(child_player.player_id, 'volume', new_child_volume)
        player.volume_level = new_volume
        return True

    async def remove_player(self, player_id):
        ''' handle a player remove '''
        self._players.pop(player_id, None)
        asyncio.ensure_future(self.mass.event('player removed', player_id))

    async def trigger_update(self, player_id):
        ''' manually trigger update for a player '''
        await self.update_player(self._players[player_id])
    
    async def update_player(self, player_details):
        ''' update (or add) player '''
        player_details = deepcopy(player_details)
        LOGGER.debug('Incoming msg from %s' % player_details.name)
        player_id = player_details.player_id
        player_changed = False
        if not player_id in self._players:
            # first message from player
            self._players[player_id] = MusicPlayer()
            player = self._players[player_id]
            player.player_id = player_id
            player.player_provider  = player_details.player_provider
            player_changed = True
        else:
            player = self._players[player_id]
        player.settings = await self.__get_player_settings(player_id)
        # handle basic player settings
        player_details.enabled = player.settings['enabled']
        player_details.name = player.settings['name'] if player.settings['name'] else player_details.name
        player_details.group_parent = player.settings['group_parent'] if player.settings['group_parent'] else player_details.group_parent
        # handle hass integration
        await self.__update_player_hass_settings(player_details, player.settings)
        # handle mute as power setting
        if player.settings['mute_as_power']:
            player_details.powered = not player_details.muted
        # combine state of group parent
        if player_details.group_parent and player_details.group_parent in self._players:
            parent_player = self._players[player_details.group_parent]
            player_details.cur_item_time = parent_player.cur_item_time
            player_details.cur_item = parent_player.cur_item
            player_details.state = parent_player.state
        # handle group volume/power setting
        player_childs = [item for item in self._players.values() if item.group_parent == player_id]
        player_details.is_group = len(player_childs) > 0
        if player_details.is_group and player.settings['apply_group_volume']:
            await self.__update_player_group_volume(player_details, player_childs)
        if player_details.is_group and player.settings['apply_group_power']:
            await self.__update_player_group_power(player_details, player_childs)
        # compare values to detect changes
        if player.cur_item and player_details.cur_item and player.cur_item.name != player_details.cur_item.name:
            player_changed = True
        player.cur_item = player_details.cur_item
        for key, cur_value in player.__dict__.items():
            if key != 'settings':
                new_value = getattr(player_details, key)
                if new_value != cur_value:
                    player_changed = True
                    setattr(player, key, new_value)
                    LOGGER.debug('key changed: %s for player %s - new value: %s' % (key, player.name, new_value))
        if player_changed:
            # player is added or updated!
            asyncio.ensure_future(self.mass.event('player updated', player))
            # is groupplayer, trigger update of its childs
            for child in player_childs:
                asyncio.create_task(self.trigger_update(child.player_id))
            # if child player in a group, trigger update of parent
            if player.group_parent:
                asyncio.create_task(self.trigger_update(player.group_parent))

    async def __update_player_hass_settings(self, player_details, player_settings):
        ''' handle home assistant integration on a player '''
        if not self.mass.hass:
            return
        player_id = player_details.player_id
        player_settings = self.mass.config['player_settings'][player_id]
        if player_settings.get('hass_power_entity') and player_settings.get('hass_power_entity_source'):
            hass_state = await self.mass.hass.get_state(
                    player_settings['hass_power_entity'],
                    attribute='source',
                    register_listener=functools.partial(self.trigger_update, player_id))
            player_details.powered = hass_state == player_settings['hass_power_entity_source']
        elif player_settings.get('hass_power_entity'):
            hass_state = await self.mass.hass.get_state(
                    player_settings['hass_power_entity'],
                    attribute='state',
                    register_listener=functools.partial(self.trigger_update, player_id))
            player_details.powered = hass_state != 'off'
        if player_settings.get('hass_volume_entity'):
            hass_state = await self.mass.hass.get_state(
                    player_settings['hass_volume_entity'], 
                    attribute='volume_level',
                    register_listener=functools.partial(self.trigger_update, player_id))
            player_details.volume_level = int(try_parse_float(hass_state)*100)
    
    async def __update_player_group_volume(self, player_details, player_childs):
        ''' handle group volume '''
        group_volume = 0
        active_players = 0
        for child_player in player_childs:
            if child_player.enabled and child_player.powered:
                group_volume += child_player.volume_level
                active_players += 1
        group_volume = group_volume / active_players if active_players else 0
        player_details.volume_level = group_volume
    
    async def __update_player_group_power(self, player_details, player_childs):
        ''' handle group power '''
        player_powered = False
        for child_player in player_childs:
            if child_player.powered:
                player_powered = True
                break
        if player_details.powered and not player_powered:
            # all childs turned off so turn off group player
            LOGGER.info('all childs turned off so turn off group player %s' % player_details.name)
            await self. player_command(player_details.player_id, 'power', 'off')
            player_details.powered = False
        elif not player_details.powered and player_powered:
            # all childs turned off but group player still off, so turn it on
            LOGGER.info('all childs turned off but group player still off, so turn it on %s' % player_details.name)
            await self. player_command(player_details.player_id, 'power', 'on')
            player_details.powered = True

    async def __get_player_settings(self, player_id):
        ''' get (or create) player config '''
        player_settings = self.mass.config['player_settings'].get(player_id,{})
        for key, def_value, desc in self.mass.config['player_settings']['__desc__']:
            if not key in player_settings:
                player_settings[key] = def_value
        self.mass.config['player_settings'][player_id] = player_settings
        return player_settings

    async def play_media(self, player_id, media_item, queue_opt='play'):
        ''' 
            play media on a player 
            player_id: id of the player
            media_item: media item(s) that should be played (Track, Album, Artist, Playlist)
            queue_opt: play, replace, next or add
        '''
        if not player_id in self._players:
            LOGGER.warning('Player %s not found' % player_id)
            return False
        player_prov = self.providers[self._players[player_id].player_provider]
        # a single item or list of items may be provided
        media_items = media_item if isinstance(media_item, list) else [media_item]
        playable_tracks = []
        for media_item in media_items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = await self.mass.music.artist_toptracks(media_item.item_id, provider=media_item.provider)
            elif media_item.media_type == MediaType.Album:
                tracks = await self.mass.music.album_tracks(media_item.item_id, provider=media_item.provider)
            elif media_item.media_type == MediaType.Playlist:
                tracks = await self.mass.music.playlist_tracks(media_item.item_id, provider=media_item.provider, offset=0, limit=0) 
            else:
                tracks = [media_item] # single track
            # check supported music providers by this player and work out how to handle playback...
            for track in tracks:
                # sort by quality
                match_found = False
                for prov_media in sorted(track.provider_ids, key=operator.itemgetter('quality'), reverse=True):
                    media_provider = prov_media['provider']
                    media_item_id = prov_media['item_id']
                    player_supported_provs = player_prov.supported_musicproviders
                    if media_provider in player_supported_provs and not self.mass.config['player_settings'][player_id]['force_http_streamer']:
                        # the provider can handle this media_type directly !
                        track.uri = await self.get_track_uri(media_item_id, media_provider, player_id)
                        playable_tracks.append(track)
                        match_found = True
                    elif 'http' in player_prov.supported_musicproviders:
                        # fallback to http streaming if supported
                        track.uri = await self.get_track_uri(media_item_id, media_provider, player_id, True)
                        playable_tracks.append(track)
                        match_found = True
                    if match_found:
                        break
        if playable_tracks:
            if self._players[player_id].shuffle_enabled:
                random.shuffle(playable_tracks)
            if queue_opt in ['next', 'play'] and len(playable_tracks) > 1:
                queue_opt = 'replace' # always assume playback of multiple items as new queue
            return await player_prov.play_media(player_id, playable_tracks, queue_opt)
        else:
            raise Exception("Musicprovider and/or media not supported by player %s !" % (player_id) )
    
    async def get_track_uri(self, item_id, provider, player_id, http_stream=False):
        ''' generate the URL/URI for a media item '''
        uri = ""
        if http_stream:
            params = {"provider": provider, "track_id": str(item_id), "player_id": str(player_id)}
            params_str = urllib.parse.urlencode(params)
            uri = 'http://%s:8095/stream?%s'% (self.local_ip, params_str)
        elif provider == "spotify":
            uri = 'spotify://spotify:track:%s' % item_id
        elif provider == "qobuz":
            uri = 'qobuz://%s.flac' % item_id
        elif provider == "file":
            uri = item_id
        return uri

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the items in the player's queue '''
        player = self._players[player_id]
        player_prov = self.providers[player.player_provider]
        return await player_prov.player_queue(player_id, offset=offset, limit=limit)

    async def get_audio_stream(self, track_id, provider, player_id=None):
        ''' get audio stream from provider and apply additional effects/processing where/if needed'''
        input_content_type = await self.mass.music.providers[provider].get_stream_content_type(track_id)
        cachefile = self.__get_track_cache_filename(track_id, provider)
        sox_effects = []
        if self.mass.config['base']['http_streamer']['volume_normalisation']:
            gain_correct = await self.__get_track_gain_correct(track_id, provider)
            LOGGER.info("apply gain correction of %s" % gain_correct)
            sox_effects += ['vol', '%s dB' % gain_correct]
        if player_id and self.mass.config['player_settings'][player_id]['sox_effects']:
            sox_effects += self.mass.config['player_settings'][player_id]['sox_effects'].split('/')
        if os.path.isfile(cachefile):
            # we have a cache file for this track which we can use
            args = ['-t', 'flac', cachefile, '-t', 'flac', '-', *sox_effects]
            process = await asyncio.create_subprocess_exec('sox', *args, 
                    stdout=asyncio.subprocess.PIPE)
            buffer_task = None
        else:
            # stream from provider
            args = ['-t', input_content_type, '-', '-t', 'flac', '-', *sox_effects]
            process = await asyncio.create_subprocess_exec('sox', *args, 
                    stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
            buffer_task = asyncio.create_task(
                    self.__fill_audio_buffer(process.stdin, track_id, provider, input_content_type))
        # yield the chunks from stdout
        while not process.stdout.at_eof():
            chunk = await process.stdout.read(2000000)
            if not chunk:
                break
            yield chunk
        await process.wait()
        LOGGER.info("streaming of track_id %s completed" % track_id)

    async def __analyze_audio(self, tmpfile, track_id, provider, content_type):
        ''' analyze track audio, for now we only calculate EBU R128 loudness '''
        LOGGER.info('Start analyzing file %s' % tmpfile)
        cachefile = self.__get_track_cache_filename(track_id, provider)
        # not needed to do processing if there already is a cachedfile
        bs1770_binary = self.__get_bs1770_binary()
        if bs1770_binary:
            # calculate integrated r128 loudness with bs1770
            analyse_dir = os.path.join(self.mass.datapath, 'analyse_info')
            analysis_file = os.path.join(analyse_dir, "%s_%s.xml" %(provider, track_id.split(os.sep)[-1]))
            if not os.path.isfile(analysis_file):
                if not os.path.isdir(analyse_dir):
                    os.makedirs(analyse_dir)
                cmd = '%s %s --xml --ebu -f %s' % (bs1770_binary, tmpfile, analysis_file)
                process = await asyncio.create_subprocess_shell(cmd)
                await process.wait()
            if self.mass.config['base']['http_streamer']['enable_cache']:
                # use sox to store cache file (optionally strip silence from start and end)
                if self.mass.config['base']['http_streamer']['trim_silence']:
                    cmd = 'sox -t %s %s -t flac -C5 %s silence 1 0.1 1%% reverse silence 1 0.1 1%% reverse' %(content_type, tmpfile, cachefile)
                else:
                    # cachefile is always stored as flac 
                    cmd = 'sox -t %s %s -t flac -C5 %s' %(content_type, tmpfile, cachefile)
                process = await asyncio.create_subprocess_shell(cmd)
                await process.wait()
        # always clean up temp file
        while os.path.isfile(tmpfile):
            os.remove(tmpfile)
            await asyncio.sleep(0.5)
        LOGGER.info('Fininished analyzing file %s' % tmpfile)
    
    async def __get_track_gain_correct(self, track_id, provider):
        ''' get the gain correction that should be applied to a track '''
        target_gain = int(self.mass.config['base']['http_streamer']['target_volume'])
        fallback_gain = int(self.mass.config['base']['http_streamer']['fallback_gain_correct'])
        analysis_file = os.path.join(self.mass.datapath, 'analyse_info', "%s_%s.xml" %(provider, track_id.split(os.sep)[-1]))
        if not os.path.isfile(analysis_file):
            return fallback_gain
        try: # read audio analysis if available
            tree = ET.parse(analysis_file)
            trackinfo = tree.getroot().find("album").find("track")
            track_lufs = trackinfo.find('integrated').get('lufs')
            gain_correct = target_gain - float(track_lufs)
        except Exception as exc:
            LOGGER.error('could not retrieve track gain - %s' % exc)
            gain_correct = fallback_gain # fallback value
            if os.path.isfile(analysis_file):
                os.remove(analysis_file)
                # cachefile = self.__get_track_cache_filename(track_id, provider)
                # reschedule analyze task to try again
                # asyncio.create_task(self.__analyze_track_audio(cachefile, track_id, provider))
        return round(gain_correct,2)

    async def __fill_audio_buffer(self, buf, track_id, provider, content_type):
        ''' get audio data from provider and write to buffer'''
        # fill the buffer with audio data
        # a tempfile is created so we can do audio analysis
        tmpfile = os.path.join(AUDIO_TEMP_DIR, '%s%s%s.tmp' % (random.randint(0, 999), track_id, random.randint(0, 999)))
        fd = open(tmpfile, 'wb')
        async for chunk in self.mass.music.providers[provider].get_audio_stream(track_id):
            buf.write(chunk)
            await buf.drain()
            fd.write(chunk)
        await buf.drain()
        buf.write_eof()
        fd.close()
        # successfull completion, send tmpfile to be processed in the background
        #asyncio.create_task(self.__process_audio(tmpfile, track_id, provider))
        run_async_background_task(self.mass.bg_executor, self.__analyze_audio, tmpfile, track_id, provider, content_type)
        LOGGER.info("fill_audio_buffer complete for track %s" % track_id)
        return

    @staticmethod
    def __get_track_cache_filename(track_id, provider):
        ''' get filename for a track to use as cache file '''
        return os.path.join(AUDIO_CACHE_DIR, '%s_%s' %(provider, track_id.split(os.sep)[-1]))

    @staticmethod
    def __get_bs1770_binary():
        ''' get the path to the bs1770 binary for the current OS '''
        import platform
        bs1770_binary = None
        if platform.system() == "Windows":
            bs1770_binary = os.path.join(os.path.dirname(__file__), "bs1770gain", "win64", "bs1770gain")
        elif platform.system() == "Darwin":
            # macos binary is x86_64 intel
            bs1770_binary = os.path.join(os.path.dirname(__file__), "bs1770gain", "osx", "bs1770gain")
        elif platform.system() == "Linux":
            architecture = platform.machine()
            if architecture.startswith('AMD64') or architecture.startswith('x86_64'):
                bs1770_binary = os.path.join(os.path.dirname(__file__), "bs1770gain", "linux64", "bs1770gain")
            # TODO: build armhf binary
        return bs1770_binary

    def load_providers(self):
        ''' dynamically load providers '''
        for item in os.listdir(MODULES_PATH):
            if (os.path.isfile(os.path.join(MODULES_PATH, item)) and not item.startswith("_") and 
                    item.endswith('.py') and not item.startswith('.')):
                module_name = item.replace(".py","")
                LOGGER.debug("Loading playerprovider module %s" % module_name)
                try:
                    mod = __import__("modules.playerproviders." + module_name, fromlist=[''])
                    if not self.mass.config['playerproviders'].get(module_name):
                        self.mass.config['playerproviders'][module_name] = {}
                    self.mass.config['playerproviders'][module_name]['__desc__'] = mod.config_entries()
                    for key, def_value, desc in mod.config_entries():
                        if not key in self.mass.config['playerproviders'][module_name]:
                            self.mass.config['playerproviders'][module_name][key] = def_value
                    mod = mod.setup(self.mass)
                    if mod:
                        self.providers[mod.prov_id] = mod
                        cls_name = mod.__class__.__name__
                        LOGGER.info("Successfully initialized module %s" % cls_name)
                except Exception as exc:
                    LOGGER.exception("Error loading module %s: %s" %(module_name, exc))
