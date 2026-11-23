import type { components } from '../api/generated/schema'

export type TrainingSetDraft = components['schemas']['TrainingSetDraft']
export type TrainingSetConditions = Omit<TrainingSetDraft, 'revision'>

type ActiveDraft = { runId: string; options: TrainingSetConditions }
type TrainingTurn =
  { draft: TrainingSetDraft } | { reply: string; awaitingTopic?: TrainingSetConditions }

const numbers = '零一二三四五六七八九十'
function number(text: string) {
  if (text === '两') return 2
  if (text.length === 1 && numbers.includes(text)) return numbers.indexOf(text)
  return Number(text)
}
const countPattern = /([0-9]+|[零一二两三四五六七八九十]+)\s*(?:道|题|个题目)/
const positionPattern = /第\s*([0-9]+|[零一二两三四五六七八九十]+)\s*(?:道|题)/
const difficultyOf = (text: string): TrainingSetDraft['difficulty'] | undefined => {
  if (/不限难度|难度不限/.test(text)) return null
  if (/入门|简单|初级/i.test(text)) return 'easy'
  if (/中等|中级/i.test(text)) return 'medium'
  if (/困难|高级/i.test(text)) return 'hard'
  return undefined
}
const difficultySuffix = /(?:入门|简单|初级|中等|中级|困难|高级)$/g

function splitTags(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/\s*[,，、;；]\s*|\s+(?:和|及|与)\s+/)
        .map((tag) => tag.trim())
        .filter(Boolean),
    ),
  ]
}

const validCount = (value: number) => Number.isInteger(value) && value >= 1 && value <= 10

export function trainingTurn(
  message: string,
  current?: ActiveDraft,
  awaitingTopic?: TrainingSetConditions,
): TrainingTurn {
  const input = message.trim()
  const explicitTitle =
    /(?:叫作?|命名为|训练集名称[为：:]?|(?:把(?:训练集)?名字)?改(?:名|标题)(?:成|为)?)[\s:：]*[「『“"《]([^」』”"》]+)[」』”"》]/.exec(
      input,
    )?.[1] ??
    /(?:改名(?:为|成)?|标题[：:]|名称[：:]|叫作?)\s*([^，。；！?？]+)[，。；！?？]?/.exec(
      input,
    )?.[1]
  const position = positionPattern.exec(input)
  const isReplacement = /(?:换|替换)\s*第|第.{0,8}题.{0,5}(?:换|替换)|^换题/.test(input)
  const countMatch = position ? null : countPattern.exec(input)
  const difficulty = difficultyOf(input)
  const explicitTags =
    /(?:知识点|标签)\s*[：:]\s*([^。；\n]+?)(?=[，,]\s*(?:难度|数量|名称|题量|\d+\s*道)|$|[。；\n])/.exec(
      input,
    )?.[1]
  const namedTopic =
    /(?:创建|建立|建|生成|做|出|安排|来)\s*(?:(?:一|1)(?:套|个|组|份)\s*)?([^，。；！?？:“"「」《》\n]+?)\s*(?:的)?训练(?:集|题单)/.exec(
      input,
    )?.[1]
  const topic = namedTopic
    ?.trim()
    .replace(/^(?:一|1)(?:套|个|组|份)\s*/, '')
    .replace(difficultySuffix, '')
    .trim()
  const unrestrictedTopic = /不限(?:知识点|主题)|(?:知识点|主题)不限/.test(input)
  const shortTopic =
    awaitingTopic &&
    !explicitTitle &&
    !countMatch &&
    difficulty === undefined &&
    /^[\p{L}\p{N}][\p{L}\p{N} +#/_-]*(?:[、,，][\p{L}\p{N} +#/_-]+)*[。！!]?$/u.test(input)
      ? input.replace(/[。！!]$/, '')
      : undefined
  const chosenTopic = explicitTags ?? topic ?? shortTopic
  const tags = unrestrictedTopic ? [] : chosenTopic ? splitTags(chosenTopic) : undefined
  const title = explicitTitle?.trim() || (topic ? topic + '训练集' : undefined)
  if (title && title.length > 120) return { reply: '训练集名称请控制在 120 字以内。' }
  if (tags && (tags.length > 10 || tags.some((tag) => tag.length > 64))) {
    return { reply: '请指定最多 10 个知识点，每个名称不超过 64 字。' }
  }

  const prior = current?.options ?? awaitingTopic
  const previous: TrainingSetConditions = {
    title: prior?.title ?? '个人训练集',
    count: prior?.count ?? 5,
    tags: prior?.tags ?? [],
    difficulty: prior?.difficulty ?? null,
    cover_each_goal: prior?.cover_each_goal ?? false,
    ranking_profile: prior?.ranking_profile ?? null,
  }
  if (isReplacement && position) {
    const replacePosition = number(position[1]!)
    if (!current || !validCount(replacePosition)) {
      return { reply: '请先生成草案，再输入要换的题号，例如「换第 2 题」。' }
    }
    if (difficulty !== undefined && difficulty !== previous.difficulty) {
      return { reply: '换题会沿用这份题单的难度。要调整整套难度，可以先说「改成简单」。' }
    }
    return {
      draft: {
        ...previous,
        title: title || previous.title,
        revision: { run_id: current.runId, replace_position: replacePosition },
      },
    }
  }
  if (isReplacement && /题/.test(input)) return { reply: '你想换第几题？例如「换第 2 题」。' }

  if (current && !title && !tags && !countMatch && difficulty === undefined) {
    return { reply: '可以告诉我新的名称、知识点、题目数量、难度，或指定要换的题。' }
  }
  if (current && /(?:再加|增加|减少|多选|少选)\s*[0-9零一二两三四五六七八九十]/.test(input)) {
    return { reply: '请告诉我调整后的总题数，例如「改成 5 道题」。' }
  }
  const count = countMatch ? number(countMatch[1]!) : previous.count
  if (!validCount(count)) return { reply: '一套训练集可以选 1 到 10 道题。' }
  const conditions: TrainingSetConditions = {
    ...previous,
    title: title || previous.title,
    tags: tags ?? previous.tags,
    count,
    difficulty: difficulty === undefined ? previous.difficulty : difficulty,
    cover_each_goal: tags ? tags.length > 1 : previous.cover_each_goal,
  }
  if (!current && !tags?.length && !unrestrictedTopic) {
    return {
      reply: '这套题想练哪个知识点？可以直接回复「数组」，或说「不限知识点」。',
      awaitingTopic: conditions,
    }
  }
  if (current && title && !tags && !countMatch && difficulty === undefined) {
    return {
      draft: { ...previous, title, revision: { run_id: current.runId, replace_position: null } },
    }
  }
  return { draft: conditions }
}
