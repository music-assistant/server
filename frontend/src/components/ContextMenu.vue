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
              <v-icon>{{item.icon}}</v-icon>
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
          @click="playlistSelected"
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
      mediaPlayItems: [
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
      ],
      showTrackInfoItem: {
        label: 'show_info',
        action: 'info',
        icon: 'info'
      },
      addToPlaylistItem: {
        label: 'add_playlist',
        action: 'add_playlist',
        icon: 'add_circle_outline'
      },
      removeFromPlaylistItem: {
        label: 'remove_playlist',
        action: 'remove_playlist',
        icon: 'remove_circle_outline'
      },
      playerQueueItems: [],
      playlists: []
    }
  },
  mounted () { },
  created () {
    this.$server.$on('showContextMenu', this.showContextMenu)
    this.$server.$on('showPlayMenu', this.showPlayMenu)
  },
  beforeDestroy () {
    this.$server.$off('showContextMenu')
    this.$server.$off('showPlayMenu')
  },
  computed: {
  },
  methods: {
    showContextMenu (item, playlist = null) {
      this.curItem = item
      this.curPlaylist = playlist
      if (!item) return
      if (item.media_type === 3) {
        // track item in list
        let items = []
        items.push(...this.mediaPlayItems)
        items.push(this.showTrackInfoItem)
        items.push(this.addToPlaylistItem)
        if (!!playlist && playlist.is_editable) {
          items.push(this.removeFromPlaylistItem)
        }
        this.menuItems = items
      } else {
        // all other playable media
        this.menuItems = this.mediaPlayItems
      }
      this.header = item.name
      this.subheader = ''
      this.visible = true
    },
    showPlayMenu (item) {
      this.curItem = item
      if (!item) return
      this.menuItems = this.mediaPlayItems
      this.header = item.name
      this.subheader = ''
      this.visible = true
    },
    itemCommand (cmd) {
      if (cmd === 'info') {
        // show track info
        this.$router.push({
          path: '/tracks/' + this.curItem.item_id,
          query: { provider: this.curItem.provider }
        })
        this.visible = false
      } else if (cmd === 'add_playlist') {
        // add to playlist
        return this.showPlaylistsMenu()
      } else if (cmd === 'remove_playlist') {
        // remove track from playlist
        this.playlistAddRemove(
          this.curItem,
          this.curPlaylist.item_id,
          'playlist_remove'
        )
        this.visible = false
      } else {
        // assume play command
        this.$server.playItem(this.curItem, cmd)
        this.visible = false
      }
    },
    playlistSelected (playlistobj) {
      this.playlistAddRemove(
        this.curItem,
        playlistobj,
        'playlist_add'
      )
      this.visible = false
    },
    playlistAddRemove (track, playlist, action = 'playlist_add') {
      /// add or remove track on playlist
      var url = `${this.$store.server}api/track/${track.item_id}`
      this.$axios
        .get(url, {
          params: {
            provider: track.provider,
            action: action,
            action_details: playlist.item_id
          }
        })
        .then(result => {
          // reload listing
          if (action === 'playlist_remove') this.$router.go()
        })
    },
    async showPlaylistsMenu () {
      // get all editable playlists
      const url = this.$store.apiAddress + 'playlists'
      let trackProviders = []
      for (let item of this.curItem.provider_ids) {
        trackProviders.push(item.provider)
      }
      let result = await this.$axios.get(url, {})
      let items = []
      for (var playlist of result.data) {
        if (
          playlist.is_editable &&
          playlist.item_id !== this.curPlaylist.item_id
        ) {
          for (let item of playlist.provider_ids) {
            if (trackProviders.includes(item.provider)) {
              items.push(playlist)
              break
            }
          }
        }
      }
      this.playlists = items
    }
  }
})
</script>
