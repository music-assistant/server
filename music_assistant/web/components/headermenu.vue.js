Vue.component("headermenu", {
  template: `<div>
    <v-navigation-drawer dark app clipped temporary v-model="menu">
        <v-list >
            <v-list-tile
               v-for="item in items" :key="item.title" @click="$router.push(item.path)">
                <v-list-tile-action>
                    <v-icon>{{ item.icon }}</v-icon>
                </v-list-tile-action>
                <v-list-tile-content>
                    <v-list-tile-title>{{ item.title }}</v-list-tile-title>
                </v-list-tile-content>
            </v-list-tile>
        </v-list>
    </v-navigation-drawer>
    
    <v-toolbar fixed flat dense dark color="transparent" scroll-off-screen > 
        <v-layout align-center>
            <v-btn icon v-on:click="menu=!menu">
              <v-icon>menu</v-icon>
            </v-btn>
            <v-btn @click="$router.go(-1)" icon>
              <v-icon>arrow_back</v-icon>
            </v-btn>
            <v-spacer></v-spacer>
            <v-spacer></v-spacer>
            <v-btn icon v-on:click="$router.push('/search')">
                <v-icon>search</v-icon>
              </v-btn>
        </v-layout>
    </v-toolbar>
</div>`,
  props: [],
  $_veeValidate: {
    validator: "new"
  },
  data() {
    return {
      menu: false,
      items: [
        { title: "Home", icon: "home", path: "/" },
        { title: "Artists", icon: "person", path: "/artists" },
        { title: "Albums", icon: "album", path: "/albums" },
        { title: "Tracks", icon: "audiotrack", path: "/tracks" },
        { title: "Playlists", icon: "playlist_play", path: "/playlists" },
        { title: "Search", icon: "search", path: "/search" },
        { title: "Config", icon: "settings", path: "/config" }
      ]
    }
  },
  mounted() { },
  methods: { }
})
