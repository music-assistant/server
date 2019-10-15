Vue.component("providericons", {
	template: `
		<div :style="'height:' + height + 'px;'">
			<span v-for="provider in uniqueProviders" :key="provider.item_id" style="padding:5px;vertical-align: middle;" v-if="!hiresonly || provider.quality > 6">
				<v-tooltip bottom>
					<template v-slot:activator="{ on }">
						<img v-on="on" :height="height" src="images/icons/hires.png" v-if="provider.quality > 6" style="margin-right:9px"/>
						<img v-on="on" :height="height" :src="'images/icons/' + provider.provider + '.png'" v-if="!hiresonly"/>
					</template>
					<div align="center" v-if="item.media_type == 3">
						<img height="35px" :src="getFileFormatLogo(provider)"/>
						<span><br>{{ getFileFormatDesc(provider) }}</span>
					</div>
					<span v-if="item.media_type != 3">{{ provider.provider }}</span>
				</v-tooltip> 
			</span>		
		</div>
`,
	props: ['item','height','compact', 'dark', 'hiresonly'],
	data (){
		return{}
	},
	mounted() { },
	created() { },
	computed: {
		uniqueProviders() {
			var keys = [];
			var qualities = [];
			if (!this.item || !this.item.provider_ids)
				return []
			let sorted_item_ids = this.item.provider_ids.sort((a,b) => (a.quality < b.quality) ? 1 : ((b.quality < a.quality) ? -1 : 0));
			if (!this.compact)
				return sorted_item_ids;
			for (provider of sorted_item_ids) {
				if (!keys.includes(provider.provider)){
					qualities.push(provider);
					keys.push(provider.provider);
				}
			}
			return qualities;
		}
	},
	methods: { 

		getFileFormatLogo(provider) {
			if (provider.quality == 0)
				return 'images/icons/mp3.png'
			else if (provider.quality == 1)
				return 'images/icons/vorbis.png'
			else if (provider.quality == 2)
				return 'images/icons/aac.png'
			else if (provider.quality > 2)
				return 'images/icons/flac.png'
			},
		getFileFormatDesc(provider) {
			var desc = '';
			if (provider.details)
				desc += ' ' + provider.details;
			return desc;
		},
		getMaxQualityFormatDesc() {
			var desc = '';
			if (provider.details)
				desc += ' ' + provider.details;
			return desc;
		}
    }
  })
