<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import DisclosureHeading from '../components/DisclosureHeading.vue'
import {
  boundaryDrafts,
  boundaryRange,
  checkBoundarySample,
  renderBoundaryInput,
  type BoundaryDraft,
  type BoundaryFields,
} from '../lib/problemBoundaryDrafts'
import type { AuthoringSample } from '../lib/problemAuthoringReview'

const props = defineProps<{ samples: readonly AuthoringSample[] }>()
const fields = reactive<BoundaryFields>({
  minimumCount: '',
  maximumCount: '',
  minimumValue: '',
  maximumValue: '',
})
const range = ref<ReturnType<typeof boundaryRange>>()
const error = ref('')
const drafts = computed(() => (range.value ? boundaryDrafts(range.value) : []))
const sampleChecks = computed(() => {
  const confirmed = range.value
  return confirmed ? props.samples.map(({ input }) => checkBoundarySample(input, confirmed)) : []
})
const previewCount = 12

watch(fields, () => {
  range.value = undefined
  error.value = ''
})

function generate() {
  range.value = undefined
  error.value = ''
  try {
    range.value = boundaryRange(fields)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '请核对输入范围。'
  }
}

function download(draft: BoundaryDraft) {
  const data = new Blob([renderBoundaryInput(draft)], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(data)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = draft.file
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <details class="boundary-drafts">
    <DisclosureHeading icon="document">生成边界输入草案</DisclosureHeading>
    <div class="boundary-body">
      <p>适用于首行声明个数、第二行列出相应整数的题目。请按题面确认范围。</p>
      <div class="boundary-fields" @keydown.enter.prevent="generate">
        <label>最少元素数<input v-model="fields.minimumCount" inputmode="numeric" /></label>
        <label>最多元素数<input v-model="fields.maximumCount" inputmode="numeric" /></label>
        <label>每项下界<input v-model="fields.minimumValue" inputmode="text" /></label>
        <label>每项上界<input v-model="fields.maximumValue" inputmode="text" /></label>
      </div>
      <button type="button" @click="generate">核对范围并生成</button>
      <p v-if="error" role="alert">{{ error }}</p>
      <template v-if="drafts.length">
        <div v-if="sampleChecks.length" aria-live="polite">
          <h3>公开输入与范围</h3>
          <ol>
            <li v-for="(status, index) in sampleChecks" :key="index">{{ status }}</li>
          </ol>
        </div>
        <p>草案只包含输入。独立核对预期输出后，可下载需要的文件用于录题。</p>
        <ul class="case-list">
          <li v-for="draft in drafts" :key="draft.file">
            <strong>{{ draft.label }}</strong>
            <pre>{{ renderBoundaryInput(draft, previewCount) }}</pre>
            <p v-if="draft.count > previewCount" class="admin-muted">
              预览显示前 {{ previewCount }} 项，文件含完整的 {{ draft.count }} 项。
            </p>
            <button type="button" @click="download(draft)">下载 {{ draft.file }}</button>
          </li>
        </ul>
      </template>
    </div>
  </details>
</template>

<style scoped>
.boundary-drafts {
  width: 100%;
  align-self: stretch;
  border-top: 1px solid var(--line);
  padding-top: 0.75rem;
}
.boundary-body {
  display: grid;
  gap: 0.7rem;
  justify-items: start;
  margin-block-start: 0.7rem;
}
.boundary-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: 0.65rem;
  width: 100%;
}
.boundary-fields input {
  width: 100%;
  box-sizing: border-box;
}
.case-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 15rem), 1fr));
  gap: 0.75rem;
  width: 100%;
  list-style: none;
  padding: 0;
}
.case-list li {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 0.6rem;
  padding: 0.65rem;
}
.case-list pre {
  overflow: auto;
  margin-block: 0.4rem;
}
</style>
