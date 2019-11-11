<template>
  <div>
    <v-list-item
      ripple
      @click.left="onclickHandler ? onclickHandler(item) : itemClicked(item)"
      @contextmenu="menuClick"
      @contextmenu.prevent
      v-longpress="menuClick"
    >
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
          <span
            v-for="(artist, artistindex) in item.artists"
            :key="artist.item_id"
          >
            <a v-on:click="itemClicked(artist)" @click.stop>{{
              artist.name
            }}</a>
            <label
              v-if="artistindex + 1 < item.artists.length"
              :key="artistindex"
              >/</label
            >
          </span>
          <a
            v-if="!!item.album && !!hidetracknum"
            v-on:click="itemClicked(item.album)"
            @click.stop
            style="color:grey"
          >
            - {{ item.album.name }}</a
          >
          <label v-if="!hidetracknum && item.track_number" style="color:grey"
            >- disc {{ item.disc_number }} track {{ item.track_number }}</label
          >
        </v-list-item-subtitle>
        <v-list-item-subtitle v-if="item.artist">
          <a v-on:click="itemClicked(item.artist)" @click.stop>{{
            item.artist.name
          }}</a>
        </v-list-item-subtitle>

        <v-list-item-subtitle v-if="!!item.owner">{{
          item.owner
        }}</v-list-item-subtitle>
      </v-list-item-content>

      <v-list-item-action v-if="!hideproviders">
        <ProviderIcons v-bind:providerIds="item.provider_ids" :height="20" />
      </v-list-item-action>

      <v-list-item-action v-if="isHiRes">
        <v-tooltip bottom>
          <template v-slot:activator="{ on }">
          <img :src="require('../assets/hires.png')" height="20" v-on="on" />
          </template>
          <span>{{ isHiRes }}</span>
        </v-tooltip>
      </v-list-item-action>

      <v-list-item-action v-if="!hidelibrary">
        <v-tooltip bottom>
          <template v-slot:activator="{ on }">
            <v-btn
              icon
              ripple
              v-on="on"
              v-on:click="toggleLibrary(item)"
              @click.prevent
              @click.stop
            >
              <v-icon height="20" v-if="item.in_library.length > 0"
                >favorite</v-icon
              >
              <v-icon height="20" v-if="item.in_library.length == 0"
                >favorite_border</v-icon
              >
            </v-btn>
          </template>
          <span v-if="item.in_library.length > 0">{{
            $t("remove_library")
          }}</span>
          <span v-if="item.in_library.length == 0">{{
            $t("add_library")
          }}</span>
        </v-tooltip>
      </v-list-item-action>

      <v-list-item-action v-if="!hideduration && !!item.duration">{{
        item.duration.toString().formatDuration()
      }}</v-list-item-action>

      <!-- menu button/icon -->
      <v-icon
        v-if="!hidemenu"
        @click="menuClick(item)"
        @click.stop
        color="grey lighten-1"
        style="margin-right:-10px;padding-left:10px"
        >more_vert</v-icon
      >
    </v-list-item>
    <v-divider></v-divider>
  </div>
</template>

<script>
import Vue from 'vue'
import ProviderIcons from '@/components/ProviderIcons.vue'

const PRESS_TIMEOUT = 600

Vue.directive('longpress', {
  bind: function (el, { value }, vNode) {
    if (typeof value !== 'function') {
      Vue.$log.warn(`Expect a function, got ${value}`)
      return
    }
    let pressTimer = null
    const start = e => {
      if (e.type === 'click' && e.button !== 0) {
        return
      }
      if (pressTimer === null) {
        pressTimer = setTimeout(() => value(e), PRESS_TIMEOUT)
      }
    }
    const cancel = () => {
      if (pressTimer !== null) {
        clearTimeout(pressTimer)
        pressTimer = null
      }
    }
    ;['mousedown', 'touchstart'].forEach(e => el.addEventListener(e, start))
    ;['click', 'mouseout', 'touchend', 'touchcancel'].forEach(e => el.addEventListener(e, cancel))
  }
})

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
    return {
      touchMoving: false,
      cancelled: false
    }
  },
  computed: {
    isHiRes () {
      for (var prov of this.item.provider_ids) {
        if (prov.quality > 6) {
          if (prov.details) {
            return prov.details
          } else if (prov.quality === 7) {
            return '44.1/48khz 24 bits'
          } else if (prov.quality === 8) {
            return '88.2/96khz 24 bits'
          } else if (prov.quality === 9) {
            return '176/192khz 24 bits'
          } else {
            return '+192kHz 24 bits'
          }
        }
      }
      return ''
    }
  },
  created () { },
  beforeDestroy () {
    this.cancelled = true
  },
  mounted () { },
  methods: {
    itemClicked (mediaItem = null) {
      // mediaItem in the list is clicked
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
    menuClick () {
      // contextmenu button clicked
      if (this.cancelled) return
      this.$server.$emit('showContextMenu', this.item)
    },
    async toggleLibrary (mediaItem) {
      // library button clicked on item
      this.cancelled = true
      await this.$server.toggleLibrary(mediaItem)
      this.cancelled = false
    }
  }
})
</script>
