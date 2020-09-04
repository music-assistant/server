<template>
  <section>
    <InfoHeader v-bind:itemDetails="itemDetails" />
    <ItemsListing :endpoint="'playlists/' + media_id + '/tracks?provider=' + provider" />
  </section>
</template>

<script>
// @ is an alias to /src
import InfoHeader from '@/components/InfoHeader.vue'
import ItemsListing from '@/components/ItemsListing.vue'

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
      items: []
    }
  },
  created () {
    this.getItemDetails()
  },
  methods: {
    async getItemDetails () {
      // get the full details for the mediaitem
      this.$store.loading = true
      const endpoint = 'playlists/' + this.media_id
      const result = await this.$server.getData(endpoint, { provider: this.provider })
      this.itemDetails = result
      this.$store.windowtitle = result.name
      this.$store.loading = false
    }
  }
}
</script>
