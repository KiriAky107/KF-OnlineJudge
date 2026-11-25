import assert from 'node:assert/strict'
import test from 'node:test'
import type { components } from '../src/api/generated/schema'
import { periodicReviewTurn } from '../src/lib/assistantPeriodicReview.ts'
import {
  hasTrainingCandidateAccess,
  isProblemSearchQuestion,
  isTrainingMessage,
  trainingChatSkill,
  trainingSearchTurn,
  trainingTurn,
} from '../src/lib/trainingSetChat.ts'

type Skill = components['schemas']['SkillVersionView']
const candidates = { name: 'training_candidates', version: 1 }

test('timed review asks for missing time and keeps the topic when the user replies', () => {
  const initial = '帮我安排阶段复习，知识点：数组、前缀和'
  assert.deepEqual(periodicReviewTurn(initial), {
    clarification: '这次想留多少时间复习？可以直接回复「30 分钟」。',
  })
  assert.deepEqual(periodicReviewTurn('三十分钟', initial), {
    request: {
      question: initial + '，三十分钟',
      available_minutes: 30,
      tags: ['数组', '前缀和'],
    },
  })
  assert.deepEqual(periodicReviewTurn('今晚只有半小时'), {
    request: { question: '今晚只有半小时', available_minutes: 30, tags: [] },
  })
  assert.deepEqual(periodicReviewTurn('帮我复习三十分钟'), {
    request: { question: '帮我复习三十分钟', available_minutes: 30, tags: [] },
  })
  assert.equal(periodicReviewTurn('二分查找的循环条件是什么？'), undefined)
  assert.ok('clarification' in periodicReviewTurn('帮我安排 240 分钟复习')!)
})

test('site problem search keeps the actual numeric difficulty range and own unsolved filter', () => {
  const message = '帮我找 3 道数组题，难度分 800 到 1200，只要我还没通过的'
  assert.equal(isTrainingMessage(message, false, false), true)
  assert.equal(isProblemSearchQuestion('创建数组训练集，挑 3 道题', false), false)
  const first = trainingSearchTurn(message)
  assert.ok('draft' in first)
  assert.equal(first.draft.count, 3)
  assert.deepEqual(first.draft.tags, ['数组'])
  assert.deepEqual(first.draft.search, {
    minimum_score: 800,
    maximum_score: 1200,
    only_unsolved: true,
  })
  const next = trainingSearchTurn('难度分 1300 分以下，已通过的也要', first.draft)
  assert.ok('draft' in next)
  assert.deepEqual(next.draft.search, {
    minimum_score: null,
    maximum_score: 1300,
    only_unsolved: false,
  })
  assert.deepEqual(next.draft.tags, ['数组'])
  assert.ok('reply' in trainingSearchTurn('难度分 1200 到 800', first.draft))
})

test('the shared composer distinguishes training commands from programming questions', () => {
  assert.equal(isTrainingMessage('给我创建一个简单题的训练集', false, false), true)
  assert.equal(isTrainingMessage('换第 2 题', true, false), true)
  assert.equal(isTrainingMessage('为什么选第 2 题？', true, false), true)
  assert.equal(isTrainingMessage('二分查找', false, true), true)
  assert.equal(isTrainingMessage('改成 4 道入门题', true, false), true)
  assert.equal(isTrainingMessage('如何建立训练集？', false, false), false)
  assert.equal(isTrainingMessage('改成用前缀和怎么做？', true, false), false)
  assert.equal(isTrainingMessage('这个数组的边界怎么处理？', true, false), false)
})

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

test('difficulty-only training selects across topics and clears an earlier topic filter', () => {
  const text = '给我创建一个简单题的训练集'
  const turn = trainingTurn(text)
  assert.ok('draft' in turn)
  assert.equal(turn.draft.count, 5)
  assert.equal(turn.draft.difficulty, 'easy')
  assert.deepEqual(turn.draft.tags, [])

  const revised = trainingTurn(text, {
    runId: 'previous',
    options: { ...turn.draft, tags: ['简单题'] },
  })
  assert.ok('draft' in revised)
  assert.deepEqual(revised.draft.tags, [])
  assert.equal(revised.draft.revision, undefined)

  const withTopic = trainingTurn('建一套 4 道入门数组题的训练集')
  assert.ok('draft' in withTopic)
  assert.equal(withTopic.draft.count, 4)
  assert.equal(withTopic.draft.difficulty, 'easy')
  assert.deepEqual(withTopic.draft.tags, ['数组'])

  const explicit = trainingTurn('创建简单题的训练集，知识点：数组')
  assert.ok('draft' in explicit)
  assert.deepEqual(explicit.draft.tags, ['数组'])
})

test('chat uses the requested evidence ranking without inventing a knowledge tag', () => {
  const initial = trainingTurn('来四道简单题，优先巩固我最近没通过的知识点')
  assert.ok('draft' in initial)
  assert.equal(initial.draft.ranking_profile, 'unresolved_first')
  assert.equal(initial.draft.difficulty, 'easy')
  assert.equal(initial.draft.count, 4)
  assert.deepEqual(initial.draft.tags, [])

  const named = trainingTurn('建一套优先巩固薄弱知识点的训练集')
  assert.ok('draft' in named)
  assert.deepEqual(named.draft.tags, [])

  const current = { runId: 'previous', options: { ...initial.draft, tags: ['数组'] } }
  const selfAssessment = trainingTurn('按我之前的难度自评排序', current)
  assert.ok('draft' in selfAssessment)
  assert.equal(selfAssessment.draft.ranking_profile, 'feedback_first')
  assert.deepEqual(selfAssessment.draft.tags, ['数组'])
  assert.equal(selfAssessment.draft.count, 4)
  assert.equal(selfAssessment.draft.revision, undefined)

  const reset = trainingTurn('恢复默认排序', current)
  assert.ok('draft' in reset)
  assert.equal(reset.draft.ranking_profile, null)
  assert.ok('reply' in trainingTurn('换第 2 题，按难度自评排序', current))
  assert.ok('reply' in trainingTurn('不要按难度自评排序', current))
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

test('selection questions describe the exact draft exercise without requesting a new draft', () => {
  const initial = trainingTurn('建一套入门数组训练集，两道题')
  assert.ok('draft' in initial)
  const current = {
    runId: 'current',
    options: initial.draft,
    items: [
      {
        position: 2,
        title: '前缀和',
        tags: ['数组', '前缀和'],
        difficulty: 'easy' as const,
        objective: '练习并核验所选目标',
        feedback_evidence: {
          rating: 'too_hard' as const,
          rated_at: '2026-11-24T11:00:00+08:00',
          training_id: 'training',
          training_item_id: 'exercise',
          problem_version_id: 'rated-problem',
          preferred_difficulty: 'easy' as const,
        },
        prerequisites: [
          {
            tag: '一维数组',
            metadata_revision: 1,
            evidence: {
              submission_id: 'submission',
              evaluation_id: 'evaluation',
              problem_id: 'problem',
              problem_version_id: 'prerequisite-problem',
              submitted_at: '2026-11-24T10:00:00+08:00',
              verdict: 'accepted',
              state: 'finished',
            },
          },
        ],
      },
    ],
  }
  const answer = trainingTurn('为什么选第二题？', current)
  assert.ok('reply' in answer)
  assert.match(answer.reply, /前缀和.*对应你指定的数组.*练习并核验所选目标/)
  assert.match(answer.reply, /难度自评「偏难」.*优先练习简单题.*一维数组的先修要求已有通过记录/)
  assert.deepEqual(trainingTurn('为什么选第 3 题？', current), {
    reply: '当前草案没有这个题号。请按草案里的顺序指定一道题。',
  })
})
