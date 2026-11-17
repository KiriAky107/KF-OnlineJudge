<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as monaco from 'monaco-editor/editor/editor.api'
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
import { editorLanguage, editorTheme } from '../lib/editorHighlighting'
import { initializeEditorHighlighting } from '../lib/monacoHighlighting'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'
const props = defineProps<{
  modelValue: string
  language: string
  theme: string
  fontSize: number
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const container = ref<HTMLDivElement>()
const failed = ref(false)
let alive = true
let editor: monaco.editor.IStandaloneCodeEditor | undefined
let model: monaco.editor.ITextModel | undefined
let subscription: monaco.IDisposable | undefined
;(
  globalThis as typeof globalThis & { MonacoEnvironment?: { getWorker: () => Worker } }
).MonacoEnvironment = { getWorker: () => new EditorWorker() }
onMounted(async () => {
  try {
    await initializeEditorHighlighting()
  } catch {
    if (alive) failed.value = true
    return
  }
  if (!alive || !container.value) return
  model = monaco.editor.createModel(props.modelValue, editorLanguage(props.language))
  editor = monaco.editor.create(container.value, {
    model,
    theme: editorTheme(props.theme),
    fontSize: props.fontSize,
    minimap: { enabled: false },
    automaticLayout: true,
    scrollBeyondLastLine: false,
    padding: { top: 18 },
    tabSize: 4,
    ariaLabel: '代码编辑器',
    lineNumbersMinChars: 3,
    wordWrap: 'on',
    renderLineHighlight: 'line',
    contextmenu: true,
  })
  subscription = editor.onDidChangeModelContent(() => {
    emit('update:modelValue', editor!.getValue())
  })
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
    if (editor) monaco.editor.setTheme(editorTheme(value))
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
  <div v-if="failed" class="editor-fallback">
    <p role="status">代码高亮加载失败，仍可使用纯文本编辑。</p>
    <textarea
      :value="modelValue"
      aria-label="源码"
      spellcheck="false"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    />
  </div>
  <div v-else ref="container" class="monaco-container" />
</template>
<style scoped>
.monaco-container {
  width: 100%;
  height: 100%;
  min-height: 320px;
}
.editor-fallback {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.editor-fallback p {
  margin: 8px 18px;
}
.editor-fallback textarea {
  flex: 1;
  min-height: 280px;
  padding: 18px;
  font-family: monospace;
  resize: none;
}
</style>
