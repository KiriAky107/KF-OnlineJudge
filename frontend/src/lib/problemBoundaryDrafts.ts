const maxDraftElements = 1_000_000n
const minimumInt64 = -(2n ** 63n)
const maximumInt64 = 2n ** 63n - 1n

export type BoundaryFields = {
  minimumCount: string
  maximumCount: string
  minimumValue: string
  maximumValue: string
}

type BoundaryRange = {
  minimumCount: number
  maximumCount: number
  minimumValue: bigint
  maximumValue: bigint
}

export type BoundaryDraft = {
  file: string
  label: string
  count: number
  pattern: readonly string[]
}

function integer(value: string | undefined) {
  return value !== undefined && /^[+-]?\d{1,20}$/.test(value) ? BigInt(value) : undefined
}

export function boundaryRange(fields: BoundaryFields): BoundaryRange {
  const read = (value: string) => integer(value.normalize('NFKC').trim().replace(/−/g, '-'))
  const minimumCount = read(fields.minimumCount)
  const maximumCount = read(fields.maximumCount)
  if (
    minimumCount === undefined ||
    maximumCount === undefined ||
    minimumCount < 0n ||
    maximumCount > maxDraftElements ||
    minimumCount > maximumCount
  ) {
    throw new Error('元素个数须在 0 到 1,000,000 之间，最少不得超过最多。')
  }
  const minimumValue = read(fields.minimumValue)
  const maximumValue = read(fields.maximumValue)
  if (
    minimumValue === undefined ||
    maximumValue === undefined ||
    minimumValue < minimumInt64 ||
    maximumValue > maximumInt64 ||
    minimumValue > maximumValue
  ) {
    throw new Error('值须在有符号 64 位整数范围内，下界不得超过上界。')
  }
  return {
    minimumCount: Number(minimumCount),
    maximumCount: Number(maximumCount),
    minimumValue,
    maximumValue,
  }
}

export function boundaryDrafts(range: BoundaryRange): BoundaryDraft[] {
  const { minimumCount, maximumCount, minimumValue, maximumValue } = range
  const low = String(minimumValue)
  const high = String(maximumValue)
  const candidates = [
    {
      file: 'minimum-size.in',
      label: minimumCount ? '最少元素，全部取下界' : '空序列',
      count: minimumCount,
      pattern: [low],
    },
    {
      file: 'maximum-size.in',
      label: '最多元素，全部取上界',
      count: maximumCount,
      pattern: [high],
    },
    {
      file: 'alternating-values.in',
      label: '最多元素，上下界交替',
      count: maximumCount,
      pattern: [low, high],
    },
  ]
  if (minimumValue <= 0n && maximumValue >= 0n && maximumCount > 0) {
    candidates.push({
      file: 'zero-values.in',
      label: '最短非空序列，全部取零',
      count: Math.max(minimumCount, 1),
      pattern: ['0'],
    })
  }
  const unique = new Map<string, BoundaryDraft>()
  for (const draft of candidates) {
    const values = [...new Set(draft.pattern.slice(0, draft.count))]
    const key = JSON.stringify([draft.count, values])
    if (!unique.has(key)) unique.set(key, { ...draft, pattern: values })
  }
  return [...unique.values()]
}

export function renderBoundaryInput(draft: BoundaryDraft, limit = draft.count) {
  const size = Math.min(draft.count, limit)
  if (!size) return draft.count + '\n'
  const repetitions = Math.floor(size / draft.pattern.length)
  const leftover = draft.pattern.slice(0, size % draft.pattern.length)
  const repeated = (draft.pattern.join(' ') + ' ').repeat(repetitions)
  return draft.count + '\n' + (repeated + leftover.join(' ')).trimEnd() + '\n'
}

export function checkBoundarySample(input: string | null, range: BoundaryRange) {
  if (input === null) return '二进制输入，需人工核对'
  const tokens = input.matchAll(/[^ \t\f\v\r\n]+/g)
  const count = integer(tokens.next().value?.[0])
  if (
    count === undefined ||
    count < BigInt(range.minimumCount) ||
    count > BigInt(range.maximumCount)
  ) {
    return '首个整数不在确认的元素个数范围内'
  }
  let elements = 0
  let outside = false
  for (const [text] of tokens) {
    elements++
    const item = integer(text)
    if (item === undefined || item < range.minimumValue || item > range.maximumValue) outside = true
  }
  if (elements !== Number(count)) return '实际元素数与声明个数不同'
  if (outside) return '有元素不在确认的值范围内'
  return '满足确认的输入结构和范围'
}
