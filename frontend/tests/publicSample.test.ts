import test from 'node:test'
import assert from 'node:assert/strict'
import { sample } from '../src/lib/publicSample.ts'
test('shared public sample display preserves unicode, empty output, binary fallback', () => {
  assert.equal(sample(btoa(String.fromCharCode(...new TextEncoder().encode('你好\n')))), '你好\n')
  assert.equal(sample(''), '')
  assert.match(sample('/w=='), /十六进制.*ff/)
})
