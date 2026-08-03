/* src/router/index.js */
import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/Login.vue') },
  { path: '/', name: 'dashboard', component: () => import('../views/Dashboard.vue') },
  { path: '/servers', name: 'servers', component: () => import('../views/Servers.vue') },
  { path: '/tokens', name: 'tokens', component: () => import('../views/Tokens.vue') },
  { path: '/api-keys', name: 'api-keys', component: () => import('../views/APIKeys.vue') },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to, from, next) => {
  const jwt = localStorage.getItem('gw-jwt')
  if (to.name !== 'login' && !jwt) return next({ name: 'login' })
  if (to.name === 'login' && jwt) return next({ name: 'dashboard' })
  next()
})

export default router
