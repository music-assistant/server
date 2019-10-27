Vue.component("contextmenu", {
	template: `
	<v-dialog :value="value" @input="$emit('input', $event)" max-width="500px">
		<v-card>
			<v-list>
				<v-subheader class="title">{{ header }}</v-subheader>
				<v-subheader v-if="subheader">{{ subheader }}</v-subheader>
				<!-- normal contextmenu items -->
				<div v-if="!show_playlists" v-for="(item, index) in menuItems">
					<v-list-tile avatar @click="itemCommand(item.action)">
						<v-list-tile-avatar>
							<v-icon>{{item.icon}}</v-icon>
						</v-list-tile-avatar>
						<v-list-tile-content>
							<v-list-tile-title>{{ $t(item.label) }}</v-list-tile-title>
						</v-list-tile-content>
					</v-list-tile>
					<v-divider></v-divider>
				</div>
				<listviewItem v-if="show_playlists"
					v-for="(item, index) in playlists"
					:key="item.item_id"
					v-bind:item="item"
					v-bind:totalitems="playlists.length"
					v-bind:index="index"
					:hideavatar="false"
					:hidetracknum="true"
					:hideproviders="false"
					:hidelibrary="true"
					:hidemenu="true"
					:context="'selectplaylist'"
					:onClick="playlistSelected"
					>
				</listviewItem>
			</v-list>
		</v-card>
      </v-dialog>
`,
	props: ['value', 'active_player'],
	watch: {
		value: function (val) {
			if (!val)
				this.show_playlists = false;
		}
	},
	data () { 
		return {
			mediaPlayItems: [
				{
					label: "play_now",
					action: "play",
					icon: "play_circle_outline"
				},
				{
					label: "play_next",
					action: "next",
					icon: "queue_play_next"
				},
				{
					label: "add_queue",
					action: "add",
					icon: "playlist_add"
				}
			],
			showTrackInfoItem: {
					label: "show_info",
					action: "info",
					icon: "info"
			},
			addToPlaylistItem: {
					label: "add_playlist",
					action: "add_playlist",
					icon: "add_circle_outline"
			},
			removeFromPlaylistItem: {
					label: "remove_playlist",
					action: "remove_playlist",
					icon: "remove_circle_outline"
			},
			playerQueueItems: [
			],
			playlists: [],
			show_playlists: false
		}
	},
	mounted() { },
	created() { },
	computed: {
		menuItems() {
			if (!this.$globals.contextmenuitem)
				return [];
			else if (this.show_playlists)
				return this.playlists;
			else if (this.$globals.contextmenucontext == 'playerqueue')
				return this.playerQueueItems; // TODO: return queue contextmenu
			else if (this.$globals.contextmenucontext == 'trackdetails') {
				// track details
				var items = [];
				items.push(...this.mediaPlayItems);
				items.push(this.addToPlaylistItem);
				return items;
			}
			else if (this.$globals.contextmenuitem.media_type == 3) {
				// track item in list
				var items = [];
				items.push(...this.mediaPlayItems);
				items.push(this.showTrackInfoItem);
				items.push(this.addToPlaylistItem);
				if (this.$globals.contextmenucontext.is_editable)
					items.push(this.removeFromPlaylistItem);
				return items;
			}
			else {
				// all other playable media
				return this.mediaPlayItems;
			}
		},
		header() {
			if (this.show_playlists)
				return this.$t('add_playlist');
			else if (!this.$globals.contextmenuitem)
				return "";
			else
				return this.$globals.contextmenuitem.name;
		},
		subheader() {
			if (this.show_playlists && !!this.$globals.contextmenuitem)
				return this.$globals.contextmenuitem.name;
			else if (!this.active_player)
				return "";
			else
				return this.$t('play_on') + this.active_player.name;
		}
	},
	methods: { 
		itemCommand(cmd) {
      		if (cmd == 'info') {
				// show track info
				this.$router.push({ path: '/tracks/' + this.$globals.contextmenuitem.item_id, query: {provider: this.$globals.contextmenuitem.provider}})
				this.$globals.showcontextmenu = false;
			}	
			else if (cmd == 'add_playlist') {
				// add to playlist
				this.getPlaylists();
				this.show_playlists = true;
			}
			else if (cmd == 'remove_playlist') {
				// remove track from playlist
				this.playlistAddRemove(this.$globals.contextmenuitem, this.$globals.contextmenucontext.item_id, 'playlist_remove');
				this.$globals.showcontextmenu = false;
			}
			else {
				// assume play command
				this.$emit('playItem', this.$globals.contextmenuitem, cmd)
				this.$globals.showcontextmenu = false;
			}
			
		},
		playlistSelected(playlistobj) {
			this.playlistAddRemove(this.$globals.contextmenuitem, playlistobj.item_id, 'playlist_add');
			this.$globals.showcontextmenu = false;
		},
		playlistAddRemove(track, playlist_id, action='playlist_add') {
			/// add or remove track on playlist
			var url = `${this.$globals.server}api/track/${track.item_id}`;
			axios
				.get(url, { params: { 
					provider: track.provider, 
					action: action, 
					action_details: playlist_id
				}})
				.then(result => {
					// reload listing
					if (action == 'playlist_remove')
						this.$router.go()
					})
				.catch(error => {
					console.log("error", error);
				});
		},
		getPlaylists() {
			// get all editable playlists
			const api_url = this.$globals.apiAddress + 'playlists';
			let track_provs = [];
			for (var prov of this.$globals.contextmenuitem.provider_ids)
				track_provs.push(prov.provider);
			axios
				.get(api_url, { })
				.then(result => {
					let items = []
					for (var playlist of result.data) {
						if (playlist.is_editable && playlist.item_id != this.$globals.contextmenucontext.item_id)
							for (var prov of playlist.provider_ids)
								if (track_provs.includes(prov.provider))
								{
									items.push(playlist);
									break
								}
					}
					this.playlists = items;
				})
				.catch(error => {
					console.log("error", error);
					this.playlists = [];
			});
		}
	}
  })
