Vue.use(VueRouter);
Vue.use(VeeValidate);
Vue.use(Vuetify);
Vue.use(VueI18n);
Vue.use(VueLoading);
Vue.use(Toasted, {duration: 5000, fullWidth: true});


const routes = [
    {
    path: '/',
    component: home
    },
    {
        path: '/config',
        component: Config,
    },
    {
        path: '/queue/:player_id',
        component: Queue,
        props: route => ({ ...route.params, ...route.query })
    },
    {
        path: '/artists/:media_id',
        component: ArtistDetails,
        props: route => ({ ...route.params, ...route.query })
    },
    {
        path: '/albums/:media_id',
        component: AlbumDetails,
        props: route => ({ ...route.params, ...route.query })
    },
    {
        path: '/tracks/:media_id',
        component: TrackDetails,
        props: route => ({ ...route.params, ...route.query })
    },
    {
        path: '/playlists/:media_id',
        component: PlaylistDetails,
        props: route => ({ ...route.params, ...route.query })
    },
    {
        path: '/search',
        component: Search,
        props: route => ({ ...route.params, ...route.query })
    },
    {
        path: '/:mediatype',
        component: Browse,
        props: route => ({ ...route.params, ...route.query })
    },
]

const globalStore = new Vue({
    data: {
        windowtitle: 'Home',
        loading: false,
        showcontextmenu: false,
        contextmenuitem: null,
        contextmenucontext: null,
        server: null,
        apiAddress: null,
        wsAddress: null
    }
})

Vue.prototype.$globals = globalStore;
Vue.prototype.isMobile = isMobile;
Vue.prototype.isInStandaloneMode = isInStandaloneMode;
Vue.prototype.toggleLibrary = toggleLibrary;
Vue.prototype.showPlayMenu = showPlayMenu;
Vue.prototype.clickItem= clickItem;

let router = new VueRouter({
    //mode: 'history',
    routes // short for `routes: routes`
})

router.beforeEach((to, from, next) => {
    next()
})

const i18n = new VueI18n({
    locale: navigator.language.split('-')[0],
    fallbackLocale: 'en',
    enableInSFC: true,
    messages
    })

var app = new Vue({
    i18n,
    el: '#app',
    watch: {},
    mounted() {
    },
    components: {
        Loading: VueLoading
    },
    created() {
        // little hack to force refresh PWA on iOS by simple reloading it every hour
        var d = new Date();
        var cur_update = d.getDay() + d.getHours();
        if (localStorage.getItem('last_update') != cur_update)
        {
            localStorage.setItem('last_update', cur_update);
            window.location.reload(true);
        }
        // TODO: retrieve serveraddress through discovery and/or user settings
        let loc = window.location;
        this.$globals.server = loc.origin + loc.pathname;
        this.$globals.apiAddress = this.$globals.server + 'api/';
        if (loc.protocol === "https:") {
            this.$globals.wsAddress = "wss://"+ loc.host + loc.pathname + 'ws';
        } else {
            this.$globals.wsAddress = "ws://"+ loc.host + loc.pathname + 'ws';
        }
    },
    data: { },
    methods: {},
    router,
    template: `
        <v-app light>
            <v-content>
                <headermenu></headermenu>
                <player></player>
                <router-view app :key="$route.path"></router-view>               
            </v-content>
            <loading :active.sync="$globals.loading" :can-cancel="true" color="#2196f3" loader="dots"></loading>
        </v-app>
    `
})