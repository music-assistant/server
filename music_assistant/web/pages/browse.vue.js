var Browse = Vue.component('Browse', {
  template: `
    <section>
      <v-list two-line>
        <listviewItem 
            v-for="(item, index) in items"
            :key="item.db_id"
            v-bind:item="item"
            v-bind:totalitems="items.length"
            v-bind:index="index"
            :hideavatar="item.media_type == 3 ? isMobile() : false"
            :hidetracknum="true"
            :hideproviders="isMobile()"
            :hidelibrary="isMobile() ? true : item.media_type != 3">
        </listviewItem>
      </v-list>
    </section>
  `,
  props: ['mediatype', 'provider'],
  data() {
    return {
      selected: [2],
      items: [],
      offset: 0
    }
  },
  created() {
    this.showavatar = true;
    mediatitle = 
    this.$globals.windowtitle = this.$t(this.mediatype)
    this.scroll(this.Browse);
    this.getItems();
  },
  methods: {
    getItems () {
      this.$globals.loading = true
      const api_url = '/api/' + this.mediatype;
      axios
        .get(api_url, { params: { offset: this.offset, limit: 50, provider: this.provider }})
        .then(result => {
          data = result.data;
          this.items.push(...data);
          this.offset += 50;
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

        if (bottomOfWindow) {
          this.getItems();
        }
      };
    }
  }
})
