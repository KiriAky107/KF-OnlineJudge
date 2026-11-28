import { requestedPracticeMinutes } from './assistantPeriodicReview.ts'

type MistakeTurn = { minutes?: number } | { clarification: string }

export function mistakePracticeTurn(
  input: string,
  hasMistakeContext: boolean,
): MistakeTurn | undefined {
  const text = input.trim()
  const similar =
    /(?:找|推荐|挑|选).{0,20}(?:(?:同类|同类型|类似|相似)的?题|这类题)/.test(text) ||
    /^(?:同类题|类似题)[。？?]?$/.test(text)
  const mentionsMistake = /错因|错题/.test(text)
  const practice =
    ((hasMistakeContext || mentionsMistake) && /补练/.test(text)) ||
    (mentionsMistake && /练|复习/.test(text))
  const duration = text
    .replace(/^(?:今天|今晚|这会儿)?\s*(?:我)?(?:只有|就剩|还有)\s*/, '')
    .replace(/(?:了)?[。！？!?]*$/, '')
  const minutes = requestedPracticeMinutes(text)
  const timeOnly = requestedPracticeMinutes(duration, true) !== undefined
  if (!similar && !practice && !(hasMistakeContext && timeOnly)) return
  if (minutes === undefined)
    return similar ? {} : { clarification: '这次想留多少时间补练？例如「30 分钟」。' }
  if (!Number.isInteger(minutes) || minutes < 5 || minutes > 180)
    return { clarification: '请给出 5 到 180 分钟的补练时间。' }
  return { minutes }
}
