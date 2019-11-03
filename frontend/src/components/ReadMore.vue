<template>
  <div>
    <a style="color: white" v-html="formattedString" @click="triggerReadMore($event, true)"/>
    <v-dialog v-model="isReadMore" width="80%">
      <v-card>
        <v-card-text class="subheading" v-html="'<br>' + text">
        </v-card-text>
      </v-card>
    </v-dialog>
  </div>
</template>

<script>
import Vue from 'vue'

export default Vue.extend({
  props: {
    lessStr: {
      type: String,
      default: ''
    },
    text: {
      type: String,
      required: true
    },
    link: {
      type: String,
      default: '#'
    },
    maxChars: {
      type: Number,
      default: 100
    }
  },
  data () {
    return {
      isReadMore: false
    }
  },
  computed: {
    formattedString () {
      var valContainer = this.text
      if (this.text.length > this.maxChars) {
        valContainer = valContainer.substring(0, this.maxChars) + '...'
      }
      return (valContainer)
    }
  },
  mounted () { },
  methods: {
    triggerReadMore (e, b) {
      if (this.link === '#') {
        e.preventDefault()
      }
      if (this.lessStr !== null || this.lessStr !== '') {
        this.isReadMore = b
      }
    }
  }
})
</script>
