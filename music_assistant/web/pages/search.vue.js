var Search = Vue.component('Search', {
  template: `
  <section>

    <v-text-field
        solo
        clearable
        :label="$t('type_to_search')"
        prepend-inner-icon="search"
        v-model="searchQuery">
      </v-text-field>

      <v-tabs
          v-model="active"
          color="transparent"
          light
          slider-color="black"
        >

        <v-tab ripple v-if="tracks.length">{{ $t('tracks') }}</v-tab>
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

          <v-tab ripple v-if="artists.length">{{ $t('artists') }}</v-tab>
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

          <v-tab ripple v-if="albums.length">{{ $t('albums') }}</v-tab>
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

          <v-tab ripple v-if="playlists.length">{{ $t('playlists') }}</v-tab>
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
  props: ['searchQuery'],
  data() {
    return {
      selected: [2],
      artists: [],
      albums: [],
      tracks: [],
      playlists: [],
      timeout: null,
    }
  },
  created() {
    this.$globals.windowtitle = this.$t('search');
    this.Search();
  },
  watch: {
    searchQuery () {
      this.Search();
    }
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
    Search () {
      this.artists = [];
      this.albums = [];
      this.tracks = [];
      this.playlists = [];
      if (this.searchQuery) {
        this.$globals.loading = true;
        console.log(this.searchQuery);
        const api_url = '/api/search'
        console.log('loading ' + api_url);
          axios
            .get(api_url, {
              params: {
                query: this.searchQuery,
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
