import { test } from 'node:test'
import assert from 'node:assert/strict'
import { trainingIntent } from '../src/api/trainingIntent.ts'

test('training creation retries and reloads retain key; changed parameters or owner do not', () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
    removeItem: (key: string) => {
      values.delete(key)
    },
  }
  const first = trainingIntent(storage, 'a', 'tags:x', () => 'first')
  assert.equal(trainingIntent(storage, 'a', 'tags:x', () => 'second').key, first.key)
  assert.equal(trainingIntent(storage, 'b', 'tags:x', () => 'other-owner').key, 'other-owner')
  assert.equal(trainingIntent(storage, 'a', 'tags:y', () => 'new-parameters').key, 'new-parameters')
  first.complete()
  assert.equal(trainingIntent(storage, 'a', 'tags:y', () => 'new-plan').key, 'new-plan')
})

test('training creation stops before sending when receipt cannot be persisted', () => {
  assert.throws(() =>
    trainingIntent(
      {
        getItem: () => null,
        setItem: () => {
          throw new Error('storage unavailable')
        },
        removeItem: () => {},
      },
      'owner',
      '{}',
    ),
  )
})
