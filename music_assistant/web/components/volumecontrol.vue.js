Vue.component("volumecontrol", {
	template: `
	<v-card>
				<v-list>
					<v-list-tile avatar>
						<v-list-tile-avatar>
								<v-icon large>{{ isGroup ? 'speaker_group' : 'speaker' }}</v-icon>
						</v-list-tile-avatar>
						<v-list-tile-content>
							<v-list-tile-title>{{ players[player_id].name }}</v-list-tile-title>
							<v-list-tile-sub-title>{{ $t('state.' + players[player_id].state) }}</v-list-tile-sub-title>
						</v-list-tile-content>
						</v-list-tile-action>
					</v-list-tile>
				</v-list>

				<v-divider></v-divider>

				<v-list two-line>

					<div v-for="child_id in volumePlayerIds" :key="child_id">
							<v-list-tile>
							
							<v-list-tile-content>

								<v-list-tile-title>
								</v-list-tile-title>
								<div class="v-list__tile__sub-title" style="position: absolute; left:47px; top:10px; z-index:99;">
									<span :style="!players[child_id].powered ? 'color:rgba(0,0,0,.38);' : 'color:rgba(0,0,0,.54);'">{{ players[child_id].name }}</span>
								</div>
								<div class="v-list__tile__sub-title" style="position: absolute; left:0px; top:-4px; z-index:99;">
									<v-btn icon @click="$emit('togglePlayerPower', child_id)">
										<v-icon :style="!players[child_id].powered ? 'color:rgba(0,0,0,.38);' : 'color:rgba(0,0,0,.54);'">power_settings_new</v-icon>
									</v-btn>
								</div>
								<v-list-tile-sub-title>
									<v-slider lazy :disabled="!players[child_id].powered" v-if="!players[child_id].disable_volume"
										:value="Math.round(players[child_id].volume_level)"
										prepend-icon="volume_down"
										append-icon="volume_up"
										@end="$emit('setPlayerVolume', child_id, $event)"
										@click:append="$emit('setPlayerVolume', child_id, 'up')"
        								@click:prepend="$emit('setPlayerVolume', child_id, 'down')"
									></v-slider>
								</v-list-tile-sub-title>
							</v-list-tile-content>
						</v-list-tile>
						<v-divider></v-divider>
					</div>
					
				</v-list>

				<v-spacer></v-spacer>
			</v-card>
`,
	props: ['value', 'players', 'player_id'],
	data (){
		return{
			}
	},
	computed: {
			volumePlayerIds() {
			var volume_ids = [this.player_id];
			for (var player_id in this.players)
				if (this.players[player_id].group_parent == this.player_id && this.players[player_id].enabled)
					volume_ids.push(player_id);
			return volume_ids;
		},
		isGroup() {
			return this.volumePlayerIds.length > 1;
		}
  },
	mounted() { },
	created() { },
	methods: {}
  })
