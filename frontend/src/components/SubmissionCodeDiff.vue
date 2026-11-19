<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import * as monaco from 'monaco-editor/editor/editor.api'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'
const props = defineProps<{ before: string; after: string }>()
const host = ref<HTMLDivElement>()
let editor: monaco.editor.IStandaloneDiffEditor | undefined
let original: monaco.editor.ITextModel | undefined
let modified: monaco.editor.ITextModel | undefined
;(
  globalThis as typeof globalThis & { MonacoEnvironment?: { getWorker: () => Worker } }
).MonacoEnvironment = { getWorker: () => new EditorWorker() }
onMounted(() => {
  if (!host.value) return
  original = monaco.editor.createModel(props.before, 'plaintext')
  modified = monaco.editor.createModel(props.after, 'plaintext')
  editor = monaco.editor.createDiffEditor(host.value, {
    readOnly: true,
    originalEditable: false,
    automaticLayout: true,
    renderSideBySide: true,
    minimap: { enabled: false },
    ignoreTrimWhitespace: false,
    ariaLabel: '左侧为对比提交，右侧为当前提交',
  })
  editor.setModel({ original, modified })
})
onBeforeUnmount(() => {
  editor?.dispose()
  original?.dispose()
  modified?.dispose()
})
</script>
<template><div ref="host" class="review-diff" /></template>
<style scoped>
.review-diff {
  height: 28rem;
  border: 1px solid var(--el-border-color);
}
</style>
