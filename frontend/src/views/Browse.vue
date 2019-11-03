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
          v-on:click="itemClicked"
          v-on:menuClick="menuClick"
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
    itemClicked (item) {
      // item in the list is clicked
      let url = ''
      if (item.media_type === 1) {
        url = '/artists/' + item.item_id
      } else if (item.media_type === 2) {
        url = '/albums/' + item.item_id
      } else if (item.media_type === 4) {
        url = '/playlists/' + item.item_id
      } else {
        // assume track (or radio) item
        this.$server.$emit('showContextMenu', item)
        return
      }
      this.$router.push({ path: url, query: { provider: item.provider } })
    },
    menuClick (item) {
      // contextmenu button clicked
      this.$server.$emit('showContextMenu', item)
    },
    async getItems () {
      // retrieve the full list of items
      let offset = 0
      let limit = 50
      while (true) {
        let items = await this.$server.getData(this.mediatype, { offset: offset, limit: limit, provider: this.provider })
        if (!items || items.length === 0) break
        this.items.push(...items)
        offset += limit
        if (items.length < limit) break
      }
    }
  }
}
</script>
