<template>
  <section>
    <v-list two-line>
      <RecycleScroller
        class="scroller"
        :items="items"
        :item-size="72"
        key-field="item_id"
        v-slot="{ item }"
        page-mode
      >
        <ListviewItem
          v-bind:item="item"
          :hideavatar="item.media_type == 3 ? $store.isMobile : false"
          :hidetracknum="true"
          :hideproviders="item.media_type < 4 ? $store.isMobile : false"
          :hidelibrary="true"
          :hidemenu="item.media_type == 3 ? $store.isMobile : false"
          :hideduration="item.media_type == 5"
        ></ListviewItem>
      </RecycleScroller>
    </v-list>
  </section>
</template>

<script>
// @ is an alias to /src
import ListviewItem from '@/components/ListviewItem.vue'

export default {
  name: 'browse',
  components: {
    ListviewItem
  },
  props: {
    mediatype: String,
    provider: String
  },
  data () {
    return {
      selected: [2],
      items: []
    }
  },
  created () {
    this.$store.windowtitle = this.$t(this.mediatype)
    this.getItems()
  },
  methods: {
    async getItems () {
      // retrieve the full list of items
      return this.$server.getAllItems(this.mediatype, this.items, { provider: this.provider })
    }
  }
}
</script>
