import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  createEditorHighlighter,
  editorLanguage,
  editorThemePresets,
} from '../src/lib/editorHighlighting.ts'

test('the editor uses the same grammar for C and C++', () => {
  assert.equal(editorLanguage('c'), 'cpp')
  for (const lang of ['cpp', 'python', 'java', 'rust']) assert.equal(editorLanguage(lang), lang)
})

test('the editor highlighter tokenizes its languages in each light and dark preset', async () => {
  const highlighter = await createEditorHighlighter()
  try {
    const selected = Object.values(editorThemePresets).flatMap(({ light, dark }) => [light, dark])
    assert.deepEqual(highlighter.getLoadedThemes().sort(), selected.sort())
    const examples = {
      cpp: 'int main() { return 42; } // comment',
      python: 'def answer(): return "hello" # comment',
      java: 'class Main { String name = "hello"; } // comment',
      rust: 'fn main() { println!("hello"); } // comment',
    }
    for (const [lang, code] of Object.entries(examples)) {
      for (const theme of selected) {
        const result = highlighter.codeToTokens(code, { lang, theme })
        assert.equal(result.tokens[0]!.map((token) => token.content).join(''), code)
        assert.ok(new Set(result.tokens[0]!.map((token) => token.color)).size > 1)
      }
    }
  } finally {
    highlighter.dispose()
  }
})
