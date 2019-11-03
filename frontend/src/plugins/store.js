import Vue from 'vue'

const globalStore = new Vue({
  data () {
    return {
      windowtitle: 'Home',
      loading: false,
      showNavigationMenu: false,
      topBarColor: '#424242',
      isMobile: false,
      isInStandaloneMode: false
    }
  },
  created () {
    this.handleWindowOptions()
    window.addEventListener('resize', this.handleWindowOptions)
  },
  destroyed () {
    window.removeEventListener('resize', this.handleWindowOptions)
  },
  methods: {
    handleWindowOptions () {
      this.isMobile = (document.body.clientWidth < 700)
      this.isInStandaloneMode = 'standalone' in window.navigator && window.navigator.standalone
    }
  }
})

export default {
  globalStore,
  // we can add objects to the Vue prototype in the install() hook:
  install (Vue, options) {
    Vue.prototype.$store = globalStore
  }
}
