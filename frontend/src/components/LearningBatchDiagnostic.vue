<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { boundedPoll } from '../api/polling'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import { newRequestId } from '../lib/requestCrypto'
import DisclosureHeading from './DisclosureHeading.vue'

type Batch = components['schemas']['BatchDiagnosticOption']
type Receipt = components['schemas']['BatchDiagnosticView']
type Outcome = components['schemas']['BatchDiagnosticSummary']['outcome']
const props = defineProps<{
  accountId: string
  submissionId: string
  disabled: boolean
  reviewRunId: string
  reviewSessionId: string
  suggested?: boolean
}>()
const lifetime = new AbortController()
const options = ref<Batch[]>([])
const version = ref('')
const selected = ref('')
const requestId = ref('')
const receiptReview = ref<string>()
const receipt = ref<Receipt>()
const expanded = ref(false)
const sending = ref(false)
const monitoring = ref(false)
const message = ref('')
const key = `kf-oj:batch-diagnostic:${props.accountId}:${props.reviewSessionId}:${props.submissionId}`
watch(
  () => props.suggested,
  (suggested) => {
    if (suggested) expanded.value = true
  },
  { immediate: true },
)
const terminal = (item: Receipt) => !['queued', 'running', 'cancel_pending'].includes(item.state)
const active = computed(() => receipt.value && !terminal(receipt.value))
const selectedOffer = computed(() =>
  options.value.some((option) => option.manifest_sha256 === selected.value),
)
const states: Record<Receipt['state'], string> = {
  queued: '等待诊断',
  running: '正在诊断',
  cancel_pending: '正在取消，等待清理',
  cancelled: '已取消',
  expired: '诊断已超时',
  unavailable: '此批次暂不可用',
  observed: '诊断完成',
  failed: '诊断运行失败',
}
const outcomes: Record<Outcome, string> = {
  reference_pending: '参考解仍待核验',
  input_not_verified: '输入校验未通过',
  reduction_verified: '较小输入稳定复现差异',
  reduction_unverified: '较小输入未稳定复现差异',
  accepted: '对拍结果一致',
  completed: '边界测量运行完成',
  compilation_error: '编译错误',
  wrong_answer: '对拍结果不一致',
  runtime_error: '运行错误',
  resource_limit: '达到资源上限',
}

function remember(id: string) {
  requestId.value = id
  if (!id) {
    receiptReview.value = undefined
    if (!selectedOffer.value) selected.value = options.value[0]?.manifest_sha256 ?? ''
  }
  try {
    if (id)
      sessionStorage.setItem(
        key,
        JSON.stringify({ id, manifest: selected.value, reviewId: receiptReview.value }),
      )
    else sessionStorage.removeItem(key)
  } catch {
    // A blocked storage API only prevents restoring the receipt after a reload.
  }
}

function setStatus(item: Receipt) {
  receipt.value = item
  if (terminal(item)) monitoring.value = false
}

const path = '/api/v1/learning/agent/diagnostic-batches/{request_id}' as const
const poll = boundedPoll({
  load: async (signal) =>
    unwrap(await api.GET(path, { params: { path: { request_id: requestId.value } }, signal })),
  terminal,
  result: setStatus,
  error: (cause) => {
    monitoring.value = false
    if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
      receipt.value = undefined
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
  if (!requestId.value) return
  message.value = ''
  monitoring.value = true
  poll.start()
}

async function start() {
  if (!selectedOffer.value || props.disabled || sending.value || active.value || monitoring.value)
    return
  const { signal } = lifetime
  sending.value = true
  message.value = ''
  try {
    const id = requestId.value || newRequestId()
    if (!requestId.value) receiptReview.value = props.reviewRunId
    remember(id)
    const item = unwrap(
      await api.PUT(path, {
        params: { path: { request_id: id } },
        headers: csrfHeaders(),
        body: {
          submission_id: props.submissionId,
          problem_version_id: version.value,
          manifest_sha256: selected.value,
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
    const { response } = await api.POST(`${path}/cancel`, {
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
  receipt.value = undefined
  message.value = ''
  monitoring.value = false
}

const syncVisibility = () => poll.visible(!document.hidden)
onMounted(async () => {
  const { signal } = lifetime
  document.addEventListener('visibilitychange', syncVisibility)
  syncVisibility()
  try {
    const submission = unwrap(
      await api.GET('/api/v1/submissions/{submission_id}', {
        params: { path: { submission_id: props.submissionId } },
        signal,
      }),
    )
    if (!['c17', 'cpp20'].includes(submission.language)) return
    const result = unwrap(
      await api.GET('/api/v1/learning/agent/diagnostic-batches/options', {
        params: {
          query: {
            submission_id: props.submissionId,
            problem_version_id: submission.problem_version_id,
          },
        },
        signal,
      }),
    )
    if (signal.aborted) return
    version.value = submission.problem_version_id
    options.value = result.options
    selected.value = result.options[0]?.manifest_sha256 ?? ''
  } catch {
    // Only advertise options once the server confirms them for this submission.
    return
  }
  try {
    const stored = JSON.parse(sessionStorage.getItem(key) ?? 'null')
    if (
      typeof stored?.id === 'string' &&
      /^[0-9a-f-]{36}$/.test(stored.id) &&
      typeof stored.manifest === 'string' &&
      /^[0-9a-f]{64}$/.test(stored.manifest)
    ) {
      selected.value = stored.manifest
      requestId.value = stored.id
      receiptReview.value = typeof stored.reviewId === 'string' ? stored.reviewId : undefined
      expanded.value = true
      refresh()
    }
  } catch {
    // The current request remains usable with storage disabled.
  }
})
onBeforeUnmount(() => {
  lifetime.abort()
  poll.dispose()
  document.removeEventListener('visibilitychange', syncVisibility)
})
</script>

<template>
  <details
    v-if="options.length || requestId || receipt"
    class="public-diagnostic"
    :open="expanded || undefined"
  >
    <DisclosureHeading icon="monitor" tone="accent">批次诊断</DisclosureHeading>
    <div class="diagnostic-body">
      <form v-if="!receipt && !monitoring" class="diagnostic-form" @submit.prevent="start">
        <p>
          <RouterLink :to="'/submissions/' + submissionId" :title="submissionId">
            提交 {{ submissionId.slice(0, 8) }}
          </RouterLink>
          <span class="small muted"> · 数据来源为站内登记批次</span>
        </p>
        <label v-if="selectedOffer" class="field">
          诊断批次
          <select v-model="selected" :disabled="disabled || sending || Boolean(requestId)">
            <option
              v-for="option in options"
              :key="option.manifest_sha256"
              :value="option.manifest_sha256"
            >
              {{ option.title }} · {{ option.purpose === 'boundary' ? '边界测量' : '对拍' }}
            </option>
          </select>
        </label>
        <p v-if="selectedOffer" class="small muted">
          运行会把这份提交的源码交给沙箱，用所选批次诊断。
        </p>
        <div class="diagnostic-actions">
          <el-button
            v-if="selectedOffer"
            native-type="submit"
            :loading="sending"
            :disabled="disabled"
          >
            {{ requestId ? '重试请求' : '确认运行' }}
          </el-button>
          <el-button v-if="requestId" text :disabled="sending" @click="refresh">刷新状态</el-button>
        </div>
      </form>
      <p v-if="monitoring && !receipt" class="small muted" role="status">正在读取诊断状态…</p>
      <p v-if="receipt" class="small" role="status">{{ states[receipt.state] }}</p>
      <section v-if="receipt?.summary" class="diagnostic-card" aria-label="批次诊断结果">
        <h3>{{ receipt.summary.purpose === 'boundary' ? '边界测量' : '对拍' }}</h3>
        <p>{{ outcomes[receipt.summary.outcome] }}</p>
      </section>
      <div v-if="receipt" class="diagnostic-actions">
        <el-button
          v-if="active && receipt.state !== 'cancel_pending'"
          text
          :disabled="sending"
          @click="cancel"
        >
          取消诊断
        </el-button>
        <el-button v-if="active && !monitoring" text :disabled="sending" @click="refresh">
          刷新状态
        </el-button>
        <el-button v-if="!active && options.length" text :disabled="disabled" @click="reset">
          再次诊断
        </el-button>
      </div>
      <p v-if="message" class="small muted" role="status">{{ message }}</p>
    </div>
  </details>
</template>

<style scoped src="./learningDiagnostic.css" />
