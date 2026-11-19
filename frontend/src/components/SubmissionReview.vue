<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import { errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import type { components } from '../api/generated/schema'
import { defaultReviewCandidate, reviewCandidates, reviewNotice } from '../lib/submissionReview'
import { displayTime } from '../lib/presentation'
const CodeDiff = defineAsyncComponent(() => import('./SubmissionCodeDiff.vue'))
const props = defineProps<{ submission: components['schemas']['SubmissionDetail'] }>()
const session = useSession()
const opened = ref(false)
const candidates = ref<components['schemas']['SubmissionSummary'][]>([])
const otherId = ref('')
const result = ref<components['schemas']['SubmissionComparison'] | null>(null)
const busy = ref(false)
const error = ref('')
let request = new AbortController()
let generation = 0
const notice = computed(() =>
  result.value
    ? reviewNotice(
        result.value.same_version,
        result.value.left.language === result.value.right.language,
      )
    : '',
)
function reset() {
  generation++
  request.abort()
  request = new AbortController()
  opened.value = false
  candidates.value = []
  otherId.value = ''
  result.value = null
  busy.value = false
  error.value = ''
}
async function open() {
  opened.value = true
  busy.value = true
  const current = generation
  try {
    const page = unwrap(
      await api.GET('/api/v1/submissions', {
        params: { query: { problem_version_id: props.submission.problem_version_id, limit: 20 } },
        signal: request.signal,
      }),
    )
    if (current !== generation) return
    candidates.value = reviewCandidates(page.items, props.submission)
    otherId.value = defaultReviewCandidate(candidates.value, props.submission)
  } catch (cause) {
    if (current === generation) error.value = errorText(cause)
  } finally {
    if (current === generation) busy.value = false
  }
}
async function compare() {
  if (busy.value || !otherId.value.trim()) return
  const current = generation
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const value = unwrap(
      await api.GET('/api/v1/submissions/{submission_id}/compare/{other_id}', {
        params: {
          path: { submission_id: props.submission.submission_id, other_id: otherId.value.trim() },
        },
        signal: request.signal,
      }),
    )
    if (current === generation) result.value = value
  } catch (cause) {
    if (current === generation) error.value = errorText(cause)
  } finally {
    if (current === generation) busy.value = false
  }
}
watch([() => props.submission.submission_id, () => session.user.value?.id], reset, {
  flush: 'sync',
})
watch(otherId, () => {
  result.value = null
  error.value = ''
})
onBeforeUnmount(() => {
  generation++
  request.abort()
})
</script>
<template>
  <section class="review-section">
    <h2>同题提交复盘</h2>
    <el-button v-if="!opened" @click="open">选择我的另一条提交</el-button>
    <template v-else>
      <p>
        选择最近 20 条同版本记录中的另一条，或输入本人同题的历史提交
        ID。左侧是对比记录，右侧是当前记录。
      </p>
      <div class="filters">
        <el-select
          v-model="otherId"
          :disabled="busy"
          placeholder="选择近期提交"
          aria-label="选择近期提交"
        >
          <el-option
            v-for="item in candidates"
            :key="item.submission_id"
            :value="item.submission_id"
            :label="`${displayTime(item.created_at)} · ${item.verdict ?? item.state} · ${item.language}`"
          />
        </el-select>
        <el-input
          v-model="otherId"
          :disabled="busy"
          placeholder="或输入历史提交 ID"
          aria-label="历史提交 ID"
        />
        <el-button :loading="busy" :disabled="!otherId.trim()" @click="compare">比较</el-button>
      </div>
      <p v-if="!busy && !candidates.length">
        近期同版本没有其他提交，可输入本人同题的其他历史提交 ID。
      </p>
      <p v-if="error" role="alert">{{ error }}</p>
      <template v-if="result">
        <p role="note">{{ notice }}</p>
        <p v-if="!result.same_version">
          已识别为同一道题的不同版本；不按测试点位置拼接结果，无法据此逐点比较。
        </p>
        <div class="review-summary">
          <div v-for="(item, index) in [result.right, result.left]" :key="index">
            <h3>{{ index === 0 ? '对比提交（左）' : '当前提交（右）' }}</h3>
            <RouterLink :to="'/submissions/' + item.submission_id">{{
              item.submission_id
            }}</RouterLink>
            <p>
              {{ displayTime(item.created_at) }} · {{ item.language }} ·
              {{ item.verdict ?? item.state }}
            </p>
            <p>题目版本：{{ item.problem_version_id }}</p>
            <p v-if="item.failure_code">系统原因：{{ item.failure_code }}（不作为解题错误）</p>
            <pre v-if="item.result?.diagnostics" class="sample-block">{{
              item.result.diagnostics
            }}</pre>
          </div>
        </div>
        <p v-if="result.left_source === result.right_source">两次源码完全一致。</p>
        <CodeDiff
          :key="result.left.submission_id + result.right.submission_id"
          :before="result.right_source"
          :after="result.left_source"
        />
      </template>
    </template>
  </section>
</template>
<style scoped>
.review-section {
  margin-top: 2rem;
}
.review-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
  gap: 1rem;
}
.review-summary > div {
  min-width: 0;
  overflow-wrap: anywhere;
}
.filters .el-input,
.filters .el-select {
  max-width: 28rem;
}
</style>
