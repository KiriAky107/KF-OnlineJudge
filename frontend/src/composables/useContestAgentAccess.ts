import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { unwrap } from '../api/transport'
import { useSession } from './useSession'

const registrationKey = 'kf-oj:contest-registration'

export function notifyContestRegistration() {
  try {
    localStorage.setItem(registrationKey, new Date().toISOString())
  } catch {
    // The server still enforces access when browser storage is disabled.
  }
}

export function useContestAgentAccess() {
  const { user } = useSession()
  const blocked = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let request: AbortController | undefined

  async function recheck() {
    if (!user.value) return
    request?.abort()
    const { signal } = (request = new AbortController())
    try {
      const status = unwrap(await api.GET('/api/v1/contests/agent-access', { signal }))
      if (signal.aborted) return
      clearTimeout(timer)
      blocked.value = Boolean(status.blocked_until)
      const transition = status.blocked_until ?? status.next_start_at
      if (transition) {
        const delay = Date.parse(transition) - Date.parse(status.checked_at)
        timer = setTimeout(
          () => {
            if (delay <= 2_147_000_000 && !status.blocked_until) blocked.value = true
            void recheck()
          },
          Math.min(Math.max(delay, 100), 2_147_000_000),
        )
      }
    } catch {
      // Authentication and access are still checked on every assistant request.
    }
  }

  function syncRegistration(event: StorageEvent) {
    if (event.key === registrationKey) void recheck()
  }

  watch(
    () => user.value?.id,
    () => {
      request?.abort()
      clearTimeout(timer)
      blocked.value = false
      void recheck()
    },
    { immediate: true },
  )
  onMounted(() => {
    window.addEventListener('focus', recheck)
    window.addEventListener('storage', syncRegistration)
  })
  onBeforeUnmount(() => {
    request?.abort()
    clearTimeout(timer)
    window.removeEventListener('focus', recheck)
    window.removeEventListener('storage', syncRegistration)
  })
  return { blocked, recheck }
}
