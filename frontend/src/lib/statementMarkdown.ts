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

const fence = conversationMarkdown.renderer.rules.fence!
conversationMarkdown.renderer.rules.fence = (tokens, index, options, env, renderer) => {
  const code = fence(tokens, index, options, env, renderer)
  if (!env?.codeToolbar || !code.startsWith('<pre')) return code
  const language = tokens[index]!.info.trim().split(/\s+/)[0] || '纯文本'
  const label = conversationMarkdown.utils.escapeHtml(language)
  return `<div class="markdown-code"><div class="markdown-code-toolbar"><span>${label}</span><button type="button" data-copy-code>复制</button></div>${code}</div>\n`
}

export function prepareStatement(source: string, links = true, codeToolbar = false) {
  const parser = links ? markdown : conversationMarkdown
  const env = { codeToolbar }
  const tokens = parser.parse(source, env)
  return {
    hasCode: tokens.some((token) => token.type === 'fence' && token.info.trim() !== 'math'),
    render(highlight?: Highlight) {
      const options = highlight ? { ...parser.options, highlight } : parser.options
      return parser.renderer.render(tokens, options, env)
    },
  }
}

export function renderStatement(source: string, links = true): string {
  return prepareStatement(source, links).render()
}
