import Vue from 'vue'
import VueRouter from 'vue-router'
import Home from '../views/Home.vue'
import Browse from '../views/Browse.vue'

Vue.use(VueRouter)

const routes = [
  {
    path: '/',
    name: 'home',
    component: Home
  },
  {
    path: '/config',
    name: 'config',
    component: () => import(/* webpackChunkName: "config" */ '../views/Config.vue'),
    props: route => ({ ...route.params, ...route.query })
  },
  {
    path: '/config/:configKey',
    name: 'configKey',
    component: () => import(/* webpackChunkName: "config" */ '../views/Config.vue'),
    props: route => ({ ...route.params, ...route.query })
  },
  {
    path: '/search',
    name: 'search',
    component: () => import(/* webpackChunkName: "search" */ '../views/Search.vue'),
    props: route => ({ ...route.params, ...route.query })
  },
  {
    path: '/:media_type/:media_id',
    name: 'itemdetails',
    component: () => import(/* webpackChunkName: "itemdetails" */ '../views/ItemDetails.vue'),
    props: route => ({ ...route.params, ...route.query })
  },
  {
    path: '/playerqueue',
    name: 'playerqueue',
    component: () => import(/* webpackChunkName: "playerqueue" */ '../views/PlayerQueue.vue'),
    props: route => ({ ...route.params, ...route.query })
  },
  {
    path: '/:mediatype',
    name: 'browse',
    component: Browse,
    props: route => ({ ...route.params, ...route.query })
  }
]

const router = new VueRouter({
  mode: 'hash',
  routes
})

export default router
