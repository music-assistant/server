'use strict'

import Vue from 'vue'
import axios from 'axios'

const axiosConfig = {
  timeout: 60 * 1000
  // withCredentials: true, // Check cross-site Access-Control
}
const _axios = axios.create(axiosConfig)

// Holds the connection to the server

const server = new Vue({

  _address: '',
  _ws: null,

  data () {
    return {
      connected: false,
      players: {},
      activePlayerId: null
    }
  },
  methods: {

    connect (serverAddress) {
      // Connect to the server
      if (!serverAddress.endsWith('/')) {
        serverAddress = serverAddress + '/'
      }
      this._address = serverAddress
      let wsAddress = serverAddress.replace('http', 'ws') + 'ws'
      this._ws = new WebSocket(wsAddress)
      this._ws.onopen = this._onWsConnect
      this._ws.onmessage = this._onWsMessage
      this._ws.onclose = this._onWsClose
      this._ws.onerror = this._onWsError
    },

    async toggleLibrary (item) {
      /// triggered when user clicks the library (heart) button
      let endpoint = item.media_type + '/' + item.item_id
      let action = 'library_remove'
      if (item.in_library.length === 0) {
        action = 'library_add'
      }
      await this.getData(endpoint, { provider: item.provider, action: action })
      if (action === '/library_remove') {
        item.in_library = []
      } else {
        item.in_library = [item.provider]
      }
    },

    getImageUrl (mediaItem, imageType = 'image', size = 0) {
      // format the image url
      if (!mediaItem || !mediaItem.media_type) return ''
      return `${this._address}api/${mediaItem.media_type}/${mediaItem.item_id}/image?type=${imageType}&provider=${mediaItem.provider}&size=${size}`
    },

    async getData (endpoint, params = {}) {
      // get data from the server
      let url = this._address + 'api/' + endpoint
      let result = await _axios.get(url, { params: params })
      return result.data
    },

    async postData (endpoint, data) {
      // post data to the server
      let url = this._address + 'api/' + endpoint
      let result = await _axios.post(url, data)
      return result.data
    },

    playerCommand (cmd, cmd_opt = null, playerId = this.activePlayerId) {
      let msgDetails = {
        player_id: playerId,
        cmd: cmd,
        cmd_args: cmd_opt
      }
      this._ws.send(JSON.stringify({ message: 'player command', message_details: msgDetails }))
    },

    async playItem (item, queueOpt) {
      this.$store.loading = true
      let endpoint = 'players/' + this.activePlayerId + '/play_media/' + item.media_type + '/' + item.item_id + '/' + queueOpt
      await this.getData(endpoint)
      this.$store.loading = false
    },

    switchPlayer (newPlayerId) {
      this.activePlayerId = newPlayerId
      localStorage.setItem('activePlayerId', newPlayerId)
      this.$emit('new player selected', newPlayerId)
    },

    _onWsConnect () {
      // Websockets connection established
      // console.log('Connected to server ' + this._address)
      this.connected = true
      // request all players
      let data = JSON.stringify({ message: 'players', message_details: null })
      this._ws.send(data)
    },

    _onWsMessage (e) {
      // Message retrieved on the websocket
      var msg = JSON.parse(e.data)
      if (msg.message === 'player changed') {
        Vue.set(this.players, msg.message_details.player_id, msg.message_details)
      } else if (msg.message === 'player added') {
        Vue.set(this.players, msg.message_details.player_id, msg.message_details)
        this._selectActivePlayer()
        this.$emit('players changed')
      } else if (msg.message === 'player removed') {
        Vue.delete(this.players, msg.message_details.player_id)
        this._selectActivePlayer()
        this.$emit('players changed')
      } else if (msg.message === 'players') {
        for (var item of msg.message_details) {
          Vue.set(this.players, item.player_id, item)
        }
        this._selectActivePlayer()
        this.$emit('players changed')
      } else {
        this.$emit(msg.message, msg.message_details)
      }
    },

    _onWsClose (e) {
      this.connected = false
      // console.log('Socket is closed. Reconnect will be attempted in 5 seconds.', e.reason)
      setTimeout(function () {
        this.connect(this._address)
      }.bind(this), 5000)
    },

    _onWsError () {
      this._ws.close()
    },

    _selectActivePlayer () {
      // auto select new active player if we have none
      if (!this.activePlayer || !this.activePlayer.enabled || this.activePlayer.group_parents.length > 0) {
        // prefer last selected player
        let lastPlayerId = localStorage.getItem('activePlayerId')
        if (lastPlayerId && this.players[lastPlayerId] && this.players[lastPlayerId].enabled) {
          this.switchPlayer(lastPlayerId)
        } else {
          // prefer the first playing player
          for (let playerId in this.players) {
            if (this.players[playerId].state === 'playing' && this.players[playerId].enabled && this.players[playerId].group_parents.length === 0) {
              this.switchPlayer(playerId)
              break
            }
          }
          // fallback to just the first player
          if (!this.activePlayer || !this.activePlayer.enabled) {
            for (let playerId in this.players) {
              if (this.players[playerId].enabled && this.players[playerId].group_parents.length === 0) {
                this.switchPlayer(playerId)
                break
              }
            }
          }
        }
      }
    }
  },
  computed: {
    activePlayer () {
      if (!this.activePlayerId) {
        return null
      } else {
        return this.players[this.activePlayerId]
      }
    }
  }
})

// install as plugin
export default {
  server,
  // we can add objects to the Vue prototype in the install() hook:
  install (Vue, options) {
    Vue.prototype.$server = server
  }
}
