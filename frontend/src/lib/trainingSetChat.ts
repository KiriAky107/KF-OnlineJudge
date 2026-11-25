import type { components } from '../api/generated/schema'
import { trainingFeedbackRatingLabels } from '../api/trainingRanking.ts'

export type TrainingSetDraft = components['schemas']['TrainingSetDraft']
export type TrainingSetConditions = Omit<
  TrainingSetDraft,
  'revision' | 'message' | 'previous_run_id'
>
type Skill = components['schemas']['SkillVersionView']

type DraftExercise = Pick<
  components['schemas']['ProposedExercise'],
  'position' | 'title' | 'difficulty' | 'tags' | 'objective' | 'feedback_evidence' | 'prerequisites'
>
type ActiveDraft = {
  runId: string
  options: TrainingSetConditions
  items?: readonly DraftExercise[]
}
type TrainingTurn =
  { draft: TrainingSetDraft } | { reply: string; awaitingTopic?: TrainingSetConditions }

export const trainingCandidateTool = (skill: Skill) =>
  skill.available_tools.find(({ name }) => name === 'training_candidates')

export function hasTrainingCandidateAccess(skill: Skill) {
  const candidate = trainingCandidateTool(skill)
  return Boolean(
    candidate &&
    skill.binding?.enabled &&
    skill.binding.authorized_tools.some(
      ({ name, version }) => name === candidate.name && version === candidate.version,
    ),
  )
}

export function trainingChatSkill(skills: Skill[], draftVersion?: string) {
  const compatible = skills.filter(
    (skill) => skill.purpose === 'focused_training' && trainingCandidateTool(skill),
  )
  if (draftVersion) {
    return compatible.find((skill) => skill.version_id === draftVersion && skill.binding)
  }
  return (
    compatible.find(hasTrainingCandidateAccess) ??
    compatible.find((skill) => skill.binding?.enabled) ??
    compatible.find((skill) => skill.origin === 'builtin' && skill.binding) ??
    compatible.find((skill) => skill.origin === 'builtin')
  )
}

const numbers = '零一二三四五六七八九十'
function number(text: string) {
  if (text === '两') return 2
  if (text.length === 1 && numbers.includes(text)) return numbers.indexOf(text)
  return Number(text)
}
const countPattern = /([0-9]+|[零一二两三四五六七八九十]+)\s*(?:道|题|个题目)/
export const trainingDifficultyLabels = { easy: '简单', medium: '中等', hard: '困难' } as const
const selectionQuestion =
  /(?:为什么|为何|为啥)\s*(?:会|要)?\s*(?:选择|选|推荐|安排)|(?:选题|推荐)(?:的)?(?:理由|依据)/
const positionPattern = /第\s*([0-9]+|[零一二两三四五六七八九十]+)\s*(?:道|题)/
const difficultyOf = (text: string): TrainingSetDraft['difficulty'] | undefined => {
  if (/不限难度|难度不限/.test(text)) return null
  if (/入门|简单|初级/i.test(text)) return 'easy'
  if (/中等|中级/i.test(text)) return 'medium'
  if (/困难|高级/i.test(text)) return 'hard'
  return undefined
}
const topicDifficulty =
  /^(?:入门|简单|初级|中等|中级|困难|高级)(?:难度)?\s*的?|(?:入门|简单|初级|中等|中级|困难|高级)$/g
const rankingPreferences = [
  [
    /优先(?:练习?|巩固|复习)?\s*(?:我(?:的)?)?(?:最近|近期)?\s*(?:还没通过|没通过|未通过|较薄弱|薄弱|待巩固)的?(?:知识点|考点)/,
    'unresolved_first',
  ],
  [
    /(?:按|优先参考|参考)(?:我(?:的)?)?(?:最近|之前)?的?(?:难度)?自评(?:排序|选题)?/,
    'feedback_first',
  ],
  [/(?:恢复|改回|改用|按)(?:默认|综合)排序/, null],
] as const

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

const searchVerb =
  /^(?:(?:再|帮我|给我|请|我想|能否|能不能)\s*)*(?:找(?:出|一下)?|搜(?:索)?|列出|推荐|挑选)/
const scoreWord = '(?:难度分(?:数)?|难度评分|评分)'
const scoreRange = new RegExp(
  `${scoreWord}\\s*[:：]?\\s*(?:在\\s*)?(\\d+)\\s*分?\\s*(?:[-－—~～]|到|至)\\s*(\\d+)(?![\\d.])`,
)
const minimumScore = new RegExp(`${scoreWord}\\s*[:：]?\\s*(?:至少|不低于|>=|≥)\\s*(\\d+)`)
const maximumScore = new RegExp(`${scoreWord}\\s*[:：]?\\s*(?:至多|不高于|<=|≤)\\s*(\\d+)`)
const scoreAbove = new RegExp(`${scoreWord}\\s*[:：]?\\s*(\\d+)\\s*分?\\s*以上`)
const scoreBelow = new RegExp(`${scoreWord}\\s*[:：]?\\s*(\\d+)\\s*分?\\s*以下`)
const exactScore = new RegExp(`${scoreWord}\\s*[:：]?\\s*(\\d+)(?![\\d.])(?:\\s*分)?`)
const clearScore = /不限(?:难度分|评分)|(?:难度分|评分)不限/
const clearTopic = /不限(?:知识点|主题)|(?:知识点|主题)不限/
const unsolvedOnly = /只(?:要|找|看).{0,10}(?:没通过|未通过|没做过|未解答)|(?:尚未|还没|未|没)通过/
const includeSolved = /(?:包括|也要|不限).{0,6}(?:已通过|通过状态)|通过状态不限|已通过的也要/

export function isProblemSearchQuestion(message: string, followingSearch: boolean) {
  const text = message.trim()
  if (/(?:训练集|训练题单)/.test(text)) return false
  if (searchVerb.test(text) && /题/.test(text)) return true
  return (
    followingSearch &&
    /^(?:改成|调整|只要|只找|只看|包括|难度分|评分|通过状态|不限|知识点[:：])/.test(text) &&
    /题|难度分|评分|通过|知识点/.test(text)
  )
}

export function trainingSearchTurn(message: string, previous?: TrainingSetDraft): TrainingTurn {
  const text = message.trim()
  const filter = previous?.search
  const range = scoreRange.exec(text)
  const minimum = minimumScore.exec(text) ?? scoreAbove.exec(text)
  const maximum = maximumScore.exec(text) ?? scoreBelow.exec(text)
  const exact = exactScore.exec(text)
  const hasNumericFilter = range || minimum || maximum || exact
  let min = filter?.minimum_score ?? null
  let max = filter?.maximum_score ?? null
  if (clearScore.test(text)) min = max = null
  else if (range) {
    min = Number(range[1])
    max = Number(range[2])
  } else if (minimum || maximum) {
    min = minimum ? Number(minimum[1]) : null
    max = maximum ? Number(maximum[1]) : null
  } else if (exact) min = max = Number(exact[1])
  if (
    [min, max].some((n) => n !== null && (!Number.isInteger(n) || n < 1 || n > 10000)) ||
    (min !== null && max !== null && min > max)
  )
    return { reply: '请给出 1 到 10000 之间的有效难度分范围，例如「难度分 800 到 1200」。' }
  if (!hasNumericFilter && !clearScore.test(text) && /难度分|评分/.test(text))
    return { reply: '你想找哪个难度分范围？例如「难度分 800 到 1200」。' }

  const countMatch = countPattern.exec(text)
  const count = countMatch ? number(countMatch[1]!) : (previous?.count ?? 5)
  if (!validCount(count)) return { reply: '一次可以找 1 到 10 道题。' }
  const explicit =
    /(?:知识点|标签)\s*[：:]\s*(.+?)(?=[，,]\s*(?:难度|评分|只|不限|包括)|[。；;\n]|$)/.exec(
      text,
    )?.[1]
  const topic = text
    .split(/[,，。；;\n]/, 1)[0]!
    .replace(searchVerb, '')
    .replace(countPattern, '')
    .trim()
    .replace(/^(?:几|一些|若干)\s*道?/, '')
    .replace(/^(?:我(?:还)?没通过的|尚未通过的|未通过的)/, '')
    .replace(/(?:相关的?|的)?(?:题目|题)\s*$/, '')
    .replace(topicDifficulty, '')
    .trim()
  const tags = clearTopic.test(text)
    ? []
    : explicit ||
        (searchVerb.test(text) && topic && !/^(?:我|难度|简单|中等|困难|已通过|没通过)/.test(topic))
      ? splitTags(explicit || topic)
      : (previous?.tags ?? [])
  if (tags.length > 10 || tags.some((tag) => tag.length > 64))
    return { reply: '请指定最多 10 个知识点，每个名称不超过 64 字。' }
  const onlyUnsolved = includeSolved.test(text)
    ? false
    : unsolvedOnly.test(text) || (filter?.only_unsolved ?? false)
  const tier = difficultyOf(text)
  return {
    draft: {
      title: '站内找题',
      count,
      tags,
      cover_each_goal: false,
      difficulty: tier === undefined ? (previous?.difficulty ?? null) : tier,
      search: { minimum_score: min, maximum_score: max, only_unsolved: onlyUnsolved },
    },
  }
}

export function isTrainingMessage(
  message: string,
  hasDraft: boolean,
  awaitingTopic: boolean,
  followingSearch = false,
) {
  const text = message.trim()
  if (/^(?:怎么|如何|什么是|能否解释|请解释)/.test(text)) return false
  if (isProblemSearchQuestion(text, followingSearch)) return true
  if (/(?:创建|建立|建|组|生成|安排|做|出|来).{0,60}(?:训练集|训练题单)/.test(text)) return true
  if (/(?:给我|帮我).{0,20}(?:找|选|组)\s*/.test(text) && countPattern.test(text)) return true
  if (!hasDraft && !awaitingTopic) return false
  if (selectionQuestion.test(text) && (positionPattern.test(text) || /选题/.test(text))) return true
  if (/(?:换|替换)\s*第|第.{0,8}题.{0,5}(?:换|替换)|^换题/.test(text)) return true
  if (/(?:改成|调整为|改为).{0,25}(?:道|题|难度|知识点|简单|中等|困难|入门|排序)/.test(text))
    return true
  if (/(?:改名|改标题|不限知识点|知识点不限|知识点[：:]|标签[：:])/.test(text)) return true
  if (rankingPreferences.some(([pattern]) => pattern.test(text))) return true
  return awaitingTopic && /^[\p{L}\p{N} +#/_、，,.-]{1,64}[。！!]?$/u.test(text)
}

export function trainingTurn(
  message: string,
  current?: ActiveDraft,
  awaitingTopic?: TrainingSetConditions,
): TrainingTurn {
  const input = message.trim()
  const position = positionPattern.exec(input)
  if (selectionQuestion.test(input)) {
    if (!current?.items?.length) return { reply: '先生成一份草案，再问我某道题的选题依据。' }
    if (!position) return { reply: '你想了解第几题？可以问「为什么选第 2 题？」' }
    const exercise = current.items.find((item) => item.position === number(position[1]!))
    if (!exercise) return { reply: '当前草案没有这个题号。请按草案里的顺序指定一道题。' }
    const facts = [`第 ${exercise.position} 题「${exercise.title}」`]
    if (exercise.difficulty) facts.push(trainingDifficultyLabels[exercise.difficulty])
    if (exercise.tags.length) facts.push(`知识点为${exercise.tags.join('、')}`)
    const matching = current.options.tags.filter((tag) => exercise.tags.includes(tag))
    const reasons = [`${facts.join('，')}。`]
    if (matching.length) reasons.push(`对应你指定的${matching.join('、')}。`)
    const feedback = exercise.feedback_evidence
    if (feedback) {
      reasons.push(
        `参考到你的难度自评「${trainingFeedbackRatingLabels[feedback.rating]}」，建议优先练习${trainingDifficultyLabels[feedback.preferred_difficulty]}题。`,
      )
    }
    const prerequisites = exercise.prerequisites?.map(({ tag }) => tag) ?? []
    if (prerequisites.length) reasons.push(`${prerequisites.join('、')}的先修要求已有通过记录。`)
    reasons.push(`在这份草案中的目标是「${exercise.objective}」。`)
    return { reply: reasons.join('') }
  }
  const explicitTitle =
    /(?:叫作?|命名为|训练集名称[为：:]?|(?:把(?:训练集)?名字)?改(?:名|标题)(?:成|为)?)[\s:：]*[「『“"《]([^」』”"》]+)[」』”"》]/.exec(
      input,
    )?.[1] ??
    /(?:改名(?:为|成)?|标题[：:]|名称[：:]|叫作?)\s*([^，。；！?？]+)[，。；！?？]?/.exec(
      input,
    )?.[1]
  const isReplacement = /(?:换|替换)\s*第|第.{0,8}题.{0,5}(?:换|替换)|^换题/.test(input)
  const countMatch = position ? null : countPattern.exec(input)
  const difficulty = difficultyOf(input)
  const preferences = rankingPreferences.flatMap(([pattern, profile]) => {
    const phrase = pattern.exec(input)
    if (!phrase) return []
    const negated = /(?:不想|不要|不用|不必|别)\s*$/.test(input.slice(0, phrase.index))
    return [{ phrase: phrase[0], profile, negated }]
  })
  if (preferences.length > 1) return { reply: '你希望按哪一种方式选题？请指定一个选题侧重。' }
  const preference = preferences[0]
  if (preference?.negated) return { reply: '可以说「恢复默认排序」，或告诉我新的选题侧重。' }
  const rankingProfile = preference?.profile
  const topicInput = preference
    ? input.replace(preference.phrase, '').replace(/^[，,。；;\s]+|[，,。；;\s]+$/g, '')
    : input
  const explicitTags =
    /(?:知识点|标签)\s*[：:]\s*([^。；\n]+?)(?=[，,]\s*(?:难度|数量|名称|题量|\d+\s*道)|$|[。；\n])/.exec(
      topicInput,
    )?.[1]
  const namedTopic =
    /(?:创建|建立|建|组|生成|做|出|安排|来)\s*(?:(?:一|1)(?:套|个|组|份)\s*)?([^，。；！?？:“"「」《》\n]+?)\s*(?:的)?训练(?:集|题单)/.exec(
      topicInput,
    )?.[1]
  const topicText = namedTopic
    ?.trim()
    .replace(/^(?:一|1)(?:套|个|组|份)\s*/, '')
    .replace(countPattern, '')
    .trim()
    .replace(/(?:题目|题)$/, '')
    .trim()
  const topic = topicText === '的' ? '' : topicText?.replace(topicDifficulty, '').trim()
  const difficultyOnly = !explicitTags && difficulty != null && !!topicText && topic === ''
  const unrestrictedTopic = difficultyOnly || /不限(?:知识点|主题)|(?:知识点|主题)不限/.test(input)
  const shortTopic =
    awaitingTopic &&
    !explicitTitle &&
    !countMatch &&
    difficulty === undefined &&
    /^[\p{L}\p{N}][\p{L}\p{N} +#/_-]*(?:[、,，][\p{L}\p{N} +#/_-]+)*[。！!]?$/u.test(topicInput)
      ? topicInput.replace(/[。！!]$/, '')
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
    if (rankingProfile !== undefined && rankingProfile !== previous.ranking_profile) {
      return { reply: '换题会沿用原排序。要调整整套选题，可以先说「优先巩固薄弱知识点」。' }
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

  const reselection = Boolean(tags || countMatch || difficulty !== undefined || preference)
  if (current && !title && !reselection) {
    return { reply: '可以调整名称、知识点、题数、难度和选题侧重，也可以指定要换的题。' }
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
    ranking_profile: rankingProfile === undefined ? previous.ranking_profile : rankingProfile,
  }
  if (!current && !tags?.length && !unrestrictedTopic && !preference) {
    return {
      reply: '这套题想练哪个知识点？可以直接回复「数组」，或说「不限知识点」。',
      awaitingTopic: conditions,
    }
  }
  if (current && title && !reselection) {
    return {
      draft: { ...previous, title, revision: { run_id: current.runId, replace_position: null } },
    }
  }
  return { draft: conditions }
}
