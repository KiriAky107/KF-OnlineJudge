import { readonly, ref } from 'vue'

type SiteTheme = 'light' | 'dark'
const key = 'kf-site-theme'
const theme = ref<SiteTheme>('light')

function apply(value: SiteTheme) {
  theme.value = value
  document.documentElement.classList.toggle('dark', value === 'dark')
}

export function initSiteTheme() {
  const system = window.matchMedia('(prefers-color-scheme: dark)')
  function sync() {
    let saved: string | null = null
    try {
      saved = localStorage.getItem(key)
    } catch {
      // The system preference still works when storage is blocked.
    }
    apply(saved === 'light' || saved === 'dark' ? saved : system.matches ? 'dark' : 'light')
  }
  sync()
  system.addEventListener('change', sync)
  window.addEventListener('storage', (event) => {
    if (event.key === key || event.key === null) sync()
  })
}

export function useSiteTheme() {
  function toggle() {
    const value = theme.value === 'dark' ? 'light' : 'dark'
    apply(value)
    try {
      localStorage.setItem(key, value)
    } catch {
      // Keep the theme for the current page.
    }
  }
  return { theme: readonly(theme), toggle }
}
