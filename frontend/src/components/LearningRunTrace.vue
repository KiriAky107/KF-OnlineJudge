<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'
import DisclosureHeading from './DisclosureHeading.vue'

type Run = components['schemas']['AgentRunView']
type Step = components['schemas']['AgentTraceStep']
const props = defineProps<{ runId: string; chatRun?: Run }>()
const trace = ref<components['schemas']['AgentRunTrace'] | null>(null)
const error = ref('')
const loading = ref(false)
let request: AbortController | undefined
let lastRefresh = 0

const names: Record<string, string> = {
  submission_history: '本人提交记录',
  source_evidence: '本人代码证据',
  training_candidates: '训练候选题',
  'responses-concept-verdict': '结合评测类别的模型提示',
  'responses-review-evidence-focus': '模型选择复核线索',
  'responses-review-control-flow': '模型复盘代码结构',
  'responses-review-control-flow-focus': '模型复盘代码结构并选择线索',
  'responses-review-comparison-cases': '模型复盘比较条件',
  'responses-review-mcp': '模型结合站内资料复盘',
  'responses-source-excerpt': '本机模型分析源码片段',
  'responses-plan-explanation': '模型训练说明',
  'responses-guided-chat': '生成答复',
}
const states = { running: '执行中', succeeded: '成功', failed: '失败', cancelled: '已取消' }
const stepErrors: Record<string, string> = {
  model_unavailable: '模型服务暂不可用',
  model_timeout: '模型服务响应超时',
  model_rate_limited: '模型提供商暂时限制请求',
  model_provider_rejected: '模型提供商未接受请求',
  model_invalid_response: '模型返回内容无效',
  model_output_limit: '模型服务达到自身的输出上限',
  model_context_limit: '本次输入超出模型上下文预算',
  model_budget_exhausted: '本次请求达到模型调用额度',
  learning_execution_interrupted: '前一执行已中断',
  learning_attempts_exhausted: '已用完接续次数',
  tool_timeout: '工具响应超时',
  tool_unavailable: '本次未获工具授权',
  tool_budget_exhausted: '已用完工具额度',
}

const phases: Record<Run['state'], string> = {
  queued: '正在排队',
  running: '正在处理',
  retry_wait: '等待重试',
  cancel_requested: '正在停止',
  cancelled: '已停止',
  succeeded: '处理完成',
  failed: '处理未完成',
}
function active(state: Run['state']) {
  return state === 'running' || state === 'cancel_requested'
}
function stepTitle(step: Step) {
  return (
    names[step.invocation_version.replace(/-v\d+$/, '')] ??
    (step.kind === 'model' ? '生成答复' : '调用工具')
  )
}
const liveStep = computed(() => {
  const latest = trace.value?.steps.at(-1)
  return latest?.state === 'running' ? latest : undefined
})
const activity = computed(() => {
  const state = props.chatRun?.state
  if (!state) return ''
  if (state === 'running' && liveStep.value) return stepTitle(liveStep.value) + '中'
  return phases[state]
})
const resourceLabels = {
  problem: '公开题面',
  evaluations: '评测统计',
  code: '代码改动摘要',
  batch_diagnostics: '批量诊断',
  recent_practice: '近期做题概览',
}
const consultedResources = computed(() => props.chatRun?.result?.model_hint?.review_resources ?? [])
const inProgress = computed(() => {
  const state = props.chatRun?.state
  return state === 'queued' || state === 'retry_wait' || (state !== undefined && active(state))
})

function duration(value: number | null) {
  if (value === null) return '暂无记录'
  return value < 1000 ? `${value} 毫秒` : `${(value / 1000).toFixed(1)} 秒`
}
function invocation(value: string) {
  const suffix = value.lastIndexOf('-')
  const name = value.slice(0, suffix)
  return names[name] ? `${names[name]} · ${value.slice(suffix + 1)}` : value
}
async function load() {
  request?.abort()
  request = new AbortController()
  const { signal } = request
  lastRefresh = Date.now()
  loading.value = true
  error.value = ''
  try {
    const data = unwrap(
      await api.GET('/api/v1/learning/agent/runs/{run_id}/trace', {
        params: { path: { run_id: props.runId } },
        signal,
      }),
    )
    if (!signal.aborted) trace.value = data
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
function toggle(event: Event) {
  if (
    event.currentTarget instanceof HTMLDetailsElement &&
    event.currentTarget.open &&
    (!props.chatRun || !trace.value) &&
    !loading.value
  ) {
    void load()
  }
}
watch(
  () => props.chatRun,
  (run, previous) => {
    if (!run || document.hidden) return
    if (
      run.state !== previous?.state &&
      (active(run.state) || previous?.state === 'running' || previous?.state === 'cancel_requested')
    ) {
      void load()
    } else if (
      active(run.state) &&
      !loading.value &&
      !error.value &&
      Date.now() - lastRefresh >= 2400
    ) {
      void load()
    }
  },
  { immediate: true },
)
watch(
  () => props.runId,
  () => {
    request?.abort()
    trace.value = null
    loading.value = false
    error.value = ''
  },
)
onBeforeUnmount(() => request?.abort())
</script>

<template>
  <details v-if="chatRun" class="chat-activity" @toggle="toggle">
    <DisclosureHeading>
      <template #indicator>
        <span class="activity-indicator" :class="{ pulsing: inProgress }" aria-hidden="true" />
      </template>
      <span role="status">执行记录 · {{ activity }}</span>
      <span v-if="!inProgress && trace?.duration_ms != null" class="muted">
        · {{ duration(trace.duration_ms) }}
      </span>
    </DisclosureHeading>
    <div class="activity-details">
      <p v-if="error" class="small muted">
        处理步骤暂时无法读取。
        <button type="button" class="text-link" @click="load">重试</button>
      </p>
      <ol v-else-if="trace?.steps.length" class="activity-steps">
        <li v-for="step in trace.steps" :key="step.sequence" :data-state="step.state">
          <span>{{ stepTitle(step) }}</span>
          <small>{{
            step.error_code
              ? (stepErrors[step.error_code] ?? states[step.state])
              : states[step.state]
          }}</small>
        </li>
      </ol>
      <p v-else class="small muted">
        {{ inProgress || loading ? '等待可显示的处理步骤…' : '暂无可显示的处理步骤。' }}
      </p>
      <p v-if="trace?.truncated" class="small muted">仅展示最近 12 步。</p>
      <p v-if="consultedResources.length" class="small muted">
        模型查阅了{{ consultedResources.map((resource) => resourceLabels[resource]).join('、') }}。
      </p>
    </div>
  </details>
  <details v-else class="run-trace" @toggle="toggle">
    <DisclosureHeading icon="clock" tone="accent">查看执行步骤</DisclosureHeading>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在读取执行步骤…</p>
    <div v-if="trace" class="stack">
      <dl class="details-list">
        <div>
          <dt>领取次数</dt>
          <dd>{{ trace.attempts }}</dd>
        </div>
        <div>
          <dt>首次排队</dt>
          <dd>{{ duration(trace.queue_ms) }}</dd>
        </div>
        <div>
          <dt>运行总时长</dt>
          <dd>{{ duration(trace.duration_ms) }}</dd>
        </div>
        <div>
          <dt>工具调用</dt>
          <dd>{{ trace.finished_tool_calls }} / {{ trace.tool_calls }} 次已结束</dd>
        </div>
        <div>
          <dt>已结束的工具累计耗时</dt>
          <dd>{{ duration(trace.tool_elapsed_ms) }}</dd>
        </div>
        <div v-if="trace.model_elapsed_ms != null">
          <dt>已结束的模型步骤耗时</dt>
          <dd>{{ duration(trace.model_elapsed_ms) }}</dd>
        </div>
        <div v-if="trace.model_usage">
          <dt>模型服务报告的用量</dt>
          <dd>
            输入 {{ trace.model_usage.input_tokens }} · 输出 {{ trace.model_usage.output_tokens }} ·
            合计 {{ trace.model_usage.total_tokens }} token
          </dd>
        </div>
      </dl>
      <ul v-if="trace.steps.length">
        <li v-for="step in trace.steps" :key="step.sequence">
          {{ step.sequence }}. {{ invocation(step.invocation_version) }} · {{ states[step.state] }}
          <span v-if="step.error_code" class="small muted" :title="step.error_code">
            · {{ stepErrors[step.error_code] ?? '未取得有效结果' }}
          </span>
        </li>
      </ul>
      <p v-else class="small muted">暂无执行步骤。</p>
      <p v-if="trace.truncated" class="small muted">仅展示最近 12 步。</p>
    </div>
    <el-button v-if="trace || error" :disabled="loading" @click="load">刷新执行步骤</el-button>
  </details>
</template>

<style scoped>
.chat-activity {
  margin-block: 0.2rem 0.6rem;
  color: var(--secondary-ink);
  font-size: 0.86rem;
}
.activity-indicator,
.activity-steps li::before {
  width: 0.42rem;
  height: 0.42rem;
  flex: none;
  border-radius: 50%;
  background: var(--el-color-primary);
}
.activity-indicator.pulsing,
.activity-steps li[data-state='running']::before {
  animation: activity-pulse 1.45s ease-in-out infinite;
}
@keyframes activity-pulse {
  50% {
    opacity: 0.35;
  }
}
@media (prefers-reduced-motion: reduce) {
  .activity-indicator.pulsing,
  .activity-steps li[data-state='running']::before {
    animation: none;
  }
}
.activity-details {
  margin: 0.4rem 0 0 0.2rem;
  padding: 0.15rem 0 0.15rem 0.75rem;
  border-inline-start: 1px solid var(--line);
}
.activity-details p {
  margin: 0.15rem 0;
}
.activity-steps {
  display: grid;
  gap: 0.3rem;
  margin: 0;
  padding: 0;
  list-style: none;
}
.activity-steps li {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}
.activity-steps li::before {
  content: '';
  align-self: center;
}
.activity-steps li[data-state='failed']::before {
  background: var(--el-color-danger);
}
.activity-steps li[data-state='cancelled']::before {
  background: var(--el-text-color-secondary);
}
.activity-steps small {
  color: var(--el-text-color-secondary);
}

.run-trace {
  border-top: 1px solid var(--line);
  padding-top: 0.75rem;
}
.run-trace > summary {
  cursor: pointer;
  margin-bottom: 0.6rem;
}
.run-trace > button {
  margin-top: 0.5rem;
}
ul {
  margin: 0;
  padding-left: 1.25rem;
}
</style>
