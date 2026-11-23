<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { boundedPoll } from '../api/polling'
import { useSession } from '../composables/useSession'
import type { components } from '../api/generated/schema'
import { sample } from '../lib/publicSample'
import { newRequestId } from '../lib/requestCrypto'
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
} | null>(null)
const changed = computed(
  () =>
    snapshot.value &&
    (snapshot.value.source !== props.source ||
      snapshot.value.stdin !== input.value ||
      snapshot.value.language !== props.language),
)
let request = new AbortController()
let generation = 0
let poll: ReturnType<typeof boundedPoll<components['schemas']['SelfTestDetail']>> | undefined
let intent: { fingerprint: string; key: string } | undefined
const terminal = (value: components['schemas']['SelfTestDetail']) =>
  ['finished', 'failed', 'cancelled'].includes(value.state)
const status = computed(() => {
  if (!result.value) return running.value ? '正在创建自测…' : ''
  if (result.value.state === 'finished' && result.value.verdict === 'accepted') return '运行完成'
  const names: Record<string, string> = {
    queued: '排队中',
    claimed: '准备中',
    compiling: '编译中',
    running: '运行中',
    cancelling: '取消中',
    cancelled: '已取消',
    failed: '运行失败',
    compilation_error: '编译错误',
    runtime_error: '运行错误',
    time_limit_exceeded: '运行超时',
    memory_limit_exceeded: '内存超限',
    output_limit_exceeded: '输出超限',
  }
  return names[result.value.verdict ?? result.value.state] ?? result.value.state
})
function selectSample(index: number) {
  try {
    input.value = new TextDecoder('utf-8', { fatal: true }).decode(
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
  running.value = false
  error.value = notice.value = ''
  intent = undefined
}
function follow(id: string, current: number) {
  poll?.dispose()
  poll = boundedPoll({
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
  if (running.value || !props.language) return
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
defineExpose({ run, running })
onBeforeUnmount(() => {
  generation++
  request.abort()
  poll?.dispose()
})
</script>
<template>
  <div class="self-test-panel">
    <el-tabs v-model="tab">
      <el-tab-pane class="self-test-input" label="自测输入" name="input">
        <div class="sample-actions">
          <el-button
            v-for="(_, index) in problem.samples"
            :key="index"
            size="small"
            @click="selectSample(index)"
            >样例 {{ index + 1 }}</el-button
          >
        </div>
        <el-input
          v-model="input"
          type="textarea"
          :rows="3"
          aria-label="自测输入"
          placeholder="输入自测数据"
          @input="sampleIndex = null"
        />
      </el-tab-pane>
      <el-tab-pane label="运行结果" name="result">
        <p v-if="status" role="status">{{ status }}</p>
        <p v-if="changed" class="muted">代码或输入已修改，以下为上次自测结果。</p>
        <p v-if="!result && !running && !error">尚未运行。</p>
        <template v-if="result">
          <p v-if="result.failure_code">{{ result.failure_code }}</p>
          <template v-if="result.compile_diagnostics"
            ><h4>编译信息</h4>
            <pre>{{ result.compile_diagnostics }}</pre>
            <small v-if="result.compile_diagnostics_truncated">显示内容已截断</small></template
          >
          <template v-if="result.stdout !== null"
            ><h4>标准输出</h4>
            <pre>{{ result.stdout || '（空）' }}</pre>
            <small v-if="result.stdout_truncated">显示内容已截断</small></template
          >
          <template v-if="result.stderr"
            ><h4>错误输出</h4>
            <pre>{{ result.stderr }}</pre>
            <small v-if="result.stderr_truncated">显示内容已截断</small></template
          >
          <template v-if="snapshot?.expected != null"
            ><h4>样例预期输出</h4>
            <pre>{{ sample(snapshot.expected) }}</pre>
          </template>
          <el-button v-if="!running && !terminal(result)" size="small" @click="refresh"
            >刷新结果</el-button
          >
        </template>
      </el-tab-pane>
    </el-tabs>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice">{{ notice }}</p>
  </div>
</template>
<style scoped>
.sample-actions {
  display: flex;
  gap: 0.4rem;
  margin-bottom: 0.5rem;
  flex-wrap: wrap;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.self-test-panel,
.self-test-panel :deep(.el-tabs),
.self-test-panel :deep(.el-tabs__content),
.self-test-panel :deep(.self-test-input) {
  display: flex;
  flex: 1;
  flex-direction: column;
}
.self-test-panel :deep(.el-tabs__content) {
  overflow: visible;
}
.self-test-input :deep(.el-textarea) {
  display: flex;
  flex: 1;
}
.self-test-input :deep(textarea) {
  resize: none;
}
h4 {
  margin-top: 0.6rem;
}
</style>
