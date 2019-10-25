var Queue = Vue.component('Queue', {
  template: `
  <section>
        <v-list two-line>
        <listviewItem 
            v-for="(item, index) in items" 
            v-bind:item="item"
            :key="item.db_id"
            :hideavatar="isMobile()"
            :hidetracknum="true"
            :hideproviders="isMobile()"
            :hidelibrary="isMobile()">
        </listviewItem>
      </v-list>
      </section>`,
  props: ['player_id'],
  data() {
    return {
      selected: [0],
      info: {},
      items: [],
      offset: 0,
    }
  },
  created() {
    this.$globals.windowtitle = this.$t('queue')
    this.getQueueTracks(0, 25);
  },
  methods: {

    getQueueTracks (offset, limit) {
      const api_url = this.$globals.apiAddress + 'players/' + this.player_id + '/queue'
      return axios.get(api_url, { params: { offset: offset, limit: limit}})
        .then(response => {
            if (response.data.length < 1 )
              return;
            this.items.push(...response.data)
            return this.getQueueTracks(offset+limit, 100)
        })
    }
  }
})
