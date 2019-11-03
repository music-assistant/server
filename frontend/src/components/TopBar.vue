<template>
  <v-app-bar app scroll-off-screen hide-on-scroll flat dense dark :color="$store.topBarColor">
    <v-layout>
      <v-toolbar-title class="body-1" v-if="$store.topBarColor != 'transparent'" style="position:fixed;width:100%;text-align:center;vertical-align:center;margin-top:11px;">{{ $store.windowtitle }}</v-toolbar-title>
      <v-btn icon v-on:click="$store.showNavigationMenu=!$store.showNavigationMenu" style="margin-left:-13px">
        <v-icon>menu</v-icon>
      </v-btn>
      <v-btn @click="$router.go(-1)" icon>
        <v-icon>arrow_back</v-icon>
      </v-btn>
      <v-spacer></v-spacer>
      <v-btn icon @click="$server.$emit('showContextMenu')" style="margin-right:-23px">
        <v-icon>more_vert</v-icon>
      </v-btn>
    </v-layout>
  </v-app-bar>
</template>

<script>
import Vue from 'vue'

export default Vue.extend({
  props: {
    'color': {
      type: String,
      default: '#424242'
    },
    'fixed': {
      type: Boolean,
      default: true
    },
    'context': {
      type: Object,
      default: null
    }
  },
  data () {
    return {
    }
  },
  mounted () { },
  methods: {}
})
</script>
