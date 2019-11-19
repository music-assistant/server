<template>
  <section>
    <ItemsListing :mediatype="mediatype" :provider="provider" :endpoint="'library/' + mediatype" />
  </section>
</template>

<script>
// @ is an alias to /src
import ItemsListing from '@/components/ItemsListing.vue'

export default {
  name: 'browse',
  components: {
    ItemsListing
  },
  props: {
    mediatype: String,
    provider: String
  },
  data () {
    return { }
  },
  created () {
    this.$store.windowtitle = this.$t(this.mediatype)
  },
  computed: { },
  methods: { }
}
</script>
