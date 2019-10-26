var AlbumDetails = Vue.component('AlbumDetails', {
  template: `
  <section>
      <infoheader v-bind:info="info" :context="'albumdetails'"/>
      <v-tabs
          v-model="active"
          color="transparent"
          light
          slider-color="black"
        >
          <v-tab ripple>Album tracks</v-tab>
          <v-tab-item>
            <v-card flat>
            <v-list two-line>
                <listviewItem 
                    v-for="(item, index) in albumtracks" 
                    v-bind:item="item"
                    :key="item.db_id"
                    v-bind:totalitems="albumtracks.length"
                    v-bind:index="index"
                    :hideavatar="true"
                    :hideproviders="isMobile()"
                    :context="'albumtracks'"
                    >
                </listviewItem>
              </v-list>
            </v-card>
          </v-tab-item>

          <v-tab ripple>Versions</v-tab>
          <v-tab-item>
            <v-card flat>
                <v-list two-line>
                  <listviewItem 
                      v-for="(item, index) in albumversions" 
                      v-bind:item="item"
                      :key="item.db_id"
                      v-bind:totalitems="albumversions.length"
                      v-bind:index="index"
                      :context="'albumtracks'"
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
      albumtracks: [],
      albumversions: [],
      offset: 0,
      active: null,
    }
  },
  created() {
    this.$globals.windowtitle = ""
    this.getInfo();
    this.getAlbumTracks();
  },
  methods: {
    getInfo () {
      const api_url = this.$globals.server + 'api/albums/' + this.media_id
      axios
        .get(api_url, { params: { provider: this.provider }})
        .then(result => {
          data = result.data;
          this.info = data;
          this.getAlbumVersions()
          this.$globals.curContext = data;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    getAlbumTracks () {
      this.$globals.loading = true;
      const api_url = this.$globals.server + 'api/albums/' + this.media_id + '/tracks'
      axios
        .get(api_url, { params: { offset: this.offset, limit: 50, provider: this.provider}})
        .then(result => {
          data = result.data;
          this.albumtracks.push(...data);
          this.offset += 50;
          this.$globals.loading = false;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    getAlbumVersions () {
      const api_url = this.$globals.server + 'api/search';
      var searchstr = this.info.artist.name + " - " + this.info.name
      axios
        .get(api_url, { params: { query: searchstr, limit: 50, media_types: 'albums', online: true}})
        .then(result => {
          data = result.data;
          this.albumversions.push(...data.albums);
          this.offset += 50;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
  }
})
