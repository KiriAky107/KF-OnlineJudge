import { newRequestId } from '../lib/requestCrypto.ts'

// Keep the session and request identifiers across a network failure or tab reload.
export function agentRunIntent(
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
  owner: string,
  fingerprint: string,
) {
  const location = `kf-oj:agent-intent:${owner}:${fingerprint}`
  let previous: { requestId?: unknown; sessionId?: unknown } | undefined
  try {
    previous = JSON.parse(storage.getItem(location) ?? 'null') ?? undefined
  } catch {
    previous = undefined
  }
  const requestId = typeof previous?.requestId === 'string' ? previous.requestId : newRequestId()
  const sessionId = typeof previous?.sessionId === 'string' ? previous.sessionId : undefined
  return {
    requestId,
    sessionId,
    bind(id: string) {
      storage.setItem(location, JSON.stringify({ requestId, sessionId: id }))
    },
    complete() {
      storage.removeItem(location)
    },
  }
}
