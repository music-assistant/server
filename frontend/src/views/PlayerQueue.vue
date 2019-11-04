<template>
  <section>
    <v-tabs v-model="activeTab">
      <v-tab> {{ $t('queue_next_tracks') + ' (' + next_items.length + ')' }}</v-tab>
      <v-tab-item>
      <v-list two-line>
        <RecycleScroller
          class="scroller"
          :items="next_items"
          :item-size="72"
          key-field="queue_item_id"
          v-slot="{ item }"
          page-mode
        >
          <ListviewItem
            v-bind:item="item"
            :hideavatar="item.media_type == 3 ? $store.isMobile : false"
            :hidetracknum="true"
            :hideproviders="$store.isMobile"
            :hidelibrary="$store.isMobile"
            :hidemenu="$store.isMobile"
            v-on:click="itemClicked"
            v-on:menuClick="menuClick"
          ></ListviewItem>
        </RecycleScroller>
      </v-list>
      </v-tab-item>
      <v-tab> {{ $t('queue_previous_tracks') + ' (' + previous_items.length + ')' }}</v-tab>
      <v-tab-item>
      <v-list two-line>
        <RecycleScroller
          class="scroller"
          :items="previous_items"
          :item-size="72"
          key-field="queue_item_id"
          v-slot="{ item }"
          page-mode
        >
          <ListviewItem
            v-bind:item="item"
            :hideavatar="item.media_type == 3 ? $store.isMobile : false"
            :hidetracknum="true"
            :hideproviders="$store.isMobile"
            :hidelibrary="$store.isMobile"
            :hidemenu="$store.isMobile"
            v-on:click="itemClicked"
            v-on:menuClick="menuClick"
          ></ListviewItem>
        </RecycleScroller>
      </v-list>
      </v-tab-item>
    </v-tabs>
  </section>
</template>

<script>
import ListviewItem from '@/components/ListviewItem.vue'

export default {
  components: {
    ListviewItem
  },
  props: {},
  data () {
    return {
      selected: [2],
      items: []
    }
  },
  computed: {
    next_items () {
      if (this.items && this.$server.activePlayer) {
        return this.items.slice(this.$server.activePlayer.cur_queue_index)
      } else return []
    },
    previous_items () {
      if (this.items && this.$server.activePlayer) {
        return this.items.slice(0, this.$server.activePlayer.cur_queue_index)
      } else return []
    }
  },
  created () {
    this.$store.windowtitle = this.$t('queue')
    if (this.$server.activePlayer) {
      this.getItems()
    }
    this.$server.$on('queue updated', this.queueUpdatedMsg)
    this.$server.$on('new player selected', this.queueUpdatedMsg)
  },
  beforeDestroy () {
    this.$server.$off('queue updated')
    this.$server.$off('new player selected')
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
    queueUpdatedMsg (msgDetails) {
      // got queue updated event
      if (msgDetails === this.$server.activePlayerId) {
        this.getItems()
      }
    },
    async getItems () {
      // retrieve the queue items
      const endpoint = 'players/' + this.$server.activePlayerId + '/queue'
      return this.$server.getAllItems(endpoint, this.items)
    }
  }
}
</script>
