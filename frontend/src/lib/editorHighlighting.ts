import { createHighlighterCore } from 'shiki/core'
import { createOnigurumaEngine } from 'shiki/engine/oniguruma'

// Only ship the grammars and themes offered by the code editor.
export function createEditorHighlighter() {
  return createHighlighterCore({
    themes: [import('shiki/themes/github-light.mjs'), import('shiki/themes/github-dark.mjs')],
    langs: [
      import('shiki/langs/cpp.mjs'),
      import('shiki/langs/python.mjs'),
      import('shiki/langs/java.mjs'),
      import('shiki/langs/rust.mjs'),
    ],
    engine: createOnigurumaEngine(import('shiki/wasm')),
  })
}

export function editorTheme(theme: string) {
  return theme === 'vs-dark' ? 'github-dark' : 'github-light'
}

export function editorLanguage(language: string) {
  return language === 'c' ? 'cpp' : language
}
