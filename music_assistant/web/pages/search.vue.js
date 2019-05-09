var Search = Vue.component('Search', {
  template: `
  <section>
      <v-flex xs12 justify-center>
        <v-card color="cyan darken-2" class="white--text" img="../images/info_gradient.jpg">
        
          <div class="text-xs-center" style="height:40px" id="whitespace_top"/>      
          <v-card-title class="display-1 justify-center" style="text-shadow: 1px 1px #000000;">
              {{ $globals.windowtitle }}
          </v-card-title>
        </v-card>
      </v-flex>    
      <v-text-field
        solo
        clearable
        label="Type here to search..."
        prepend-inner-icon="search"
        v-on:input="onSearchBoxInput"
        v-model="searchquery">
      </v-text-field>

      <v-tabs
          v-model="active"
          color="transparent"
          light
          slider-color="black"
        >

        <v-tab ripple v-if="tracks.length">Tracks</v-tab>
          <v-tab-item v-if="tracks.length">
            <v-card flat>
                <v-list two-line style="margin-left:15px; margin-right:15px">
                    <listviewItem 
                        v-for="(item, index) in tracks" 
                        v-bind:item="item"
                        :key="item.db_id"
                        v-bind:totalitems="tracks.length"
                        v-bind:index="index"
                        :hideavatar="isMobile()"
                        :hidetracknum="true"
                        :hideproviders="isMobile()"
                        :hideduration="isMobile()"
                        :showlibrary="true">
                    </listviewItem>
              </v-list>
            </v-card>
          </v-tab-item>

          <v-tab ripple v-if="artists.length">Artists</v-tab>
          <v-tab-item v-if="artists.length">
            <v-card flat>
            <v-list two-line>
                  <listviewItem 
                      v-for="(item, index) in artists" 
                      v-bind:item="item"
                      :key="item.db_id"
                      v-bind:totalitems="artists.length"
                      v-bind:index="index"
                      :hideproviders="isMobile()"
                      >
                  </listviewItem>
                </v-list>
            </v-card>
          </v-tab-item>

          <v-tab ripple v-if="albums.length">Albums</v-tab>
          <v-tab-item v-if="albums.length">
            <v-card flat>
                <v-list two-line>
                    <listviewItem 
                        v-for="(item, index) in albums" 
                        v-bind:item="item"
                        :key="item.db_id"
                        v-bind:totalitems="albums.length"
                        v-bind:index="index"
                        :hideproviders="isMobile()"
                        >
                    </listviewItem>
              </v-list>
            </v-card>
          </v-tab-item>

          <v-tab ripple v-if="playlists.length">Playlists</v-tab>
          <v-tab-item v-if="playlists.length">
            <v-card flat>
                <v-list two-line>
                    <listviewItem 
                        v-for="(item, index) in playlists" 
                        v-bind:item="item"
                        :key="item.db_id"
                        v-bind:totalitems="playlists.length"
                        v-bind:index="index"
                        :hidelibrary="true">
                    </listviewItem>
              </v-list>
            </v-card>
          </v-tab-item>

        </v-tabs>

      </section>`,
  props: [],
  data() {
    return {
      selected: [2],
      artists: [],
      albums: [],
      tracks: [],
      playlists: [],
      timeout: null,
      searchquery: ''
    }
  },
  created() {
    this.$globals.windowtitle = "Search";
  },
  methods: {
    toggle (index) {
      const i = this.selected.indexOf(index)
      if (i > -1) {
        this.selected.splice(i, 1)
      } else {
        this.selected.push(index)
        console.log("selected: "+ this.items[index].name);
      }
    },
    onSearchBoxInput (index) {
      clearTimeout(this.timeout);
      this.timeout = setTimeout(this.Search, 600);
    },
    Search () {
      if (!this.searchquery) {
        this.artists = [];
        this.albums = [];
        this.tracks = [];
        this.playlists = [];
      }
      else {
        this.$globals.loading = true;
        console.log(this.searchquery);
        const api_url = '/api/search'
        console.log('loading ' + api_url);
          axios
            .get(api_url, {
              params: {
                query: this.searchquery,
                online: true,
                limit: 3
              }
            })
            .then(result => {
              data = result.data;
              this.artists = data.artists;
              this.albums = data.albums;
              this.tracks = data.tracks;
              this.playlists = data.playlists;
              this.$globals.loading = false;
            })
            .catch(error => {
              console.log("error", error);
            });
        } 
        
    },
  }
})
