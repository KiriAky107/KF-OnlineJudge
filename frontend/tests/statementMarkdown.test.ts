import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderStatement } from '../src/lib/statementMarkdown.ts'

test('problem statements combine Markdown, both LaTeX delimiter styles and raster images', () => {
  const result = renderStatement(
    [
      '## 输入格式',
      '',
      '1. 读入 $a_i$ 和 \\(b_i\\)。',
      '',
      '\\[',
      '\\sum_{i=1}^n a_i',
      '\\]',
      '',
      '![流程图](https://example.com/diagram.png)',
      '',
      '![内嵌图](data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7)',
      '',
      '| n | 数量 |',
      '| - | - |',
      '| 1 | 2 |',
    ].join('\n'),
  )
  assert.match(result, /<h2>输入格式<\/h2>/)
  assert.match(result, /<ol>/)
  assert.equal((result.match(/class="katex"/g) ?? []).length, 3)
  assert.match(result, /class="katex-display"/)
  assert.match(
    result,
    /<img src="https:\/\/example\.com\/diagram\.png" alt="流程图" loading="lazy" decoding="async" referrerpolicy="no-referrer"/,
  )
  assert.match(result, /<img src="data:image\/gif;base64,/)
  assert.match(result, /<table>/)
})

test('raw Markdown HTML stays text and the math renderer never loads external graphics', () => {
  const result = renderStatement(
    '<mark>文字</mark>\n\n$\\includegraphics{https://example.com/diagram.png}$',
  )
  assert.match(result, /&lt;mark&gt;文字&lt;\/mark&gt;/)
  assert.doesNotMatch(result, /<img\b/)
})
