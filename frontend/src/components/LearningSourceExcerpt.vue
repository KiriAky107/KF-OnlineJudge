<script setup lang="ts">
import DisclosureHeading from './DisclosureHeading.vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { ApiError, errorText, unwrap } from '../api/transport'

const props = defineProps<{ submissionId: string; disabled: boolean; chat?: boolean }>()
const selection = defineModel<components['schemas']['SourceLineRange'] | null>({ required: true })
const first = ref<number | ''>(1)
const last = ref<number | ''>(1)
const excerpt = ref<components['schemas']['SubmissionSourceExcerpt']>()
const loading = ref(false)
const error = ref('')
const lines = computed(() => excerpt.value?.text.split(/\r\n|[\r\n]/) ?? [])
let request: AbortController | undefined

function clear() {
  request?.abort()
  excerpt.value = undefined
  error.value = ''
  loading.value = false
  selection.value = null
}

watch([first, last], clear, { flush: 'sync' })
onBeforeUnmount(clear)

function approve(event: Event) {
  const current = excerpt.value
  const checkbox = event.target as HTMLInputElement
  selection.value =
    current && checkbox.checked
      ? { start_line: current.start_line, end_line: current.end_line }
      : null
}

async function preview() {
  const start = first.value
  const end = last.value
  if (typeof start !== 'number' || typeof end !== 'number' || loading.value) return
  clear()
  const { signal } = (request = new AbortController())
  loading.value = true
  try {
    const result = unwrap(
      await api.GET('/api/v1/submissions/{submission_id}/source-excerpt', {
        params: {
          path: { submission_id: props.submissionId },
          query: { start_line: start, end_line: end },
        },
        signal,
      }),
    )
    if (!signal.aborted) excerpt.value = result
  } catch (cause) {
    if (signal.aborted) return
    error.value =
      cause instanceof ApiError && cause.status === 422
        ? '请选择源码中的有效行号。一次最多预览 24 行，且不超过 2 KiB。'
        : errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
</script>

<template>
  <details class="source-excerpt">
    <DisclosureHeading icon="document" tone="muted">预览本人源码片段</DisclosureHeading>
    <form class="stack" @submit.prevent="preview">
      <div class="excerpt-range">
        <label class="field">
          起始行
          <input v-model.number="first" type="number" min="1" required :disabled="disabled" />
        </label>
        <label class="field">
          结束行
          <input v-model.number="last" type="number" min="1" required :disabled="disabled" />
        </label>
      </div>
      <div>
        <el-button native-type="submit" :loading="loading" :disabled="disabled">
          读取片段
        </el-button>
      </div>
      <p v-if="error" role="alert" class="small">{{ error }}</p>
      <div v-if="excerpt" class="stack">
        <p class="small muted" aria-live="polite">
          {{ excerpt.source.language }} · 第 {{ excerpt.start_line }}–{{ excerpt.end_line }} 行
        </p>
        <pre class="sample-block"><code><span
          v-for="(line, index) in lines"
          :key="index"
          class="source-line"
        ><span class="muted line-number" aria-hidden="true">{{ excerpt.start_line + index }}</span><span>{{ line }}</span></span></code></pre>
        <label v-if="excerpt.text.trim()" class="excerpt-approval">
          <input
            type="checkbox"
            :checked="selection !== null"
            :disabled="disabled"
            @change="approve"
          />
          本次由本机模型分析以上片段
        </label>
        <small v-if="selection" class="muted">
          {{ chat ? '以上片段仅供本轮本机模型分析。' : '模型只会收到以上源码、语言和行号。' }}
        </small>
      </div>
    </form>
  </details>
</template>

<style scoped>
.source-excerpt > summary {
  cursor: pointer;
}
.source-excerpt > form {
  margin-block-start: 12px;
}
.excerpt-range {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.field {
  display: grid;
  min-inline-size: 0;
  gap: 0.35rem;
}
.field input {
  inline-size: 100%;
}
.source-line {
  display: grid;
  grid-template-columns: 4ch minmax(0, 1fr);
  min-block-size: 1lh;
  gap: 12px;
  overflow-wrap: anywhere;
}
.line-number {
  text-align: end;
  user-select: none;
}
.excerpt-approval {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}
</style>
