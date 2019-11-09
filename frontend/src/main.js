import Vue from 'vue'
import App from './App.vue'
import './registerServiceWorker'
import router from './router'
import i18n from './i18n'
import 'roboto-fontface/css/roboto/roboto-fontface.css'
import 'material-design-icons-iconfont/dist/material-design-icons.css'
import VueVirtualScroller from 'vue-virtual-scroller'
import 'vue-virtual-scroller/dist/vue-virtual-scroller.css'
import vuetify from './plugins/vuetify'
import store from './plugins/store'
import server from './plugins/server'
import '@babel/polyfill'
import VueLogger from 'vuejs-logger'

const isProduction = process.env.NODE_ENV === 'production'
const loggerOptions = {
  isEnabled: true,
  logLevel: isProduction ? 'error' : 'debug',
  stringifyArguments: false,
  showLogLevel: true,
  showMethodName: false,
  separator: '|',
  showConsoleColors: true
}

Vue.config.productionTip = false
Vue.use(VueLogger, loggerOptions)
Vue.use(VueVirtualScroller)
Vue.use(store)
Vue.use(server)

// eslint-disable-next-line no-extend-native
String.prototype.formatDuration = function () {
  var secNum = parseInt(this, 10) // don't forget the second param
  var hours = Math.floor(secNum / 3600)
  var minutes = Math.floor((secNum - (hours * 3600)) / 60)
  var seconds = secNum - (hours * 3600) - (minutes * 60)
  if (hours < 10) { hours = '0' + hours }
  if (minutes < 10) { minutes = '0' + minutes }
  if (seconds < 10) { seconds = '0' + seconds }
  if (hours === '00') { return minutes + ':' + seconds } else { return hours + ':' + minutes + ':' + seconds }
}

new Vue({
  router,
  i18n,
  vuetify,
  render: h => h(App)
}).$mount('#app')
