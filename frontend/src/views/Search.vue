<template>
  <section>
    <v-text-field
      dense
      clearable
      :label="$t('type_to_search')"
      append-icon="search"
      v-model="searchInput"
      v-on:keyup.enter="$router.push({ path: 'search', query: { searchQuery: searchInput } })"
      @click:append="$router.push({ path: 'search', query: { searchQuery: searchInput } })"
      style="margin-left:15px; margin-right:15px; margin-top:18px;margin-bottom:-8px"
    >
    </v-text-field>

    <v-tabs
      show-arrows
      :v-model="activeTab"
      grow
      background-color="rgba(0,0,0,.75)"
      dark
    >
      <v-tab
        ripple
        v-if="tracks.length"
      >{{ $t("tracks") }}</v-tab>
      <v-tab-item v-if="tracks.length">
        <v-card flat>
          <v-list
            two-line
            style="margin-left:15px; margin-right:15px"
          >
            <listviewItem
              v-for="(item, index) in tracks"
              v-bind:item="item"
              :key="item.db_id"
              v-bind:totalitems="tracks.length"
              v-bind:index="index"
              :hideavatar="$store.isMobile"
              :hidetracknum="true"
              :hideproviders="$store.isMobile"
              :hideduration="$store.isMobile"
              :showlibrary="true"
            >
            </listviewItem>
          </v-list>
        </v-card>
      </v-tab-item>

      <v-tab
        ripple
        v-if="artists.length"
      >{{ $t("artists") }}</v-tab>
      <v-tab-item v-if="artists.length">
        <v-card flat>
          <v-list two-line>
            <listviewItem
              v-for="(item, index) in artists"
              v-bind:item="item"
              :key="item.db_id"
              v-bind:totalitems="artists.length"
              v-bind:index="index"
              :hideproviders="$store.isMobile"
            >
            </listviewItem>
          </v-list>
        </v-card>
      </v-tab-item>

      <v-tab
        ripple
        v-if="albums.length"
      >{{ $t("albums") }}</v-tab>
      <v-tab-item v-if="albums.length">
        <v-card flat>
          <v-list two-line>
            <listviewItem
              v-for="(item, index) in albums"
              v-bind:item="item"
              :key="item.db_id"
              v-bind:totalitems="albums.length"
              v-bind:index="index"
              :hideproviders="$store.isMobile"
            >
            </listviewItem>
          </v-list>
        </v-card>
      </v-tab-item>

      <v-tab
        ripple
        v-if="playlists.length"
      >{{ $t("playlists") }}</v-tab>
      <v-tab-item v-if="playlists.length">
        <v-card flat>
          <v-list two-line>
            <listviewItem
              v-for="(item, index) in playlists"
              v-bind:item="item"
              :key="item.db_id"
              v-bind:totalitems="playlists.length"
              v-bind:index="index"
              :hidelibrary="true"
            >
            </listviewItem>
          </v-list>
        </v-card>
      </v-tab-item>
    </v-tabs>
  </section>
</template>

<script>
// @ is an alias to /src
import ListviewItem from '@/components/ListviewItem.vue'

export default {
  components: {
    ListviewItem
  },
  props: [
    'searchQuery', 'activeTab'
  ],
  data () {
    return {
      searchInput: '',
      selected: [2],
      artists: [],
      albums: [],
      tracks: [],
      playlists: [],
      timeout: null
    }
  },
  watch: {
    searchQuery: function (val) {
      this.Search()
    }
  },
  created () {
    this.$server.$on('refresh_listing', this.Search)
    this.$store.windowtitle = this.$t('search')
    this.Search()
  },
  methods: {
    async Search () {
      if (this.searchQuery && this.$server.connected) {
        this.$store.loading = true
        const params = { query: this.searchQuery, online: true, limit: 10 }
        const result = await this.$server.getData('search', params)
        this.artists = result.artists
        this.albums = result.albums
        this.tracks = result.tracks
        this.playlists = result.playlists
        this.$store.loading = false
        this.searchInput = this.searchQuery
      } else {
        this.artists = []
        this.albums = []
        this.tracks = []
        this.playlists = []
      }
    }
  }
}
</script>
