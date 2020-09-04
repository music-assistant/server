<template>
  <div>
  <img
    v-for="prov of uniqueProviders" :key="prov.provider"
    :height="height"
    :src="require('../assets/' + prov.provider + '.png')"
    style="margin-right:6px;margin-top:6px;"
  />
  </div>
</template>

<script>
import Vue from 'vue'

export default Vue.extend({
  props: {
    providerIds: Array,
    height: Number
  },
  data () {
    return {
      isHiRes: false
    }
  },
  computed: {
    uniqueProviders: function () {
      var output = []
      var keys = []
      if (!this.providerIds) return []
      this.providerIds.forEach(function (prov) {
        var key = prov.provider
        if (keys.indexOf(key) === -1) {
          keys.push(key)
          output.push(prov)
        }
      })
      return output
    }
  },
  mounted () { },
  methods: {
  }
})
</script>
