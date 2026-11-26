import { createHighlighterCore } from 'shiki/core'
import { createOnigurumaEngine } from 'shiki/engine/oniguruma'

export const editorThemePresets = {
  'github-classic': { label: 'GitHub 经典', light: 'github-light', dark: 'github-dark' },
  vscode: { label: 'VS Code', light: 'light-plus', dark: 'dark-plus' },
  github: { label: 'GitHub Default', light: 'github-light-default', dark: 'github-dark-default' },
  'atom-one': { label: 'Atom One', light: 'one-light', dark: 'one-dark-pro' },
} as const
export type EditorThemePreset = keyof typeof editorThemePresets
export type EditorAppearance = 'light' | 'dark'
export type EditorAppearancePreference = EditorAppearance | 'site'
export type EditorTheme = (typeof editorThemePresets)[EditorThemePreset][EditorAppearance]

export const editorFontSize = { min: 12, max: 22, default: 14 } as const
const fontSizeKey = 'kf-editor-font-size'

function validFontSize(value: number) {
  return Number.isInteger(value) && value >= editorFontSize.min && value <= editorFontSize.max
}

export function savedEditorFontSize() {
  try {
    const value = Number(localStorage.getItem(fontSizeKey))
    if (validFontSize(value)) return value
  } catch {
    // Storage can be unavailable on this origin.
  }
  return editorFontSize.default
}

export function saveEditorFontSize(value: number) {
  if (!validFontSize(value)) return
  try {
    localStorage.setItem(fontSizeKey, String(value))
  } catch {
    // The editor can keep using the selected size until it closes.
  }
}

export function savedEditorTheme(): {
  preset: EditorThemePreset
  appearance: EditorAppearancePreference
} {
  try {
    const stored = localStorage.getItem('kf-editor-theme')?.split(':')
    if (stored?.[0] && Object.hasOwn(editorThemePresets, stored[0])) {
      return {
        preset: stored[0] as EditorThemePreset,
        appearance: stored[1] === 'dark' || stored[1] === 'light' ? stored[1] : 'site',
      }
    }
  } catch {
    // Storage can be unavailable on this origin.
  }
  return { preset: 'github-classic', appearance: 'site' }
}

// Only ship the grammars and themes offered by the code editor.
export function createEditorHighlighter() {
  return createHighlighterCore({
    themes: [
      import('shiki/themes/github-light.mjs'),
      import('shiki/themes/github-dark.mjs'),
      import('shiki/themes/light-plus.mjs'),
      import('shiki/themes/dark-plus.mjs'),
      import('shiki/themes/github-light-default.mjs'),
      import('shiki/themes/github-dark-default.mjs'),
      import('shiki/themes/one-light.mjs'),
      import('shiki/themes/one-dark-pro.mjs'),
    ],
    langs: [
      import('shiki/langs/cpp.mjs'),
      import('shiki/langs/python.mjs'),
      import('shiki/langs/java.mjs'),
      import('shiki/langs/rust.mjs'),
    ],
    engine: createOnigurumaEngine(import('shiki/wasm')),
  })
}

export function editorLanguage(language: string) {
  return language === 'c' ? 'cpp' : language
}
