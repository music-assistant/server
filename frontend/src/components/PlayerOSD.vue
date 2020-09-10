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
      v-if="!$store.isMobile"
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
         <!-- streaming quality details -->
        <v-list-item-action v-if="streamDetails">
          <v-menu
            :close-on-content-click="false"
            :nudge-width="250"
            offset-x
            top
            @click.native.prevent
          >
            <template v-slot:activator="{ on }">
              <v-btn icon v-on="on">
              <v-img contain v-if="streamDetails.quality > 6" :src="require('../assets/hires.png')" height="30" />
              <v-img contain v-if="streamDetails.quality <= 6" :src="streamDetails.content_type ? require('../assets/' + streamDetails.content_type + '.png') : ''" height="30" style='filter: invert(100%);' />
              </v-btn>
            </template>
            <v-list v-if="streamDetails">
              <v-subheader class="title">{{ $t('stream_details') }}</v-subheader>
                <v-list-item tile dense>
                  <v-list-item-icon>
                    <v-img max-width="50" contain :src="streamDetails.provider ? require('../assets/' + streamDetails.provider + '.png') : ''" />
                  </v-list-item-icon>
                  <v-list-item-content>
                    <v-list-item-title>{{ streamDetails.provider }}</v-list-item-title>
                  </v-list-item-content>
                </v-list-item>
                <v-divider></v-divider>
                <v-list-item tile dense>
                  <v-list-item-icon>
                    <v-img max-width="50" contain :src="streamDetails.content_type ? require('../assets/' + streamDetails.content_type + '.png') : ''" style='filter: invert(100%);' />
                  </v-list-item-icon>
                  <v-list-item-content>
                    <v-list-item-title>{{ streamDetails.sample_rate/1000 }} kHz / {{ streamDetails.bit_depth }} bits </v-list-item-title>
                  </v-list-item-content>
                </v-list-item>
                <v-divider></v-divider>
                <div v-if="playerQueueDetails.crossfade_enabled">
                  <v-list-item tile dense>
                  <v-list-item-icon>
                    <v-img max-width="50" contain :src="require('../assets/crossfade.png')"/>
                  </v-list-item-icon>
                  <v-list-item-content>
                    <v-list-item-title>{{ $t('crossfade_enabled') }}</v-list-item-title>
                  </v-list-item-content>
                </v-list-item>
                <v-divider></v-divider>
                </div>
                <div v-if="streamVolumeLevelAdjustment">
                  <v-list-item tile dense>
                  <v-list-item-icon>
                    <v-icon color="black" style="margin-left:13px">volume_up</v-icon>
                  </v-list-item-icon>
                  <v-list-item-content>
                    <v-list-item-title style="margin-left:12px">{{ streamVolumeLevelAdjustment }}</v-list-item-title>
                  </v-list-item-content>
                </v-list-item>
                <v-divider></v-divider>
                </div>
            </v-list>
          </v-menu>
        </v-list-item-action>
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
        style="height:62px;margin-bottom:5px;margin-top:-4px;background-color:black;"
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
        <v-list-item-action style="padding:16px;" v-if="$server.activePlayer">
          <v-btn
            text
            icon
            @click="$router.push('/playerqueue/')"
          >
            <v-flex xs12 class="vertical-btn">
              <v-icon>queue_music</v-icon>
              <span class="caption" style="padding-top: 5px">{{ $t("queue") }}</span>
            </v-flex>
          </v-btn>
        </v-list-item-action>

        <!-- active player volume -->
        <v-list-item-action style="padding:16px;" v-if="$server.activePlayer && !$store.isMobile">
          <v-menu
            :close-on-content-click="false"
            :nudge-width="250"
            offset-x
            top
            @click.native.prevent
          >
            <template v-slot:activator="{ on }">
              <v-btn icon v-on="on">
                <v-flex xs12 class="vertical-btn">
                  <v-icon>volume_up</v-icon>
                  <span class="caption" style="padding-top: 5px">{{
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
        <v-list-item-action style="padding:15px;margin-right:15px">
          <v-btn text icon @click="$server.$emit('showPlayersMenu')">
            <v-flex xs12 class="vertical-btn">
              <v-icon>speaker</v-icon>
              <span class="caption" v-if="$server.activePlayer" style="padding-top: 5px">{{
                truncateString($server.activePlayer.name, 12)
              }}</span>
              <span class="caption" v-else> </span>
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
      playerQueueDetails: {}
    }
  },
  watch: { },
  computed: {
    curQueueItem () {
      if (this.playerQueueDetails) {
        return this.playerQueueDetails.cur_item
      } else {
        return null
      }
    },
    progress () {
      if (!this.curQueueItem) return 0
      var totalSecs = this.curQueueItem.duration
      var curSecs = this.playerQueueDetails.cur_item_time
      var curPercent = curSecs / totalSecs * 100
      return curPercent
    },
    playerCurTimeStr () {
      if (!this.curQueueItem) return '0:00'
      var curSecs = this.playerQueueDetails.cur_item_time
      return curSecs.toString().formatDuration()
    },
    playerTotalTimeStr () {
      if (!this.curQueueItem) return '0:00'
      var totalSecs = this.curQueueItem.duration
      return totalSecs.toString().formatDuration()
    },
    progressBarWidth () {
      return window.innerWidth - 160
    },
    streamDetails () {
      if (!this.playerQueueDetails.cur_item || !this.playerQueueDetails.cur_item || !this.playerQueueDetails.cur_item.streamdetails || !this.playerQueueDetails.cur_item.streamdetails.provider || !this.playerQueueDetails.cur_item.streamdetails.content_type) return {}
      return this.playerQueueDetails.cur_item.streamdetails
    },
    streamVolumeLevelAdjustment () {
      if (!this.streamDetails || !this.streamDetails.sox_options) return ''
      if (this.streamDetails.sox_options.includes('vol ')) {
        var re = /(.*vol\s+)(.*)(\s+dB.*)/
        var volLevel = this.streamDetails.sox_options.replace(re, '$2')
        return volLevel + ' dB'
      }
      return ''
    }
  },
  created () {
    this.$server.$on('queue updated', this.queueUpdatedMsg)
    this.$server.$on('new player selected', this.getQueueDetails)
  },
  methods: {
    playerCommand (cmd, cmd_opt = null) {
      this.$server.playerCommand(cmd, cmd_opt, this.$server.activePlayerId)
    },
    artistClick (item) {
      // artist entry clicked within the listviewItem
      var url = '/artists/' + item.item_id
      this.$router.push({ path: url, query: { provider: item.provider } })
    },
    queueUpdatedMsg (data) {
      const queueId = this.$server.players[this.$server.activePlayerId].active_queue
      if (data.player_id === queueId) {
        for (const [key, value] of Object.entries(data)) {
          Vue.set(this.playerQueueDetails, key, value)
        }
      }
    },
    async getQueueDetails () {
      if (this.$server.activePlayer) {
        const queueId = this.$server.players[this.$server.activePlayerId].active_queue
        const endpoint = 'players/' + queueId + '/queue'
        this.playerQueueDetails = await this.$server.getData(endpoint)
      }
    },
    truncateString (str, num) {
      // If the length of str is less than or equal to num
      // just return str--don't truncate it.
      if (str.length <= num) {
        return str
      }
      // Return str truncated with '...' concatenated to the end of str.
      return str.slice(0, num) + '...'
    }
  }
})
</script>
