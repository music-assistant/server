Vue.component("listdialog", {
	template: `
	<v-dialog :value="value" @input="$emit('input', $event)" max-width="500px">
        <v-card>
			<v-list>
				<v-subheader class="title">{{ header }}</v-subheader>
				<v-subheader>{{ subheader }}</v-subheader>
				<div v-for="(item, index) in items">
					<v-list-tile avatar @click="$emit('onClick', item)">
						<v-list-tile-avatar>
							<v-icon>{{item.icon}}</v-icon>
						</v-list-tile-avatar>
						<v-list-tile-content>
							<v-list-tile-title>{{ $t(item.label) }}</v-list-tile-title>
						</v-list-tile-content>
					</v-list-tile>
					<v-divider></v-divider>
				</div>
			</v-list>
		</v-card>
      </v-dialog>
`,
	props: ['value', 'items', 'header', 'subheader'],
	data () { 
		return {}
	},
	mounted() { },
	created() { },
	computed: { },
	methods: { }
  })
