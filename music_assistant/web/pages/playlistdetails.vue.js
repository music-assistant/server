var PlaylistDetails = Vue.component('PlaylistDetails', {
  template: `
  <section>
      <infoheader v-bind:info="info"/>
      <v-tabs
          v-model="active"
          color="transparent"
          light
          slider-color="black"
        >
          <v-tab ripple>Playlist tracks</v-tab>
          <v-tab-item>
            <v-card flat>
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
            </v-card>
          </v-tab-item>
        </v-tabs>
      </section>`,
  props: ['provider', 'media_id'],
  data() {
    return {
      selected: [2],
      info: {},
      items: [],
      offset: 0,
    }
  },
  created() {
    this.$globals.windowtitle = "Playlist info"
    this.getInfo();
    this.getPlaylistTracks();
    this.scroll(this.Browse);
  },
  methods: {
    getInfo () {
      const api_url = '/api/playlists/' + this.media_id
      axios
      .get(api_url, { params: { provider: this.provider }})
        .then(result => {
          data = result.data;
          this.info = data;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    getPlaylistTracks () {
      this.$globals.loading = true
      const api_url = '/api/playlists/' + this.media_id + '/tracks'
      axios
        .get(api_url, { params: { offset: this.offset, limit: 25, provider: this.provider}})
        .then(result => {
          data = result.data;
          this.items.push(...data);
          this.offset += 25;
          this.$globals.loading = false;
        })
        .catch(error => {
          console.log("error", error);
        });
        
    },
    scroll (Browse) {
      window.onscroll = () => {
        let bottomOfWindow = document.documentElement.scrollTop + window.innerHeight === document.documentElement.offsetHeight;
        if (bottomOfWindow) {
          this.getPlaylistTracks();
        }
      };
    }
  }
})
