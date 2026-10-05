import { onMounted, onScopeDispose, ref } from 'vue'
import { fetchLiveness } from '../api/health'

export function usePlatformHealth() {
  const state = ref<'loading' | 'connected' | 'unavailable'>('loading')
  const version = ref('')
  let controller: AbortController | undefined

  async function refresh() {
    controller?.abort()
    const request = new AbortController()
    controller = request
    state.value = 'loading'
    try {
      const health = await fetchLiveness(request.signal)
      if (!request.signal.aborted) {
        version.value = health.version
        state.value = 'connected'
      }
    } catch {
      if (!request.signal.aborted) state.value = 'unavailable'
    }
  }

  onMounted(refresh)
  onScopeDispose(() => controller?.abort())
  return { state, version, refresh }
}
