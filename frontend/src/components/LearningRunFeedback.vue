<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { displayTime } from '../lib/presentation'

type Run = components['schemas']['AgentRunView']
type Input = components['schemas']['UpdateRunFeedback']
const props = defineProps<{
  runId: string
  purpose: NonNullable<Run['result']>['purpose']
  saved: Run['feedback']
}>()
const emit = defineEmits<{ saved: [run: Run] }>()

const ratingLabels: Record<NonNullable<Input['rating']>, string> = {
  helpful: '有帮助',
  partly_helpful: '部分有帮助',
  not_helpful: '没有帮助',
}
const recommendation = computed(
  () => props.purpose === 'focused_training' || props.purpose === 'periodic_review',
)
const reasonLabels = computed<Record<NonNullable<Input['reason']>, string>>(() => ({
  unclear_next_step: '下一步不明确',
  unrelated_hint: recommendation.value ? '推荐与训练目标不符' : '建议与问题不符',
  insufficient_evidence: '依据不足',
}))
const rating = ref<Input['rating']>(null)
const reason = ref<NonNullable<Input['reason']> | ''>('')
const saving = ref(false)
const error = ref('')
const notice = ref('')
const controller = new AbortController()

watch(
  () => props.saved?.rated_at,
  () => {
    rating.value = props.saved?.rating ?? null
    reason.value = props.saved?.reason ?? ''
  },
  { immediate: true },
)

async function save(remove = false) {
  if (saving.value || (!remove && !rating.value)) return
  saving.value = true
  error.value = ''
  notice.value = ''
  const { signal } = controller
  try {
    const run = unwrap(
      await api.PUT('/api/v1/learning/agent/runs/{run_id}/feedback', {
        params: { path: { run_id: props.runId } },
        body: {
          rating: remove ? null : rating.value,
          reason: remove ? null : reason.value || null,
        },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (signal.aborted) return
    emit('saved', run)
    notice.value = remove ? '已撤回反馈。' : '已保存反馈。'
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = false
  }
}
onBeforeUnmount(() => controller.abort())
</script>

<template>
  <section class="stack hint-feedback native-form" aria-label="本人反馈">
    <h3>{{ recommendation ? '这次推荐有帮助吗？' : '这次建议有帮助吗？' }}</h3>
    <form class="stack" @submit.prevent="save()">
      <label>
        我的反馈
        <select v-model="rating" :disabled="saving">
          <option :value="null" disabled>选择反馈</option>
          <option v-for="(label, value) in ratingLabels" :key="value" :value="value">
            {{ label }}
          </option>
        </select>
      </label>
      <label>
        原因（可选）
        <select v-model="reason" :disabled="saving || !rating">
          <option value="">不填写</option>
          <option v-for="(label, value) in reasonLabels" :key="value" :value="value">
            {{ label }}
          </option>
        </select>
      </label>
      <div class="feedback-actions">
        <el-button
          native-type="submit"
          :disabled="
            saving ||
            !rating ||
            (rating === saved?.rating && (reason || null) === (saved?.reason ?? null))
          "
          >保存反馈</el-button
        >
        <el-button v-if="saved" :disabled="saving" @click="save(true)">撤回反馈</el-button>
        <span v-if="saved" class="small muted">{{ displayTime(saved.rated_at) }} 已保存</span>
      </div>
    </form>
    <p class="small muted">仅本人查看此反馈。</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.hint-feedback {
  border-top: 1px solid var(--line);
  padding-top: 0.75rem;
}
.hint-feedback label,
.feedback-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  column-gap: 0.6rem;
  row-gap: 0.45rem;
}
.feedback-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}
</style>
