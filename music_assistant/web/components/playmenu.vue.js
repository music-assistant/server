Vue.component("playmenu", {
	template: `
	<v-dialog :value="value" @input="$emit('input', $event)" max-width="500px" v-if="$globals.playmenuitem">
        <v-card>
		<v-list>
		<v-subheader class="title">{{ !!$globals.playmenuitem ? $globals.playmenuitem.name : 'nix' }}</v-subheader>
			<v-subheader>{{ $t('play_on') }} {{ active_player.name }}</v-subheader>
			
			<v-list-tile avatar @click="itemClick('play')">
				<v-list-tile-avatar>
					<v-icon>play_circle_outline</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>{{ $t('play_now') }}</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider></v-divider>

			<v-list-tile avatar @click="itemClick('next')">
				<v-list-tile-avatar>
					<v-icon>queue_play_next</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>{{ $t('play_next') }}</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider></v-divider>

			<v-list-tile avatar @click="itemClick('add')">
				<v-list-tile-avatar>
					<v-icon>playlist_add</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>{{ $t('add_queue') }}</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider></v-divider>

			<v-list-tile avatar @click="itemClick('info')" v-if="$globals.playmenuitem.media_type == 3">
				<v-list-tile-avatar>
					<v-icon>info</v-icon>
				</v-list-tile-avatar>
				<v-list-tile-content>
					<v-list-tile-title>{{ $t('show_info') }}</v-list-tile-title>
				</v-list-tile-content>
			</v-list-tile>
			<v-divider v-if="$globals.playmenuitem.media_type == 3"/>
			
		</v-list>
        </v-card>
      </v-dialog>
`,
	props: ['value', 'active_player'],
	data (){
		return{
			fav: true,
			message: false,
			hints: true,
			}
	},
	mounted() { },
	created() { },
	methods: { 
		itemClick(cmd) {
      if (cmd == 'info')
				this.$router.push({ path: '/tracks/' + this.$globals.playmenuitem.item_id, params: {provider: this.$globals.playmenuitem.provider}})
			else
				this.$emit('playItem', this.$globals.playmenuitem, cmd)
			// close dialog
			this.$globals.showplaymenu = false;
    },
	}
  })
