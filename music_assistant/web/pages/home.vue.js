var home = Vue.component("Home", {
  template: `
  <section>
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
    this.$globals.windowtitle = this.$t('home');
    this.items= [
        { title: this.$t('artists'), icon: "person", path: "/artists" },
        { title: this.$t('albums'), icon: "album", path: "/albums" },
        { title: this.$t('tracks'), icon: "audiotrack", path: "/tracks" },
        { title: this.$t('playlists'), icon: "playlist_play", path: "/playlists" },
        { title: this.$t('search'), icon: "search", path: "/search" }
    ]
  },
  methods: {
    click (item) {
      console.log("selected: "+ item.path);
      router.push({path: item.path})
    }
  }
});
