<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as monaco from 'monaco-editor/editor/editor.api'
import 'monaco-editor/editor/contrib/suggest/browser/suggestController'
import 'monaco-editor/features/bracketMatching/register'
import 'monaco-editor/features/clipboard/register'
import 'monaco-editor/features/comment/register'
import 'monaco-editor/features/contextmenu/register'
import 'monaco-editor/features/find/register'
import 'monaco-editor/features/linesOperations/register'
import 'monaco-editor/features/wordOperations/register'
import 'monaco-editor/languages/definitions/cpp/register'
import 'monaco-editor/languages/definitions/python/register'
import 'monaco-editor/languages/definitions/java/register'
import 'monaco-editor/languages/definitions/rust/register'
import { enableEditorCompletions } from '../lib/editorCompletions'
import { editorLanguage, type EditorTheme } from '../lib/editorHighlighting'
import { currentMonacoTheme, initializeEditorHighlighting } from '../lib/monacoHighlighting'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'
const props = defineProps<{
  modelValue: string
  language: string
  theme: EditorTheme
  fontSize: number
  readOnly?: boolean
  semanticVersionId?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const container = ref<HTMLDivElement>()
const highlightFailed = ref(false)
let alive = true
let editor: monaco.editor.IStandaloneCodeEditor | undefined
let model: monaco.editor.ITextModel | undefined
let subscription: monaco.IDisposable | undefined
;(
  globalThis as typeof globalThis & { MonacoEnvironment?: { getWorker: () => Worker } }
).MonacoEnvironment = { getWorker: () => new EditorWorker() }
onMounted(async () => {
  if (!container.value) return
  model = monaco.editor.createModel(props.modelValue, editorLanguage(props.language))
  editor = monaco.editor.create(container.value, {
    model,
    theme: currentMonacoTheme(props.theme),
    fontSize: props.fontSize,
    minimap: { enabled: false },
    automaticLayout: true,
    scrollBeyondLastLine: false,
    padding: { top: 18 },
    tabSize: 4,
    ariaLabel: props.readOnly ? '提交源码' : '代码编辑器',
    readOnly: props.readOnly,
    domReadOnly: props.readOnly,
    lineNumbersMinChars: 3,
    wordWrap: 'on',
    renderLineHighlight: 'line',
    contextmenu: true,
    acceptSuggestionOnEnter: 'off',
    acceptSuggestionOnCommitCharacter: false,
    tabCompletion: 'off',
    wordBasedSuggestions: 'off',
  })
  if (!props.readOnly) {
    enableEditorCompletions(
      model,
      () => props.language,
      () => props.semanticVersionId,
    )
    subscription = editor.onDidChangeModelContent(() => {
      emit('update:modelValue', editor!.getValue())
    })
  }
  try {
    await initializeEditorHighlighting()
    if (!alive) return
    monaco.editor.setTheme(currentMonacoTheme(props.theme))
  } catch {
    if (alive) highlightFailed.value = true
  }
})
watch(
  () => props.modelValue,
  (value) => {
    if (model && value !== model.getValue()) model.setValue(value)
  },
)
watch(
  () => props.language,
  (value) => {
    if (model) monaco.editor.setModelLanguage(model, editorLanguage(value))
  },
)
watch(
  () => props.theme,
  (value) => {
    if (editor) monaco.editor.setTheme(currentMonacoTheme(value))
  },
)
watch(
  () => props.fontSize,
  (value) => editor?.updateOptions({ fontSize: value }),
)
onBeforeUnmount(() => {
  alive = false
  subscription?.dispose()
  editor?.dispose()
  model?.dispose()
})
</script>
<template>
  <p v-if="highlightFailed" class="editor-error" role="status">正在使用编辑器内置高亮。</p>
  <div ref="container" class="monaco-container" />
</template>
<style scoped>
.monaco-container {
  width: 100%;
  height: 100%;
  min-height: 0;
}
.editor-error {
  margin: 8px 18px;
}
</style>
