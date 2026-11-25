<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'
import { editorLanguages } from '../composables/judgingLanguage'
import { languageLabels } from '../composables/useCodeDraft'
import { displayTime } from '../lib/presentation'

const props = withDefaults(defineProps<{ problemVersion?: string; disabled: boolean }>(), {
  problemVersion: undefined,
})
const target = defineModel<string>({ required: true })
const items = ref<components['schemas']['SubmissionSummary'][]>([])
const error = ref('')
const loading = ref(false)
const fromPrevious = computed(
  () => target.value && !items.value.some(({ submission_id }) => submission_id === target.value),
)
const verdicts: Partial<Record<NonNullable<(typeof items.value)[number]['verdict']>, string>> = {
  accepted: 'Accepted',
  wrong_answer: 'Wrong Answer',
  compilation_error: 'Compile Error',
  runtime_error: 'Runtime Error',
  time_limit_exceeded: 'Time Limit Exceeded',
  memory_limit_exceeded: 'Memory Limit Exceeded',
  output_limit_exceeded: 'Output Limit Exceeded',
}

watch(
  () => props.problemVersion,
  async (problemVersion, _previous, onCleanup) => {
    const controller = new AbortController()
    const { signal } = controller
    onCleanup(() => controller.abort())
    items.value = []
    loading.value = true
    error.value = ''
    try {
      const page = unwrap(
        await api.GET('/api/v1/submissions', {
          params: { query: { problem_version_id: problemVersion, limit: 20 } },
          signal,
        }),
      )
      if (!signal.aborted) items.value = page.items.filter((item) => item.state === 'finished')
    } catch (cause) {
      if (!signal.aborted) error.value = errorText(cause)
    } finally {
      if (!signal.aborted) loading.value = false
    }
  },
  { immediate: true },
)
</script>

<template>
  <div class="review-target">
    <label class="field">
      选择本人提交
      <select v-model="target" :disabled="disabled || loading">
        <option value="">{{ loading ? '正在读取最近评测' : '选择一条已完成评测' }}</option>
        <option v-if="fromPrevious" :value="target">当前选定的提交</option>
        <option v-for="item in items" :key="item.submission_id" :value="item.submission_id">
          {{ displayTime(item.created_at) }} ·
          {{ languageLabels[editorLanguages[item.language]] }} ·
          {{ item.verdict ? (verdicts[item.verdict] ?? item.verdict) : '已完成' }}
        </option>
      </select>
    </label>
    <p v-if="error" class="small muted" role="status">{{ error }}</p>
    <p v-else-if="!loading && !items.length && !target" class="small muted">
      最近的提交里没有已完成评测。也可以从个人提交详情打开复盘。
    </p>
  </div>
</template>

<style scoped>
.review-target {
  display: grid;
  min-inline-size: 0;
}
.review-target select {
  inline-size: 100%;
}
</style>
