<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { prepareStatement } from '../lib/statementMarkdown'
import type { markdownHighlighter } from '../lib/markdownHighlighting'
import 'katex/dist/katex.min.css'

const props = withDefaults(
  defineProps<{ source: string; links?: boolean; highlightCode?: boolean }>(),
  { links: true },
)
const content = computed(() => prepareStatement(props.source, props.links, props.highlightCode))
const highlighter = shallowRef<Awaited<ReturnType<typeof markdownHighlighter>>>()
const rendered = computed(() => content.value.render(highlighter.value))
watch(
  () => props.highlightCode && content.value.hasCode,
  async (needed, _previous, onCleanup) => {
    if (!needed || highlighter.value) return
    let cancelled = false
    onCleanup(() => (cancelled = true))
    try {
      const { markdownHighlighter } = await import('../lib/markdownHighlighting')
      const loaded = await markdownHighlighter()
      if (!cancelled) highlighter.value = loaded
    } catch {
      // The Markdown parser already renders an escaped plain-code block.
    }
  },
  { immediate: true },
)

async function copyCode(event: MouseEvent) {
  if (!(event.target instanceof HTMLElement)) return
  const button = event.target.closest<HTMLButtonElement>('[data-copy-code]')
  const code = button?.closest('.markdown-code')?.querySelector('pre code')
  if (!button || !code) return
  try {
    await navigator.clipboard.writeText(code.textContent ?? '')
    button.textContent = '已复制'
  } catch {
    const selection = window.getSelection()
    if (!selection) return
    const range = document.createRange()
    range.selectNodeContents(code)
    selection.removeAllRanges()
    selection.addRange(range)
    button.textContent = '已选中'
  }
}
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- Only parser-generated HTML; source HTML and trusted TeX commands are disabled. -->
  <div class="statement-markdown" @click="copyCode" v-html="rendered" />
</template>

<style scoped>
.statement-markdown {
  line-height: 1.8;
  min-width: 0;
  overflow-wrap: anywhere;
}
.statement-markdown :deep(p) {
  margin: 0.7em 0;
}
.statement-markdown :deep(h1),
.statement-markdown :deep(h2),
.statement-markdown :deep(h3),
.statement-markdown :deep(h4) {
  margin: 1.15em 0 0.45em;
  color: var(--el-text-color-primary);
  font-weight: 650;
  line-height: 1.4;
}
.statement-markdown :deep(h1) {
  font-size: 1.35em;
}
.statement-markdown :deep(h2) {
  font-size: 1.2em;
}
.statement-markdown :deep(h3),
.statement-markdown :deep(h4) {
  font-size: 1.05em;
}
.statement-markdown :deep(a) {
  color: var(--el-color-primary);
  text-decoration: underline;
}
.statement-markdown :deep(ul),
.statement-markdown :deep(ol) {
  padding-inline-start: 1.65em;
  margin-block: 0.65em;
}
.statement-markdown :deep(blockquote) {
  margin-inline: 0;
  padding-inline-start: 1em;
  border-inline-start: 3px solid var(--el-color-primary-light-5);
  color: var(--el-text-color-secondary);
}
.statement-markdown :deep(code) {
  background: var(--el-fill-color-light);
  border-radius: var(--el-border-radius-base);
  padding: 0.12em 0.3em;
}
.statement-markdown :deep(pre) {
  overflow-x: auto;
  padding: 0.75em 1em;
  background: var(--el-fill-color-light);
  border-radius: var(--el-border-radius-base);
}
.statement-markdown :deep(pre code) {
  background: transparent;
  padding: 0;
}
.statement-markdown :deep(.markdown-code) {
  margin-block: 0.85em;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--el-border-radius-base);
}
.statement-markdown :deep(.markdown-code-toolbar) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6rem;
  padding: 0.25rem 0.75rem;
  background: var(--el-fill-color-light);
  color: var(--secondary-ink);
  font-size: 0.8em;
}
.statement-markdown :deep(.markdown-code-toolbar button) {
  border: 0;
  border-radius: 4px;
  padding: 0.2rem 0.45rem;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
}
.statement-markdown :deep(.markdown-code-toolbar button:hover) {
  color: var(--strong-ink);
  background: var(--el-fill-color);
}
.statement-markdown :deep(.markdown-code pre) {
  margin: 0;
  border-radius: 0;
  font-size: 0.85em;
  line-height: 1.55;
}
:global(.dark .statement-markdown .shiki) {
  background-color: var(--surface-soft) !important;
}
:global(.dark .statement-markdown .shiki),
:global(.dark .statement-markdown .shiki span) {
  color: var(--shiki-dark) !important;
}
.statement-markdown :deep(table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  margin-block: 0.75em;
}
.statement-markdown :deep(th),
.statement-markdown :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 0.28em 0.65em;
}
.statement-markdown :deep(th) {
  background: var(--el-fill-color-lighter);
}
.statement-markdown :deep(img) {
  max-width: 100%;
  height: auto;
  vertical-align: middle;
}
.statement-markdown :deep(.katex-display) {
  max-width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  padding-block: 0.15em;
}
</style>
