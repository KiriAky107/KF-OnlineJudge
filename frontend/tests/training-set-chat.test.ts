import assert from 'node:assert/strict'
import test from 'node:test'
import type { components } from '../src/api/generated/schema'
import {
  hasTrainingCandidateAccess,
  trainingChatSkill,
  trainingTurn,
} from '../src/lib/trainingSetChat.ts'

type Skill = components['schemas']['SkillVersionView']
const candidates = { name: 'training_candidates', version: 1 }

test('chat chooses a compatible skill and keeps a draft bound to its original version', () => {
  const latest: Skill = {
    version_id: 'latest',
    origin: 'builtin',
    slug: 'focused-training',
    title: '专项训练',
    description: '',
    release: 2,
    purpose: 'focused_training',
    default_parameters: {},
    available_tools: [candidates],
  }
  const older: Skill = {
    ...latest,
    version_id: 'older',
    release: 1,
    binding: {
      skill_version_id: 'older',
      enabled: false,
      parameters: { daily_minutes: 25 },
      authorized_tools: [candidates],
    },
  }
  const draftSkill: Skill = {
    ...latest,
    origin: 'trial',
    version_id: 'private',
    available_tools: [],
    binding: { skill_version_id: 'private', enabled: true, parameters: {}, authorized_tools: [] },
  }
  const authorized: Skill = {
    ...draftSkill,
    version_id: 'other',
    available_tools: [candidates],
    binding: { ...draftSkill.binding!, skill_version_id: 'other', authorized_tools: [candidates] },
  }
  assert.equal(trainingChatSkill([latest, draftSkill]), latest)
  assert.equal(trainingChatSkill([latest, older, draftSkill]), older)
  assert.equal(trainingChatSkill([latest, older, draftSkill, authorized]), authorized)
  assert.equal(trainingChatSkill([latest, older, authorized], 'older'), older)
  assert.equal(trainingChatSkill([latest, authorized], 'older'), undefined)
  assert.equal(trainingChatSkill([latest, authorized], 'latest'), undefined)
  assert.equal(trainingChatSkill([latest, draftSkill], 'private'), undefined)
  assert.equal(
    hasTrainingCandidateAccess({ ...authorized, available_tools: [{ ...candidates, version: 2 }] }),
    false,
  )
})

test('a named training request keeps explicit tags, count and difficulty', () => {
  const turn = trainingTurn('建一套二分查找训练集，四道入门题')
  assert.ok('draft' in turn)
  assert.equal(turn.draft.title, '二分查找训练集')
  assert.equal(turn.draft.count, 4)
  assert.deepEqual(turn.draft.tags, ['二分查找'])
  assert.equal(turn.draft.difficulty, 'easy')
  const two = trainingTurn('知识点：二分查找、前缀和，4 道')
  assert.ok('draft' in two)
  assert.deepEqual(two.draft.tags, ['二分查找', '前缀和'])
  assert.equal(two.draft.cover_each_goal, true)
})

test('a replacement references the reviewed draft and does not change the count', () => {
  const turn = trainingTurn('换第 2 题', {
    runId: 'previous',
    options: { title: '二分训练', count: 4, tags: ['二分查找'], cover_each_goal: false },
  })
  assert.ok('draft' in turn)
  assert.equal(turn.draft.count, 4)
  assert.deepEqual(turn.draft.revision, { run_id: 'previous', replace_position: 2 })
  const newCount = trainingTurn('改成 3 道题', { runId: 'revised', options: turn.draft })
  assert.ok('draft' in newCount)
  assert.equal(newCount.draft.count, 3)
  assert.equal(newCount.draft.revision, undefined)
  assert.ok('reply' in trainingTurn('做一个训练集'))
})

test('a topic clarification keeps conditions from the previous turn', () => {
  const first = trainingTurn('来两道入门题，名称：周末练习')
  assert.ok('reply' in first && first.awaitingTopic)

  const topic = trainingTurn('数组。', undefined, first.awaitingTopic)
  assert.ok('draft' in topic)
  assert.equal(topic.draft.title, '周末练习')
  assert.equal(topic.draft.count, 2)
  assert.equal(topic.draft.difficulty, 'easy')
  assert.deepEqual(topic.draft.tags, ['数组'])

  const unrestricted = trainingTurn('知识点不限', undefined, first.awaitingTopic)
  assert.ok('draft' in unrestricted)
  assert.deepEqual(unrestricted.draft.tags, [])
  assert.equal(unrestricted.draft.count, 2)

  const withoutContext = trainingTurn('数组。')
  assert.ok('reply' in withoutContext)
})
