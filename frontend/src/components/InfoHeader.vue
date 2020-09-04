<template>
  <v-flex v-observe-visibility="visibilityChanged">
    <v-card
      tile
      color="black"
      class="white--text"
      :img="require('../assets/info_gradient.jpg')"
      style="margin-top:-60px;"
      height="290"
    >
      <v-img
        class="white--text"
        width="100%"
        height="360"
        position="center top"
        :src="$server.getImageUrl(itemDetails, 'fanart')"
        gradient="to bottom, rgba(0,0,0,.90), rgba(0,0,0,.75)"
      >
        <div class="text-xs-center" style="height:40px;" id="whitespace_top" />

        <v-layout style="margin-left:5pxmargin-right:5px;" v-if="itemDetails">
          <!-- left side: cover image -->
          <v-flex xs5 pa-5 v-if="!$store.isMobile">
            <v-img
              :src="$server.getImageUrl(itemDetails)"
              :lazy-src="require('../assets/default_artist.png')"
              width="220px"
              height="220px"
              style="border: 4px solid rgba(0,0,0,.33);border-radius: 6px;"
            ></v-img>
          </v-flex>

          <v-flex>
            <!-- Main title -->
            <v-card-title
              style="text-shadow: 1px 1px #000000;"
            >
              {{ itemDetails.name }}
            </v-card-title>

            <!-- other details -->
            <v-card-subtitle>
              <!-- version -->
              <div
                v-if="itemDetails.version"
                class="caption"
                style="color: white;"
              >
                {{ itemDetails.version }}
              </div>

              <!-- item artists -->
              <div
                class="title"
                style="text-shadow: 1px 1px #000000;"
                v-if="itemDetails.artists"
              >
                <v-icon
                  color="#cccccc"
                  style="margin-left: -3px;margin-right:3px"
                  small
                  >person</v-icon
                >
                <span
                  v-for="(artist, artistindex) in itemDetails.artists"
                  :key="artist.db_id"
                >
                  <a style="color: primary" v-on:click="artistClick(artist)">{{
                    artist.name
                  }}</a>
                  <span
                    style="color: #cccccc"
                    v-if="artistindex + 1 < itemDetails.artists.length"
                    :key="artistindex"
                    >{{ " / " }}</span
                  >
                </span>
              </div>

              <!-- album artist -->
              <div class="title" v-if="itemDetails.artist">
                <v-icon
                  color="#cccccc"
                  style="margin-left: -3px;margin-right:3px"
                  small
                  >person</v-icon
                >
                <a
                  style="color: primary"
                  v-on:click="artistClick(itemDetails.artist)"
                  >{{ itemDetails.artist.name }}</a
                >
              </div>

              <!-- playlist owner -->
              <div
                class="title"
                style="text-shadow: 1px 1px #000000;"
                v-if="itemDetails.owner"
              >
                <v-icon
                  color="#cccccc"
                  style="margin-left: -3px;margin-right:3px"
                  small
                  >person</v-icon
                >
                <a style="color:primary">{{ itemDetails.owner }}</a>
              </div>

              <div
                v-if="itemDetails.album"
                style="color:#ffffff;text-shadow: 1px 1px #000000;"
              >
                <v-icon
                  color="#cccccc"
                  style="margin-left: -3px;margin-right:3px"
                  small
                  >album</v-icon
                >
                <a
                  style="color:#ffffff"
                  v-on:click="albumClick(itemDetails.album)"
                  >{{ itemDetails.album.name }}</a
                >
              </div>
            </v-card-subtitle>

            <!-- play/info buttons -->
            <div style="margin-left:14px;">
              <v-btn
                color="primary"
                tile
                @click="$server.$emit('showPlayMenu', itemDetails)"
              >
                <v-icon left dark>play_circle_filled</v-icon>
                {{ $t("play") }}
              </v-btn>
              <v-btn
                style="margin-left:10px;"
                v-if="
                  !$store.isMobile &&
                    !!itemDetails.in_library &&
                    itemDetails.in_library.length == 0
                "
                color="primary"
                tile
                @click="toggleLibrary(itemDetails)"
              >
                <v-icon left dark>favorite_border</v-icon>
                {{ $t("add_library") }}
              </v-btn>
              <v-btn
                style="margin-left:10px;"
                v-if="
                  !$store.isMobile &&
                    !!itemDetails.in_library &&
                    itemDetails.in_library.length > 0
                "
                color="primary"
                tile
                @click="toggleLibrary(itemDetails)"
              >
                <v-icon left dark>favorite</v-icon>
                {{ $t("remove_library") }}
              </v-btn>
            </div>

            <!-- Description/metadata -->
            <v-card-subtitle class="body-2">
              <div class="justify-left" style="text-shadow: 1px 1px #000000;">
                <ReadMore
                  :text="getDescription()"
                  :max-chars="$store.isMobile ? 140 : 260"
                />
              </div>
            </v-card-subtitle>
          </v-flex>
          <!-- tech specs and provider icons -->
          <div style="margin-top:15px">
            <ProviderIcons
              v-bind:providerIds="itemDetails.provider_ids"
              :height="25"
            />
          </div>
        </v-layout>
      </v-img>
      <!-- <div class="text-xs-center" v-if="itemDetails.tags" style="height:30px;margin-top:-8px;margin-left:15px;margin-right:15px;">
        <v-chip small color="white" outlined v-for="tag of itemDetails.tags" :key="tag">{{ tag }}</v-chip>
      </div> -->
    </v-card>
  </v-flex>
</template>

<script>
import Vue from 'vue'
import ProviderIcons from '@/components/ProviderIcons.vue'
import ReadMore from '@/components/ReadMore.vue'
import VueObserveVisibility from 'vue-observe-visibility'
Vue.use(VueObserveVisibility)

export default Vue.extend({
  components: {
    ProviderIcons,
    ReadMore
  },
  props: ['itemDetails'],
  data () {
    return {}
  },
  mounted () { },
  created () {
    this.$store.topBarTransparent = true
  },
  beforeDestroy () {
    this.$store.topBarTransparent = false
    this.$store.topBarContextItem = null
  },
  watch: {
    itemDetails: function (val) {
      // set itemDetails as contextitem
      if (val) {
        this.$store.topBarContextItem = val
      }
    }
  },
  methods: {
    visibilityChanged (isVisible, entry) {
      if (isVisible) this.$store.topBarTransparent = true
      else this.$store.topBarTransparent = false
    },
    artistClick (item) {
      // artist entry clicked
      var url = '/artists/' + item.item_id
      this.$router.push({ path: url, query: { provider: item.provider } })
    },
    albumClick (item) {
      // album entry clicked
      var url = '/albums/' + item.item_id
      this.$router.push({ path: url, query: { provider: item.provider } })
    },
    toggleLibrary (item) {
      // library button clicked on item
      this.$server.toggleLibrary(item)
    },
    getDescription () {
      var desc = ''
      if (!this.itemDetails) return ''
      if (this.itemDetails.metadata && this.itemDetails.metadata.description) {
        return this.itemDetails.metadata.description
      } else if (this.itemDetails.metadata && this.itemDetails.metadata.biography) {
        return this.itemDetails.metadata.biography
      } else if (this.itemDetails.metadata && this.itemDetails.metadata.copyright) {
        return this.itemDetails.metadata.copyright
      } else if (this.itemDetails.artists) {
        this.itemDetails.artists.forEach(function (artist) {
          if (artist.metadata && artist.metadata.biography) {
            desc = artist.metadata.biography
          }
        })
      }
      return desc
    },
    getQualityInfo () {

    },
    getUniqueProviders () {
      var keys = []
      var qualities = []
      if (!this.providerIds) return []
      const sortedItemIds = this.providerIds.slice()
      sortedItemIds.sort((a, b) =>
        a.quality < b.quality ? 1 : b.quality < a.quality ? -1 : 0
      )
      for (var item of sortedItemIds) {
        if (!keys.includes(item.provider)) {
          qualities.push(item)
          keys.push(item.provider)
        }
      }
      return qualities
    }
  }
})
</script>
