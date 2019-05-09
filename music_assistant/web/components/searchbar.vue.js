Vue.component("searchbar", {
  template: `
  <section class="section searchbar">
    <div class="container">
      <b-field>
        <b-autocomplete size="is-medium"
        expanded
          v-model="searchQuery"
          :data="filteredDataArray"
          placeholder="e.g. Eminem"
          icon="magnify"
          @select="option => selected = option"
          @keyup.enter="onClickSearch"
        ></b-autocomplete>
        <p class="control" v-if="searchQuery">
               <button @click="onClickClearSearch" class="button  is-medium "><i class="fas fa-times"></i></button>
            </p>
      </b-field>
    </div>
  </section>
  `,
  data () {
    return {
      data: [],
      searchQuery: '',
      selected: null
    }
  },
  props: {
    recentSearch: {
      type: Array,
      required: true
    },
    newSearchQuery: {
      type: String,
      required: true
    },
    settings: {
      type: Object,
      required: true
    }
  },
  mounted () {
    this.searchQuery = this.settings.initialSearchQuery
    this.onClickSearch()
  },
  watch: {
    searchQuery: {
      handler: _.debounce(function (val) {
        if (val === '') {
          this.$store.commit('CLEAR_SEARCH')
        } else {
          if (val !== this.newSearchQuery) {
            this.onClickSearch()
          }
        }
      }, 1000)
    },
    newSearchQuery (val) {
      this.searchQuery = val
    }
  },
  computed: {
    filteredDataArray () {
      return this.recentSearch.filter(option => {
        return (
          option
            .toString()
            .toLowerCase()
            .indexOf(this.searchQuery.toLowerCase()) >= 0
        )
      })
    }
  },
  methods: {
    onClickSearch () {
      this.$emit('clickSearch', this.searchQuery)
    },
    onClickClearSearch () {
      this.searchQuery = ''
      this.$emit('clickClearSearch')
    }
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