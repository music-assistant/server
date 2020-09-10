<template>
  <v-card>
    <v-list>
    <v-list-item style="height:50px;padding-bottom:5;">
      <v-list-item-avatar tile style="margin-left:-10px;">
        <v-icon large>{{
          players[player_id].is_group ? "speaker_group" : "speaker"
        }}</v-icon>
      </v-list-item-avatar>
      <v-list-item-content style="margin-left:-15px;">
        <v-list-item-title>{{ players[player_id].name }}</v-list-item-title>
        <v-list-item-subtitle>{{
          $t("state." + players[player_id].state)
        }}</v-list-item-subtitle>
      </v-list-item-content>
    </v-list-item>
    <v-divider></v-divider>
    <div v-for="child_id in volumePlayerIds" :key="child_id">
      <div
        class="body-2"
        :style="
          !players[child_id].powered
            ? 'color:rgba(0,0,0,.38);'
            : 'color:rgba(0,0,0,.54);'
        "
      >
        <v-btn
          icon
          @click="togglePlayerPower(child_id)"
          style="margin-left:8px"
          :style="
            !players[child_id].powered
              ? 'color:rgba(0,0,0,.38);'
              : 'color:rgba(0,0,0,.54);'
          "
        >
          <v-icon>power_settings_new</v-icon>
        </v-btn>
        <span style="margin-left:10px">{{ players[child_id].name }}</span>
        <div
          style="margin-top:-8px;margin-left:15px;margin-right:15px;height:35px;"
        >
          <v-slider
            lazy
            :disabled="!players[child_id].powered"
            :value="Math.round(players[child_id].volume_level)"
            prepend-icon="volume_down"
            append-icon="volume_up"
            @end="setPlayerVolume(child_id, $event)"
            @click:append="setPlayerVolume(child_id, 'up')"
            @click:prepend="setPlayerVolume(child_id, 'down')"
          ></v-slider>
        </div>
      </div>
      <v-divider></v-divider>
    </div>
    </v-list>
  </v-card>
</template>

<script>
import Vue from 'vue'

export default Vue.extend({
  props: ['value', 'players', 'player_id'],
  data () {
    return {}
  },
  computed: {
    volumePlayerIds () {
      var allIds = [this.player_id]
      for (const groupChildId of this.players[this.player_id].group_childs) {
        if (this.players[groupChildId]) {
          allIds.push(groupChildId)
        }
      }
      return allIds
    }
  },
  mounted () { },
  methods: {
    setPlayerVolume: function (playerId, newVolume) {
      // if (newVolume === 'up') {
      //   this.$server.playerCommand('volume_up', null, playerId)
      // } else if (newVolume === 'down') {
      //   this.$server.playerCommand('volume_down', null, playerId)
      // } else {
      //   this.$server.playerCommand('volume_set', newVolume, playerId)
      //   this.players[playerId].volume_level = newVolume
      // }
      if (newVolume === 'up') {
        newVolume = this.$server.players[playerId].volume_level + 1
      } else if (newVolume === 'down') {
        newVolume = this.$server.players[playerId].volume_level - 1
      }
      this.$server.playerCommand('volume_set', newVolume, playerId)
      this.players[playerId].volume_level = newVolume
    },
    togglePlayerPower: function (playerId) {
      this.$server.playerCommand('power_toggle', null, playerId)
    }
  }
})
</script>
