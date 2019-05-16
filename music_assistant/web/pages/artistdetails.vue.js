var ArtistDetails = Vue.component('ArtistDetails', {
  template: `
  <section>
      <infoheader v-bind:info="info"/>
      <v-tabs
          v-model="active"
          color="transparent"
          light
          slider-color="black"
        >
          <v-tab ripple>Top tracks</v-tab>
          <v-tab-item>
            <v-card flat>
            <v-list two-line>
                  <listviewItem 
                      v-for="(item, index) in toptracks" 
                      v-bind:item="item"
                      v-bind:totalitems="toptracks.length"
                      v-bind:index="index"
                      :key="item.db_id"
                      :hideavatar="isMobile()"
                      :hidetracknum="true"
                      :hideproviders="isMobile()"
                      :hidelibrary="isMobile()">
                  </listviewItem>
                </v-list>
            </v-card>
          </v-tab-item>

          <v-tab ripple>Albums</v-tab>
          <v-tab-item>
            <v-card flat>
                <v-list two-line>
                    <listviewItem 
                        v-for="(item, index) in artistalbums" 
                        v-bind:item="item"
                        :key="item.db_id"
                        v-bind:totalitems="artistalbums.length"
                        v-bind:index="index"
                        :hideproviders="isMobile()"
                        >
                    </listviewItem>
              </v-list>
            </v-card>
          </v-tab-item>
        </v-tabs>
      </section>`,
  props: ['media_id', 'provider'],
  data() {
    return {
      selected: [2],
      info: {},
      toptracks: [],
      artistalbums: [],
      bg_image: "../images/info_gradient.jpg",
      active: null,
      playmenu: false,
      playmenuitem: null
    }
  },
  created() {
    this.$globals.windowtitle = ""
    this.getInfo();
  },
  methods: {
    getFanartImage() {
      if (this.info.metadata && this.info.metadata.fanart)
        return this.info.metadata.fanart;
      else if (this.info.artists)
        for (artist in this.info.artists)
          if (artist.info.metadata && artist.data.metadata.fanart)
              return artist.metadata.fanart;
    },
    getInfo (lazy=true) {
      this.$globals.loading = true;
      const api_url = '/api/artists/' + this.media_id
      axios
        .get(api_url, { params: { lazy: lazy, provider: this.provider }})
        .then(result => {
          data = result.data;
          this.info = data;
          this.$globals.loading = false;
          if (data.is_lazy == true)
              // refresh the info if we got a lazy object
              this.timeout1 = setTimeout(function(){
                  this.getInfo(false);
              }.bind(this), 1000);
          else {
            this.getArtistTopTracks();
            this.getArtistAlbums();
          }
        })
        .catch(error => {
          console.log("error", error);
          this.$globals.loading = false;
        });
    },
    getArtistTopTracks () {
      
      const api_url = '/api/artists/' + this.media_id + '/toptracks'
      axios
      .get(api_url, { params: { provider: this.provider }})
        .then(result => {
          data = result.data;
          this.toptracks = data;
        })
        .catch(error => {
          console.log("error", error);
        });
        
    },
    getArtistAlbums () {
      const api_url = '/api/artists/' + this.media_id + '/albums'
      console.log('loading ' + api_url);
      axios
      .get(api_url, { params: { provider: this.provider }})
        .then(result => {
          data = result.data;
          this.artistalbums = data;
        })
        .catch(error => {
          console.log("error", error);
        });
    },
  }
})
