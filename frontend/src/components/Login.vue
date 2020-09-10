<template>

  <v-dialog
    :value="showLoginForm"
    persistent
    max-width="600px"
  >
    <v-card>
      <v-toolbar
        dark
        flat
      >
        <v-toolbar-title>{{ this.$t('login.header') }}</v-toolbar-title>
        <v-spacer></v-spacer>
      </v-toolbar>
      <v-card-text>
        <v-form
          ref="form"
          v-model="valid"
          lazy-validation
        >
          <v-text-field
            v-model="serverAddress"
            :label="this.$t('login.server')"
            prepend-icon="mdi-server"
            name="server"
            type="text"
            :rules="validateServerAddress"
            style="margin-top:20px"
            @change="connectError = ''"
          ></v-text-field>
          <v-text-field
            v-model="username"
            :label="this.$t('login.username')"
            name="username"
            prepend-icon="mdi-account"
            type="text"
            placeholder="admin"
            :rules="validateUsername"
            @change="connectError = ''"
          ></v-text-field>
          <v-text-field
            v-model="password"
            :label="this.$t('login.password')"
            name="password"
            prepend-icon="mdi-lock"
            type="password"
            :rules="validatePassword"
            @change="connectError = ''"
          ></v-text-field>
          <v-checkbox
            v-model="allowCredentialsSave"
            :label="this.$t('login.save_creds')"
          ></v-checkbox>
        </v-form>
      </v-card-text>
      <v-card-text
        v-if="connectError"
        style="color: red"
      >
        {{ connectError }}
      </v-card-text>
      <v-card-actions>
        <v-spacer></v-spacer>
        <v-btn
          :disabled="!valid"
          color="success"
          class="mr-4"
          @click="validate"
        >{{ this.$t('login.login') }}</v-btn>
        <v-btn
          color="error"
          class="mr-4"
          @click="reset"
        >
          {{ this.$t('login.reset_form') }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script>
import axios from 'axios'
export default {
  props: {
    source: String
  },
  data () {
    return {
      servers: [],
      showLoginForm: false,
      serverAddress: '',
      username: '',
      password: '',
      valid: true,
      allowCredentialsSave: false,
      connectError: ''
    }
  },
  methods: {
    async submitLogin () {
      // login form is submitted, validate input and try to connect
      if (!this.serverAddress || !this.username) {
        return
      }
      // try to fix some common mistakes
      if (!this.serverAddress.startsWith('http')) {
        // add default scheme if ommitted
        this.serverAddress = 'http://' + this.serverAddress
      }
      const host = this.serverAddress.split('://')[1]
      if (!host.includes(':')) {
        // add default port if ommitted
        this.serverAddress = this.serverAddress + ':8095'
      }
      // connect to server
      if (await this.$server.connect(this.serverAddress, this.username, this.password)) {
        this.showLoginForm = false
        // store new values in browser storage at successfull login
        localStorage.setItem('serverAddress', this.serverAddress)
        localStorage.setItem('username', this.username)
        if (this.allowCredentialsSave) {
          localStorage.setItem('password', this.password)
        }
      } else {
        this.showLoginForm = true
        this.connectError = this.$t('login.login_failed')
      }
    },
    async validate () {
      this.$refs.form.validate()
      await this.submitLogin()
    },
    reset () {
      this.$refs.form.reset()
    },
    resetValidation () {
      this.$refs.form.resetValidation()
    },
    async getServerInfo (serverAddress) {
      if (!serverAddress) {
        return
      }
      if (!serverAddress.endsWith('/')) {
        serverAddress = serverAddress + '/'
      }
      const url = serverAddress + 'info'
      try {
        const result = await axios.get(url, { timeout: 500 })
        return result.data
      } catch {
        return false
      }
    },
    async getDefaultServer () {
      // try to get default server
      const loc = window.location
      // auto select local server if it exists
      var localServerAddress = loc.origin + loc.pathname
      localServerAddress = localServerAddress.replace(':8080', ':8095')
      const serverInfo = await this.getServerInfo(localServerAddress)
      if (serverInfo !== false) {
        return localServerAddress
      }
      return null
    }
  },
  async created () {
    // work out if we have cached credentials and connect
    this.serverAddress = localStorage.getItem('serverAddress')
    this.username = localStorage.getItem('username')
    this.password = localStorage.getItem('password')
    if (!this.serverAddress) { this.serverAddress = await this.getDefaultServer() }
    if (!this.username) { this.username = 'admin' }
    if (!this.password) { this.password = '' }
    // TODO: show warning in UI if default (blank) password in use
    if (await this.$server.connect(this.serverAddress, this.username, this.password) === true) {
      // server connected !
      this.showLoginForm = false
    } else {
      // connect failed or no credentials stored, show dialog
      this.showLoginForm = true
    }
  },
  computed: {
    validateServerAddress () {
      const rules = []
      if (!this.serverAddress) {
        const rule = this.$t('login.server_empty')
        rules.push(rule)
      }
      return rules
    },
    validateUsername () {
      const rules = []

      if (!this.username) {
        const rule = this.$t('login.username_empty')
        rules.push(rule)
      }
      return rules
    },
    validatePassword () {
      const rules = []
      // default password is empty
      return rules
    }
  }
}
</script>
