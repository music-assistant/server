<template>
  <section>
    <v-app-bar app flat dense color="#E0E0E0" style="margin-top:45px;">
      <v-text-field
        v-model="search"
        clearable
        prepend-inner-icon="search"
        label="Search"
        dense
        hide-details
        outlined
        color="rgba(0,0,0,.54)"
      ></v-text-field>
      <v-spacer></v-spacer>
      <v-select
        v-model="sortBy"
        :items="sortKeys"
        prepend-inner-icon="sort"
        dense
        hide-details
        outlined
        color="rgba(0,0,0,.54)"
      ></v-select>

      <v-btn
        color="rgba(0,0,0,.54)"
        outlined
        @click="sortDesc = !sortDesc"
        style="width:15px;height:40px;margin-left:5px"
      >
        <v-icon v-if="sortDesc">arrow_upward</v-icon>
        <v-icon v-if="!sortDesc">arrow_downward</v-icon>
      </v-btn>
      <v-spacer></v-spacer>
      <v-btn
        color="rgba(0,0,0,.54)"
        outlined
        @click="useListView = !useListView"
        style="width:15px;height:40px;margin-left:5px"
      >
        <v-icon v-if="useListView">view_list</v-icon>
        <v-icon v-if="!useListView">grid_on</v-icon>
      </v-btn>
    </v-app-bar>
    <v-data-iterator
      :items="items"
      :search="search"
      :sort-by="sortBy"
      :sort-desc="sortDesc"
      :custom-filter="filteredItems"
      hide-default-footer
      disable-pagination
      loading
    >
      <template v-slot:default="props">
        <v-container fluid v-if="!useListView">
          <v-row dense align-content="stretch" align="stretch">
            <v-col v-for="item in props.items" :key="item.item_id" align-self="stretch">
              <PanelviewItem
                :item="item"
                :thumbWidth="thumbWidth"
                :thumbHeight="thumbHeight"
              /> </v-col
          ></v-row>
        </v-container>
        <v-list two-line v-if="useListView">
          <RecycleScroller
            class="scroller"
            :items="props.items"
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
      </template>
    </v-data-iterator>
  </section>
</template>

<script>
// @ is an alias to /src
import ListviewItem from '@/components/ListviewItem.vue'
import PanelviewItem from '@/components/PanelviewItem.vue'

export default {
  name: 'browse',
  components: {
    ListviewItem,
    PanelviewItem
  },
  props: {
    mediatype: String,
    provider: String
  },
  data () {
    return {
      items: [],
      useListView: false,
      itemsPerPageArray: [50, 100, 200],
      search: '',
      filter: {},
      sortDesc: false,
      page: 1,
      itemsPerPage: 50,
      sortBy: 'name',
      sortKeys: [ ]
    }
  },
  created () {
    this.$store.windowtitle = this.$t(this.mediatype)
    if (this.mediatype === 'albums') {
      this.sortKeys.push({ text: 'Name', value: 'name' })
      this.sortKeys.push({ text: 'Artist', value: 'artist.name' })
      this.sortKeys.push({ text: 'Year', value: 'year' })
      this.useListView = false
    } else if (this.mediatype === 'tracks') {
      this.sortKeys.push({ text: 'Title', value: 'name' })
      this.sortKeys.push({ text: 'Artist', value: 'artists[0].name' })
      this.sortKeys.push({ text: 'Album', value: 'album.name' })
      this.useListView = true
    } else {
      this.sortKeys.push({ text: 'Name', value: 'name' })
      this.useListView = false
    }
    this.getItems()
    this.$server.$on('refresh_listing', this.getItems)
  },
  computed: {
    filteredKeys () {
      return this.keys.filter(key => key !== `Name`)
    },
    thumbWidth () {
      return this.$store.isMobile ? 120 : 175
    },
    thumbHeight () {
      if (this.$store.isMobile) {
        // mobile resolution
        if (this.mediatype === 'artists') return this.thumbWidth * 1.5
        else return this.thumbWidth * 1.95
      } else {
        // non-mobile resolution
        if (this.mediatype === 'artists') return this.thumbWidth * 1.5
        else return this.thumbWidth * 1.8
      }
    }
  },
  methods: {
    async getItems () {
      // retrieve the full list of items
      let endpoint = 'library/' + this.mediatype
      return this.$server.getAllItems(endpoint, this.items)
    },
    filteredItems (items, search) {
      if (!search) return items
      search = search.toLowerCase()
      let newLst = []
      for (let item of items) {
        if (item.name.toLowerCase().includes(search)) {
          newLst.push(item)
        } else if (item.artist && item.artist.name.toLowerCase().includes(search)) {
          newLst.push(item)
        } else if (item.album && item.album.name.toLowerCase().includes(search)) {
          newLst.push(item)
        } else if (item.artists && item.artists[0].name.toLowerCase().includes(search)) {
          newLst.push(item)
        }
      }
      return newLst
    }
  }
}
</script>
