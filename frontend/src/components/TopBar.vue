<template>
  <v-app-bar app flat dense dark :color="color">
    <v-layout>
      <div class="body-1" v-if="!$store.topBarTransparent" style="position:fixed;width:100%;text-align:center;vertical-align:center;margin-top:11px;">{{ $store.windowtitle }}</div>
      <v-btn icon v-on:click="$store.showNavigationMenu=!$store.showNavigationMenu" style="margin-left:-13px">
        <v-icon>menu</v-icon>
      </v-btn>
      <v-btn @click="$router.go(-1)" icon>
        <v-icon>arrow_back</v-icon>
      </v-btn>
      <v-spacer></v-spacer>
      <v-btn v-if="$store.topBarContextItem" icon @click="$server.$emit('showContextMenu', $store.topBarContextItem)" style="margin-right:-23px">
        <v-icon>more_vert</v-icon>
      </v-btn>
    </v-layout>
  </v-app-bar>
</template>

<script>
import Vue from 'vue'

export default Vue.extend({
  props: { },
  data () {
    return {
    }
  },
  computed: {
    color () {
      if (this.$store.topBarTransparent) {
        return 'transparent'
      } else return 'black'
    }
  },
  mounted () { },
  methods: {}
})
</script>
