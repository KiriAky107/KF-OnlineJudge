<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import { errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import type { components } from '../api/generated/schema'
import { chartPosition, formatMeasurement } from '../lib/performance'
import { displayTime } from '../lib/presentation'
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
const metrics = {
  cpu: { label: '运行 CPU 时间', icon: 'clock', unit: 'ms', rawUnit: 'cpu_microseconds' },
  memory: { label: '运行内存峰值', icon: 'monitor', unit: 'MiB', rawUnit: 'bytes' },
} as const
type PerformanceMetric = components['schemas']['PerformanceMetric']
const ceiling = computed(() => {
  const value = summary.value
  if (!value?.available) return 100
  const peak = Math.max(...value.buckets.map((bucket) => bucket.count))
  return Math.max(5, Math.ceil((peak / value.sample_count) * 20) * 5)
})
const verticalTicks = computed(() => [0, ceiling.value / 2, ceiling.value])
const horizontalTicks = computed(() => [
  ...new Set([0n, 1n, 2n, 3n, 4n].map((point) => String((BigInt(end.value) * point) / 4n))),
])
const marker = computed(() => 54 + chartPosition(summary.value?.current ?? '0', end.value) * 548)
const reasons = {
  not_accepted: '本次没有最终通过成绩，无法比较。',
  measurement_missing: '本次缺少该指标的可信测量。',
  environment_unverified: '这条历史成绩未绑定可验证的测量环境。',
  insufficient_samples: '同组其他有效通过记录不足 5 条。',
}
function format(value: string | null, key = metric.value) {
  return formatMeasurement(value, metrics[key].rawUnit)
}
function betterThan(value: PerformanceMetric) {
  return ((value.strictly_slower_count! / value.sample_count) * 100).toFixed(1)
}
function barHeight(count: number, sampleCount: number) {
  return ((count * 100) / (sampleCount * ceiling.value)) * 152
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
    aria-label="运行表现排行"
    data-testid="submission-performance"
  >
    <header class="performance-heading">
      <div>
        <h2><UiIcon name="rankings" />运行表现排行</h2>
        <p>与同题目版本、语言、工具链和测量环境的有效通过记录比较</p>
      </div>
      <el-button size="small" :loading="busy" @click="load">刷新排行</el-button>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="busy" role="status">正在读取评测统计…</p>
    <template v-if="data && summary">
      <div class="performance-metrics" role="group" aria-label="选择分布指标">
        <button
          v-for="(item, key) in metrics"
          :key="key"
          type="button"
          :aria-pressed="metric === key"
          @click="metric = key"
        >
          <span class="metric-label"><UiIcon :name="item.icon" />{{ item.label }}</span>
          <strong
            :data-testid="metric === key ? 'performance-current' : undefined"
            :data-raw-value="data[key].current"
          >
            {{ format(data[key].current, key) }}
            <small v-if="data[key].current !== null">{{ item.unit }}</small>
          </strong>
          <span
            v-if="data[key].available"
            class="metric-percentile"
            :data-testid="metric === key ? 'performance-summary' : undefined"
            :data-sample-count="data[key].sample_count"
            :data-greater="data[key].strictly_slower_count"
            :data-tied="data[key].tied_count"
          >
            优于 {{ betterThan(data[key]) }}% 的记录
          </span>
          <span v-else class="metric-unavailable">暂无可比排行</span>
        </button>
      </div>
      <p v-if="!summary.available" class="performance-unavailable" role="status">
        {{ reasons[summary.reason!] }}
      </p>
      <template v-else>
        <div class="performance-chart">
          <svg viewBox="0 0 640 216" role="group" :aria-label="metrics[metric].label + '分布'">
            <g v-for="(tick, index) in verticalTicks" :key="tick" aria-hidden="true">
              <line
                x1="54"
                x2="602"
                :y1="178 - index * 76"
                :y2="178 - index * 76"
                class="chart-gridline"
              />
              <text x="45" :y="182 - index * 76" text-anchor="end">{{ tick }}%</text>
            </g>
            <template v-for="(bucket, index) in summary.buckets" :key="index">
              <rect
                v-if="bucket.count"
                tabindex="0"
                class="chart-bucket"
                :class="{ 'chart-bucket--active': index === active }"
                :x="54 + chartPosition(bucket.start, end) * 548"
                :width="
                  Math.max(
                    1,
                    (chartPosition(bucket.end, end) - chartPosition(bucket.start, end)) * 548 - 2,
                  )
                "
                :y="178 - barHeight(bucket.count, summary.sample_count)"
                :height="barHeight(bucket.count, summary.sample_count)"
                @mouseenter="active = index"
                @focus="active = index"
              >
                <title>
                  {{ format(bucket.start) }}–{{ format(bucket.end) }} {{ unit }}，{{ bucket.count }}
                  条
                </title>
              </rect>
            </template>
            <line :x1="marker" :x2="marker" y1="22" y2="181" class="chart-marker" />
            <circle :cx="marker" cy="20" r="4" class="chart-marker-dot" />
            <text
              v-for="tick in horizontalTicks"
              :key="tick"
              :x="54 + chartPosition(tick, end) * 548"
              y="200"
              :text-anchor="tick === '0' ? 'start' : tick === end ? 'end' : 'middle'"
            >
              {{ format(tick) }} {{ unit }}
            </text>
          </svg>
          <p class="chart-caption" role="status">
            <template v-if="selected">
              {{ format(selected.start) }}–{{ format(selected.end) }} {{ unit }}（上界{{
                selected.upper_inclusive ? '包含' : '不含'
              }}） · {{ selected.count }} 条 ·
              {{ ((selected.count / summary.sample_count) * 100).toFixed(1) }}%
            </template>
            <template v-else
              ><span class="current-dot"></span> 本次提交 · {{ format(summary.current) }}
              {{ unit }}</template
            >
          </p>
        </div>
      </template>
      <p class="small muted comparison-note">
        参与比较 {{ summary.sample_count }} 条记录<template v-if="summary.available"
          >，其中 {{ summary.tied_count }} 条与本次相同</template
        >。 最近最多 {{ data.window_limit }} 条{{ summary.window_truncated ? '（已截断）' : '' }}。
        快照 {{ displayTime(data.as_of) }}。
      </p>
    </template>
  </section>
</template>
<style scoped>
.performance-distribution {
  padding: clamp(14px, 2.4vw, 22px);
  border: 1px solid var(--el-border-color);
  border-radius: 14px;
  background: var(--el-bg-color, #fff);
}
.performance-heading {
  display: flex;
  gap: 10px;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
}
.performance-heading h2,
.metric-label {
  display: flex;
  gap: 8px;
  align-items: center;
}
.performance-heading h2 {
  font-size: 17px;
}
.performance-heading p {
  color: var(--el-text-color-regular);
  font-size: 12px;
}
.performance-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(9rem, 100%), 1fr));
  gap: 10px;
  margin-top: 14px;
}
.performance-metrics button {
  display: grid;
  gap: 5px;
  text-align: start;
  padding: 11px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-primary);
}
.performance-metrics button[aria-pressed='true'] {
  border-color: #87b5e0;
  background: var(--accent-bg);
}
.performance-metrics button strong {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.metric-label,
.metric-percentile,
.metric-unavailable,
.chart-caption {
  font-size: 12px;
}
.metric-percentile {
  color: #166348;
  font-weight: 650;
}
.metric-unavailable {
  color: var(--el-text-color-regular);
}
.performance-chart,
.performance-unavailable {
  margin-top: 12px;
}
.performance-chart svg {
  display: block;
  width: 100%;
  max-height: 248px;
}
.performance-chart text {
  fill: var(--el-text-color-regular);
  font-size: 12px;
}
.chart-gridline {
  stroke: var(--el-border-color);
}
.chart-bucket {
  fill: var(--el-color-primary);
}
.chart-bucket--active {
  fill: #2467ac;
}
.chart-marker {
  stroke: var(--success-ink);
  stroke-dasharray: 4 3;
  stroke-width: 2;
  pointer-events: none;
}
.chart-marker-dot,
.current-dot {
  fill: var(--success-ink);
  background: var(--success-ink);
}
.current-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.comparison-note {
  margin-top: 7px;
}
</style>
