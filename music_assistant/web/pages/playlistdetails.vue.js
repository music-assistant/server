var PlaylistDetails = Vue.component('PlaylistDetails', {
  template: `
  <section>
      <infoheader v-bind:info="info" :context="'playlistdetails'"/>
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
                      :key="index"
                      :hideavatar="isMobile()"
                      :hidetracknum="true"
                      :hideproviders="isMobile()"
                      :hidelibrary="isMobile()"
                      :hidemenu="true"
                      v-bind:context="info"
                      >
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
      active: 0,
      full_list_loaded: false
    }
  },
  created() {
    this.$globals.windowtitle = ""
    this.getInfo();
    this.getPlaylistTracks();
    this.scroll(this.Browse);
  },
  methods: {
    async getInfo () {
      const api_url = this.$globals.apiAddress + 'playlists/' + this.media_id
      axios
      .get(api_url, { params: { provider: this.provider }})
        .then(result => {
          data = result.data;
          this.info = data;
          this.$globals.curContext = data;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    async getPlaylistTracks () {
      if (this.full_list_loaded)
        return;
      this.$globals.loading = true;
      const api_url = this.$globals.apiAddress + 'playlists/' + this.media_id + '/tracks'
      let limit = 20;
      axios
        .get(api_url, { params: { offset: this.offset, limit: limit, provider: this.provider}})
        .then(result => {
          this.$globals.loading = false;
          data = result.data;
          if (data.length < limit)
          {
            this.full_list_loaded = true;
            this.$globals.loading = false;
            if (data.length == 0)
              return
          }
          this.items.push(...data);
          this.offset += limit;
          
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    scroll (Browse) {
      window.onscroll = () => {
        let bottomOfWindow = document.documentElement.scrollTop + window.innerHeight === document.documentElement.offsetHeight;
        if (bottomOfWindow && !this.full_list_loaded) {
          this.getPlaylistTracks();
        }
      };
    }
  }
})
