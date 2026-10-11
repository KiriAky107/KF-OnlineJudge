// Persist UUID receipts, never file bytes, passwords, roles or session tokens.
export type ImportIntent = {
  owner: string
  operation_id: string
  archive_digest: string
  archive_bytes: number
  created: number
}
export type MutationIntent = {
  owner: string
  operation_id: string
  fingerprint: string
  created: number
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export function importIntent(
  previous: ImportIntent | null,
  owner: string,
  digest: string,
  bytes: number,
  now = Date.now(),
  makeId = () => crypto.randomUUID(),
): ImportIntent {
  if (!/^[0-9a-f]{64}$/.test(digest) || !Number.isInteger(bytes) || bytes < 1 || bytes > 67108864)
    throw new Error('请选择 1 字节至 64 MiB 的 ZIP 题包。')
  if (
    previous &&
    previous.owner === owner &&
    previous.archive_digest === digest &&
    previous.archive_bytes === bytes &&
    uuid.test(previous.operation_id) &&
    now >= previous.created &&
    now - previous.created < 86400000
  )
    return previous
  return {
    owner,
    operation_id: makeId(),
    archive_digest: digest,
    archive_bytes: bytes,
    created: now,
  }
}
export function mutationIntent(
  previous: MutationIntent | null,
  owner: string,
  fingerprint: string,
  now = Date.now(),
  makeId = () => crypto.randomUUID(),
): MutationIntent {
  if (
    previous &&
    previous.owner === owner &&
    previous.fingerprint === fingerprint &&
    uuid.test(previous.operation_id) &&
    now >= previous.created &&
    now - previous.created < 86400000
  )
    return previous
  return { owner, operation_id: makeId(), fingerprint, created: now }
}
const prefix = 'kf-admin-receipt-v1-'
export function loadIntent<T>(kind: 'import' | 'mutation'): T | null {
  try {
    const value = localStorage.getItem(prefix + kind)
    return value && value.length <= 2048 ? (JSON.parse(value) as T) : null
  } catch {
    return null
  }
}
export function saveIntent(
  kind: 'import' | 'mutation',
  value: ImportIntent | MutationIntent | null,
) {
  try {
    if (value) localStorage.setItem(prefix + kind, JSON.stringify(value))
    else localStorage.removeItem(prefix + kind)
  } catch {
    /* In-memory intent still deduplicates this page. */
  }
}
export function clearIntents() {
  saveIntent('import', null)
  saveIntent('mutation', null)
}
export function hasMaintenance(
  user: { capabilities?: { problem_maintenance: boolean } } | null,
): boolean {
  return user?.capabilities?.problem_maintenance === true
}
