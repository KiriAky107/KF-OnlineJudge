import createClient from 'openapi-fetch'
import type { paths } from './generated/schema'
import { boundedFetch, rejectSession401 } from './transport'

let expired: () => void = () => {}
let authorizationVersion = 0
export function advanceAuthorization() {
  authorizationVersion++
}
export function onUnauthorized(handler: () => void) {
  expired = handler
}

export const api = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL || '',
  fetch: boundedFetch(
    globalThis.fetch.bind(globalThis),
    () => expired(),
    () => authorizationVersion,
  ),
})
api.use({ onResponse: rejectSession401 })

// Only the bounded administrator ZIP endpoint uses the longer upload deadline.
// Ordinary login/submission requests keep their existing ten-second budget.
export const uploadApi = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE_URL || '',
  fetch: boundedFetch(
    globalThis.fetch.bind(globalThis),
    () => expired(),
    () => authorizationVersion,
    40000,
  ),
})
