<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { boundedPoll } from '../api/polling'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import { diagnosticCategories } from '../lib/diagnostic'
import { newRequestId } from '../lib/requestCrypto'
import DisclosureHeading from './DisclosureHeading.vue'
import JudgementStatus from './JudgementStatus.vue'
import LearningMistakeProposal from './LearningMistakeProposal.vue'

type Diagnostic = components['schemas']['PublicDiagnosticView']
type Verdict = components['schemas']['DiagnosticObservation']['o2']
const props = defineProps<{
  accountId: string
  submissionId: string
  disabled: boolean
  review?: components['schemas']['SubmissionReviewResult']
  reviewRunId?: string
  withdrawnMistakeId?: string
}>()
const emit = defineEmits<{ mistakeSaved: []; openMistakes: [] }>()
const route = useRoute()
const available = ref(false)
const ubsanAvailable = ref(false)
const expanded = ref(false)
const loading = ref(false)
const sending = ref(false)
const monitoring = ref(false)
const confirmed = ref(false)
const version = ref('')
const sampleCount = ref(0)
const ordinal = ref(0)
const mode = ref<Diagnostic['mode']>('optimization')
const requestId = ref('')
const receiptReview = ref<string>()
const diagnostic = ref<Diagnostic>()
const message = ref('')
const lifetime = new AbortController()
const receiptKey = `kf-oj:public-diagnostic:${props.accountId}:${props.submissionId}`
const terminal = (item: Diagnostic) => !['queued', 'running', 'cancel_pending'].includes(item.state)
const active = computed(() => diagnostic.value && !terminal(diagnostic.value))
const states: Record<Diagnostic['state'], string> = {
  queued: '等待诊断',
  running: '正在诊断',
  cancel_pending: '正在取消，等待清理',
  cancelled: '已取消',
  expired: '诊断已超时',
  unavailable: '诊断不可用',
  observed: '诊断完成',
  failed: '诊断运行失败',
}
const verdicts: Record<Verdict, string> = {
  accepted: '通过',
  wrong_answer: '答案错误',
  compilation_error: '编译错误',
  runtime_error: '运行错误',
  time_limit_exceeded: '运行超时',
  memory_limit_exceeded: '内存超限',
  output_limit_exceeded: '输出超限',
  cancelled: '已取消',
  system_error: '系统错误',
}

const reviewedEvaluation = computed(() =>
  props.review?.anchor_submission_id === props.submissionId
    ? props.review.records.find(
        (record) =>
          record.submission_id === props.submissionId &&
          record.problem_version_id === version.value,
      )
    : undefined,
)
const matchedReview = computed(() =>
  props.review?.anchor_submission_id === props.submissionId ? props.reviewRunId : undefined,
)
const sampleHandoff = computed(() => {
  const index = diagnostic.value?.sample_ordinal
  if (!version.value || index == null || index < 0 || index >= sampleCount.value) return undefined
  const sameWorkspace = route.name === 'problem-workspace' && route.params.id === version.value
  return {
    name: 'problem-workspace',
    params: { id: version.value },
    query: {
      ...(sameWorkspace ? route.query : {}),
      selfTestSample: String(index + 1),
      selfTestDiagnostic:
        diagnostic.value?.state === 'observed' ? diagnostic.value.request_id : undefined,
    },
  }
})

function remember(id: string, reviewId = receiptReview.value) {
  requestId.value = id
  receiptReview.value = id ? reviewId : undefined
  if (!id && !ubsanAvailable.value) mode.value = 'optimization'
  try {
    if (id)
      sessionStorage.setItem(
        receiptKey,
        JSON.stringify({
          id,
          ordinal: ordinal.value,
          mode: mode.value,
          reviewId: receiptReview.value,
        }),
      )
    else sessionStorage.removeItem(receiptKey)
  } catch {
    // The current view can still display and refresh this request.
  }
}

function setStatus(item: Diagnostic) {
  diagnostic.value = item
  ordinal.value = item.sample_ordinal
  mode.value = item.mode
  if (requestId.value) remember(requestId.value)
  if (terminal(item)) monitoring.value = false
}

const poll = boundedPoll({
  load: async (signal) =>
    unwrap(
      await api.GET('/api/v1/learning/agent/diagnostics/{request_id}', {
        params: { path: { request_id: requestId.value } },
        signal,
      }),
    ),
  terminal,
  result: setStatus,
  error: (cause) => {
    monitoring.value = false
    // Drop displayed observations after a failed authorization check.
    if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
      diagnostic.value = undefined
      if (cause.status === 404) remember('')
    }
    message.value = errorText(cause)
  },
  exhausted: () => {
    monitoring.value = false
    message.value = '等待时间已到，可以手动刷新状态。'
  },
  interval: 2000,
  maxTime: 200_000,
})

function refresh() {
  message.value = ''
  monitoring.value = true
  poll.start()
}

async function open(event: Event) {
  if (!(event.target as HTMLDetailsElement).open || version.value || loading.value) return
  loading.value = true
  message.value = ''
  const { signal } = lifetime
  try {
    const submission = unwrap(
      await api.GET('/api/v1/submissions/{submission_id}', {
        params: { path: { submission_id: props.submissionId } },
        signal,
      }),
    )
    if (!['c17', 'cpp20'].includes(submission.language)) {
      message.value = '公开样例诊断支持 C17 和 C++20 提交。'
      return
    }
    const problem = unwrap(
      await api.GET('/api/v1/problems/versions/{version_id}', {
        params: { path: { version_id: submission.problem_version_id } },
        signal,
      }),
    )
    if (signal.aborted) return
    version.value = submission.problem_version_id
    sampleCount.value = problem.samples.length
  } catch (cause) {
    if (!signal.aborted) message.value = errorText(cause)
  } finally {
    loading.value = false
  }
}

async function start() {
  if (!confirmed.value || !version.value || !sampleCount.value || active.value || sending.value)
    return
  const { signal } = lifetime
  sending.value = true
  confirmed.value = false
  message.value = ''
  try {
    const id = requestId.value || newRequestId()
    if (!requestId.value) receiptReview.value = matchedReview.value
    remember(id)
    const item = unwrap(
      await api.PUT('/api/v1/learning/agent/diagnostics/{request_id}', {
        params: { path: { request_id: id } },
        headers: csrfHeaders(),
        body: {
          submission_id: props.submissionId,
          problem_version_id: version.value,
          sample_ordinal: ordinal.value,
          mode: mode.value,
          source_execution_confirmed: true,
          review_run_id: receiptReview.value,
        },
        signal,
      }),
    )
    if (signal.aborted) return
    setStatus(item)
    if (!terminal(item)) refresh()
  } catch (cause) {
    if (signal.aborted) return
    if (cause instanceof ApiError && cause.status !== 409 && cause.status < 500) remember('')
    message.value = errorText(cause)
  } finally {
    sending.value = false
  }
}

async function cancel() {
  if (!requestId.value || sending.value) return
  sending.value = true
  try {
    const { response } = await api.POST('/api/v1/learning/agent/diagnostics/{request_id}/cancel', {
      params: { path: { request_id: requestId.value } },
      headers: csrfHeaders(),
      signal: lifetime.signal,
    })
    if (!response.ok) throw new ApiError(response.status)
    if (!lifetime.signal.aborted) refresh()
  } catch (cause) {
    if (!lifetime.signal.aborted) message.value = errorText(cause)
  } finally {
    sending.value = false
  }
}

function reset() {
  poll.pause()
  remember('')
  diagnostic.value = undefined
  message.value = ''
}

watch([ordinal, mode], () => (confirmed.value = false))
const syncVisibility = () => poll.visible(!document.hidden)
onMounted(async () => {
  document.addEventListener('visibilitychange', syncVisibility)
  syncVisibility()
  try {
    const result = unwrap(
      await api.GET('/api/v1/learning/agent/diagnostics/availability', { signal: lifetime.signal }),
    )
    if (!result.available || lifetime.signal.aborted) return
    available.value = true
    ubsanAvailable.value = result.ubsan === true
    try {
      const stored = JSON.parse(sessionStorage.getItem(receiptKey) ?? 'null')
      if (
        typeof stored?.id === 'string' &&
        /^[0-9a-f-]{36}$/.test(stored.id) &&
        Number.isInteger(stored.ordinal) &&
        stored.ordinal >= 0
      ) {
        ordinal.value = stored.ordinal
        mode.value = stored.mode === 'ubsan' ? 'ubsan' : 'optimization'
        requestId.value = stored.id
        receiptReview.value = typeof stored.reviewId === 'string' ? stored.reviewId : undefined
        expanded.value = true
        refresh()
      }
    } catch {
      // A blocked storage API does not block this view's current request.
    }
  } catch {
    // This optional action is only shown after the server advertises availability.
  }
})
onBeforeUnmount(() => {
  lifetime.abort()
  poll.dispose()
  document.removeEventListener('visibilitychange', syncVisibility)
})
</script>

<template>
  <details v-if="available" class="public-diagnostic" :open="expanded || undefined" @toggle="open">
    <DisclosureHeading icon="monitor" tone="accent">公开样例诊断</DisclosureHeading>
    <div class="diagnostic-body">
      <p v-if="loading" class="small muted" role="status">正在读取公开样例…</p>
      <p v-else-if="version && !sampleCount" class="small muted">此版本没有公开样例。</p>
      <form
        v-if="sampleCount && !diagnostic && !loading && !monitoring"
        class="diagnostic-form"
        @submit.prevent="start"
      >
        <label class="field">
          公开样例
          <select v-model.number="ordinal" :disabled="disabled || sending || Boolean(requestId)">
            <option v-for="number in sampleCount" :key="number" :value="number - 1">
              样例 {{ number }}
            </option>
          </select>
        </label>
        <label v-if="ubsanAvailable" class="field">
          诊断方式
          <select v-model="mode" :disabled="disabled || sending || Boolean(requestId)">
            <option value="optimization">比较编译优化</option>
            <option value="ubsan">检查未定义行为（UBSan）</option>
          </select>
        </label>
        <label class="diagnostic-consent">
          <input v-model="confirmed" type="checkbox" :disabled="disabled || sending" />
          同意在沙箱中用所选公开样例运行这份提交的源码
        </label>
        <div class="diagnostic-actions">
          <el-button native-type="submit" :loading="sending" :disabled="disabled || !confirmed">
            {{ requestId ? '重试请求' : '运行诊断' }}
          </el-button>
          <el-button v-if="requestId && !monitoring" text :disabled="sending" @click="refresh">
            刷新状态
          </el-button>
        </div>
      </form>
      <p v-if="diagnostic" class="small" role="status">{{ states[diagnostic.state] }}</p>
      <section v-if="diagnostic?.state === 'observed'" class="diagnostic-card" aria-label="诊断卡">
        <h3>诊断卡 · 公开样例 {{ diagnostic.sample_ordinal + 1 }}</h3>
        <div>
          <h4>已观察</h4>
          <p v-if="reviewedEvaluation" class="small">
            原提交评测
            <JudgementStatus
              :state="reviewedEvaluation.state"
              :verdict="reviewedEvaluation.verdict"
            />
          </p>
          <dl v-if="diagnostic.observation" class="diagnostic-observation">
            <div>
              <dt><code>-O2</code></dt>
              <dd>{{ verdicts[diagnostic.observation.o2] }}</dd>
            </div>
            <div v-if="diagnostic.observation.o0">
              <dt><code>-O0</code></dt>
              <dd>{{ verdicts[diagnostic.observation.o0] }}</dd>
            </div>
          </dl>
          <p v-if="diagnostic.ubsan" class="small">
            UBSan 运行结果 · {{ verdicts[diagnostic.ubsan.verdict] }}
          </p>
        </div>
        <div>
          <h4>待验证线索</h4>
          <p v-if="diagnostic.ubsan?.finding" class="small">
            {{ diagnosticCategories[diagnostic.ubsan.finding.category] }} · 第
            {{ diagnostic.ubsan.finding.line }} 行，第 {{ diagnostic.ubsan.finding.column }} 列
          </p>
          <p v-if="diagnostic.ubsan?.finding" class="small muted">
            线索来自程序的标准错误输出，请对照原提交核查。
          </p>
          <p
            v-else-if="
              diagnostic.observation?.o0 && diagnostic.observation.o0 !== diagnostic.observation.o2
            "
            class="small"
          >
            两种优化设置在这个样例上的结果不同。具体原因需要对照源码验证。
          </p>
          <p v-else class="small muted">本次诊断未定位到具体原因。</p>
        </div>
        <div>
          <h4>自查办法</h4>
          <p v-if="diagnostic.ubsan?.finding" class="small">
            用该样例手算到报告行，记录运算前的值，核对类型范围和下标边界。
          </p>
          <p v-else class="small">用该样例手算关键分支，记录中间值并核对题面给出的输出。</p>
          <div class="diagnostic-actions">
            <RouterLink v-if="sampleHandoff" :to="sampleHandoff" class="text-link">
              将样例填入自测
            </RouterLink>
            <RouterLink :to="'/submissions/' + submissionId" class="text-link">
              查看原提交
            </RouterLink>
          </div>
        </div>
        <LearningMistakeProposal
          v-if="diagnostic.ubsan?.finding"
          :key="diagnostic.request_id"
          :request-id="diagnostic.request_id"
          :disabled="disabled"
          :withdrawn-id="withdrawnMistakeId"
          @saved="emit('mistakeSaved')"
          @open="emit('openMistakes')"
        />
      </section>
      <div v-if="diagnostic" class="diagnostic-actions">
        <el-button
          v-if="active && diagnostic.state !== 'cancel_pending'"
          text
          :disabled="sending"
          @click="cancel"
        >
          取消诊断
        </el-button>
        <el-button v-if="active && !monitoring" text :disabled="sending" @click="refresh">
          刷新状态
        </el-button>
        <el-button v-if="!active && sampleCount" text :disabled="disabled" @click="reset">
          再次诊断
        </el-button>
      </div>
      <p v-if="message" class="small muted" role="status">{{ message }}</p>
    </div>
  </details>
</template>

<style scoped src="./learningDiagnostic.css" />
