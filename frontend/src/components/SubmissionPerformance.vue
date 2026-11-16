<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import { errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import type { components } from '../api/generated/schema'
import { chartPosition, formatMeasurement } from '../lib/performance'
import UiIcon from './UiIcon.vue'

const props = defineProps<{ submissionId: string }>()
const session = useSession()
const data = ref<components['schemas']['SubmissionPerformance'] | null>(null)
const busy = ref(false)
const error = ref('')
const metric = ref<'cpu' | 'memory'>('cpu')
const active = ref<number | null>(null)
let request: AbortController | undefined
let generation = 0
const summary = computed(() => data.value?.[metric.value])
const unit = computed(() => (metric.value === 'cpu' ? 'ms' : 'MiB'))
const selected = computed(() =>
  active.value === null ? undefined : summary.value?.buckets[active.value],
)
const end = computed(() => summary.value?.buckets.at(-1)?.end ?? '1')
const ceiling = computed(() => Math.max(1, ...(summary.value?.buckets.map((b) => b.count) ?? [])))
const reasons = {
  not_accepted: '本次没有最终通过成绩，无法比较。',
  measurement_missing: '本次缺少该指标的可信测量。',
  environment_unverified: '这条历史成绩未绑定可验证的测量环境。',
  insufficient_samples: '同组其他有效通过记录不足 5 条。',
}
function format(value: string | null) {
  return formatMeasurement(value, metric.value === 'cpu' ? 'cpu_microseconds' : 'bytes')
}
async function load() {
  if (busy.value || !session.user.value) return
  request?.abort()
  const controller = new AbortController()
  request = controller
  const current = ++generation
  busy.value = true
  data.value = null
  error.value = ''
  try {
    const value = unwrap(
      await api.GET('/api/v1/submissions/{submission_id}/performance', {
        params: { path: { submission_id: props.submissionId } },
        signal: controller.signal,
      }),
    )
    if (generation === current) data.value = value
  } catch (cause) {
    if (generation === current) error.value = errorText(cause)
  } finally {
    if (generation === current) busy.value = false
  }
}
watch(
  [() => props.submissionId, () => session.user.value?.id],
  () => {
    generation++
    request?.abort()
    data.value = null
    busy.value = false
    error.value = ''
    active.value = null
    void load()
  },
  { immediate: true },
)
watch(metric, () => (active.value = null))
onBeforeUnmount(() => {
  generation++
  request?.abort()
})
</script>
<template>
  <section
    class="performance-distribution"
    aria-label="答题表现分布"
    data-testid="submission-performance"
  >
    <header class="performance-heading">
      <div>
        <h2><UiIcon name="rankings" />答题表现分布</h2>
        <p>同题目版本、语言、工具链与测量环境的通过记录</p>
      </div>
      <el-button :loading="busy" @click="load">刷新表现</el-button>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="busy" role="status">正在读取真实评测统计…</p>
    <template v-if="data && summary">
      <div class="performance-metrics" aria-label="选择分布指标">
        <button
          v-for="(title, key) in { cpu: '运行 CPU 时间', memory: '运行内存峰值' }"
          :key="key"
          :aria-pressed="metric === key"
          @click="metric = key"
        >
          {{ title }}
        </button>
      </div>
      <p data-testid="performance-current" :data-raw-value="summary.current">
        本次：{{ format(summary.current) }} {{ unit }}
      </p>
      <p v-if="!summary.available" role="status">{{ reasons[summary.reason!] }}</p>
      <template v-else>
        <p
          data-testid="performance-summary"
          :data-sample-count="summary.sample_count"
          :data-greater="summary.strictly_slower_count"
          :data-tied="summary.tied_count"
        >
          优于 {{ ((summary.strictly_slower_count! / summary.sample_count) * 100).toFixed(1) }}%
          的比较记录； 同值 {{ summary.tied_count }} 条（不计优于）。
        </p>
        <svg
          viewBox="0 0 640 250"
          role="img"
          :aria-label="`${metric === 'cpu' ? 'CPU 时间' : '内存'}分布，${summary.sample_count} 条记录`"
        >
          <line x1="40" y1="214" x2="620" y2="214" stroke="currentColor" />
          <rect
            v-for="(bucket, index) in summary.buckets"
            :key="index"
            tabindex="0"
            :x="40 + chartPosition(bucket.start, end) * 580"
            :width="
              Math.max(
                1,
                (chartPosition(bucket.end, end) - chartPosition(bucket.start, end)) * 580 - 2,
              )
            "
            :y="214 - (bucket.count / ceiling) * 170"
            :height="(bucket.count / ceiling) * 170"
            fill="var(--el-color-primary)"
            @mouseenter="active = index"
            @focus="active = index"
          >
            <title>
              {{ format(bucket.start) }}–{{ format(bucket.end) }} {{ unit }}，{{ bucket.count }} 条
            </title>
          </rect>
          <line
            :x1="40 + chartPosition(summary.current!, end) * 580"
            :x2="40 + chartPosition(summary.current!, end) * 580"
            y1="30"
            y2="220"
            stroke="var(--el-color-danger)"
            stroke-width="2"
          />
          <text x="40" y="245">0</text>
          <text x="620" y="245" text-anchor="end">{{ format(end) }} {{ unit }}</text>
        </svg>
        <p v-if="selected" role="status">
          {{ format(selected.start) }}–{{ format(selected.end) }} {{ unit }} （上界{{
            selected.upper_inclusive ? '包含' : '不含'
          }}）：{{ selected.count }} 条
        </p>
      </template>
      <p class="small muted">
        比较 {{ summary.sample_count }} 条其他有效 AC，按记录数计，不按人数计。 最近最多
        {{ data.window_limit }} 条{{ summary.window_truncated ? '（已截断）' : '' }}； 快照
        {{ data.as_of
        }}<template v-if="summary.oldest_sample_at">，最早 {{ summary.oldest_sample_at }}</template
        >。
      </p>
    </template>
  </section>
</template>
<style scoped>
.performance-distribution {
  padding: 20px;
  border: 1px solid var(--el-border-color);
  border-radius: 14px;
  margin: 20px 0;
}
.performance-heading {
  display: flex;
  gap: 12px;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
}
.performance-heading h2 {
  display: flex;
  gap: 8px;
  align-items: center;
}
.performance-metrics {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.performance-metrics button {
  padding: 10px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
  background: var(--el-bg-color);
  color: var(--el-text-color-primary);
  cursor: pointer;
}
.performance-metrics button[aria-pressed='true'] {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
}
svg {
  width: 100%;
  max-height: 290px;
}
svg text {
  fill: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
