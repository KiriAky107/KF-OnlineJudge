import type { components } from '../api/generated/schema'

export type PeriodicReviewRequest = components['schemas']['PeriodicReviewRequest']
type ReviewTurn = { request: PeriodicReviewRequest } | { clarification: string }

const reviewIntent =
  /阶段复习|周期复习|复习(?:安排|计划|题单)|(?:安排|制定|帮我|请|想).{0,30}复习|^(?:今天|今晚|这周)?复习[。！!？?]?$/
const timePart =
  /([+-]?[0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百]+)\s*个?(小时|分钟)|(?:半个?小时)/g

function timeNumber(value: string) {
  if (/^[+-]?\d/.test(value)) return Number(value)
  const digits = '零一二三四五六七八九'
  let total = 0
  let digit = 0
  for (const character of value.replaceAll('两', '二')) {
    if (character === '百' || character === '十') {
      total += (digit || 1) * (character === '百' ? 100 : 10)
      digit = 0
    } else digit = digits.indexOf(character)
  }
  return total + digit
}

export function periodicReviewTurn(
  input: string,
  previousQuestion?: string,
): ReviewTurn | undefined {
  const text = input.trim()
  const parts = [...text.matchAll(timePart)]
  const timeReply =
    previousQuestion && parts.length && /^[\s，。！!]*$/.test(text.replace(timePart, ''))
  if (
    !timeReply &&
    !reviewIntent.test(text) &&
    !(parts.length && /复习/.test(text)) &&
    !(parts.length && /^(?:今晚|今天|这会儿)?\s*(?:我)?(?:只有|还有|就剩)/.test(text))
  )
    return undefined

  const question = timeReply ? `${previousQuestion}，${text}` : text
  if (!parts.length) return { clarification: '这次想留多少时间复习？可以直接回复「30 分钟」。' }
  const minutes = parts.reduce((sum, part) => {
    if (part[1] === undefined) return sum + 30
    return sum + timeNumber(part[1]) * (part[2] === '小时' ? 60 : 1)
  }, 0)
  if (!Number.isInteger(minutes) || minutes < 5 || minutes > 180)
    return { clarification: '请给出 5 到 180 分钟的本次复习时间。' }
  if (question.length > 1200) return { clarification: '复习要求请控制在 1200 字以内。' }

  const withoutTime = question.replace(timePart, '')
  const topic =
    /(?:知识点|标签)[:：]\s*(.+?)(?=[，,。；;\n]|$)/.exec(withoutTime)?.[1] ??
    /复习(?:一下|一遍)?\s*([^，,。；;？?\n]+?)(?=\s*(?:知识点)?[，,。；;]|$)/.exec(withoutTime)?.[1]
  const tags =
    topic && !/^(?:计划|题单|安排|一下|我的|最近|做过|错题|进度)/.test(topic)
      ? [
          ...new Set(
            topic
              .split(/[、/]/)
              .map((tag) => tag.trim())
              .filter(Boolean),
          ),
        ]
      : []
  if (tags.length > 10 || tags.some((tag) => tag.length > 64))
    return { clarification: '请指定最多 10 个知识点，每个名称不超过 64 字。' }
  return { request: { question, available_minutes: minutes, tags } }
}
