var Browse = Vue.component('Browse', {
  template: `
    <section>
      <v-list two-line>
        <listviewItem 
            v-for="(item, index) in items"
            :key="item.item_id+item.provider"
            v-bind:item="item"
            v-bind:totalitems="items.length"
            v-bind:index="index"
            :hideavatar="item.media_type == 3 ? isMobile() : false"
            :hidetracknum="true"
            :hideproviders="isMobile()"
            :hidelibrary="true"
            :hidemenu="item.media_type == 3"
            :context="mediatype"
            >
        </listviewItem>
      </v-list>
    </section>
  `,
  props: ['mediatype', 'provider'],
  data() {
    return {
      selected: [2],
      items: [],
      offset: 0,
      full_list_loaded: false
    }
  },
  created() {
    this.$globals.windowtitle = this.$t(this.mediatype)
    this.scroll(this.Browse);
    if (!this.full_list_loaded)
      this.getItems();
  },
  methods: {
    getItems () {
      if (this.full_list_loaded)
        return;
      this.$globals.loading = true
      const api_url = this.$globals.apiAddress + this.mediatype;
      const limit = 20;
      axios
        .get(api_url, { params: { offset: this.offset, limit: limit, provider: this.provider }})
        .then(result => {
          data = result.data;
          if (data.length < limit)
          {
            this.full_list_loaded = true;
            this.$globals.loading = false;
            if (data.length == 0)
              return
          }
          this.items.push(...data);
          this.offset += limit;
          this.$globals.loading = false;
        })
        .catch(error => {
          console.log("error", error);
          this.showProgress = false;
        });
    },
    scroll (Browse) {
      window.onscroll = () => {
        let bottomOfWindow = document.documentElement.scrollTop + window.innerHeight === document.documentElement.offsetHeight;
        if (bottomOfWindow && !this.full_list_loaded) {
          this.getItems();
        }
      };
    }
  }
})
