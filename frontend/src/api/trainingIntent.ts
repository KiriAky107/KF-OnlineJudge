// Keep a pending creation key across refreshes, but never store learning evidence.
export function trainingIntent(
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
  owner: string,
  fingerprint: string,
  makeId: () => string = () => crypto.randomUUID(),
) {
  const location = 'kf-oj:training-intent:' + owner
  const raw = storage.getItem(location)
  const previous = raw ? JSON.parse(raw) : null
  const key: string =
    previous?.fingerprint === fingerprint && typeof previous?.key === 'string'
      ? previous.key
      : makeId()
  storage.setItem(location, JSON.stringify({ fingerprint, key }))
  return { key, complete: () => storage.removeItem(location) }
}
