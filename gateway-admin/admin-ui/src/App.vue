<!-- src/App.vue -->
<template>
  <router-view v-if="$route.name === 'login'" />
  <div v-else class="shell">
    <Sidebar :page="currentPage" @navigate="nav" />
    <div class="main">
      <Topbar :page="currentPage" />
      <div class="content"><router-view /></div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from './components/Sidebar.vue'
import Topbar from './components/Topbar.vue'

const route = useRoute()
const router = useRouter()
const currentPage = computed(() => route.name)
function nav(name) { router.push({ name }) }
</script>

<style scoped>
/* ── Extracted from docs/superpowers/mockups/gateway-admin.html ── */
.shell { display: flex; min-height: 100vh; }
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.content { padding: 28px; max-width: 1120px; width: 100%; margin: 0 auto; }
</style>
