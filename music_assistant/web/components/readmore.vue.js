Vue.component("read-more", {
	template: `
	<div>
		<span v-html="formattedString"/> <a style="color:white" :href="link" id="readmore" v-if="text.length > maxChars" v-on:click="triggerReadMore($event, true)">{{moreStr}}</a></p>
		<v-dialog v-model="isReadMore" width="80%">
			<v-card>
				<v-card-text class="subheading"><span v-html="text"/></v-card-text>
			</v-card>
			</v-dialog>
	</div>`,
	props: {
		moreStr: {
			type: String,
			default: 'read more'
		},
		lessStr: {
			type: String,
			default: ''
		},
		text: {
			type: String,
			required: true
		},
		link: {
			type: String,
			default: '#'
		},
		maxChars: {
			type: Number,
			default: 100
		}
	},
	$_veeValidate: {
	  validator: "new"
	},
	data (){
		return{
			isReadMore: false
		}
	},
	mounted() { },
	computed: {
		formattedString(){
			var val_container = this.text;
			if(this.text.length > this.maxChars){
				val_container = val_container.substring(0,this.maxChars) + '...';
			}
			return(val_container);
		}
	},

	methods: {
		triggerReadMore(e, b){
			if(this.link == '#'){
				e.preventDefault();
			}
			if(this.lessStr !== null || this.lessStr !== '')
			{
				this.isReadMore = b;
			}
		}
	}
  })
