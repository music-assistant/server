<template>
  <section>
    <InfoHeader v-bind:itemDetails="itemDetails" />
    <v-tabs dark show-arrows v-model="activeTab" grow hide-slider background-color="rgba(0,0,0,.45)">
      <v-tab v-for="tab in tabs" :key="tab.label">
        {{ $t(tab.label) }}</v-tab
      >
      <v-tab-item v-for="tab in tabs" :key="tab.label">
        <ItemsListing :endpoint="tab.endpoint" />
      </v-tab-item>
    </v-tabs>
  </section>
</template>

<style scoped>
.scroller {
  height: 100%;
}
</style>

<script>
// @ is an alias to /src
import ItemsListing from '@/components/ItemsListing.vue'
import InfoHeader from '@/components/InfoHeader.vue'

export default {
  components: {
    ItemsListing,
    InfoHeader
  },
  props: {
    media_id: String,
    provider: String,
    media_type: String
  },
  data () {
    return {
      itemDetails: {},
      activeTab: 0,
      tabs: []
    }
  },
  created () {
    if (this.media_type === 'artists') {
      // artist details
      this.tabs = [
        {
          label: 'artist_toptracks',
          endpoint: 'artists/' + this.media_id + '/toptracks?provider=' + this.provider
        },
        {
          label: 'artist_albums',
          endpoint: 'artists/' + this.media_id + '/albums?provider=' + this.provider
        }
      ]
    } else if (this.media_type === 'albums') {
      // album details
      this.tabs = [
        {
          label: 'album_tracks',
          endpoint: 'albums/' + this.media_id + '/tracks?provider=' + this.provider
        },
        {
          label: 'album_versions',
          endpoint: 'albums/' + this.media_id + '/versions?provider=' + this.provider
        }
      ]
    } else if (this.media_type === 'tracks') {
      // track details
      this.tabs = [
        {
          label: 'track_versions',
          endpoint: 'tracks/' + this.media_id + '/versions?provider=' + this.provider
        }
      ]
    } else if (this.media_type === 'playlists') {
      // playlist details
      this.tabs = [
        {
          label: 'playlist_tracks',
          endpoint: 'playlists/' + this.media_id + '/tracks?provider=' + this.provider
        }
      ]
    }
    this.getItemDetails()
  },
  methods: {
    async getItemDetails () {
      // get the full details for the mediaitem
      this.$store.loading = true
      const endpoint = this.media_type + '/' + this.media_id
      const result = await this.$server.getData(endpoint, { provider: this.provider })
      this.itemDetails = result
      this.$store.windowtitle = result.name
      this.$store.loading = false
    }
  }
}
</script>
