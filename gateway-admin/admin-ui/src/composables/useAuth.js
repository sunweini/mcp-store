/* src/composables/useAuth.js */
import { ref, computed } from 'vue'

const jwt = ref(localStorage.getItem('gw-jwt'))

function setJwt(token) {
  jwt.value = token
  localStorage.setItem('gw-jwt', token)
}

export function useAuth() {
  const isAuthed = computed(() => !!jwt.value)

  async function login(username, password) {
    const resp = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    if (!resp.ok) throw new Error((await resp.json()).detail || '登录失败')
    const data = await resp.json()
    setJwt(data.token)
    return data
  }

  function logout() {
    jwt.value = null
    localStorage.removeItem('gw-jwt')
  }

  return { token: jwt, isAuthed, login, logout }
}
