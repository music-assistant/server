'use strict'

import Vue from 'vue'
import axios from 'axios'
import oboe from 'oboe'

const axiosConfig = {
  timeout: 5 * 1000
  // withCredentials: true, // Check cross-site Access-Control
}
const _axios = axios.create(axiosConfig)

// Holds the connection to the server

const server = new Vue({

  _address: '',
  _ws: null,
  _serverAddress: null,
  _username: null,
  _password: null,

  data () {
    return {
      connected: false,
      players: {},
      activePlayerId: null,
      syncStatus: [],
      tokenInfo: {}
    }
  },
  methods: {

    async reconnect () {
      // Reconnect to the server with stored creds
      return this.connect(this._serverAddress, this._username, this._password)
    },
    async connect (serverAddress, username, password) {
      // Connect to the server
      if (serverAddress && !serverAddress.endsWith('/')) {
        serverAddress = serverAddress + '/'
      }
      const url = serverAddress + 'login'
      const data = JSON.stringify({ username: username, password: password })
      try {
        Vue.$log.info('Connecting to ' + serverAddress)
        const result = await _axios.post(url, data)
        this.tokenInfo = result.data
      } catch {
        Vue.$log.error('login failed for ' + serverAddress)
        return false
      }
      _axios.defaults.headers.common.Authorization = 'Bearer ' + this.tokenInfo.token
      this._address = serverAddress
      const wsAddress = serverAddress.replace('http', 'ws') + 'ws'
      this._ws = new WebSocket(wsAddress)
      this._ws.onopen = this._onWsConnect
      this._ws.onmessage = this._onWsMessage
      this._ws.onclose = this._onWsClose
      this._ws.onerror = this._onWsError
      this._serverAddress = serverAddress
      this._username = username
      this._password = password
      return true
    },

    async toggleLibrary (item) {
      /// triggered when user clicks the library (heart) button
      if (item.in_library.length === 0) {
        // add to library
        await this.putData('library', item)
        item.in_library = [item.provider]
      } else {
        // remove from library
        await this.deleteData('library', item)
        item.in_library = []
      }
    },

    getImageUrl (mediaItem, imageType = 'image', size = 0) {
      // format the image url
      if (!mediaItem || !mediaItem.media_type) return ''
      if (mediaItem.provider === 'database' && imageType === 'image') {
        return `${this._address}api/${mediaItem.media_type}/${mediaItem.item_id}/thumb?provider=${mediaItem.provider}&size=${size}`
      } else if (mediaItem.metadata && mediaItem.metadata[imageType]) {
        return mediaItem.metadata[imageType]
      } else if (mediaItem.album && mediaItem.album.metadata && mediaItem.album.metadata[imageType]) {
        return mediaItem.album.metadata[imageType]
      } else if (mediaItem.artist && mediaItem.artist.metadata && mediaItem.artist.metadata[imageType]) {
        return mediaItem.artist.metadata[imageType]
      } else if (mediaItem.album && mediaItem.album.artist && mediaItem.album.artist.metadata && mediaItem.album.artist.metadata[imageType]) {
        return mediaItem.album.artist.metadata[imageType]
      } else if (mediaItem.artists && mediaItem.artists[0].metadata && mediaItem.artists[0].metadata[imageType]) {
        return mediaItem.artists[0].metadata[imageType]
      } else if (imageType === 'fanart') {
        // fallback to normal image instead of fanart
        return this.getImageUrl(mediaItem, 'image', size)
      } else return ''
    },

    async getData (endpoint, params = {}) {
      // get data from the server
      const url = this._address + 'api/' + endpoint
      const result = await _axios.get(url, { params: params })
      Vue.$log.debug('getData', endpoint, result)
      return result.data
    },

    async postData (endpoint, data) {
      // post data to the server
      const url = this._address + 'api/' + endpoint
      data = JSON.stringify(data)
      const result = await _axios.post(url, data)
      Vue.$log.debug('postData', endpoint, result)
      return result.data
    },

    async putData (endpoint, data) {
      // put data to the server
      const url = this._address + 'api/' + endpoint
      data = JSON.stringify(data)
      const result = await _axios.put(url, data)
      Vue.$log.debug('putData', endpoint, result)
      return result.data
    },

    async deleteData (endpoint, dataObj) {
      // delete data on the server
      const url = this._address + 'api/' + endpoint
      dataObj = JSON.stringify(dataObj)
      const result = await _axios.delete(url, { data: dataObj })
      Vue.$log.debug('deleteData', endpoint, result)
      return result.data
    },

    async getAllItems (endpoint, list, params = null) {
      // retrieve all items and fill list
      let url = this._address + 'api/' + endpoint
      if (params) {
        var urlParams = new URLSearchParams(params)
        url += '?' + urlParams.toString()
      }
      let index = 0
      const headers = { Authorization: 'Bearer ' + this.tokenInfo.token }
      oboe({ url: url, headers: headers })
        .node('items.*', function (item) {
          Vue.set(list, index, item)
          index += 1
        })
        .done(function (fullList) {
          // truncate list if needed
          if (list.length > fullList.items.length) {
            list.splice(fullList.items.length)
          }
        })
    },

    playerCommand (cmd, cmd_opt = '', playerId = this.activePlayerId) {
      const endpoint = 'players/' + playerId + '/cmd/' + cmd
      this.postData(endpoint, cmd_opt)
    },

    async playItem (item, queueOpt) {
      this.$store.loading = true
      const endpoint = 'players/' + this.activePlayerId + '/play_media/' + queueOpt
      await this.postData(endpoint, item)
      this.$store.loading = false
    },

    switchPlayer (newPlayerId) {
      if (newPlayerId !== this.activePlayerId) {
        this.activePlayerId = newPlayerId
        localStorage.setItem('activePlayerId', newPlayerId)
        this.$emit('new player selected', newPlayerId)
      }
    },

    async _onWsConnect () {
      // Websockets connection established
      this._ws.send(JSON.stringify({ message: 'login', message_details: this.tokenInfo.token }))
      // retrieve all players once through api
      const players = await this.getData('players')
      for (const player of players) {
        Vue.set(this.players, player.player_id, player)
      }
      this._selectActivePlayer()
      this.$emit('players changed')
    },

    async _onWsMessage (e) {
      // Message retrieved on the websocket
      var msg = JSON.parse(e.data)
      if (msg.message === 'login') {
        // login was successfull
        Vue.$log.info('Connected to websocket ' + this._address)
        this.connected = true
        this.$emit('refresh_listing')
        // register callbacks
        this._ws.send(JSON.stringify({ message: 'add_event_listener' }))
      } else if (msg.message === 'player changed') {
        Vue.set(this.players, msg.message_details.player_id, msg.message_details)
      } else if (msg.message === 'player added') {
        Vue.set(this.players, msg.message_details.player_id, msg.message_details)
        this._selectActivePlayer()
        this.$emit('players changed')
      } else if (msg.message === 'player removed') {
        Vue.delete(this.players, msg.message_details.player_id)
        this._selectActivePlayer()
        this.$emit('players changed')
      } else if (msg.message === 'music sync status') {
        this.syncStatus = msg.message_details
      } else {
        this.$emit(msg.message, msg.message_details)
      }
    },

    _onWsClose (e) {
      this.connected = false
      Vue.$log.error('Socket is closed. Reconnect will be attempted in 5 seconds.', e.reason)
      setTimeout(function () {
        this.reconnect()
      }.bind(this), 5000)
    },

    _onWsError () {
      this._ws.close()
    },

    _selectActivePlayer () {
      // auto select new active player if we have none
      if (!this.activePlayer || !this.activePlayer.available) {
        // prefer last selected player
        const lastPlayerId = localStorage.getItem('activePlayerId')
        if (lastPlayerId && this.players[lastPlayerId] && this.players[lastPlayerId].available) {
          this.switchPlayer(lastPlayerId)
        } else {
          // prefer the first playing player
          for (const playerId in this.players) {
            if (this.players[playerId].state === 'playing' && this.players[playerId].available) {
              this.switchPlayer(playerId)
              break
            }
          }
          // fallback to just the first player
          if (!this.activePlayer || !this.activePlayer.enabled) {
            for (const playerId in this.players) {
              if (this.players[playerId].available) {
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
