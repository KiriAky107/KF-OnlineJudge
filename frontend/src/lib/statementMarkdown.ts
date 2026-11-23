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

export function renderStatement(source: string): string {
  return markdown.render(source)
}
