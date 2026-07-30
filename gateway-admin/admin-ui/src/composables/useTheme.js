/* src/composables/useTheme.js */
import { ref, watchEffect } from 'vue'

const theme = ref(localStorage.getItem('gw-theme') || 'dark')

watchEffect(() => {
  document.documentElement.setAttribute('data-theme', theme.value)
  localStorage.setItem('gw-theme', theme.value)
})

export function useTheme() {
  const toggleTheme = () => theme.value = theme.value === 'dark' ? 'light' : 'dark'
  return { theme, toggleTheme }
}
