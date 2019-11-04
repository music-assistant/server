<template>
  <div>
    <v-list-item ripple @click="itemClicked(item)">
      <v-list-item-avatar tile color="grey" v-if="!hideavatar">
        <img
          :src="$server.getImageUrl(item, 'image', 80)"
          :lazy-src="require('../assets/file.png')"
          style="border: 1px solid rgba(0,0,0,.22);"
        />
      </v-list-item-avatar>

      <v-list-item-content>
        <v-list-item-title>
          {{ item.name }}
          <span v-if="!!item.version">({{ item.version }})</span>
        </v-list-item-title>

        <v-list-item-subtitle v-if="item.artists">
          <span v-for="(artist, artistindex) in item.artists" :key="artist.item_id">
            <a v-on:click="itemClicked(artist)" @click.stop>{{ artist.name }}</a>
            <label v-if="artistindex + 1 < item.artists.length" :key="artistindex">/</label>
          </span>
          <a
            v-if="!!item.album && !!hidetracknum"
            v-on:click="itemClicked(item.album)"
            @click.stop
            style="color:grey"
          > - {{ item.album.name }}</a>
          <label
            v-if="!hidetracknum && item.track_number"
            style="color:grey"
          >- disc {{ item.disc_number }} track {{ item.track_number }}</label>
        </v-list-item-subtitle>
        <v-list-item-subtitle v-if="item.artist">
          <a v-on:click="itemClicked(item.artist)" @click.stop>{{ item.artist.name }}</a>
        </v-list-item-subtitle>

        <v-list-item-subtitle v-if="!!item.owner">{{ item.owner }}</v-list-item-subtitle>
      </v-list-item-content>

      <v-list-item-action v-if="!hideproviders">
      <ProviderIcons
        v-bind:providerIds="item.provider_ids"
        :height="20"
      />
      </v-list-item-action>

      <v-list-item-action v-if="isHiRes">
        <img
          :src="require('../assets/hires.png')"
          height="20"
        />
      </v-list-item-action>

      <v-list-item-action v-if="!hidelibrary">
        <v-tooltip bottom>
          <template v-slot:activator="{ on }">
            <v-btn icon ripple v-on="on" v-on:click="toggleLibrary(item)" @click.stop>
              <v-icon height="20" v-if="item.in_library.length > 0">favorite</v-icon>
              <v-icon height="20" v-if="item.in_library.length == 0">favorite_border</v-icon>
            </v-btn>
          </template>
          <span v-if="item.in_library.length > 0">{{ $t('remove_library') }}</span>
          <span v-if="item.in_library.length == 0">{{ $t('add_library') }}</span>
        </v-tooltip>
      </v-list-item-action>

      <v-list-item-action
        v-if="!hideduration && !!item.duration"
      >{{ item.duration.toString().formatDuration() }}</v-list-item-action>

      <!-- menu button/icon -->
      <v-icon
        v-if="!hidemenu"
        @click="menuClick(item)"
        @click.stop
        color="grey lighten-1"
        style="margin-right:-10px;padding-left:10px"
      >more_vert</v-icon>
    </v-list-item>
    <v-divider></v-divider>
  </div>
</template>

<script>
import Vue from 'vue'
import ProviderIcons from '@/components/ProviderIcons.vue'

export default Vue.extend({
  components: {
    ProviderIcons
  },
  props: {
    item: Object,
    index: Number,
    totalitems: Number,
    hideavatar: Boolean,
    hidetracknum: Boolean,
    hideproviders: Boolean,
    hidemenu: Boolean,
    hidelibrary: Boolean,
    hideduration: Boolean,
    onclickHandler: null
  },
  data () {
    return {}
  },
  computed: {
    isHiRes () {
      for (var prov of this.item.provider_ids) {
        if (prov.quality > 6) {
          return true
        }
      }
      return false
    }
  },
  mounted () { },
  methods: {
    itemClicked (mediaItem) {
      // mediaItem in the list is clicked
      if (this.onclickHandler) return this.onclickHandler(mediaItem)
      let url = ''
      if (mediaItem.media_type === 1) {
        url = '/artists/' + mediaItem.item_id
      } else if (mediaItem.media_type === 2) {
        url = '/albums/' + mediaItem.item_id
      } else if (mediaItem.media_type === 4) {
        url = '/playlists/' + mediaItem.item_id
      } else {
        // assume track (or radio) item
        this.$server.$emit('showPlayMenu', mediaItem)
        return
      }
      this.$router.push({ path: url, query: { provider: mediaItem.provider } })
    },
    menuClick (mediaItem) {
      // contextmenu button clicked
      this.$server.$emit('showContextMenu', mediaItem)
    },
    toggleLibrary (mediaItem) {
      // library button clicked on item
      this.$server.toggleLibrary(mediaItem)
    }
  }
})
</script>
