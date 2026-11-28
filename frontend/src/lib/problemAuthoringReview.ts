import MarkdownIt from 'markdown-it'

const reader = new MarkdownIt('commonmark')
const sectionLabels = {
  input: /^(?:输入(?:格式|说明|描述)?|input(?: format| specification| description)?)$/,
  output: /^(?:输出(?:格式|说明|描述)?|output(?: format| specification| description)?)$/,
  constraints: /^(?:数据范围(?:与约定)?|约束(?:条件)?|限制(?:条件)?|constraints?|limits?)$/,
} as const
type Section = keyof typeof sectionLabels
type Side = 'input' | 'output'
type SampleState = 'matched' | 'different' | 'missing' | 'ambiguous' | 'binary'

export type AuthoringSample = Record<Side, string | null>

function textValue(value: string) {
  return value.normalize('NFKC').trim().replace(/\s+/g, ' ').replace(/:$/, '').trim().toLowerCase()
}

function sideOf(word: string): Side {
  return word === '输入' || word === 'input' ? 'input' : 'output'
}

function sampleLabel(value: string, insideSample: boolean) {
  const directionThenNumber =
    /^(?:样例|示例|sample|example)\s*(输入|输出|input|output)(?:\s*#?([1-9]\d?))?$/.exec(value)
  if (directionThenNumber) {
    return {
      side: sideOf(directionThenNumber[1]),
      ordinal: Number(directionThenNumber[2]) || undefined,
    }
  }
  const numberThenDirection =
    /^(?:样例|示例|sample|example)\s*#?([1-9]\d?)\s*(输入|输出|input|output)$/.exec(value)
  if (numberThenDirection) {
    return {
      side: sideOf(numberThenDirection[2]),
      ordinal: Number(numberThenDirection[1]) || undefined,
    }
  }
  if (insideSample && /^(?:输入|输出|input|output)$/.test(value)) {
    return { side: sideOf(value), ordinal: undefined }
  }
}

function sampleText(value: string) {
  return value.replace(/\r\n?/g, '\n').replace(/\n$/, '')
}

function compare(blocks: string[], actual: string | null): SampleState {
  if (actual === null) return 'binary'
  if (blocks.length > 1) return 'ambiguous'
  if (!blocks.length) return 'missing'
  return sampleText(blocks[0]) === sampleText(actual) ? 'matched' : 'different'
}

export function decodeAuthoringSample(value: string): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(
      Uint8Array.from(atob(value), (character) => character.charCodeAt(0)),
    )
  } catch {
    return null
  }
}

export function reviewProblemDraft(statement: string, samples: readonly AuthoringSample[]) {
  const tokens = reader.parse(statement.slice(0, 262144), {})
  const sections: Record<Section, boolean> = { input: false, output: false, constraints: false }
  const constraints: string[] = []
  const blocks = new Map<number, Record<Side, string[]>>()
  const next = { input: 1, output: 1 }
  let group: { heading: number; ordinal?: number } | undefined
  let activeConstraints: number | undefined
  let capture: string[] | undefined
  for (const [index, token] of tokens.entries()) {
    const previous = tokens[index - 1]
    if (
      token.type === 'inline' &&
      previous?.level === 0 &&
      (previous.type === 'heading_open' || previous.type === 'paragraph_open')
    ) {
      const name = textValue(
        token.children?.map((child) => child.content).join('') ?? token.content,
      )
      if (previous.type === 'heading_open') {
        const heading = Number(previous.tag.slice(1))
        if (group && heading <= group.heading) group = undefined
        if (activeConstraints !== undefined && heading <= activeConstraints)
          activeConstraints = undefined
        capture = undefined
        const sampleGroup = /^(?:样例|示例|sample|example)(?:\s*#?([1-9]\d?))?$/.exec(name)
        if (sampleGroup) group = { heading, ordinal: Number(sampleGroup[1]) || undefined }
        for (const section of Object.keys(sectionLabels) as Section[]) {
          if (!sectionLabels[section].test(name)) continue
          sections[section] = true
          if (section === 'constraints') activeConstraints = heading
        }
      }
      const sample = sampleLabel(name, group !== undefined)
      if (sample) {
        const ordinal = sample.ordinal ?? group?.ordinal ?? next[sample.side]
        next[sample.side] = Math.max(next[sample.side], ordinal + 1)
        const pair = blocks.get(ordinal) ?? { input: [], output: [] }
        capture = pair[sample.side]
        blocks.set(ordinal, pair)
        continue
      }
      if (previous.type === 'heading_open') continue
    }
    if (capture && (token.type === 'fence' || token.type === 'code_block') && token.level === 0) {
      capture.push(token.content)
    } else if (
      activeConstraints !== undefined &&
      token.type === 'inline' &&
      constraints.length < 4
    ) {
      const lines = token.content
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
      constraints.push(...lines.slice(0, 4 - constraints.length).map((line) => line.slice(0, 150)))
    }
  }
  return {
    sections,
    constraints,
    samples: samples.map((actual, index) => {
      const pair = blocks.get(index + 1)
      return {
        input: compare(pair?.input ?? [], actual.input),
        output: compare(pair?.output ?? [], actual.output),
      }
    }),
    extraSampleLabels: [...blocks.keys()]
      .filter((key) => key > samples.length)
      .sort((a, b) => a - b),
  }
}
