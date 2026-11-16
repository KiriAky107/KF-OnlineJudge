import * as monaco from 'monaco-editor/editor/editor.api'
import { shikiToMonaco } from '@shikijs/monaco'
import { createEditorHighlighter } from './editorHighlighting'

// The adapter registers tokenizers and wraps Monaco theme handling globally.
// Keep it alive across editor mounts, including the plain-text toggle.
let ready: Promise<void> | undefined
export function initializeEditorHighlighting() {
  return (ready ??= createEditorHighlighter().then((highlighter) => {
    shikiToMonaco(highlighter, monaco)
  }))
}
