var TrackDetails = Vue.component('TrackDetails', {
  template: `
  <section>
      <infoheader v-bind:info="info"/>
      <v-tabs
          v-model="active"
          color="transparent"
          light
          slider-color="black"
        >
          <v-tab ripple>Other versions</v-tab>
          <v-tab-item>
            <v-card flat>
                <v-list two-line>
                  <listviewItem 
                      v-for="(item, index) in trackversions" 
                      v-bind:item="item"
                      :key="item.db_id"
                      v-bind:totalitems="trackversions.length"
                      v-bind:index="index"
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
      trackversions: [],
      offset: 0,
      active: null,
    }
  },
  created() {
    this.$globals.windowtitle = "Track info"
    this.getInfo();
  },
  methods: {
    getInfo () {
      this.$globals.loading = true;
      const api_url = '/api/tracks/' + this.media_id
      axios
        .get(api_url, { params: { provider: this.provider }})
        .then(result => {
          data = result.data;
          this.info = data;
          this.getTrackVersions()
          this.$globals.loading = false;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
    getTrackVersions () {
      const api_url = '/api/search';
      var searchstr = this.info.artists[0].name + " - " + this.info.name
      axios
        .get(api_url, { params: { query: searchstr, limit: 50, media_types: 'tracks', online: true}})
        .then(result => {
          data = result.data;
          this.trackversions.push(...data.tracks);
          this.offset += 50;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
  }
})
