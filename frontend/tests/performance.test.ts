import assert from 'node:assert/strict'
import test from 'node:test'
import { chartPosition, formatMeasurement } from '../src/lib/performance.ts'

test('measurements preserve raw integer precision and distinguish missing from zero', () => {
  assert.equal(formatMeasurement(null, 'bytes'), '—')
  assert.equal(formatMeasurement('0', 'cpu_microseconds'), '0')
  assert.equal(formatMeasurement('10000', 'cpu_microseconds'), '10')
  assert.equal(formatMeasurement('9007199254740993', 'cpu_microseconds'), '9007199254740.993')
  assert.equal(formatMeasurement('9007199254740992', 'cpu_microseconds'), '9007199254740.992')
  assert.equal(formatMeasurement('1', 'bytes'), '<0.000001')
  assert.equal(chartPosition('0', '1'), 0)
  assert.equal(chartPosition('9223372036854775807', '9223372036854775807'), 1)
})
