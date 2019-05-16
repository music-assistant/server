Vue.component("infoheader", {
	template: `
		<v-flex xs12>
          <v-card color="cyan darken-2" class="white--text" img="../images/info_gradient.jpg">
            <v-img
              class="white--text"
              width="100%"
              :height="isMobile() ? '300' : '370'"
              position="center top" 
              :src="getFanartImage()"
              gradient="to top right, rgba(100,115,201,.33), rgba(25,32,72,.7)"
            >
            <div class="text-xs-center" style="height:40px" id="whitespace_top"/>

            <v-layout style="margin-left:5px;margin-right:5px">
              
              <!-- left side: cover image -->
              <v-flex xs5 pa-4 v-if="!isMobile()">
								<v-img :src="getThumb()" lazy-src="/images/default_artist.png" width="250px" height="250px" style="border: 4px solid grey;border-radius: 15px;"></v-img>
								
								<!-- tech specs and provider icons -->
								<div style="margin-top:10px;">
									<providericons v-bind:item="info" :height="30" :compact="false"/>
								</div>
              </v-flex>
              
              <v-flex>
                  <!-- Main title -->
                  <v-card-title class="display-1" style="text-shadow: 1px 1px #000000;padding-bottom:0px;">
											{{ info.name }} 
											<span class="subheading" v-if="!!info.version" style="padding-left:10px;"> ({{ info.version }})</span>
									</v-card-title>
									
									<!-- item artists -->
									<v-card-title style="text-shadow: 1px 1px #000000;padding-top:0px;padding-bottom:10px;">
											<span v-if="!!info.artists" v-for="(artist, artistindex) in info.artists" class="headline" :key="artist.db_id">
													<a style="color:#2196f3" v-on:click="clickItem(artist)">{{ artist.name }}</a>
													<label style="color:#2196f3" v-if="artistindex + 1 < info.artists.length" :key="artistindex"> / </label>
											</span>
											<span v-if="info.artist" class="headline">
													<a style="color:#2196f3" v-on:click="clickItem(info.artist)">{{ info.artist.name }}</a>
											</span>
											<span v-if="info.owner" class="headline">
													<a style="color:#2196f3" v-on:click="">{{ info.owner }}</a>
											</span>
									</v-card-title>

									<v-card-title v-if="info.album" style="color:#ffffff;text-shadow: 1px 1px #000000;padding-top:0px;padding-bottom:10px;">
											<a class="headline" style="color:#ffffff" v-on:click="clickItem(info.album)">{{ info.album.name }}</a>
									</v-card-title>

                  <!-- play/info buttons -->
                  <div style="margin-left:8px;">
                      <v-btn color="blue-grey" @click="showPlayMenu(info)"  class="white--text"><v-icon v-if="!isMobile()" left dark>play_circle_outline</v-icon>Play</v-btn>
                      <v-btn v-if="!!info.in_library && info.in_library.length == 0" color="blue-grey" @click="toggleLibrary(info)"  class="white--text"><v-icon v-if="!isMobile()" left dark>favorite_border</v-icon>Add to library</v-btn>
                      <v-btn v-if="!!info.in_library && info.in_library.length > 0" color="blue-grey" @click="toggleLibrary(info)"  class="white--text"><v-icon v-if="!isMobile()" left dark>favorite</v-icon>Remove from library</v-btn>
                  </div>

                  <!-- Description/metadata -->
                  <v-card-title class="subheading">
                      <div class="justify-left" style="text-shadow: 1px 1px #000000;">
                          <read-more :text="getDescription()" :max-chars="isMobile() ? 200 : 350"></read-more>
                      </div>
                  </v-card-title>

              </v-flex>
            </v-layout>
              
            </v-img>
            <div class="text-xs-center" v-if="info.tags">
                <v-chip small color="white"  outline v-for="(tag, index) in info.tags" :key="tag" >{{ tag }}</v-chip>
            </div>
            
          </v-card>
        </v-flex>
`,
	props: ['info'],
	data (){
		return{}
	},
	mounted() { },
	created() { },
	methods: { 
		getFanartImage() {
			var img = '';
			if (!this.info)
				return ''
      if (this.info.metadata && this.info.metadata.fanart)
				img = this.info.metadata.fanart;
			else if (this.info.artists)
					this.info.artists.forEach(function(artist) {
						if (artist.metadata && artist.metadata.fanart)
							img = artist.metadata.fanart;
					});
			else if (this.info.artist && this.info.artist.metadata.fanart)
				img = this.info.artist.metadata.fanart;
			return img;
		},
		getThumb() {
			var img = '';
			if (!this.info)
				return ''
      if (this.info.metadata && this.info.metadata.image)
				img = this.info.metadata.image;
			else if (this.info.album && this.info.album.metadata && this.info.album.metadata.image)
				img = this.info.album.metadata.image;
			else if (this.info.artists)
					this.info.artists.forEach(function(artist) {
						if (artist.metadata && artist.metadata.image)
							img = artist.metadata.image;
					});
			return img;
		},
		getDescription() {
			var desc = '';
			if (!this.info)
				return ''
      if (this.info.metadata && this.info.metadata.description)
				return this.info.metadata.description;
			else if (this.info.metadata && this.info.metadata.biography)
				return this.info.metadata.biography;
			else if (this.info.metadata && this.info.metadata.copyright)
				return this.info.metadata.copyright;
			else if (this.info.artists)
			{
				this.info.artists.forEach(function(artist) {
					console.log(artist.metadata.biography);
					if (artist.metadata && artist.metadata.biography)
							desc = artist.metadata.biography;
				});
			}
			return desc;
		},
	}
})
