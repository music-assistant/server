Vue.component("qualityicon", {
	template: `
			<div :style="'height:' + height + 'px;'">
				
				<v-tooltip bottom>
						<template v-slot:activator="{ on }">
								<img height="100%" v-on="on" v-if="item.metadata && item.metadata.hires" src="images/icons/hires.png" style="align:center;vertical-align: middle;padding-right:10px;padding-left:10px;"/>
								<img height="100%" v-on="on" v-if="!dark && !hiresonly" :src="getFileFormatLogo()" style="align:center;vertical-align: middle;"/>
								<img height="100%" v-on="on" v-if="dark && !hiresonly" :src="getFileFormatLogo()" style="filter: invert(1);align:center;vertical-align: middle;"/>
						</template>
						<span>{{ getFileFormatDesc() }}</span>
				</v-tooltip>
				<span v-if="!compact" class="body-2" style="vertical-align: middle;">{{ getFileFormatDesc() }}</span>
			</div>
`,
	props: ['item','height','compact', 'dark', 'hiresonly'],
	data (){
		return{}
	},
	mounted() { },
	created() { },
	methods: { 

		getFileFormatLogo() {
			if (this.item.quality == 0)
				return 'images/icons/mp3.png'
			else if (this.item.quality == 1)
				return 'images/icons/vorbis.png'
			else if (this.item.quality == 2)
				return 'images/icons/aac.png'
			else if (this.item.quality > 2)
				return 'images/icons/flac.png'
			},
			getFileFormatDesc() {
				var desc = '';
				if (this.item && this.item.quality)
				{
					if (this.item.quality == 0)
						desc = 'MP3';
					else if (this.item.quality == 1)
						desc = 'Ogg Vorbis';
					else if (this.item.quality == 2)
						desc = 'AAC';
					else if (this.item.quality > 2)
						desc = 'FLAC';
					else
						desc = 'unknown';
				}
				// append details
				if (this.item && this.item.metadata)
				{
					if (!!this.item.metadata && this.item.metadata.maximum_technical_specifications)
						desc += ' ' + this.item.metadata.maximum_technical_specifications;
					if (!!this.item.metadata && this.item.metadata.sample_rate && this.item.metadata.bit_depth)
						desc += ' ' + this.item.metadata.sample_rate + 'kHz ' + this.item.metadata.bit_depth + 'bit';
				}
				return desc;
				}
    }
  })
