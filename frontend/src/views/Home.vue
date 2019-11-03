<template>
  <section>
    <v-list tile>
      <v-list-item tile
        v-for="item in items" :key="item.title" @click="$router.push(item.path)">
          <v-list-item-icon style="margin-left:15px">
            <v-icon>{{ item.icon }}</v-icon>
          </v-list-item-icon>
          <v-list-item-content>
            <v-list-item-title v-text="item.title"></v-list-item-title>
          </v-list-item-content>
      </v-list-item>
    </v-list>
  </section>
</template>

<script>

export default {
  name: 'home',
  data () {
    return {
      items: [
        { title: this.$t('artists'), icon: 'person', path: '/artists' },
        { title: this.$t('albums'), icon: 'album', path: '/albums' },
        { title: this.$t('tracks'), icon: 'audiotrack', path: '/tracks' },
        { title: this.$t('playlists'), icon: 'playlist_play', path: '/playlists' },
        { title: this.$t('search'), icon: 'search', path: '/search' }
      ]
    }
  },
  created () {
    this.$store.windowtitle = this.$t('musicassistant')
  }
}
</script>
