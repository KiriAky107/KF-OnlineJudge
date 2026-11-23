import * as monaco from 'monaco-editor/editor/editor.api'
import { shikiToMonaco } from '@shikijs/monaco'
import { createEditorHighlighter, editorThemePresets, type EditorTheme } from './editorHighlighting'

// The adapter changes Monaco theme handling for every editor on the page.
let ready: Promise<void> | undefined
let attached = false

export function currentMonacoTheme(theme: EditorTheme) {
  if (attached) return theme
  return Object.values(editorThemePresets).some((preset) => preset.dark === theme)
    ? 'vs-dark'
    : 'vs'
}

export function initializeEditorHighlighting() {
  return (ready ??= createEditorHighlighter().then((highlighter) => {
    shikiToMonaco(highlighter, monaco)
    attached = true
  }))
}
