import assert from 'node:assert/strict'
import { test } from 'node:test'
import { submissionIntent } from '../src/api/submissionIntent.ts'
import { boundedPoll } from '../src/api/polling.ts'
import { ApiError, boundedFetch, unwrap } from '../src/api/transport.ts'

function memory() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  }
}
const input = {
  problem_version_id: 'version',
  language: 'c17' as const,
  source: 'synthetic ordinary text, never executed',
}
const flush = async () => {
  await new Promise<void>((resolve) => setImmediate(resolve))
}

test('timeout and reload reuse persisted key; double click has one flight; content and owner change key', async () => {
  const storage = memory()
  const keys: string[] = []
  const intent = submissionIntent(storage, 'owner-a')
  let respond: (id: string) => void = () => {}
  const send = async (key: string) => {
    keys.push(key)
    return new Promise<string>((resolve) => {
      respond = resolve
    })
  }
  const a = intent.submit(input, send)
  const b = intent.submit(input, send)
  assert.equal(a, b)
  await flush()
  assert.equal(keys.length, 1)
  respond('real-id')
  assert.equal(await a, 'real-id')
  assert.equal(await submissionIntent(storage, 'owner-a').submit(input, send), 'real-id')
  const uncertain = { ...input, source: 'changed synthetic content' }
  await assert.rejects(
    intent.submit(uncertain, async (key) => {
      keys.push(key)
      throw new Error('timeout')
    }),
  )
  const failedKey = keys.at(-1)
  assert.notEqual(keys[0], failedKey)
  await submissionIntent(storage, 'owner-a').submit(uncertain, async (key) => {
    assert.equal(key, failedKey)
    return 'confirmed-id'
  })
  await submissionIntent(storage, 'owner-b').submit(uncertain, async (key) => {
    assert.notEqual(key, failedKey)
    return 'different-owner-id'
  })
})

test('unavailable intent storage or oversized UTF-8 source fails before POST', async () => {
  let calls = 0
  const send = async () => {
    calls++
    return 'id'
  }
  const denied = {
    getItem: () => null,
    setItem: () => {
      throw new Error('denied')
    },
  }
  await assert.rejects(submissionIntent(denied, 'a').submit(input, send))
  await assert.rejects(
    submissionIntent(memory(), 'a').submit({ ...input, source: 'é'.repeat(131073) }, send),
  )
  assert.equal(calls, 0)
})

test('terminal response stops polling; failure stops until manual retry; requests and time are bounded', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] })
  const results: number[] = []
  let calls = 0,
    errors = 0,
    exhausted = 0
  let fail = false
  const poll = boundedPoll({
    load: async () => {
      calls++
      if (fail) throw new ApiError(401)
      return calls
    },
    terminal: (n) => n === 2,
    result: (n) => results.push(n),
    error: () => {
      errors++
    },
    exhausted: () => {
      exhausted++
    },
    interval: 10,
    maxRequests: 2,
    maxTime: 100,
  })
  poll.start()
  await flush()
  t.mock.timers.tick(10)
  await flush()
  assert.deepEqual(results, [1, 2])
  t.mock.timers.tick(1000)
  await flush()
  assert.equal(calls, 2)
  fail = true
  poll.start()
  await flush()
  t.mock.timers.tick(100)
  await flush()
  assert.equal(calls, 3)
  assert.equal(errors, 1)
  fail = false
  poll.start()
  await flush()
  t.mock.timers.tick(10)
  await flush()
  t.mock.timers.tick(10)
  await flush()
  assert.equal(calls, 5)
  assert.equal(exhausted, 1)
  poll.start()
  await flush()
  poll.visible(false)
  t.mock.timers.tick(1000)
  poll.visible(true)
  await flush()
  assert.equal(exhausted, 2)
  poll.dispose()
})

test('hidden and unmounted requests abort; stale responses cannot replace current route data', async () => {
  const pending: { signal: AbortSignal; done: (v: string) => void }[] = []
  const results: string[] = []
  const poll = boundedPoll({
    load: (signal) =>
      new Promise<string>((done) => {
        pending.push({ signal, done })
      }),
    terminal: () => true,
    result: (n) => results.push(n),
    error: () => assert.fail('stale error'),
    exhausted: () => {},
  })
  poll.start()
  poll.visible(false)
  assert.equal(pending[0]!.signal.aborted, true)
  poll.visible(true)
  pending[0]!.done('stale')
  await flush()
  assert.deepEqual(results, [])
  pending[1]!.done('current')
  await flush()
  assert.deepEqual(results, ['current'])
  poll.start()
  poll.dispose()
  assert.equal(pending[2]!.signal.aborted, true)
  pending[2]!.done('unmounted')
  await flush()
  assert.deepEqual(results, ['current'])
})

test('transport expires identity on401, preserves cancellation and never fills failure with preview data', async () => {
  let expired = 0
  const fetcher = boundedFetch(
    async (request) => {
      assert.equal((request as Request).credentials, 'same-origin')
      assert.equal((request as Request).cache, 'no-store')
      return new Response('{}', { status: 401 })
    },
    () => {
      expired++
    },
  )
  const response = await fetcher('http://localhost/api/v1/auth/me')
  assert.equal(expired, 1)
  assert.throws(() => unwrap({ response, data: {} }), ApiError)
  assert.throws(() => unwrap({ response: new Response('', { status: 200 }) }))
  const offline = boundedFetch(
    async () => {
      throw new Error('synthetic private detail')
    },
    () => {},
  )
  await assert.rejects(offline('http://localhost/api'), /网络中断/)
})
