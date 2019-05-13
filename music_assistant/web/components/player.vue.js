Vue.component("player", {
  template: `
  <div>

    <!-- player bar in footer -->
    <v-footer app light height="auto">
      
      <v-card class="flex" tile style="background-color:#e8eaed;">
        <v-list-tile avatar ripple style="margin-bottom:15px;">

              <v-list-tile-avatar v-if="!isMobile() && active_player.cur_item" style="align-items:center;padding-top:15px;">
                  <img v-if="active_player.cur_item.metadata && active_player.cur_item.metadata.image" :src="active_player.cur_item.metadata.image"/>
                  <img v-if="!active_player.cur_item.metadata.image && active_player.cur_item.album && active_player.cur_item.album.metadata && active_player.cur_item.album.metadata.image" :src="active_player.cur_item.album.metadata.image"/>
              </v-list-tile-avatar>

              <v-list-tile-content v-if="!isMobile()" style="align-items:center;padding-top:15px;">
                  <v-list-tile-title class="title">{{ active_player.cur_item ? active_player.cur_item.name : active_player.name }}</v-list-tile-title>
                  <v-list-tile-sub-title v-if="active_player.cur_item && active_player.cur_item.artists">
                      <span v-for="(artist, artistindex) in active_player.cur_item.artists">
                          <a v-on:click="clickItem(artist)" @click.stop="">{{ artist.name }}</a>
                          <label v-if="artistindex + 1 < active_player.cur_item.artists.length" :key="artistindex"> / </label>
                      </span>
                  </v-list-tile-sub-title>
              </v-list-tile-content>


              <!-- player controls -->
              <v-list-tile-content>
                  <v-layout row style="content-align: center;vertical-align: middle; margin-top:10px;">
                    <v-btn icon style="padding:5px;" @click="playerCommand('previous')"><v-icon color="rgba(0,0,0,.54)">skip_previous</v-icon></v-btn>
                    <v-btn icon style="padding:5px;" v-if="active_player.state == 'playing'" @click="playerCommand('pause')"><v-icon size="45" color="rgba(0,0,0,.65)" style="margin-top:-9px;">pause</v-icon></v-btn>
                    <v-btn icon style="padding:5px;" v-if="active_player.state != 'playing'" @click="playerCommand('play')"><v-icon size="45" color="rgba(0,0,0,.65)" style="margin-top:-9px;">play_arrow</v-icon></v-btn>
                    <v-btn icon style="padding:5px;" @click="playerCommand('next')"><v-icon color="rgba(0,0,0,.54)">skip_next</v-icon></v-btn>
                  </v-layout>
              </v-list-tile-content>

              <!-- active player queue button -->
              <v-list-tile-action style="padding:30px;" v-if="!isMobile() && active_player_id">
                  <v-btn flat icon @click="$router.push('/queue/' + active_player_id)">
                      <v-flex xs12 class="vertical-btn">
                      <v-icon large>queue_music</v-icon>
                      <span class="caption">Queue</span>
                    </v-flex>    
                  </v-btn>
              </v-list-tile-action> 

              <!-- active player volume -->
              <v-list-tile-action style="padding:30px;" v-if="active_player_id">
                  <v-menu :close-on-content-click="false" :nudge-width="250" offset-x top>
                    <template v-slot:activator="{ on }">
                        <v-btn flat icon v-on="on">
                            <v-flex xs12 class="vertical-btn">
                            <v-icon large>volume_up</v-icon>
                            <span class="caption">{{ Math.round(players[active_player_id].volume_level) }}</span>
                          </v-flex>    
                        </v-btn>
                    </template>
                    <volumecontrol v-bind:players="players" v-bind:player_id="active_player_id" v-on:setPlayerVolume="setPlayerVolume" v-on:togglePlayerPower="togglePlayerPower"/>
                  </v-menu>
              </v-list-tile-action> 

              <!-- active player btn -->
              <v-list-tile-action style="padding:30px;margin-right:-13px;">
                  <v-btn flat icon @click="menu = !menu">
                      <v-flex xs12 class="vertical-btn">
                      <v-icon large>speaker</v-icon>
                      <span class="caption">{{ active_player_id ? players[active_player_id].name : '' }}</span>
                    </v-flex>    
                  </v-btn>
              </v-list-tile-action>
          </v-list-tile>

          <!-- progress bar -->
          <div style="color:rgba(0,0,0,.65); height:35px;width:100%; vertical-align: middle; left:15px; right:0; bottom:0" v-if="!isMobile()">
            <v-layout row style="vertical-align: middle">
              <span style="text-align:left; width:60px; margin-top:7px; margin-left:15px;">{{ player_time_str_cur }}</span>
              <v-progress-linear v-model="progress"></v-progress-linear>
              <span style="text-align:right; width:60px; margin-top:7px; margin-right: 15px;">{{ player_time_str_total }}</span>
            </v-layout>
        </div>

      </v-card>
    </v-footer>

    <!-- players side menu -->
    <v-navigation-drawer right app clipped temporary v-model="menu">
        <v-card-title class="headline">
            <b>Players</b>
        </v-card-title>
        <v-list two-line>
            <v-divider></v-divider>
            <div v-for="(player, player_id, index) in players" :key="player_id" v-if="player.enabled && !player.group_parent">
              <v-list-tile avatar ripple style="margin-left: -5px; margin-right: -15px" @click="switchPlayer(player.player_id)" :style="active_player_id == player.player_id ? 'background-color: rgba(50, 115, 220, 0.3);' : ''">
                  <v-list-tile-avatar>
                      <v-icon size="45">{{ isGroup(player.player_id) ? 'speaker_group' : 'speaker' }}</v-icon>
                  </v-list-tile-avatar>
                  <v-list-tile-content>
                      <v-list-tile-title class="title">{{ player.name }}</v-list-tile-title>

                      <v-list-tile-sub-title v-if="player.cur_item" class="body-1" :key="player.state">
                          {{ player.state }}
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
    <playmenu v-model="$globals.showplaymenu" v-on:playItem="playItem"/>
  </div>
  
  `,
  props: [],
  $_veeValidate: {
    validator: "new"
  },
  watch: {},
  data() {
    return {
      menu: false,
      players: {},
      active_player_id: "",
      ws: null
    }
  },
  mounted() { },
  created() {
    this.connectWS();
    this.updateProgress();
  },
  computed: {

    active_player() {
      if (this.players && this.active_player_id && this.active_player_id in this.players)
          return this.players[this.active_player_id];
      else
          return {
            name: 'no player selected',
            cur_item: null,
            cur_item_time: 0,
            player_id: '',
            volume_level: 0,
            state: 'stopped'
          };
    },
    progress() {
      if (!this.active_player.cur_item)
        return 0;
      var total_sec = this.active_player.cur_item.duration;
      var cur_sec = this.active_player.cur_item_time;
      var cur_percent = cur_sec/total_sec*100;
      return cur_percent;
    },
    player_time_str_cur() {
      if (!this.active_player.cur_item || !this.active_player.cur_item_time)
        return "0:00";
      var cur_sec = this.active_player.cur_item_time;
      return cur_sec.toString().formatDuration();
    },
    player_time_str_total() {
      if (!this.active_player.cur_item)
        return "0:00";
      var total_sec = this.active_player.cur_item.duration;
      return total_sec.toString().formatDuration();
    }
  },
  methods: { 
    playerCommand (cmd, cmd_opt=null, player_id=this.active_player_id) {
      if (cmd_opt)
        cmd = cmd + '/' + cmd_opt
      cmd = 'players/' + player_id + '/cmd/' + cmd;
      this.ws.send(cmd);
    },
    playItem(item, queueopt) {
      console.log('playItem: ' + item);
      var api_url = 'api/players/' + this.active_player_id + '/play_media/' + item.media_type + '/' + item.item_id + '/' + queueopt;
      axios
      .get(api_url, {
        params: {
          provider: item.provider
        }
      })
      .then(result => {
        console.log(result.data);
      })
      .catch(error => {
        console.log("error", error);
      });
    },
    switchPlayer (new_player_id) {
      this.active_player_id = new_player_id;
    },
    isGroup(player_id) {
			for (var item in this.players)
				if (this.players[item].group_parent == player_id && this.players[item].enabled)
					return true;
			return false;
    },
    updateProgress: function(){           
      this.intervalid2 = setInterval(function(){
          if (this.active_player.state == 'playing')
              this.active_player.cur_item_time +=1;
      }.bind(this), 1000);
    },
    setPlayerVolume: function(player_id, new_volume) {
      this.players[player_id].volume_level = new_volume;
      this.playerCommand('volume', new_volume, player_id);
    },
    togglePlayerPower: function(player_id) {
      if (this.players[player_id].powered)
        this.playerCommand('power', 'off', player_id);
      else
        this.playerCommand('power', 'on', player_id);
    },
    connectWS() {
      var loc = window.location, new_uri;
      if (loc.protocol === "https:") {
          new_uri = "wss:";
      } else {
          new_uri = "ws:";
      }
      new_uri += "/" + loc.host;
      new_uri += loc.pathname + "ws";
      this.ws = new WebSocket(new_uri);

      this.ws.onopen = function() {
        console.log('websocket connected!');
        this.ws.send('players');
      }.bind(this);
    
      this.ws.onmessage = function(e) {
        var msg = JSON.parse(e.data);
        var players = [];
        if (msg.message == 'player updated')
          players = [msg.message_details];
        else if (msg.message == 'players')
          players = msg.message_details;
        
        for (var item of players)
          if (item.player_id in this.players)
              this.players[item.player_id] = Object.assign({}, this.players[item.player_id], item);
          else
            this.$set(this.players, item.player_id, item)

        // select new active player
        // TODO: store previous player in local storage
        if (!this.active_player_id)
          for (var player_id in this.players)
            if (this.players[player_id].state == 'playing' && this.players[player_id].enabled) {
              // prefer the first playing player
              this.active_player_id = player_id;
              break; 
            }
        if (!this.active_player_id)
          for (var player_id in this.players) {
            // fallback to just the first player
            if (this.players[player_id].enabled)
            {
              this.active_player_id = player_id;
              break; 
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
    }
  }
})
