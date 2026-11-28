<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'
import DisclosureHeading from '../components/DisclosureHeading.vue'
import { displayTime } from '../lib/presentation'

type Observations = components['schemas']['DiagnosticObservations']
type Count = components['schemas']['DiagnosticObservationCount']
type Practice = components['schemas']['PracticeObservations']
const days = ref(30)
const cohort = ref<Observations['cohort']>('all')
const observations = ref<Observations>()
const loading = ref(false)
const message = ref('')
const training = ref<Practice>()
const trainingLoading = ref(false)
const trainingError = ref('')
const methodNames = {
  optimization: '公开样例优化对照',
  ubsan: '公开样例 UBSan',
  differential: '有界对拍',
  boundary: '边界测量',
} satisfies Record<Count['method'], string>
const trainingNames = {
  focused_training: '专项训练',
  periodic_review: '阶段复习',
} satisfies Record<Practice['drafts'][number]['purpose'], string>
const practiceNames = {
  direct: '站内直接创建',
  assistant_saved: '从助手草案保存',
  shared: '官方或团队题单',
} satisfies Record<Practice['practice'][number]['cohort'], string>
const selfTestNames = {
  direct_sample: '无关联诊断的公开样例',
  diagnostic_sample: '从诊断打开的公开样例',
} satisfies Record<Practice['self_tests'][number]['cohort'], string>
const ratio = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
let request = new AbortController()
let trainingRequest = new AbortController()

async function load() {
  request.abort()
  request = new AbortController()
  const { signal } = request
  observations.value = undefined
  message.value = ''
  loading.value = true
  try {
    const result = unwrap(
      await api.GET('/api/v1/admin/learning/diagnostics', {
        params: { query: { days: days.value, cohort: cohort.value } },
        signal,
      }),
    )
    if (!signal.aborted) observations.value = result
  } catch (cause) {
    if (!signal.aborted) message.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

async function loadTraining() {
  trainingRequest.abort()
  trainingRequest = new AbortController()
  const { signal } = trainingRequest
  training.value = undefined
  trainingError.value = ''
  trainingLoading.value = true
  try {
    const result = unwrap(
      await api.GET('/api/v1/admin/learning/practice', {
        params: { query: { days: days.value } },
        signal,
      }),
    )
    if (!signal.aborted) training.value = result
  } catch (cause) {
    if (!signal.aborted) trainingError.value = errorText(cause)
  } finally {
    if (!signal.aborted) trainingLoading.value = false
  }
}

function formatRate(count: number, denominator: number) {
  if (!denominator) return '样本不足'
  return count + ' / ' + denominator + '（' + ratio.format(count / denominator) + '）'
}

function refresh() {
  void load()
  void loadTraining()
}

watch([days, cohort], load, { immediate: true })
watch(days, loadTraining, { immediate: true })
onBeforeUnmount(() => {
  request.abort()
  trainingRequest.abort()
})
</script>

<template>
  <div class="admin-management">
    <header class="admin-page-heading">
      <h1>学习观察</h1>
      <p>查看诊断、题单保存和练习通过记录。</p>
    </header>
    <section class="admin-real-panel observation-panel" aria-label="诊断观察">
      <div class="observation-filters">
        <label>
          时间范围
          <select v-model.number="days">
            <option :value="7">最近 7 天</option>
            <option :value="30">最近 30 天</option>
            <option :value="90">最近 90 天</option>
          </select>
        </label>
        <label>
          复盘关联
          <select v-model="cohort">
            <option value="all">全部诊断</option>
            <option value="linked_review">关联提交复盘</option>
            <option value="unlinked">无关联复盘记录</option>
          </select>
        </label>
        <button type="button" :disabled="loading || trainingLoading" @click="refresh">刷新</button>
      </div>
      <p v-if="loading" role="status">正在读取诊断记录…</p>
      <p v-if="message" role="alert">{{ message }}</p>
      <template v-if="observations">
        <p class="admin-muted">
          {{ displayTime(observations.start_at) }} 至 {{ displayTime(observations.end_at) }}
        </p>
        <div class="observation-table">
          <table>
            <thead>
              <tr>
                <th scope="col">诊断方式</th>
                <th scope="col">结果记录</th>
                <th scope="col">缺配置证据</th>
                <th scope="col">命中 / 可统计样本</th>
                <th scope="col">平均领取至结束</th>
                <th scope="col">最高沙箱时限</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in observations.items" :key="row.method">
                <th scope="row">{{ methodNames[row.method] }}</th>
                <td>{{ row.observed_count }}</td>
                <td>{{ row.untracked_count }}</td>
                <td>{{ formatRate(row.finding_count, row.eligible_count) }}</td>
                <td>
                  {{
                    row.mean_claim_to_finish_ms === null ? '—' : row.mean_claim_to_finish_ms + ' ms'
                  }}
                </td>
                <td>
                  {{
                    row.max_sandbox_budget_us === null
                      ? '—'
                      : row.max_sandbox_budget_us / 1000 + ' ms'
                  }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <details class="metric-definitions">
          <DisclosureHeading icon="rankings" tone="muted">诊断指标口径</DisclosureHeading>
          <p>按已完成的执行记录计数。重复运行各计一条。缺少执行配置的历史记录计入结果总数。</p>
          <ul>
            <li>优化对照纳入 O2 通过或同时取得 O2 与 O0 结果的记录。两种结果不同计为命中。</li>
            <li>UBSan 纳入获得运行结论的记录。返回受信源码位置的已知问题计为命中。</li>
            <li>有界对拍纳入已核验参考和输入的比较。答案不同或较小输入稳定复现差异计为命中。</li>
            <li>边界测量纳入有运行结果的样本。达到资源上限计为命中。</li>
          </ul>
          <p>无关联复盘记录表示该次诊断未绑定站内复盘。独立练习对照需要单独组织和标记。</p>
        </details>
      </template>
    </section>
    <section v-if="training" class="admin-real-panel observation-panel" aria-label="自测观察">
      <h2>公开样例自测</h2>
      <div class="observation-table">
        <table class="training-table">
          <thead>
            <tr>
              <th scope="col">样例来源</th>
              <th scope="col">已完成自测</th>
              <th scope="col">输出差异 / 可比对</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in training.self_tests" :key="row.cohort">
              <th scope="row">{{ selfTestNames[row.cohort] }}</th>
              <td>{{ row.completed_count }}</td>
              <td>{{ formatRate(row.mismatch_count, row.compared_count) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="admin-muted">
        只比较由服务端核验过输入的公开样例。分母要求正常退出且输出完整可比对，输出与题面预期不同计为一次检出。
      </p>
    </section>
    <section class="admin-real-panel observation-panel" aria-label="训练观察">
      <h2>训练观察</h2>
      <p v-if="trainingLoading" role="status">正在读取训练记录…</p>
      <p v-if="trainingError" role="alert">{{ trainingError }}</p>
      <template v-if="training">
        <h3>助手草案保存率</h3>
        <div class="observation-table">
          <table class="training-table">
            <thead>
              <tr>
                <th scope="col">草案类型</th>
                <th scope="col">已保存 / 草案</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in training.drafts" :key="row.purpose">
                <th scope="row">{{ trainingNames[row.purpose] }}</th>
                <td>{{ formatRate(row.saved_count, row.draft_count) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <h3>后续通过率</h3>
        <div class="observation-table">
          <table class="training-table">
            <thead>
              <tr>
                <th scope="col">题单来源</th>
                <th scope="col">可观察的练习题</th>
                <th scope="col">通过 / 已尝试</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in training.practice" :key="row.cohort">
                <th scope="row">{{ practiceNames[row.cohort] }}</th>
                <td>{{ row.exercise_count }}</td>
                <td>{{ formatRate(row.accepted_count, row.attempted_count) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <details class="metric-definitions">
          <DisclosureHeading icon="trainings" tone="muted">训练指标口径</DisclosureHeading>
          <ul>
            <li>保存率统计所选时段内已完成、有题目的助手草案。查题请求不计入草案。</li>
            <li>
              后续通过率统计在所选时段内完成了 {{ training.follow_up_days }} 天观察的练习题。
              每个账号和题目版本只取最早一份题单，练习前已通过的题目排除。
            </li>
            <li>通过率的分母为观察期内有正式评测的练习题。各来源分别展示，因果对照须单独评估。</li>
          </ul>
        </details>
      </template>
    </section>
  </div>
</template>

<style scoped>
.observation-panel {
  justify-items: stretch;
}
.observation-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 0.75rem;
}
.observation-table {
  width: 100%;
  overflow-x: auto;
}
.observation-table table {
  min-width: 46rem;
}
.observation-table .training-table {
  min-width: 25rem;
}
.observation-table tbody th {
  font-weight: 600;
}
.metric-definitions ul {
  padding-inline-start: 1.25rem;
}
</style>
