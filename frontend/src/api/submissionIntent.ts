// No source is stored here. Fingerprints bind an immutable request and owner to a UUID.
import type { components } from './generated/schema'
export type SubmissionInput = {
  problem_version_id: string
  language: components['schemas']['kf_oj__contracts__judging_v2__Language']
  source: string
}
type Intent = { fingerprint: string; key: string; submissionId?: string }

export function submissionIntent(storage: Pick<Storage, 'getItem' | 'setItem'>, owner: string) {
  const prefix = 'kf-oj:submission-intent:' + owner + ':'
  let pending: Promise<string> | undefined
  return {
    submit(input: SubmissionInput, send: (key: string) => Promise<string>): Promise<string> {
      if (pending) return pending
      const snapshot = { ...input }
      pending = (async () => {
        const bytes = new TextEncoder().encode(snapshot.source)
        if (!bytes.length || bytes.length > 262144) throw new Error('源码必须为 1 至 262144 字节。')
        const hash = await crypto.subtle.digest(
          'SHA-256',
          new TextEncoder().encode(JSON.stringify(snapshot)),
        )
        const fingerprint = Array.from(new Uint8Array(hash), (v) =>
          v.toString(16).padStart(2, '0'),
        ).join('')
        const location = prefix + snapshot.problem_version_id
        let intent: Intent
        try {
          const raw: unknown = JSON.parse(storage.getItem(location) ?? 'null')
          intent =
            raw &&
            typeof raw === 'object' &&
            'fingerprint' in raw &&
            raw.fingerprint === fingerprint &&
            'key' in raw &&
            typeof raw.key === 'string' &&
            /^[0-9a-f-]{36}$/.test(raw.key)
              ? (raw as Intent)
              : { fingerprint, key: crypto.randomUUID() }
          storage.setItem(location, JSON.stringify(intent))
        } catch {
          throw new Error('浏览器无法保存提交凭据。请恢复本地存储后重试，草稿仍在编辑器中。')
        }
        if (intent.submissionId) return intent.submissionId
        const id = await send(intent.key)
        // The key was saved BEFORE the network call. If this write fails a retry still uses it.
        try {
          storage.setItem(location, JSON.stringify({ ...intent, submissionId: id }))
        } catch {
          /* stable key already persisted */
        }
        return id
      })().finally(() => {
        pending = undefined
      })
      return pending
    },
  }
}
