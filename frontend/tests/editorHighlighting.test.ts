import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  createEditorHighlighter,
  editorLanguage,
  editorTheme,
} from '../src/lib/editorHighlighting.ts'

test('editor maps existing settings without changing source language capabilities', () => {
  assert.equal(editorLanguage('c'), 'cpp')
  for (const lang of ['cpp', 'python', 'java', 'rust']) assert.equal(editorLanguage(lang), lang)
  assert.equal(editorTheme('vs'), 'github-light')
  assert.equal(editorTheme('vs-dark'), 'github-dark')
})

test('the editor highlighter tokenizes its languages in both themes', async () => {
  const highlighter = await createEditorHighlighter()
  try {
    assert.deepEqual(highlighter.getLoadedThemes().sort(), ['github-dark', 'github-light'])
    const examples = {
      cpp: 'int main() { return 42; } // comment',
      python: 'def answer(): return "hello" # comment',
      java: 'class Main { String name = "hello"; } // comment',
      rust: 'fn main() { println!("hello"); } // comment',
    }
    for (const [lang, code] of Object.entries(examples)) {
      for (const theme of highlighter.getLoadedThemes()) {
        const result = highlighter.codeToTokens(code, { lang, theme })
        assert.equal(result.tokens[0]!.map((token) => token.content).join(''), code)
        assert.ok(new Set(result.tokens[0]!.map((token) => token.color)).size > 1)
      }
    }
  } finally {
    highlighter.dispose()
  }
})
