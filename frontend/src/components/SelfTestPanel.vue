<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { activeJudgementPolling, boundedPoll } from '../api/polling'
import { useSession } from '../composables/useSession'
import type { components } from '../api/generated/schema'
import { sample } from '../lib/publicSample'
import { newRequestId } from '../lib/requestCrypto'
import JudgementStatus from './JudgementStatus.vue'
import UiIcon from './UiIcon.vue'
const props = defineProps<{
  problem: components['schemas']['ProblemDetail']
  language: components['schemas']['SubmissionSummary']['language'] | null
  source: string
}>()
const emit = defineEmits<{ run: [] }>()
const session = useSession()
const router = useRouter()
const route = useRoute()
const tab = ref('input')
const input = ref('')
const byteLimit = 65536
const encoder = new TextEncoder()
const inputBytes = computed(() => encoder.encode(input.value).length)
const sampleIndex = ref<number | null>(null)
const expected = computed(() =>
  sampleIndex.value === null ? null : props.problem.samples[sampleIndex.value]?.output,
)
const result = ref<components['schemas']['SelfTestDetail'] | null>(null)
const running = ref(false)
const error = ref('')
const notice = ref('')
const snapshot = ref<{
  source: string
  stdin: string
  language: string
  expected: string | null
  sampleIndex: number | null
} | null>(null)
const changed = computed(
  () =>
    snapshot.value &&
    (snapshot.value.source !== props.source ||
      snapshot.value.stdin !== input.value ||
      snapshot.value.language !== props.language ||
      snapshot.value.sampleIndex !== sampleIndex.value),
)
let request = new AbortController()
let generation = 0
let poll: ReturnType<typeof boundedPoll<components['schemas']['SelfTestDetail']>> | undefined
let intent: { fingerprint: string; key: string } | undefined
const terminal = (value: components['schemas']['SelfTestDetail']) =>
  ['finished', 'failed', 'cancelled'].includes(value.state)
const completed = computed(
  () => result.value?.state === 'finished' && result.value.verdict === 'accepted',
)
const comparison = computed(() => {
  const run = result.value
  const expectedOutput = snapshot.value?.expected
  if (!completed.value || !run || expectedOutput == null) return null
  const stdout = run.stdout
  // The public result is UTF-8 text. Truncation or replacement loses byte-level evidence.
  if (stdout == null || run.stdout_truncated || stdout.includes('\uFFFD')) return 'unavailable'
  const bytes = encoder.encode(stdout)
  const expectedBytes = atob(expectedOutput)
  return bytes.length === expectedBytes.length &&
    bytes.every((byte, i) => byte === expectedBytes.charCodeAt(i))
    ? 'match'
    : 'different'
})
function selectSample(index: number) {
  try {
    input.value = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(
      Uint8Array.from(atob(props.problem.samples[index]!.input), (c) => c.charCodeAt(0)),
    )
    sampleIndex.value = index
    error.value = ''
    tab.value = 'input'
  } catch {
    error.value = '该样例不是文本，请自行填写自测输入。'
  }
}
function reset() {
  generation++
  request.abort()
  request = new AbortController()
  poll?.dispose()
  result.value = null
  snapshot.value = null
  tab.value = 'input'
  running.value = false
  error.value = notice.value = ''
  intent = undefined
}
function follow(id: string, current: number) {
  poll?.dispose()
  poll = boundedPoll({
    ...activeJudgementPolling,
    load: async (signal) =>
      unwrap(
        await api.GET('/api/v1/self-tests/{self_test_id}', {
          params: { path: { self_test_id: id } },
          signal,
        }),
      ),
    terminal,
    result: (value) => {
      if (current === generation) {
        result.value = value
        running.value = !terminal(value)
        if (terminal(value)) intent = undefined
      }
    },
    error: (cause) => {
      if (current === generation) {
        error.value = errorText(cause)
        running.value = false
      }
    },
    exhausted: () => {
      if (current === generation) {
        notice.value = '自动刷新已暂停。'
        running.value = false
      }
    },
  })
  poll.start()
}
async function run() {
  if (running.value || !props.language || inputBytes.value > byteLimit) return
  if (!session.user.value) {
    await router.push({ path: '/login', query: { next: route.fullPath } })
    return
  }
  const current = ++generation
  request.abort()
  request = new AbortController()
  poll?.dispose()
  error.value = notice.value = ''
  result.value = null
  running.value = true
  tab.value = 'result'
  const body = {
    problem_version_id: props.problem.version_id,
    language: props.language,
    source: props.source,
    stdin: input.value,
  }
  snapshot.value = {
    source: body.source,
    stdin: body.stdin,
    language: body.language,
    expected: expected.value ?? null,
    sampleIndex: sampleIndex.value,
  }
  try {
    const fingerprint = JSON.stringify(body)
    if (intent?.fingerprint !== fingerprint) intent = { fingerprint, key: newRequestId() }
    emit('run')
    const value = unwrap(
      await api.POST('/api/v1/self-tests', {
        body: { ...body, idempotency_key: intent.key },
        headers: csrfHeaders(),
        signal: request.signal,
      }),
    )
    if (current !== generation) return
    result.value = value
    if (terminal(value)) {
      running.value = false
      intent = undefined
    } else follow(value.self_test_id, current)
  } catch (cause) {
    if (current === generation) {
      error.value = errorText(cause)
      running.value = false
    }
  }
}
function refresh() {
  if (!result.value || terminal(result.value)) return
  error.value = notice.value = ''
  running.value = true
  follow(result.value.self_test_id, generation)
}
watch(
  () => props.problem.version_id,
  () => {
    reset()
    input.value = ''
    sampleIndex.value = null
    if (props.problem.samples.length) selectSample(0)
  },
  { immediate: true, flush: 'sync' },
)
watch(
  () => session.user.value?.id,
  () => {
    reset()
    input.value = ''
    sampleIndex.value = null
    if (props.problem.samples.length) selectSample(0)
  },
  { flush: 'sync' },
)
onBeforeUnmount(() => {
  generation++
  request.abort()
  poll?.dispose()
})
</script>
<template>
  <section class="self-test-panel" aria-label="在线自测">
    <header class="test-toolbar">
      <el-radio-group v-model="tab" size="small" aria-label="自测面板">
        <el-radio-button value="input">测试用例</el-radio-button>
        <el-radio-button value="result">运行结果</el-radio-button>
      </el-radio-group>
      <el-button
        size="small"
        type="success"
        plain
        :loading="running"
        :disabled="!language || inputBytes > byteLimit"
        @click="run"
      >
        运行自测
      </el-button>
    </header>

    <div v-show="tab === 'input'" class="test-body test-input">
      <div v-if="problem.samples.length" class="sample-actions">
        <el-button
          v-for="(_, index) in problem.samples"
          :key="index"
          size="small"
          :type="sampleIndex === index ? 'primary' : 'default'"
          :aria-pressed="sampleIndex === index"
          @click="selectSample(index)"
          >样例 {{ index + 1 }}</el-button
        >
      </div>
      <el-input
        v-model="input"
        type="textarea"
        :rows="3"
        resize="none"
        aria-label="自测输入"
        placeholder="输入自测数据（可留空）"
        @input="sampleIndex = null"
      />
      <p class="input-budget" :class="{ 'input-budget--exceeded': inputBytes > byteLimit }">
        {{ inputBytes.toLocaleString() }} / {{ byteLimit.toLocaleString() }} 字节
      </p>
      <p v-if="error" role="alert">{{ error }}</p>
    </div>

    <div v-show="tab === 'result'" class="test-body test-result">
      <div
        v-if="running || result"
        class="test-status"
        :data-tone="
          comparison === 'match'
            ? 'success'
            : comparison === 'different' || (result && terminal(result) && !completed)
              ? 'error'
              : 'info'
        "
        role="status"
      >
        <div class="test-status-title">
          <span v-if="running && !result" class="test-spinner" aria-hidden="true"></span>
          <span v-if="!result">Creating Self-test</span>
          <template v-else-if="completed">
            <span lang="en">{{
              comparison === 'match'
                ? 'Accepted'
                : comparison === 'different'
                  ? 'Wrong Answer'
                  : 'Completed'
            }}</span>
            <span v-if="comparison === 'match' && snapshot?.sampleIndex != null"
              >样例 {{ snapshot.sampleIndex + 1 }} 通过</span
            >
            <span v-if="comparison === 'different'">实际输出与预期不同</span>
            <span v-if="comparison === 'unavailable'">输出无法逐字节核对</span>
          </template>
          <JudgementStatus
            v-else
            plain
            :state="result.state"
            :verdict="result.verdict"
            :language="result.language"
          />
        </div>
        <div
          v-if="result && (result.cpu_time_us != null || result.memory_peak_bytes != null)"
          class="test-metrics"
        >
          <span v-if="result.cpu_time_us != null"
            ><UiIcon name="clock" /> CPU {{ result.cpu_time_us / 1000 }} ms</span
          >
          <span v-if="result.memory_peak_bytes != null"
            ><UiIcon name="monitor" /> 内存
            {{ (result.memory_peak_bytes / 1048576).toFixed(2) }} MiB</span
          >
        </div>
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <p v-if="!result && !running && !error" class="muted">运行自测后可在此查看结果。</p>
      <p v-if="changed" class="muted">代码或用例已修改，以下为上次自测的结果。</p>
      <template v-if="result && terminal(result)">
        <p v-if="result.failure_code" role="alert">系统原因：{{ result.failure_code }}</p>
        <dl v-if="snapshot" class="test-output">
          <div>
            <dt>自测输入</dt>
            <dd>
              <pre tabindex="0">{{ snapshot.stdin || '（无输入）' }}</pre>
            </dd>
          </div>
          <div v-if="snapshot.expected !== null">
            <dt>预期输出</dt>
            <dd>
              <pre tabindex="0">{{ sample(snapshot.expected) || '（空）' }}</pre>
            </dd>
          </div>
          <div v-if="result.stdout != null">
            <dt>实际输出</dt>
            <dd>
              <pre tabindex="0">{{ result.stdout || '（空）' }}</pre>
              <small v-if="result.stdout_truncated">输出已截断</small>
            </dd>
          </div>
          <div v-if="result.compile_diagnostics">
            <dt>编译器返回</dt>
            <dd>
              <pre tabindex="0">{{ result.compile_diagnostics }}</pre>
              <small v-if="result.compile_diagnostics_truncated">诊断已截断</small>
            </dd>
          </div>
          <div v-if="result.stderr">
            <dt>错误输出</dt>
            <dd>
              <pre tabindex="0">{{ result.stderr }}</pre>
              <small v-if="result.stderr_truncated">输出已截断</small>
            </dd>
          </div>
        </dl>
      </template>
      <el-button v-if="result && !running && !terminal(result)" size="small" @click="refresh"
        >刷新结果</el-button
      >
    </div>
  </section>
</template>
<style scoped>
.self-test-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.test-toolbar,
.sample-actions,
.test-status-title,
.test-metrics,
.test-metrics > span {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}
.test-toolbar {
  padding: 7px 14px;
  border-bottom: 1px solid var(--el-border-color);
  background: var(--el-fill-color-lighter);
}
.test-body {
  padding: 10px 14px;
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--el-bg-color, #fff);
  font-size: 13px;
}
.test-input {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.test-input :deep(.el-textarea) {
  display: flex;
  flex: 1;
}
.test-input :deep(textarea),
.test-output pre {
  font-family: ui-monospace, monospace;
  font-size: 13px;
}
.input-budget {
  align-self: flex-end;
  color: var(--el-text-color-regular);
  font-variant-numeric: tabular-nums;
}
.input-budget--exceeded {
  color: #a32d48;
}
.test-status {
  margin-bottom: 10px;
  padding: 8px 12px;
  border-radius: 7px;
  border-inline-start: 4px solid currentColor;
  background: #edf5ff;
  color: #285783;
}
.test-status[data-tone='success'] {
  background: #e9f7ee;
  color: #155f46;
}
.test-status[data-tone='error'] {
  background: #fff0f1;
  color: #922f49;
}
.test-status-title {
  font-size: 14px;
  font-weight: 700;
}
.test-status-title :deep(.verdict) {
  padding: 0;
  font: inherit;
}
.test-metrics {
  margin-top: 3px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.test-spinner {
  width: 13px;
  height: 13px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.75s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(1turn);
  }
}
@media (prefers-reduced-motion: reduce) {
  .test-spinner {
    animation: none;
  }
}
.test-output {
  display: grid;
  gap: 8px;
  margin: 8px 0 0;
}
.test-output > div {
  display: grid;
  grid-template-columns: minmax(5.5rem, 15%) minmax(0, 1fr);
  gap: 8px;
  align-items: start;
}
.test-output dt {
  padding-top: 6px;
  font-weight: 600;
}
.test-output dd,
.test-output pre {
  margin: 0;
}
.test-output pre {
  padding: 6px 9px;
  max-height: 8rem;
  overflow: auto;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color);
  border-radius: 5px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 470px) {
  .test-output > div {
    grid-template-columns: minmax(0, 1fr);
    gap: 3px;
  }
}
</style>
