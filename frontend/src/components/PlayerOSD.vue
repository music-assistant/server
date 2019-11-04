<template>
  <v-footer
    app
    fixed
    padless
    light
    elevation="10"
    style="background-color: black;"
  >
    <v-card
      dense
      flat
      light
      subheader
      tile
      width="100%"
      color="#E0E0E0"
      style="margin-top:1px;"
    >
      <!-- now playing media -->
      <v-list-item two-line>
        <v-list-item-avatar tile v-if="curQueueItem">
          <img
            :src="$server.getImageUrl(curQueueItem)"
            :lazy-src="require('../assets/file.png')"
            style="border: 1px solid rgba(0,0,0,.54);"
          />
        </v-list-item-avatar>
        <v-list-item-avatar v-else>
          <v-icon>speaker</v-icon>
        </v-list-item-avatar>

        <v-list-item-content>
          <v-list-item-title v-if="curQueueItem">
            {{ curQueueItem.name }}</v-list-item-title
          >
          <v-list-item-title v-else-if="$server.activePlayer">
            {{ $server.activePlayer.name }}</v-list-item-title
          >
          <v-list-item-subtitle v-if="curQueueItem" style="color: primary">
            <span
              v-for="(artist, artistindex) in curQueueItem.artists"
              :key="artistindex"
            >
              <a v-on:click="artistClick(artist)" @click.stop="">{{
                artist.name
              }}</a>
              <label
                v-if="artistindex + 1 < curQueueItem.artists.length"
                :key="artistindex"
              >
                /
              </label>
            </span>
          </v-list-item-subtitle>
        </v-list-item-content>
      </v-list-item>

      <!-- progress bar -->
      <div
        class="body-2"
        style="height:30px;width:100%;color:rgba(0,0,0,.65);margin-top:-12px;background-color:#E0E0E0;"
        align="center"
      >
        <div
          style="height:12px;margin-left:22px;margin-right:20px;margin-top:2px;"
          v-if="curQueueItem"
        >
          <span class="left">
            {{ playerCurTimeStr }}
          </span>
          <span class="right">
            {{ playerTotalTimeStr }}
          </span>
        </div>
      </div>
      <v-progress-linear
        fixed
        light
        :value="progress"
        v-if="curQueueItem"
        :style="
          'margin-top:-22px;margin-left:80px;width:' + progressBarWidth + 'px;'
        "
      />
    </v-card>

      <!-- Control buttons -->
      <v-list-item
        dark
        dense
        style="height:44px;margin-bottom:5px;margin-top:-4px;background-color:black;"
      >
        <v-list-item-action v-if="$server.activePlayer" style="margin-top:15px">
          <v-btn small icon @click="playerCommand('previous')">
            <v-icon>skip_previous</v-icon>
          </v-btn>
        </v-list-item-action>
        <v-list-item-action
          v-if="$server.activePlayer"
          style="margin-left:-32px;margin-top:15px"
        >
          <v-btn icon x-large @click="playerCommand('play_pause')">
            <v-icon size="50">{{
              $server.activePlayer.state == "playing" ? "pause" : "play_arrow"
            }}</v-icon>
          </v-btn>
        </v-list-item-action>
        <v-list-item-action v-if="$server.activePlayer" style="margin-top:15px">
          <v-btn icon small @click="playerCommand('next')">
            <v-icon>skip_next</v-icon>
          </v-btn>
        </v-list-item-action>
        <!-- player controls -->
        <v-list-item-content> </v-list-item-content>

        <!-- active player queue button -->
        <v-list-item-action style="padding:28px;" v-if="$server.activePlayer">
          <v-btn
            small
            text
            icon
            @click="$router.push('/playerqueue/')"
          >
            <v-flex xs12 class="vertical-btn">
              <v-icon>queue_music</v-icon>
              <span class="overline">{{ $t("queue") }}</span>
            </v-flex>
          </v-btn>
        </v-list-item-action>

        <!-- active player volume -->
        <v-list-item-action style="padding:20px;" v-if="$server.activePlayer && !$store.isMobile">
          <v-menu
            :close-on-content-click="false"
            :nudge-width="250"
            offset-x
            top
            @click.native.prevent
          >
            <template v-slot:activator="{ on }">
              <v-btn small icon v-on="on">
                <v-flex xs12 class="vertical-btn">
                  <v-icon>volume_up</v-icon>
                  <span class="overline">{{
                    Math.round($server.activePlayer.volume_level)
                  }}</span>
                </v-flex>
              </v-btn>
            </template>
            <VolumeControl
              v-bind:players="$server.players"
              v-bind:player_id="$server.activePlayer.player_id"
            />
          </v-menu>
        </v-list-item-action>

        <!-- active player btn -->
        <v-list-item-action style="padding:20px;margin-right:15px">
          <v-btn small text icon @click="$server.$emit('showPlayersMenu')">
            <v-flex xs12 class="vertical-btn">
              <v-icon>speaker</v-icon>
              <span class="overline" v-if="$server.activePlayer">{{
                $server.activePlayer.name
              }}</span>
              <span class="overline" v-else> </span>
            </v-flex>
          </v-btn>
        </v-list-item-action>
      </v-list-item>
      <!-- add some additional whitespace in standalone mode only -->
      <v-card
        dense
        flat
        light
        subheader
        tile
        width="100%"
        color="black"
        style="height:20px" v-if="$store.isInStandaloneMode"/>
  </v-footer>
</template>

<style scoped>
.vertical-btn {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.divider {
  height: 1px;
  width: 100%;
  background-color: #cccccc;
}
.right {
  float: right;
}
.left {
  float: left;
}
</style>

<script>
import Vue from 'vue'
import VolumeControl from '@/components/VolumeControl.vue'

export default Vue.extend({
  components: {
    VolumeControl
  },
  props: [],
  data () {
    return {
      curQueueItem: null
    }
  },
  watch: {
    curQueueItemId: function (val) {
      // get info for current track
      if (val == null) {
        this.curQueueItem = null
      } else {
        let endpoint = 'players/' + this.$server.activePlayerId + '/queue/' + val
        this.$server.getData(endpoint)
          .then(result => {
            this.curQueueItem = result
          })
      }
    }
  },
  computed: {
    curQueueItemId () {
      if (this.$server.activePlayer) {
        return this.$server.activePlayer.cur_queue_item
      } else {
        return null
      }
    },
    progress () {
      if (!this.curQueueItem) return 0
      var totalSecs = this.curQueueItem.duration
      var curSecs = this.$server.activePlayer.cur_time
      var curPercent = curSecs / totalSecs * 100
      return curPercent
    },
    playerCurTimeStr () {
      if (!this.curQueueItem) return '0:00'
      if (!this.$server.activePlayer.cur_time) return '0:00'
      var curSecs = this.$server.activePlayer.cur_time
      return curSecs.toString().formatDuration()
    },
    playerTotalTimeStr () {
      if (!this.curQueueItem) return '0:00'
      var totalSecs = this.curQueueItem.duration
      return totalSecs.toString().formatDuration()
    },
    progressBarWidth () {
      return window.innerWidth - 160
    }
  },
  methods: {
    playerCommand (cmd, cmd_opt = null) {
      this.$server.playerCommand(cmd, cmd_opt, this.$server.activePlayerId)
    },
    artistClick (item) {
      // artist entry clicked within the listviewItem
      var url = '/artists/' + item.item_id
      this.$router.push({ path: url, query: { provider: item.provider } })
    }
  }
})
</script>
