<template>
  <section>
    <InfoHeader v-bind:itemDetails="itemDetails" />
    <v-tabs grow show-arrows v-model="activeTab">
      <v-tab
        v-for="tab in tabs"
        :key="tab.label"
      > {{ $t(tab.label) + ' (' + tab.items.length + ')' }}</v-tab>
      <v-tab-item
        v-for="tab in tabs"
        :key="tab.label"
      >
        <v-card flat>
          <v-list two-line>
            <RecycleScroller
              class="scroller"
              :items="tab.items"
              :item-size="72"
              key-field="item_id"
              v-slot="{ item }"
              page-mode
            >
              <ListviewItem
                v-bind:item="item"
                :hideavatar="$store.isMobile || tab.label === 'album_tracks'"
                :hidetracknum="tab.label !== 'album_tracks'"
                :hideproviders="$store.isMobile"
                :hidelibrary="$store.isMobile"
                :hidemenu="item.media_type == 3 ? $store.isMobile : false"
                v-on:click="itemClicked"
                v-on:menuClick="menuClick"
              ></ListviewItem>
            </RecycleScroller>
          </v-list>
        </v-card>
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
import ListviewItem from '@/components/ListviewItem.vue'
import InfoHeader from '@/components/InfoHeader.vue'

export default {
  components: {
    ListviewItem,
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
      items: [],
      activeTab: 0,
      tabs: []
    }
  },
  async created () {
    // retrieve the item details
    await this.getItemDetails()
    if (this.media_type === 'artists') {
      // artist details
      this.tabs = [
        {
          label: 'artist_toptracks',
          endpoint: 'artists/' + this.media_id + '/toptracks',
          items: []
        },
        {
          label: 'artist_albums',
          endpoint: 'artists/' + this.media_id + '/albums',
          items: []
        }
      ]
    } else if (this.media_type === 'albums') {
      // album details
      this.tabs = [
        {
          label: 'album_tracks',
          endpoint: 'albums/' + this.media_id + '/tracks',
          items: []
        },
        {
          label: 'album_versions',
          endpoint: 'albums/' + this.media_id + '/versions',
          items: []
        }
      ]
    } else if (this.media_type === 'tracks') {
      // track details
      this.tabs = [
        {
          label: 'track_versions',
          endpoint: 'tracks/' + this.media_id + '/versions',
          items: []
        }
      ]
    } else if (this.media_type === 'playlists') {
      // playlist details
      this.tabs = [
        {
          label: 'playlist_tracks',
          endpoint: 'playlists/' + this.media_id + '/tracks',
          paginated: true,
          items: []
        }
      ]
    }
    // retrieve the tabs with additional details
    for (var tab of this.tabs) {
      this.getTabItems(tab)
    }
  },
  methods: {
    itemClicked (item) {
      // listitem was clicked
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
      // contextmenu button (within listitem) clicked
      this.$server.$emit('showContextMenu', item)
    },
    async getItemDetails () {
      // get the full details for the mediaitem
      this.$store.loading = true
      const endpoint = this.media_type + '/' + this.media_id
      let result = await this.$server.getData(endpoint, { provider: this.provider })
      this.itemDetails = result
      this.$store.windowtitle = result.name
      this.$store.loading = false
    },
    async getTabItems (tab) {
      // retrieve the lists of items for each tab
      let offset = 0
      let limit = 50
      let paginated = 'paginated' in tab ? tab.paginated : false
      while (true) {
        let items = await this.$server.getData(tab.endpoint, { offset: offset, limit: limit, provider: this.provider })
        if (!items || items.length === 0) break
        tab.items.push(...items)
        offset += limit
        if (items.length < limit || !paginated) break
      }
    }
  }
}
</script>
