import { test } from 'node:test'
import assert from 'node:assert/strict'
import { displayRuntimeVersion, displayTime } from '../src/lib/presentation.ts'

test('runtime labels keep published versions and omit commit and digest metadata', () => {
  assert.equal(displayRuntimeVersion('16.2.1+r23+gd564253eb6c8'), '16.2.1+r23')
  assert.equal(displayRuntimeVersion('CPython 3.13.16'), 'CPython 3.13.16')
  assert.equal(displayRuntimeVersion('OpenJDK 21.0.12.1+1-LTS'), 'OpenJDK 21.0.12.1+1-LTS')
  for (const digest of ['a'.repeat(64), 'A'.repeat(64), 'b'.repeat(40)]) {
    assert.equal(displayRuntimeVersion(`runtime sha256:${digest}`), 'runtime 已校验构建')
    assert.equal(displayRuntimeVersion(`runtime ${digest}`), 'runtime 已校验构建')
  }
})

test('dates do not invent a timestamp when a value is unavailable', () => {
  assert.equal(displayTime(''), '时间暂不可用')
  assert.equal(displayTime('invalid'), '时间暂不可用')
  const actual = displayTime('2026-11-11T15:41:51.000Z')
  assert.match(actual, /2026/)
  assert.doesNotMatch(actual, /NaN|undefined|Invalid/)
})
