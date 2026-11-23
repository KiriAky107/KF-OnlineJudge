<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'

const props = defineProps<{ runId: string }>()
const trace = ref<components['schemas']['AgentRunTrace'] | null>(null)
const error = ref('')
const loading = ref(false)
let request: AbortController | undefined

const names: Record<string, string> = {
  submission_history: '本人提交记录',
  source_evidence: '本人代码证据',
  training_candidates: '训练候选题',
  'responses-concept-verdict': '结合评测类别的模型提示',
  'responses-review-evidence-focus': '模型选择复核线索',
  'responses-review-control-flow': '模型复盘代码结构',
  'responses-review-control-flow-focus': '模型复盘代码结构并选择线索',
  'responses-review-comparison-cases': '模型复盘比较条件',
  'responses-source-excerpt': '本机模型分析源码片段',
  'responses-plan-explanation': '模型训练说明',
}
const states = { running: '执行中', succeeded: '成功', failed: '失败', cancelled: '已取消' }
const stepErrors: Record<string, string> = {
  model_unavailable: '模型服务暂不可用',
  model_timeout: '模型服务响应超时',
  model_rate_limited: '模型提供商暂时限制请求',
  model_provider_rejected: '模型提供商未接受请求',
  model_invalid_response: '模型返回内容无效',
  model_output_limit: '模型输出达到本次长度上限',
  learning_execution_interrupted: '前一执行已中断',
  learning_attempts_exhausted: '已用完接续次数',
  tool_timeout: '工具响应超时',
  tool_unavailable: '本次未获工具授权',
  tool_budget_exhausted: '已用完工具额度',
}

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
  if (event.currentTarget instanceof HTMLDetailsElement && event.currentTarget.open) void load()
}
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
  <details class="run-trace" @toggle="toggle">
    <summary>查看执行步骤</summary>
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
