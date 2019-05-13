Vue.component("playmenu", {
	template: `
	<v-dialog :value="value" @input="$emit('input', $event)" max-width="500px" v-if="$globals.playmenuitem">
        <v-card>
		<v-list>
		<v-subheader>{{ !!$globals.playmenuitem ? $globals.playmenuitem.name : 'nix' }}</v-subheader>
			<v-subheader>Play on: beneden</v-subheader>
			
			<v-list-tile avatar @click="$emit('playItem', $globals.playmenuitem, 'play')">
				<v-list-tile-avatar>
					<v-icon>play_circle_outline</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>Play Now</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider></v-divider>

			<v-list-tile avatar @click="$emit('playItem', $globals.playmenuitem, 'replace')">
				<v-list-tile-avatar>
					<v-icon>play_circle_outline</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>Replace</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider></v-divider>

			<v-list-tile avatar @click="$emit('playItem', $globals.playmenuitem, 'next')">
				<v-list-tile-avatar>
					<v-icon>queue_play_next</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>Play Next</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider></v-divider>

			<v-list-tile avatar @click="$emit('playItem', $globals.playmenuitem, 'add')">
				<v-list-tile-avatar>
					<v-icon>playlist_add</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>Add to Queue</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider></v-divider>

			<v-list-tile avatar @click="" v-if="$globals.playmenuitem.media_type != 3">
				<v-list-tile-avatar>
					<v-icon>shuffle</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>Play now (shuffle)</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider v-if="$globals.playmenuitem.media_type != 3"/>

			<v-list-tile avatar @click="" v-if="$globals.playmenuitem.media_type == 3">
				<v-list-tile-avatar>
					<v-icon>info</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>Show info</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider v-if="$globals.playmenuitem.media_type == 3"/>
			
		</v-list>
        </v-card>
      </v-dialog>
`,
	props: ['value'],
	data (){
		return{
			fav: true,
			message: false,
			hints: true,
			}
	},
	mounted() { },
	created() { },
	methods: { }
  })
