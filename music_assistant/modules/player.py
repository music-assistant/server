#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import run_periodic, LOGGER, try_parse_int, try_parse_float, get_ip, run_async_background_task
from models import MediaType, PlayerState, MusicPlayer, TrackQuality
import operator
import random
from copy import deepcopy
import functools
import urllib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "playerproviders" )


class Player():
    ''' several helpers to handle playback through player providers '''
    
    def __init__(self, mass):
        self.mass = mass
        self.providers = {}
        self._players = {}
        self.local_ip = get_ip()
        # dynamically load provider modules
        self.load_providers()
    
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
        if cmd == 'volume' and (cmd_args == 'up' or '+' in cmd_args):
            cmd_args = player.volume_level + 2
        elif cmd == 'volume' and (cmd_args == 'down' or '-' in cmd_args):
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
        # normal execution of command on player
        prov_id = self._players[player_id].player_provider
        prov = self.providers[prov_id]
        await prov.player_command(player_id, cmd, cmd_args)
        # handle play on power on
        if cmd == 'power' and cmd_args == 'on' and player.settings['play_power_on']:
            await prov.player_command(player_id, 'play')
        # handle group volume/power for group players
        await self.__player_group_commands(player, cmd, cmd_args)

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

    async def __player_group_commands(self, player, cmd, cmd_args):
        ''' handle group power/volume commands '''
        if player.is_group:
            player_childs = [item for item in self._players.values() if item.group_parent == player.player_id]
            if player.settings['apply_group_volume'] and cmd == 'volume':
                return await self.__player_command_group_volume(player, player_childs, cmd_args)
            if player.settings['apply_group_power'] and cmd == 'power' and cmd_args == 'off':
                for item in player_childs:
                    if item.powered:
                        await self.player_command(item.player_id, cmd, cmd_args)
        elif player.group_parent and player.group_parent in self._players:
            group_player = self._players[player.group_parent]
            if group_player.settings['apply_group_power']:
                player_childs = [item for item in self._players.values() if item.group_parent == group_player.player_id]
                if not group_player.powered and cmd == 'power' and cmd_args == 'on':
                    # power on group player
                    await self.player_command(group_player.player_id, 'power', 'on')
                elif group_player.powered and cmd == 'power' and cmd_args == 'off':
                    # check if the group player should still be turned on
                    new_powered = False
                    for child_player in player_childs:
                        if child_player.player_id != player.player_id and child_player.powered:
                            new_powered = True
                            break
                    if not new_powered:
                        await self.player_command(group_player.player_id, 'power', 'off')
    
    async def remove_player(self, player_id):
        ''' handle a player remove '''
        self._players.pop(player_id, None)
        asyncio.ensure_future(self.mass.event('player removed', player_id))

    async def trigger_update(self, player_id):
        ''' manually trigger update for a player '''
        if player_id in self._players:
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
        player.settings = await self.__get_player_settings(player_details)
        # handle basic player settings
        player_details.enabled = player.settings['enabled']
        player_details.name = player.settings['name'] if player.settings['name'] else player_details.name
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
        if player_details.is_group:
            player_childs = [item for item in self._players.values() if item.group_parent == player_id]
            if player.settings['apply_group_volume']:
                player_details.volume_level = await self.__get_group_volume(player_childs)
            if player.settings['apply_group_power']:
                player_details.powered = await self.__get_group_power(player_childs)
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
            # for child in player_childs:
            #     asyncio.create_task(self.trigger_update(child.player_id))
            # # if child player in a group, trigger update of parent
            # if player.group_parent:
            #     asyncio.create_task(self.trigger_update(player.group_parent))

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
    
    async def __get_group_volume(self, player_childs):
        ''' handle group volume '''
        group_volume = 0
        active_players = 0
        for child_player in player_childs:
            if child_player.enabled and child_player.powered:
                group_volume += child_player.volume_level
                active_players += 1
        group_volume = group_volume / active_players if active_players else 0
        return group_volume

    async def __get_group_power(self, player_childs):
        ''' handle group volume '''
        group_power = False
        for child_player in player_childs:
            if child_player.enabled and child_player.powered:
                group_power = True
                break
        return group_power
    
    async def __get_player_settings(self, player_details):
        ''' get (or create) player config '''
        player_id = player_details.player_id
        config_entries = [ # default config entries for a player
            ("enabled", False, "player_enabled"),
            ("name", "", "player_name"),
            ("mute_as_power", False, "player_mute_power"),
            ("disable_volume", False, "player_disable_vol"),
            ("sox_effects", '', "http_streamer_sox_effects"),
            ("max_sample_rate", '96000', "max_sample_rate"),
            ("force_http_streamer", False, "force_http_streamer")
        ]
        if player_details.is_group:
            config_entries += [ # group player settings
                ("apply_group_volume", False, "player_group_vol"),
                ("apply_group_power", False, "player_group_pow")
            ]
        if player_details.is_group or not player_details.group_parent:
            config_entries += [ # play on power on setting
                ("play_power_on", False, "player_power_play"),
            ]
        if self.mass.config['base'].get('homeassistant',{}).get("enabled"):
            # append hass specific config entries
            config_entries += [("hass_power_entity", "", "hass_player_power"),
                            ("hass_power_entity_source", "", "hass_player_source"),
                            ("hass_volume_entity", "", "hass_player_volume")]
        player_settings = self.mass.config['player_settings'].get(player_id,{})
        for key, def_value, desc in config_entries:
            if not key in player_settings:
                if (isinstance(def_value, str) and def_value.startswith('<')):
                    player_settings[key] = None
                else:
                    player_settings[key] = def_value
        self.mass.config['player_settings'][player_id] = player_settings
        self.mass.config['player_settings'][player_id]['__desc__'] = config_entries
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
            uri = 'http://%s:%s/stream?%s'% (self.local_ip, self.mass.config['base']['web']['http_port'], params_str)
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
