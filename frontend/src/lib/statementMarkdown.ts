import MarkdownIt from 'markdown-it'
import { katex } from '@mdit/plugin-katex'

const markdown = new MarkdownIt({ html: false, linkify: true }).use(katex, {
  delimiters: 'all',
  mathFence: true,
  trust: false,
  throwOnError: false,
})

const renderImage = markdown.renderer.rules.image!
markdown.renderer.rules.image = (tokens, index, options, env, renderer) => {
  const token = tokens[index]!
  token.attrSet('loading', 'lazy')
  token.attrSet('decoding', 'async')
  token.attrSet('referrerpolicy', 'no-referrer')
  return renderImage(tokens, index, options, env, renderer)
}

const conversationMarkdown = new MarkdownIt({ html: false })
  .disable(['link', 'image', 'autolink'])
  .use(katex, { delimiters: 'all', mathFence: true, trust: false, throwOnError: false })

type Highlight = (code: string, language: string) => string

for (const parser of [markdown, conversationMarkdown]) {
  const fence = parser.renderer.rules.fence!
  parser.renderer.rules.fence = (tokens, index, options, env, renderer) => {
    const code = fence(tokens, index, options, env, renderer)
    if (!env?.codeToolbar || !code.startsWith('<pre')) return code
    const language = tokens[index]!.info.trim().split(/\s+/)[0] || '纯文本'
    const label = parser.utils.escapeHtml(language)
    return `<div class="markdown-code"><div class="markdown-code-toolbar"><span>${label}</span><button type="button" data-copy-code>复制</button></div>${code}</div>\n`
  }
}

function prepareTokens(
  parser: typeof markdown,
  tokens: ReturnType<typeof markdown.parse>,
  env: { codeToolbar: boolean },
) {
  return {
    hasCode: tokens.some((token) => token.type === 'fence' && token.info.trim() !== 'math'),
    render(highlight?: Highlight) {
      const options = highlight ? { ...parser.options, highlight } : parser.options
      return parser.renderer.render(tokens, options, env)
    },
  }
}

export type PreparedStatement = ReturnType<typeof prepareTokens>

export function prepareStatement(source: string, links = true, codeToolbar = false) {
  const parser = links ? markdown : conversationMarkdown
  const env = { codeToolbar }
  return prepareTokens(parser, parser.parse(source, env), env)
}

export function prepareNoteBlocks(source: string, offset = 0) {
  const env = { codeToolbar: true }
  const tokens = markdown.parse(source, env)
  const indexed = tokens.flatMap((token, index) =>
    token.level === 0 && token.map ? [{ index, lines: token.map }] : [],
  )
  const lineStarts = [
    0,
    ...Array.from(source.matchAll(/\r\n?|\n/g), (newline) => newline.index + newline[0].length),
  ]
  return indexed.map(({ index, lines }, i) => {
    const start = lineStarts[lines[0]!] ?? source.length
    const end = lineStarts[lines[1]!] ?? source.length
    return {
      start: offset + start,
      end: offset + end - (source.slice(start, end).match(/[\r\n]+$/)?.[0].length ?? 0),
      content: prepareTokens(markdown, tokens.slice(index, indexed[i + 1]?.index), env),
    }
  })
}

export function renderStatement(source: string, links = true): string {
  return prepareStatement(source, links).render()
}
