module.exports = {
  pluginOptions: {
    i18n: {
      locale: 'en',
      fallbackLocale: 'en',
      localeDir: 'locales',
      enableInSFC: true
    }
  },

  transpileDependencies: [
    'vuetify'
  ],

  outputDir: '../music_assistant/web',
  publicPath: ''
}
