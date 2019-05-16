Vue.component("listviewItem", {
  template: `
    <div>
    <v-list-tile
    avatar
    ripple
    @click="clickItem(item)">

          <v-list-tile-avatar color="grey" v-if="!hideavatar">
              <img v-if="(item.media_type != 3) && item.metadata && item.metadata.image" :src="item.metadata.image"/>
              <img v-if="(item.media_type == 3) && item.album && item.album.metadata && item.album.metadata.image" :src="item.album.metadata.image"/>
              <v-icon v-if="(item.media_type == 3) && item.album && item.album.metadata && !item.album.metadata.image">audiotrack</v-icon>
              <v-icon v-if="(item.media_type != 1 && item.media_type != 3) && (!item.metadata || !item.metadata.image)">album</v-icon>
              <v-icon v-if="(item.media_type == 1) && (!item.metadata || !item.metadata.image)">person</v-icon>
              <v-icon v-if="(item.media_type == 3) && (!item.metadata || !item.album.metadata.image)">audiotrack</v-icon>
          </v-list-tile-avatar>
          
          <v-list-tile-content>
            
            <v-list-tile-title>
                {{ item.name }}<span v-if="!!item.version"> ({{ item.version }})</span>
            </v-list-tile-title>
            
            <v-list-tile-sub-title v-if="item.artists">
                <span v-for="(artist, artistindex) in item.artists">
                    <a v-on:click="clickItem(artist)" @click.stop="">{{ artist.name }}</a>
                    <label v-if="artistindex + 1 < item.artists.length" :key="artistindex"> / </label>
                </span>
                <a v-if="!!item.album && !!hidetracknum" v-on:click="clickItem(item.album)" @click.stop="" style="color:grey">  -  {{ item.album.name }}</a>
                <label v-if="!hidetracknum && item.track_number" style="color:grey">  -  disc {{ item.disc_number }} track {{ item.track_number }}</label>
            </v-list-tile-sub-title>
            <v-list-tile-sub-title v-if="item.artist">
                <a v-on:click="clickItem(item.artist)" @click.stop="">{{ item.artist.name }}</a>
            </v-list-tile-sub-title>

            <v-list-tile-sub-title v-if="!!item.owner">
                {{ item.owner }}
            </v-list-tile-sub-title>

          </v-list-tile-content>

          <providericons v-bind:item="item" :height="20" :compact="true" :dark="true" :hiresonly="hideproviders"/>

          <v-list-tile-action v-if="!hidelibrary">
              <v-tooltip bottom>
                  <template v-slot:activator="{ on }">
                      <v-btn icon ripple v-on="on" v-on:click="toggleLibrary(item)" @click.stop="" >
                          <v-icon height="20" v-if="item.in_library.length > 0">favorite</v-icon>
                          <v-icon height="20" v-if="item.in_library.length == 0">favorite_border</v-icon>
                      </v-btn>
                  </template>
                  <span v-if="item.in_library.length > 0">Item is added to the library</span>
                  <span v-if="item.in_library.length == 0">Add item to the library</span>
              </v-tooltip>
          </v-list-tile-action>

          <v-list-tile-action v-if="!hideduration && !!item.duration">
              {{ item.duration.toString().formatDuration() }}
          </v-list-tile-action> 
        
          <!-- menu button/icon -->
          <v-icon v-if="!hidemenu" @click="showPlayMenu(item)" @click.stop="" color="grey lighten-1" style="margin-right:-10px;padding-left:10px">more_vert</v-icon>
          

        </v-list-tile>
        <v-divider v-if="index + 1 < totalitems" :key="index"></v-divider>
        </div>
     `,
props: ['item', 'index', 'totalitems', 'hideavatar', 'hidetracknum', 'hideproviders', 'hidemenu', 'hidelibrary', 'hideduration'],
data() {
  return {}
  },
methods: {
  }
})
