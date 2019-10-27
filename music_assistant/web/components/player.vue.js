Vue.component("player", {
  template: `
  <div>

    <!-- player bar in footer -->
    <v-footer app light height="auto">
      
      <v-card class="flex" tile style="background-color:#e8eaed;">
        <!-- divider -->
        <v-list-tile avatar ripple style="height:1px;background-color:#cccccc;"/>

        <!-- now playing media -->
        <v-list-tile avatar ripple>

              <v-list-tile-avatar v-if="cur_player_item" style="align-items:center;padding-top:15px;">
                  <img v-if="cur_player_item && cur_player_item.metadata && cur_player_item.metadata.image" :src="cur_player_item.metadata.image"/>
                  <img v-if="cur_player_item && !cur_player_item.metadata.image && cur_player_item.album && cur_player_item.album.metadata && cur_player_item.album.metadata.image" :src="cur_player_item.album.metadata.image"/>
              </v-list-tile-avatar>

              <v-list-tile-content style="align-items:center;padding-top:15px;">
                  <v-list-tile-title class="title">{{ cur_player_item ? cur_player_item.name : active_player.name }}</v-list-tile-title>
                  <v-list-tile-sub-title v-if="cur_player_item && cur_player_item.artists">
                      <span v-for="(artist, artistindex) in cur_player_item.artists">
                          <a v-on:click="clickItem(artist)" @click.stop="">{{ artist.name }}</a>
                          <label v-if="artistindex + 1 < cur_player_item.artists.length" :key="artistindex"> / </label>
                      </span>
                  </v-list-tile-sub-title>
              </v-list-tile-content>

          </v-list-tile>

          <!-- progress bar -->
          <div style="color:rgba(0,0,0,.65); height:30px;width:100%; vertical-align: middle; left:15px; right:0; margin-bottom:5px; margin-top:5px">
            <v-layout row style="vertical-align: middle" v-if="cur_player_item">
              <span style="text-align:left; width:60px; margin-top:7px; margin-left:15px;">{{ player_time_str_cur }}</span>
              <v-progress-linear v-model="progress"></v-progress-linear>
              <span style="text-align:right; width:60px; margin-top:7px; margin-right: 15px;">{{ player_time_str_total }}</span>
            </v-layout>
        </div>

        <!-- divider -->
        <v-list-tile avatar ripple style="height:1px;background-color:#cccccc;"/>

          <!-- Control buttons -->
          <v-list-tile light avatar ripple style="margin-bottom:5px;">
              
          <!-- player controls -->
              <v-list-tile-content>
                  <v-layout row style="content-align: left;vertical-align: middle; margin-top:10px;margin-left:-15px">
                    <v-btn small icon style="padding:5px;" @click="playerCommand('previous')"><v-icon color="rgba(0,0,0,.54)">skip_previous</v-icon></v-btn>
                    <v-btn small icon style="padding:5px;" v-if="active_player.state == 'playing'" @click="playerCommand('pause')"><v-icon size="45" color="rgba(0,0,0,.65)" style="margin-top:-9px;">pause</v-icon></v-btn>
                    <v-btn small icon style="padding:5px;" v-if="active_player.state != 'playing'" @click="playerCommand('play')"><v-icon size="45" color="rgba(0,0,0,.65)" style="margin-top:-9px;">play_arrow</v-icon></v-btn>
                    <v-btn small icon style="padding:5px;" @click="playerCommand('next')"><v-icon color="rgba(0,0,0,.54)">skip_next</v-icon></v-btn>
                  </v-layout>
              </v-list-tile-content>

              <!-- active player queue button -->
              <v-list-tile-action style="padding:20px;" v-if="active_player_id">
                  <v-btn x-small flat icon @click="$router.push('/queue/' + active_player_id)">
                      <v-flex xs12 class="vertical-btn">
                      <v-icon>queue_music</v-icon>
                      <span class="caption">{{ $t('queue') }}</span>
                    </v-flex>    
                  </v-btn>
              </v-list-tile-action> 

              <!-- active player volume -->
              <v-list-tile-action style="padding:20px;" v-if="active_player_id">
                  <v-menu :close-on-content-click="false" :nudge-width="250" offset-x top>
                    <template v-slot:activator="{ on }">
                        <v-btn x-small flat icon v-on="on">
                            <v-flex xs12 class="vertical-btn">
                            <v-icon>volume_up</v-icon>
                            <span class="caption">{{ Math.round(players[active_player_id].volume_level) }}</span>
                          </v-flex>    
                        </v-btn>
                    </template>
                    <volumecontrol v-bind:players="players" v-bind:player_id="active_player_id" v-on:setPlayerVolume="setPlayerVolume" v-on:togglePlayerPower="togglePlayerPower"/>
                  </v-menu>
              </v-list-tile-action> 

              <!-- active player btn -->
              <v-list-tile-action style="padding:30px;margin-right:-13px;">
                  <v-btn x-small flat icon @click="menu = !menu;createAudioPlayer();">
                      <v-flex xs12 class="vertical-btn">
                      <v-icon>speaker</v-icon>
                      <span class="caption">{{ active_player_id ? players[active_player_id].name : '' }}</span>
                    </v-flex>    
                  </v-btn>
              </v-list-tile-action>
          </v-list-tile>

          <!-- add some additional whitespace in standalone mode only -->
          <v-list-tile avatar ripple style="height:14px" v-if="isInStandaloneMode()"/>

      </v-card>
    </v-footer>

    <!-- players side menu -->
    <v-navigation-drawer right app clipped temporary v-model="menu">
        <v-card-title class="headline">
            <b>{{ $t('players') }}</b>
        </v-card-title>
        <v-list two-line>
            <v-divider></v-divider>
            <div v-for="(player, player_id, index) in players" :key="player_id" v-if="player.enabled && player.group_parents.length == 0">
              <v-list-tile avatar ripple style="margin-left: -5px; margin-right: -15px" @click="switchPlayer(player.player_id)" :style="active_player_id == player.player_id ? 'background-color: rgba(50, 115, 220, 0.3);' : ''">
                  <v-list-tile-avatar>
                      <v-icon size="45">{{ player.is_group ? 'speaker_group' : 'speaker' }}</v-icon>
                  </v-list-tile-avatar>
                  <v-list-tile-content>
                      <v-list-tile-title class="title">{{ player.name }}</v-list-tile-title>

                      <v-list-tile-sub-title v-if="cur_player_item" class="body-1" :key="player.state">
                          {{ $t('state.' + player.state) }}
                      </v-list-tile-sub-title>

                  </v-list-tile-content>

                  <v-list-tile-action style="padding:30px;" v-if="active_player_id">
                      <v-menu :close-on-content-click="false" :nudge-width="250" offset-x right>
                        <template v-slot:activator="{ on }">
                            <v-btn flat icon style="color:rgba(0,0,0,.54);" v-on="on">
                                <v-flex xs12 class="vertical-btn">
                                <v-icon>volume_up</v-icon>
                                <span class="caption">{{ Math.round(player.volume_level) }}</span>
                              </v-flex>    
                            </v-btn>
                        </template>
                        <volumecontrol v-bind:players="players" v-bind:player_id="player.player_id" v-on:setPlayerVolume="setPlayerVolume" v-on:togglePlayerPower="togglePlayerPower"/>
                      </v-menu>
                  </v-list-tile-action> 
              </v-list-tile>
            <v-divider></v-divider>
            </div>
        </v-list>
    </v-navigation-drawer>
    <contextmenu v-model="$globals.showcontextmenu" v-on:playItem="playItem" :active_player="active_player" />
  </div>
  
  `,
  props: [],
  $_veeValidate: {
    validator: "new"
  },
  watch: {
    cur_queue_item: function (val) {
      // get info for current track
      if (!val)
        this.cur_player_item = null;
      else {
        const api_url = this.$globals.server + 'api/players/' + this.active_player_id + '/queue/' + val;
        axios
          .get(api_url)
          .then(result => {
            if (result.data)
              this.cur_player_item = result.data;
          })
          .catch(error => {
            console.log("error", error);
          });
      }
    }
  },
  data() {
    return {
      menu: false,
      players: {},
      active_player_id: "",
      ws: null,
      file: "",
      audioPlayer: null,
      audioPlayerId: '',
      audioPlayerName: '',
      cur_player_item: null
    }
  },
  mounted() { 
    
  },
  created() {
    // connect the websocket
    this.connectWS();
  },
  computed: {
    cur_queue_item() {
      if (this.active_player)
        return this.active_player.cur_queue_item;
      else
        return null;
    },
    active_player() {
      if (this.players && this.active_player_id && this.active_player_id in this.players)
          return this.players[this.active_player_id];
      else
          return {
            name: this.$t('no_player'),
            cur_item: null,
            cur_time: 0,
            player_id: '',
            volume_level: 0,
            state: 'stopped'
          };
    },
    progress() {
      if (!this.cur_player_item)
        return 0;
      var total_sec = this.cur_player_item.duration;
      var cur_sec = this.active_player.cur_time;
      var cur_percent = cur_sec/total_sec*100;
      return cur_percent;
    },
    player_time_str_cur() {
      if (!this.cur_player_item || !this.active_player.cur_time)
        return "0:00";
      var cur_sec = this.active_player.cur_time;
      return cur_sec.toString().formatDuration();
    },
    player_time_str_total() {
      if (!this.cur_player_item)
        return "0:00";
      var total_sec = this.cur_player_item.duration;
      return total_sec.toString().formatDuration();
    }
  },
  methods: { 
    playerCommand (cmd, cmd_opt=null, player_id=this.active_player_id) {
      let msg_details = {
        player_id: player_id,
        cmd: cmd,
        cmd_args: cmd_opt
      }
      this.ws.send(JSON.stringify({message:'player command', message_details: msg_details}));
    },
    playItem(item, queueopt) {
      this.$globals.loading = true;
      var api_url = 'api/players/' + this.active_player_id + '/play_media/' + item.media_type + '/' + item.item_id + '/' + queueopt;
      axios
      .get(api_url, {
        params: {
          provider: item.provider
        }
      })
      .then(result => {
        this.$globals.loading = false;
      })
      .catch(error => {
        this.$globals.loading = false;
      });
    },
    switchPlayer (new_player_id) {
      this.active_player_id = new_player_id;
      localStorage.setItem('active_player_id', new_player_id);
    },
    setPlayerVolume: function(player_id, new_volume) {
      this.players[player_id].volume_level = new_volume;
      if (new_volume == 'up')
        this.playerCommand('volume_up', null, player_id);
      else if (new_volume == 'down')
        this.playerCommand('volume_down', null, player_id);
      else
        this.playerCommand('volume_set', new_volume, player_id);
    },
    togglePlayerPower: function(player_id) {
      if (this.players[player_id].powered)
        this.playerCommand('power_off', null, player_id);
      else
        this.playerCommand('power_on', null, player_id);
    },
    handleAudioPlayerCommand(data) {
      /// we received a command for our built-in audio player
      if (data.cmd == 'play')
        this.audioPlayer.play();
      else if (data.cmd == 'pause')
        this.audioPlayer.pause();
      else if (data.cmd == 'stop')
        {
          console.log('stop called');
          this.audioPlayer.pause();
          this.audioPlayer = new Audio();
          let msg_details = {
            player_id: this.audioPlayerId,
            state: 'stopped'
          }
          this.ws.send(JSON.stringify({message:'webplayer state', message_details: msg_details}));
        }
      else if (data.cmd == 'volume_set')
        this.audioPlayer.volume = data.volume_level/100;
      else if (data.cmd == 'volume_mute')
        this.audioPlayer.mute = data.is_muted;
      else if (data.cmd == 'play_uri')
        {
          this.audioPlayer.src = data.uri;
          this.audioPlayer.load();
        }
    },
    createAudioPlayer(data) {
      if (!navigator.userAgent.includes("Chrome"))
        return // streaming flac only supported on chrome browser
      if (localStorage.getItem('audio_player_id'))
        // get player id from local storage
        this.audioPlayerId = localStorage.getItem('audio_player_id');
      else
      {
        // generate a new (randomized) player id
        this.audioPlayerId = (Date.now().toString(36) + Math.random().toString(36).substr(2, 5)).toUpperCase();
        localStorage.setItem('audio_player_id', this.audioPlayerId);
      }
      this.audioPlayerName = 'Webplayer ' + this.audioPlayerId.substring(1, 4);
      this.audioPlayer = new Audio();
      this.audioPlayer.autoplay = false;
      this.audioPlayer.preload = 'none';
      let msg_details = {
        player_id: this.audioPlayerId,
        name: this.audioPlayerName,
        state: 'stopped',
        powered: true,
        volume_level: this.audioPlayer.volume * 100,
        muted: this.audioPlayer.muted,
        cur_uri: this.audioPlayer.src
      }
      // register the player on the server
      this.ws.send(JSON.stringify({message:'webplayer register', message_details: msg_details}));
      // add event handlers
      this.audioPlayer.addEventListener("canplaythrough", event => {
        /* the audio is now playable; play it if permissions allow */
        this.audioPlayer.play();
      });
      const timeupdateHandler = (event) => {
        // currenTime of player updated, sent state (throttled at 1 sec)
        msg_details['cur_time'] = Math.round(this.audioPlayer.currentTime);
        this.ws.send(JSON.stringify({message:'webplayer state', message_details: msg_details}));
      }
      const throttledTimeUpdateHandler = this.throttle(timeupdateHandler, 1000);
      this.audioPlayer.addEventListener("timeupdate",throttledTimeUpdateHandler);

      this.audioPlayer.addEventListener("volumechange", event => {
        /* the audio is now playable; play it if permissions allow */
        console.log('volume: ' + this.audioPlayer.volume);
        msg_details['volume_level'] = this.audioPlayer.volume*100;
        msg_details['muted'] = this.audioPlayer.muted;
        this.ws.send(JSON.stringify({message:'webplayer state', message_details: msg_details}));
      });
      this.audioPlayer.addEventListener("playing", event => {
        msg_details['state'] = 'playing';
        this.ws.send(JSON.stringify({message:'webplayer state', message_details: msg_details}));
      });
      this.audioPlayer.addEventListener("pause", event => {
        msg_details['state'] = 'paused';
        this.ws.send(JSON.stringify({message:'webplayer state', message_details: msg_details}));
      });
      this.audioPlayer.addEventListener("ended", event => {
        msg_details['state'] = 'stopped';
        this.ws.send(JSON.stringify({message:'webplayer state', message_details: msg_details}));
      });
      const heartbeatMessage = (event) => {
        // heartbeat message
        this.ws.send(JSON.stringify({message:'webplayer state', message_details: msg_details}));
      }
      setInterval(heartbeatMessage, 5000);

    },
    connectWS() {
      this.ws = new WebSocket(this.$globals.wsAddress);

      this.ws.onopen = function() {
        console.log('websocket connected! ' + this.$globals.wsAddress);
        this.createAudioPlayer();
        data = JSON.stringify({message:'players', message_details: null});
        this.ws.send(data);
      }.bind(this);
    
      this.ws.onmessage = function(e) {
        var msg = JSON.parse(e.data);
        if (msg.message == 'player changed' || msg.message == 'player added')
          {
            Vue.set(this.players, msg.message_details.player_id, msg.message_details);
        }
        else if (msg.message == 'player removed') {
          Vue.delete(this.players, msg.message_details.player_id)
        }
        else if (msg.message == 'players') {
          for (var item of msg.message_details) {
              Vue.set(this.players, item.player_id, item);
          }
        }
        else if (msg.message == 'webplayer command' && msg.message_details.player_id == this.audioPlayerId) {
          // message for our audio player
          this.handleAudioPlayerCommand(msg.message_details);
        }

        // select new active player
        if (!this.active_player_id || !this.players[this.active_player_id].enabled) {
          // prefer last selected player
          last_player = localStorage.getItem('active_player_id')
          if (last_player && this.players[last_player] && this.players[last_player].enabled)
            this.active_player_id = last_player;
          else
          {
            // prefer the first playing player
            for (var player_id in this.players)
              if (this.players[player_id].state == 'playing' && this.players[player_id].enabled && this.players[player_id].group_parents.length == 0) {
                this.active_player_id = player_id;
                break; 
              }
              // fallback to just the first player
              if (!this.active_player_id || !this.players[this.active_player_id].enabled)
                for (var player_id in this.players) {
                  if (this.players[player_id].enabled && this.players[player_id].group_parents.length == 0)
                  {
                    this.active_player_id = player_id;
                    break; 
                  }
                }
          }
        }
      }.bind(this);
    
      this.ws.onclose = function(e) {
        console.log('Socket is closed. Reconnect will be attempted in 5 seconds.', e.reason);
        setTimeout(function() {
          this.connectWS();
        }.bind(this), 5000);
      }.bind(this);
    
      this.ws.onerror = function(err) {
        console.error('Socket encountered error: ', err.message, 'Closing socket');
        this.ws.close();
      }.bind(this);
    },
    throttle (callback, limit) {
      var wait = false;
      return function () {
          if (!wait) {
          callback.apply(null, arguments);
          wait = true;
          setTimeout(function () {
              wait = false;
          }, limit);
          }
      }
  }
  }
})
