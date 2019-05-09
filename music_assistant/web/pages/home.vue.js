var home = Vue.component("Home", {
  template: `
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
      { title: 'Artists', path: '/browse/library/artists', icon: "person" },
      { title: 'Albums', path: '/browse/library/albums', icon: "album" },
      { title: 'Tracks', path: '/browse/library/tracks', icon: "audiotrack" },
      { title: 'Playlists', path: '/browse/library/playlists', icon: "playlist_play" }
    ]
  },
  methods: {
    click (item) {
      console.log("selected: "+ item.path);
      router.push({path: item.path})
    }
  }
});
