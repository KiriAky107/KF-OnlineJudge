import { test } from 'node:test'
import assert from 'node:assert/strict'
import { decodeAuthoringSample, reviewProblemDraft } from '../src/lib/problemAuthoringReview.ts'

const statement = [
  '# 计数问题',
  '## 输入格式',
  '第一行读入整数。',
  '## 输出格式',
  '输出答案。',
  '## 数据范围',
  '- 1 ≤ n ≤ 100',
  '## 样例 1',
  '### 输入',
  '```text',
  '3  5',
  '```',
  '### 输出',
  '```text',
  '8',
  '```',
  '## Sample Input #2',
  '```',
  '0',
  '```',
  '## Sample Output 2',
  '```',
  '0',
  '```',
].join('\n')

test('pairs grouped and numbered examples without changing meaningful whitespace', () => {
  const result = reviewProblemDraft(statement, [
    { input: '3  5\r\n', output: '8' },
    { input: '0', output: '1' },
  ])
  assert.deepEqual(result.sections, { input: true, output: true, constraints: true })
  assert.deepEqual(result.constraints, ['1 ≤ n ≤ 100'])
  assert.deepEqual(result.samples, [
    { input: 'matched', output: 'matched' },
    { input: 'matched', output: 'different' },
  ])
  assert.deepEqual(reviewProblemDraft(statement, [{ input: '3 5', output: '8' }]).samples, [
    { input: 'different', output: 'matched' },
  ])
})

test('does not interpret example code as headings and keeps unknown or binary samples unverified', () => {
  const nested = '# Sample Input 1\n```text\n# Data Constraints\n```\n```\nsecond\n```'
  const result = reviewProblemDraft(nested, [{ input: 'value', output: null }])
  assert.deepEqual(result.sections, { input: false, output: false, constraints: false })
  assert.deepEqual(result.samples, [{ input: 'ambiguous', output: 'binary' }])
  assert.deepEqual(
    reviewProblemDraft(statement, [{ input: '3  5', output: '8' }]).extraSampleLabels,
    [2],
  )
  assert.equal(decodeAuthoringSample('/w=='), null)
  assert.equal(decodeAuthoringSample('MAo='), '0\n')
})

test('recognizes a standalone sample label written with bold text', () => {
  const formatted = '**样例输入 1：**\n\n~~~text\n0\n~~~\n\n**样例输出 1：**\n\n~~~text\n\n~~~'
  assert.deepEqual(reviewProblemDraft(formatted, [{ input: '0', output: '' }]).samples, [
    { input: 'matched', output: 'matched' },
  ])
})
