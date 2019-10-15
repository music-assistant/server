Vue.component("searchbox", {
  template: `
  <v-dialog :value="$globals.showsearchbox" @input="$emit('input', $event)" max-width="500px">
      <v-text-field
            solo
            clearable
            :label="$t('type_to_search')"
            prepend-inner-icon="search"
            v-model="searchQuery">
          </v-text-field>
      </v-dialog>
  `,
  data () {
    return {
      searchQuery: "",
    }
  },
  props: ['value'],
  mounted () {
    this.searchQuery = "" // TODO: set to last searchquery ?
  },
  watch: {
    searchQuery: {
      handler: _.debounce(function (val) {
        this.onSearch();
        // if (this.searchQuery)
        //     this.$globals.showsearchbox = false;
      }, 1000)
    },
    newSearchQuery (val) {
      this.searchQuery = val
    }
  },
  computed: {},
  methods: {
    onSearch () {
      //this.$emit('clickSearch', this.searchQuery)
      console.log(this.searchQuery);
      router.push({ path: '/search', query: {searchQuery: this.searchQuery}});
    },
  }
})
/* <style>
.searchbar {
  padding: 1rem 1.5rem!important;
  width: 100%;
  box-shadow: 0 0 70px 0 rgba(0, 0, 0, 0.3);
  background: #fff;
}
</style> */