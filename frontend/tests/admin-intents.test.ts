import { test } from 'node:test'
import assert from 'node:assert/strict'
import { importIntent, mutationIntent, hasMaintenance } from '../src/admin/intents.ts'
const first = '11111111-1111-4111-8111-111111111111',
  second = '22222222-2222-4222-8222-222222222222'
test('import receipt survives retry but new content/actor/expiry receives a new UUID', () => {
  const value = importIntent(null, 'owner', 'a'.repeat(64), 100, 1000, () => first)
  assert.equal(
    importIntent(value, 'owner', 'a'.repeat(64), 100, 2000, () => second),
    value,
  )
  assert.equal(
    importIntent(value, 'owner', 'b'.repeat(64), 100, 2000, () => second).operation_id,
    second,
  )
  assert.equal(
    importIntent(value, 'other', 'a'.repeat(64), 100, 2000, () => second).operation_id,
    second,
  )
  assert.equal(
    importIntent(value, 'owner', 'a'.repeat(64), 100, 86402000, () => second).operation_id,
    second,
  )
  assert.throws(() => importIntent(value, 'owner', 'a'.repeat(64), 67108865))
  assert.deepEqual(Object.keys(value).sort(), [
    'archive_bytes',
    'archive_digest',
    'created',
    'operation_id',
    'owner',
  ])
})
test('mutation retries preserve UUID and changing any precondition creates a new receipt', () => {
  const v = mutationIntent(null, 'owner', 'publish:candidate:v1', 1000, () => first)
  assert.equal(
    mutationIntent(v, 'owner', 'publish:candidate:v1', 2000, () => second),
    v,
  )
  assert.equal(
    mutationIntent(v, 'owner', 'publish:candidate:v2', 2000, () => second).operation_id,
    second,
  )
  assert.equal(
    mutationIntent(v, 'owner', 'disable:problem:v1', 2000, () => second).operation_id,
    second,
  )
  assert.equal(hasMaintenance(null), false)
  assert.equal(hasMaintenance({ capabilities: { problem_maintenance: false } }), false)
  assert.equal(hasMaintenance({ capabilities: { problem_maintenance: true } }), true)
})
