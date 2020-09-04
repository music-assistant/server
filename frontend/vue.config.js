module.exports = {
  pluginOptions: {
    i18n: {
      locale: 'en',
      fallbackLocale: 'en',
      localeDir: 'locales',
      enableInSFC: true
    },
    cordovaPath: 'src-cordova'
  },
  pwa: {
    name: 'Music Assistant',
    themeColor: '#424242',
    msTileColor: '#424242',
    appleMobileWebAppCapable: 'yes',
    appleMobileWebAppStatusBarStyle: 'black'
  },
  transpileDependencies: [
    'vuetify'
  ],
  outputDir: '../music_assistant/web',
  publicPath: ''
}
