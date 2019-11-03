<template>
  <v-app light>
    <TopBar />
    <NavigationMenu></NavigationMenu>
    <v-content>
      <!-- <player></player> -->
      <router-view app :key="$route.path"></router-view>
    </v-content>
    <PlayerOSD :showPlayerSelect="showPlayerSelect" />
    <ContextMenu/>
    <PlayerSelect/>
    <v-overlay :value="$store.loading">
      <v-progress-circular indeterminate size="64"></v-progress-circular>
    </v-overlay>
  </v-app>
</template>

<script>
import Vue from 'vue'
import NavigationMenu from './components/NavigationMenu.vue'
import TopBar from './components/TopBar.vue'
import ContextMenu from './components/ContextMenu.vue'
import PlayerOSD from './components/PlayerOSD.vue'
import PlayerSelect from './components/PlayerSelect.vue'

export default Vue.extend({
  name: 'App',
  components: {
    NavigationMenu,
    TopBar,
    ContextMenu,
    PlayerOSD,
    PlayerSelect
  },
  data: () => ({
    showPlayerSelect: false
  }),
  created () {
    // TODO: retrieve serveraddress through discovery and/or user settings
    let serverAddress = ''
    if (process.env.NODE_ENV === 'production') {
      let loc = window.location
      serverAddress = loc.origin + loc.pathname
    } else {
      serverAddress = 'http://192.168.1.79:8095/'
    }
    this.$server.connect(serverAddress)
  }
})
</script>
