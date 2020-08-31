<template>
  <v-dialog v-model="visible" @input="$emit('input', $event)" max-width="500px">
    <v-card>
      <!-- normal contextmenu items -->
      <v-list v-if="playlists.length === 0">
        <v-subheader class="title">{{ header }}</v-subheader>
        <v-subheader v-if="subheader">{{ subheader }}</v-subheader>
        <div v-for="item of menuItems" :key="item.label">
          <v-list-item @click="itemCommand(item.action)">
            <v-list-item-avatar>
              <v-icon>{{ item.icon }}</v-icon>
            </v-list-item-avatar>
            <v-list-item-content>
              <v-list-item-title>{{ $t(item.label) }}</v-list-item-title>
            </v-list-item-content>
          </v-list-item>
          <v-divider></v-divider>
        </div>
      </v-list>
      <!-- playlists selection -->
      <v-list v-if="playlists.length > 0">
        <v-subheader class="title">{{ header }}</v-subheader>
        <listviewItem
          v-for="(item, index) in playlists"
          :key="item.item_id"
          v-bind:item="item"
          v-bind:totalitems="playlists.length"
          v-bind:index="index"
          :hideavatar="false"
          :hidetracknum="true"
          :hideproviders="false"
          :hidelibrary="true"
          :hidemenu="true"
          :onclickHandler="addToPlaylist"
        ></listviewItem>
      </v-list>
    </v-card>
  </v-dialog>
</template>

<script>
import Vue from 'vue'
import ListviewItem from '@/components/ListviewItem.vue'

export default Vue.extend({
  components:
  {
    ListviewItem
  },
  props:
    {},
  watch:
    {},
  data () {
    return {
      visible: false,
      menuItems: [],
      header: '',
      subheader: '',
      curItem: null,
      curPlaylist: null,
      playerQueueItems: [],
      playlists: []
    }
  },
  mounted () { },
  created () {
    this.$server.$on('showContextMenu', this.showContextMenu)
    this.$server.$on('showPlayMenu', this.showPlayMenu)
  },
  computed: {
  },
  methods: {
    showContextMenu (mediaItem) {
      // show contextmenu items for the given mediaItem
      this.playlists = []
      if (!mediaItem) return
      this.curItem = mediaItem
      const curBrowseContext = this.$store.topBarContextItem
      const menuItems = []
      // show info
      if (mediaItem !== curBrowseContext) {
        menuItems.push({
          label: 'show_info',
          action: 'info',
          icon: 'info'
        })
      }
      // add to library
      if (mediaItem.in_library.length === 0) {
        menuItems.push({
          label: 'add_library',
          action: 'toggle_library',
          icon: 'favorite_border'
        })
      }
      // remove from library
      if (mediaItem.in_library.length > 0) {
        menuItems.push({
          label: 'remove_library',
          action: 'toggle_library',
          icon: 'favorite'
        })
      }
      // remove from playlist (playlist tracks only)
      if (curBrowseContext && curBrowseContext.media_type === 4) {
        this.curPlaylist = curBrowseContext
        if (mediaItem.media_type === 3 && curBrowseContext.is_editable) {
          menuItems.push({
            label: 'remove_playlist',
            action: 'remove_playlist',
            icon: 'remove_circle_outline'
          })
        }
      }
      // add to playlist action (tracks only)
      if (mediaItem.media_type === 3) {
        menuItems.push({
          label: 'add_playlist',
          action: 'add_playlist',
          icon: 'add_circle_outline'
        })
      }
      this.menuItems = menuItems
      this.header = mediaItem.name
      this.subheader = ''
      this.visible = true
    },
    showPlayMenu (mediaItem) {
      // show playmenu items for the given mediaItem
      this.playlists = []
      this.curItem = mediaItem
      if (!mediaItem) return
      const menuItems = [
        {
          label: 'play_now',
          action: 'play',
          icon: 'play_circle_outline'
        },
        {
          label: 'play_next',
          action: 'next',
          icon: 'queue_play_next'
        },
        {
          label: 'add_queue',
          action: 'add',
          icon: 'playlist_add'
        }
      ]
      this.menuItems = menuItems
      this.header = mediaItem.name
      this.subheader = ''
      this.visible = true
    },
    async showPlaylistsMenu () {
      // get all editable playlists
      const trackProviders = []
      for (const item of this.curItem.provider_ids) {
        trackProviders.push(item.provider)
      }
      const playlists = await this.$server.getData('library/playlists')
      const items = []
      for (var playlist of playlists.items) {
        if (
          playlist.is_editable &&
          (!this.curPlaylist || playlist.item_id !== this.curPlaylist.item_id)
        ) {
          for (const item of playlist.provider_ids) {
            if (trackProviders.includes(item.provider)) {
              items.push(playlist)
              break
            }
          }
        }
      }
      this.playlists = items
    },
    itemCommand (cmd) {
      if (cmd === 'info') {
        // show media info
        let endpoint = ''
        if (this.curItem.media_type === 1) endpoint = 'artists'
        if (this.curItem.media_type === 2) endpoint = 'albums'
        if (this.curItem.media_type === 3) endpoint = 'tracks'
        if (this.curItem.media_type === 4) endpoint = 'playlists'
        if (this.curItem.media_type === 5) endpoint = 'radios'
        this.$router.push({
          path: '/' + endpoint + '/' + this.curItem.item_id,
          query: { provider: this.curItem.provider }
        })
        this.visible = false
      } else if (cmd === 'playmenu') {
        // show play menu
        return this.showPlayMenu(this.curItem)
      } else if (cmd === 'add_playlist') {
        // add to playlist
        return this.showPlaylistsMenu()
      } else if (cmd === 'remove_playlist') {
        // remove track from playlist
        this.removeFromPlaylist(
          this.curItem,
          this.curPlaylist.item_id,
          'playlist_remove'
        )
        this.visible = false
      } else if (cmd === 'toggle_library') {
        // add/remove to/from library
        this.$server.toggleLibrary(this.curItem)
        this.visible = false
      } else {
        // assume play command
        this.$server.playItem(this.curItem, cmd)
        this.visible = false
      }
    },
    addToPlaylist (playlistObj) {
      /// add track to playlist
      const endpoint = 'playlists/' + playlistObj.item_id + '/tracks'
      this.$server.putData(endpoint, this.curItem)
        .then(result => {
          this.visible = false
        })
    },
    removeFromPlaylist (track, playlistId) {
      /// remove track from playlist
      const endpoint = 'playlists/' + playlistId + '/tracks'
      this.$server.deleteData(endpoint, track)
        .then(result => {
          // reload listing
          this.$server.$emit('refresh_listing')
        })
    }
  }
})
</script>
