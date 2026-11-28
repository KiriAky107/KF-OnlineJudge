import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mistakePracticeTurn } from '../src/lib/mistakePractice.ts'

test('personal practice is selected for similar problems and timed follow-ups in context', () => {
  assert.deepEqual(mistakePracticeTurn('帮我找几道类似的题', false), {})
  assert.deepEqual(mistakePracticeTurn('今晚只有 30 分钟。', true), { minutes: 30 })
  assert.deepEqual(mistakePracticeTurn('三十分钟', true), { minutes: 30 })
  assert.deepEqual(mistakePracticeTurn('按最近的错因补练半小时', false), { minutes: 30 })
  assert.ok('clarification' in mistakePracticeTurn('按这个错因补练', false)!)
  assert.ok('clarification' in mistakePracticeTurn('今晚只有 0 分钟', true)!)
})

test('ordinary conversations and named problem searches keep their existing routing', () => {
  assert.equal(mistakePracticeTurn('今晚只有 30 分钟', false), undefined)
  assert.equal(mistakePracticeTurn('今晚还有 30 分钟的作业', true), undefined)
  assert.equal(mistakePracticeTurn('找三道图论题', true), undefined)
  assert.equal(mistakePracticeTurn('安排 30 分钟阶段复习', true), undefined)
})
