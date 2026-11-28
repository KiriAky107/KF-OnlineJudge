import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  boundaryDrafts,
  boundaryRange,
  checkBoundarySample,
  renderBoundaryInput,
} from '../src/lib/problemBoundaryDrafts.ts'

const fields = {
  minimumCount: '0',
  maximumCount: '4',
  minimumValue: '-9223372036854775808',
  maximumValue: '9223372036854775807',
}

test('preserves signed 64-bit endpoints and generates valid bounded input drafts', () => {
  const range = boundaryRange(fields)
  const drafts = boundaryDrafts(range)
  assert.deepEqual(
    drafts.map((draft) => draft.count),
    [0, 4, 4, 1],
  )
  assert.equal(renderBoundaryInput(drafts[0]), '0\n')
  assert.equal(
    renderBoundaryInput(drafts[2]),
    '4\n-9223372036854775808 9223372036854775807 -9223372036854775808 9223372036854775807\n',
  )
  assert.equal(renderBoundaryInput(drafts[2], 1), '4\n-9223372036854775808\n')
  for (const draft of drafts) {
    assert.equal(checkBoundarySample(renderBoundaryInput(draft), range), '满足确认的输入结构和范围')
  }
})

test('deduplicates equal ranges and diagnoses sample structure, not expected output', () => {
  const range = boundaryRange({
    minimumCount: '1',
    maximumCount: '1',
    minimumValue: '２',
    maximumValue: '+2',
  })
  assert.deepEqual(
    boundaryDrafts(range).map((draft) => renderBoundaryInput(draft)),
    ['1\n2\n'],
  )
  assert.equal(checkBoundarySample('1\n', range), '实际元素数与声明个数不同')
  assert.equal(checkBoundarySample('1\n3\n', range), '有元素不在确认的值范围内')
  assert.equal(checkBoundarySample('1\n+2\n', range), '满足确认的输入结构和范围')
  assert.equal(checkBoundarySample('0\n', range), '首个整数不在确认的元素个数范围内')
  assert.equal(checkBoundarySample(null, range), '二进制输入，需人工核对')
  assert.throws(() => boundaryRange({ ...fields, maximumCount: '1000001' }), /元素个数/)
  assert.throws(() => boundaryRange({ ...fields, minimumCount: '-1' }), /元素个数/)
  assert.throws(() => boundaryRange({ ...fields, minimumValue: '-9223372036854775809' }), /64 位/)
  assert.throws(() => boundaryRange({ ...fields, maximumValue: '1e8' }), /64 位/)
})
