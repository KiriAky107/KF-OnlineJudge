import { readonly, ref } from 'vue'
import { api, onUnauthorized } from '../api/client'
import { ApiError, csrfHeaders, unwrap } from '../api/transport'
import type { components } from '../api/generated/schema'

const user = ref<components['schemas']['CurrentUser'] | null>(null)
let generation = 0
let pending: Promise<boolean> | undefined
onUnauthorized(() => {
  generation++
  user.value = null
})

async function refresh() {
  if (pending) return pending
  const current = generation
  pending = (async () => {
    try {
      const data = unwrap(await api.GET('/api/v1/auth/me'))
      if (current === generation) user.value = data
      return current === generation && user.value !== null
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return false
      throw error
    } finally {
      pending = undefined
    }
  })()
  return pending
}

async function login(username: string, password: string) {
  const current = ++generation
  user.value = null
  const data = unwrap(await api.POST('/api/v1/auth/login', { body: { username, password } }))
  if (current === generation) user.value = data.user
}

async function logout() {
  const response = await api.POST('/api/v1/auth/logout', { headers: csrfHeaders() })
  if (!response.response.ok && response.response.status !== 401)
    throw new ApiError(response.response.status)
  generation++
  user.value = null
}

export function useSession() {
  return { user: readonly(user), refresh, login, logout }
}
