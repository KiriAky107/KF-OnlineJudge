import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  reviewCandidates,
  defaultReviewCandidate,
  reviewNotice,
} from '../src/lib/submissionReview.ts'
const current = {
  submission_id: 'now',
  created_at: '2026-11-19T12:00:00Z',
  problem_version_id: 'v1',
}
test('candidate selection excludes current and other versions; defaults to prior submission', () => {
  const newer = { ...current, submission_id: 'newer', created_at: '2026-11-19T13:00:00Z' }
  const prior = { ...current, submission_id: 'prior', created_at: '2026-11-19T11:00:00Z' }
  const items = reviewCandidates(
    [newer, current, prior, { ...prior, problem_version_id: 'v2' }],
    current,
  )
  assert.deepEqual(items, [newer, prior])
  assert.equal(defaultReviewCandidate(items, current), 'prior')
  assert.equal(defaultReviewCandidate([], current), '')
})
test('different versions/languages are facts, not inferred improvement', () => {
  assert.match(reviewNotice(false, false), /题目版本不同/)
  assert.match(reviewNotice(false, false), /语言不同/)
  assert.match(reviewNotice(true, true), /不证明/)
  assert.doesNotMatch(reviewNotice(true, true), /题目版本不同/)
})
