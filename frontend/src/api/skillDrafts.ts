import { api } from './client'
import type { components } from './generated/schema'
import { trainingIntent } from './trainingIntent'
import { csrfHeaders, unwrap } from './transport'
import { sha256Fingerprint } from '../lib/requestCrypto'

export type CreateSkillDraft = Omit<components['schemas']['CreateSkillDraft'], 'request_id'>

export async function createSkillDraft(body: CreateSkillDraft, owner: string, signal: AbortSignal) {
  const raw = new TextEncoder().encode(JSON.stringify(body))
  const fingerprint = sha256Fingerprint(raw)
  signal.throwIfAborted()
  const intent = trainingIntent(sessionStorage, 'skill-draft:' + owner, fingerprint)
  const draft = unwrap(
    await api.POST('/api/v1/learning/agent/skill-drafts', {
      body: { ...body, request_id: intent.key },
      headers: csrfHeaders(),
      signal,
    }),
  )
  signal.throwIfAborted()
  intent.complete()
  return draft
}
