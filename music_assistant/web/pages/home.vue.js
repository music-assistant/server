var home = Vue.component("Home", {
  template: `
  <section>
      <v-flex xs12 justify-center>
        <v-card color="cyan darken-2" class="white--text" img="../images/info_gradient.jpg">
        
          <div class="text-xs-center" style="height:40px" id="whitespace_top"/>      
          <v-card-title class="display-1 justify-center" style="text-shadow: 1px 1px #000000;">
              Music Assistant
          </v-card-title>
        </v-card>
      </v-flex>    
      <v-list>
        <v-list-tile 
          v-for="item in items" :key="item.title" @click="$router.push(item.path)">
            <v-list-tile-action style="margin-left:15px">
                <v-icon>{{ item.icon }}</v-icon>
            </v-list-tile-action>
            <v-list-tile-content>
                <v-list-tile-title>{{ item.title }}</v-list-tile-title>
            </v-list-tile-content>
        </v-list-tile>
      </v-list>
  </section>
`,
  props: ["title"],
  $_veeValidate: {
    validator: "new"
  },
  data() {
    return {
      result: null,
      showProgress: false
    };
  },
  created() {
    this.$globals.windowtitle = "Home"
    this.items= [
        { title: "Artists", icon: "person", path: "/artists" },
        { title: "Albums", icon: "album", path: "/albums" },
        { title: "Tracks", icon: "audiotrack", path: "/tracks" },
        { title: "Playlists", icon: "playlist_play", path: "/playlists" },
        { title: "Search", icon: "search", path: "/search" }
    ]
  },
  methods: {
    click (item) {
      console.log("selected: "+ item.path);
      router.push({path: item.path})
    }
  }
});
