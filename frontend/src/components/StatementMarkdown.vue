<script setup lang="ts">
import { computed } from 'vue'
import { renderStatement } from '../lib/statementMarkdown'
import 'katex/dist/katex.min.css'

const props = defineProps<{ source: string }>()
const rendered = computed(() => renderStatement(props.source))
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- Only parser-generated HTML; source HTML and trusted TeX commands are disabled. -->
  <div class="statement-markdown" v-html="rendered" />
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
