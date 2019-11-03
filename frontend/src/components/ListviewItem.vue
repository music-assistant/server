<template>
  <div>
    <v-list-item ripple @click="$emit('click', item)">
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
            <a v-on:click="artistClick(artist)" @click.stop>{{ artist.name }}</a>
            <label v-if="artistindex + 1 < item.artists.length" :key="artistindex">/</label>
          </span>
          <a
            v-if="!!item.album && !!hidetracknum"
            v-on:click="albumClick(item.album)"
            @click.stop
            style="color:grey"
          > - {{ item.album.name }}</a>
          <label
            v-if="!hidetracknum && item.track_number"
            style="color:grey"
          >- disc {{ item.disc_number }} track {{ item.track_number }}</label>
        </v-list-item-subtitle>
        <v-list-item-subtitle v-if="item.artist">
          <a v-on:click="artistClick(item.artist)" @click.stop>{{ item.artist.name }}</a>
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
        @click="$emit('menuClick', item)"
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
    hideduration: Boolean
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
    artistClick (item) {
      // artist entry clicked within the listviewItem
      var url = '/artists/' + item.item_id
      this.$router.push({ path: url, query: { provider: item.provider } })
    },
    albumClick (item) {
      // album entry clicked within the listviewItem
      var url = '/albums/' + item.item_id
      this.$router.push({ path: url, query: { provider: item.provider } })
    },
    toggleLibrary (item) {
      // library button clicked on item
      this.$server.toggleLibrary(item)
    }
  }
})
</script>
