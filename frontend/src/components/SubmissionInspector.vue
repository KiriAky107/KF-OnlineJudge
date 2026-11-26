<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import { activeJudgementPolling, boundedPoll } from '../api/polling'
import type { components } from '../api/generated/schema'
import PanelCard from './PanelCard.vue'
import JudgementStatus from './JudgementStatus.vue'
import SubmissionPerformance from './SubmissionPerformance.vue'
import SubmissionReview from './SubmissionReview.vue'
import { editorLanguages, preparationName } from '../composables/judgingLanguage'
import { displayTime } from '../lib/presentation'
import { formatMeasurement } from '../lib/performance'
import { useSiteTheme } from '../composables/useSiteTheme'
import { editorThemePresets, savedEditorTheme, type EditorTheme } from '../lib/editorHighlighting'
import { languageLabels } from '../composables/useCodeDraft'
const CodeEditor = defineAsyncComponent(() => import('./CodeEditor.vue'))
const props = defineProps<{ submissionId: string; embedded?: boolean; theme?: EditorTheme }>()
const choices = savedEditorTheme()
const { theme: siteTheme } = useSiteTheme()
const sourceTheme = computed(
  () =>
    props.theme ??
    editorThemePresets[choices.preset][
      choices.appearance === 'site' ? siteTheme.value : choices.appearance
    ],
)
const compilationLabels: Record<components['schemas']['CompilationStatus'], string> = {
  succeeded: '成功',
  compilation_error: '失败',
  compilation_limit: '资源超限',
  cancelled: '已取消',
  system_error: '系统错误',
}
const route = useRoute()
const id = computed(() => props.submissionId)
const session = useSession()
const submission = ref<components['schemas']['SubmissionDetail'] | null>(null)
const error = ref('')
const notice = ref('')
const source = ref<string | null>(null)
const sourceError = ref('')
const copyStatus = ref('')
const cancelling = ref(false)
const sourceBusy = ref(false)
let request = new AbortController()
let generation = 0
let poll: ReturnType<typeof boundedPoll<components['schemas']['SubmissionDetail']>> | undefined
const terminal = computed(
  () => !!submission.value && ['finished', 'failed', 'cancelled'].includes(submission.value.state),
)
const cases = computed(() => submission.value?.result?.cases ?? [])
const passedCases = computed(() => cases.value.filter((row) => row.verdict === 'accepted').length)
const sourceHeight = computed(() =>
  Math.min(520, Math.max(144, (source.value?.split('\n').length ?? 1) * 20 + 30)),
)
function cpuTime(value?: number | null) {
  return value == null ? '—' : `${formatMeasurement(value, 'cpu_microseconds')} ms`
}
function memory(value?: number | null) {
  return value == null ? '—' : `${formatMeasurement(value, 'bytes')} MiB`
}
function visibility() {
  poll?.visible(document.visibilityState === 'visible')
}
function reset() {
  const current = ++generation
  request.abort()
  request = new AbortController()
  poll?.dispose()
  submission.value = null
  source.value = null
  error.value = ''
  notice.value = ''
  sourceError.value = ''
  copyStatus.value = ''
  cancelling.value = sourceBusy.value = false
  if (!session.user.value) {
    error.value = '请登录后读取个人提交。'
    return
  }
  const submissionId = props.submissionId
  poll = boundedPoll({
    ...activeJudgementPolling,
    load: async (signal) =>
      unwrap(
        await api.GET('/api/v1/submissions/{submission_id}', {
          params: { path: { submission_id: submissionId } },
          signal,
        }),
      ),
    terminal: (s) => ['finished', 'failed', 'cancelled'].includes(s.state),
    result: (s) => {
      if (current === generation) {
        const firstResult = !submission.value
        submission.value = s
        error.value = ''
        if (firstResult && source.value === null && !sourceError.value) void readSource()
      }
    },
    error: (cause) => {
      if (current === generation) {
        submission.value = null
        if (sourceBusy.value) {
          request.abort()
          request = new AbortController()
        }
        source.value = null
        error.value = errorText(cause)
      }
    },
    exhausted: () => {
      if (current === generation) notice.value = '自动刷新已暂停，可手动刷新状态。'
    },
  })
  visibility()
  poll.start()
}
function refresh() {
  if (!poll) return
  error.value = ''
  notice.value = ''
  poll.start()
}
async function cancel() {
  if (cancelling.value || !submission.value) return
  const current = generation
  cancelling.value = true
  try {
    unwrap(
      await api.POST('/api/v1/submissions/{submission_id}/cancel', {
        params: { path: { submission_id: props.submissionId } },
        headers: csrfHeaders(),
        signal: request.signal,
      }),
    )
    if (current === generation) {
      notice.value = '取消已受理，等待执行停止与清理确认。'
      poll?.start()
    }
  } catch (cause) {
    if (current === generation) error.value = errorText(cause)
  } finally {
    if (current === generation) cancelling.value = false
  }
}
async function readSource() {
  if (sourceBusy.value || !submission.value) return
  const current = generation
  const signal = request.signal
  sourceBusy.value = true
  sourceError.value = ''
  try {
    const value = unwrap(
      await api.GET('/api/v1/submissions/{submission_id}/source', {
        params: { path: { submission_id: props.submissionId } },
        signal,
      }),
    )
    if (current === generation && !signal.aborted) source.value = value.source
  } catch (cause) {
    if (current === generation && !signal.aborted) sourceError.value = errorText(cause)
  } finally {
    if (current === generation) sourceBusy.value = false
  }
}
async function copySource() {
  if (source.value === null) return
  const current = generation
  try {
    await navigator.clipboard.writeText(source.value)
    if (current === generation) copyStatus.value = '已复制源码。'
  } catch {
    if (current === generation) copyStatus.value = '请在源码区域选中并复制。'
  }
}
watch([() => props.submissionId, () => session.user.value?.id], reset, {
  immediate: true,
  flush: 'sync',
})
onMounted(() => document.addEventListener('visibilitychange', visibility))
onBeforeUnmount(() => {
  generation++
  poll?.dispose()
  request.abort()
  document.removeEventListener('visibilitychange', visibility)
})
</script>
<template>
  <article class="submission-layout" aria-label="提交详情">
    <p v-if="error" class="submission-feedback" role="alert">
      {{ error }}
      <RouterLink
        v-if="!session.user.value"
        :to="{ path: '/login', query: { next: route.fullPath } }"
      >
        重新登录
      </RouterLink>
      <el-button v-else size="small" @click="refresh">重试</el-button>
    </p>
    <p v-if="notice" class="submission-feedback" role="status">{{ notice }}</p>

    <template v-if="submission">
      <section class="submission-hero" :data-verdict="submission.verdict">
        <div class="submission-hero-main">
          <div>
            <component :is="embedded ? 'h2' : 'h1'" class="submission-verdict">
              <JudgementStatus
                role="status"
                plain
                :state="submission.state"
                :verdict="submission.verdict"
                :language="submission.language"
              />
            </component>
            <p v-if="cases.length">{{ passedCases }} / {{ cases.length }} 个已执行测试点通过</p>
          </div>
          <div class="submission-actions">
            <el-button size="small" @click="refresh">刷新状态</el-button>
            <el-button v-if="!terminal" size="small" :loading="cancelling" @click="cancel"
              >取消评测</el-button
            >
            <RouterLink
              v-if="embedded"
              class="el-button el-button--small"
              :to="'/submissions/' + id"
            >
              打开完整详情
            </RouterLink>
          </div>
        </div>
        <dl class="submission-facts">
          <div>
            <dt>语言</dt>
            <dd>{{ languageLabels[editorLanguages[submission.language]] }}</dd>
          </div>
          <div>
            <dt>运行 CPU 时间</dt>
            <dd>{{ cpuTime(submission.result?.cpu_time_us) }}</dd>
          </div>
          <div>
            <dt>运行内存峰值</dt>
            <dd>{{ memory(submission.result?.memory_peak_bytes) }}</dd>
          </div>
          <div>
            <dt>提交时间</dt>
            <dd>
              <time :datetime="submission.created_at">{{
                displayTime(submission.created_at)
              }}</time>
            </dd>
          </div>
        </dl>
        <div class="submission-links">
          <RouterLink :to="'/problems/' + submission.problem_version_id">返回题目版本</RouterLink>
          <RouterLink v-if="!embedded" to="/submissions">个人提交</RouterLink>
          <RouterLink
            v-if="!embedded"
            :to="{ path: '/trainings/assistant', query: { submission: id } }"
          >
            针对这次提交答疑
          </RouterLink>
          <RouterLink
            v-if="!embedded"
            :to="{
              path: '/trainings/assistant',
              query: { submission: id, purpose: 'submission_review' },
            }"
          >
            复盘这次提交
          </RouterLink>
        </div>
      </section>

      <section
        v-if="submission.result || (terminal && submission.failure_code)"
        class="submission-compiler"
      >
        <header>
          <h2>编译器返回</h2>
          <span v-if="submission.result">
            {{ preparationName(submission.language) }}状态：{{
              compilationLabels[submission.result.compilation_status]
            }}
          </span>
        </header>
        <p v-if="submission.failure_code" role="alert">系统原因：{{ submission.failure_code }}</p>
        <template v-if="submission.result">
          <pre
            v-if="submission.result.diagnostics"
            class="submission-diagnostics"
            data-testid="compiler-diagnostics"
            >{{ submission.result.diagnostics }}</pre>
          <p v-if="submission.result.diagnostics_truncated">
            {{ preparationName(submission.language) }}诊断已截断。
          </p>
          <p v-if="!submission.result.cleanup_complete" role="alert">
            资源清理未确认，服务保留该任务并隔离容量。
          </p>
        </template>
      </section>
      <p v-else-if="terminal" class="submission-feedback">本次提交没有执行报告。</p>

      <SubmissionPerformance
        v-if="terminal && submission.verdict === 'accepted'"
        :submission-id="id"
      />

      <PanelCard
        v-if="cases.length"
        class="submission-section"
        title="测试点详情"
        :subtitle="'已通过 ' + passedCases + ' / ' + cases.length"
      >
        <ol class="submission-cases" data-testid="submission-cases">
          <li
            v-for="row in cases"
            :key="row.position"
            class="case-tile"
            :data-verdict="row.verdict"
          >
            <span class="case-number">测试点 {{ row.position + 1 }}</span>
            <JudgementStatus state="finished" :verdict="row.verdict" plain />
            <dl class="case-facts">
              <div>
                <dt>CPU</dt>
                <dd>{{ cpuTime(row.cpu_time_us) }}</dd>
              </div>
              <div>
                <dt>内存</dt>
                <dd>{{ memory(row.memory_peak_bytes) }}</dd>
              </div>
            </dl>
          </li>
        </ol>
      </PanelCard>

      <PanelCard
        class="submission-section"
        title="提交源码"
        :subtitle="languageLabels[editorLanguages[submission.language]]"
        data-testid="submission-source"
      >
        <template #actions>
          <el-button v-if="source !== null" size="small" @click="copySource">复制源码</el-button>
        </template>
        <p v-if="copyStatus" role="status">{{ copyStatus }}</p>
        <p v-if="sourceBusy" role="status">正在读取源码…</p>
        <p v-if="sourceError" role="alert">
          {{ sourceError }} <el-button size="small" @click="readSource">重试</el-button>
        </p>
        <SubmissionReview class="source-review" :submission="submission" />
        <div
          v-if="source !== null"
          class="submission-source-code"
          :style="{ height: sourceHeight + 'px' }"
        >
          <CodeEditor
            :model-value="source"
            :language="editorLanguages[submission.language]"
            :theme="sourceTheme"
            :font-size="14"
            read-only
          />
        </div>
      </PanelCard>
    </template>
    <p v-else-if="!error" class="submission-feedback" role="status">正在读取提交…</p>
  </article>
</template>

<style scoped>
.submission-layout {
  display: grid;
  gap: 14px;
  min-width: 0;
}
.submission-hero {
  --summary-accent: var(--accent-ink);
  --summary-surface: var(--accent-bg);
  padding: 17px clamp(16px, 3vw, 28px);
  border: 1px solid color-mix(in srgb, var(--summary-accent) 23%, var(--summary-surface));
  border-radius: 14px;
  background: var(--summary-surface);
  color: var(--summary-accent);
}
.submission-hero[data-verdict='accepted'] {
  --summary-accent: var(--success-ink);
  --summary-surface: var(--success-soft);
}
.submission-hero[data-verdict='compilation_error'] {
  --summary-accent: var(--warn-ink);
  --summary-surface: var(--warn-soft);
}
.submission-hero:is(
  [data-verdict='wrong_answer'],
  [data-verdict='runtime_error'],
  [data-verdict='system_error']
) {
  --summary-accent: var(--danger-ink);
  --summary-surface: var(--danger-soft);
}
.submission-hero-main,
.submission-actions,
.submission-links {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
}
.submission-hero-main {
  justify-content: space-between;
  align-items: flex-start;
}
.submission-verdict {
  margin: 0;
  font-size: clamp(24px, 4vw, 36px);
  line-height: 1.18;
}
.submission-verdict :deep(.verdict) {
  padding: 0;
  font: inherit;
  font-weight: 750;
  white-space: normal;
}
.submission-hero-main p {
  margin-top: 5px;
  font-size: 13px;
  font-weight: 600;
}
.submission-actions :deep(.el-button) {
  margin: 0;
}
.submission-facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 8rem), 1fr));
  gap: 12px;
  margin: 12px 0 9px;
  padding-top: 9px;
  border-top: 1px solid color-mix(in srgb, currentColor 24%, transparent);
}
.submission-facts dt,
.case-facts dt {
  font-size: 12px;
  opacity: 0.88;
}
.submission-facts dd,
.case-facts dd {
  margin: 2px 0 0;
  font-variant-numeric: tabular-nums;
  font-weight: 650;
}
.submission-links {
  font-size: 12px;
}
.submission-links a {
  text-decoration: underline;
  text-underline-offset: 2px;
}
.submission-feedback {
  padding: 10px 12px;
}
.submission-compiler {
  padding: 12px 16px;
  border: 1px solid var(--el-border-color);
  border-radius: 12px;
  background: var(--el-bg-color, #fff);
}
.submission-compiler header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 5px 14px;
}
.submission-compiler h2,
.submission-compiler header span {
  font-size: 14px;
}
.submission-compiler header span {
  color: var(--el-text-color-regular);
}
.submission-diagnostics {
  padding: 12px 14px;
  max-height: 16rem;
  margin: 8px 0 0;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-primary);
}
.submission-cases {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 10rem), 1fr));
  gap: 10px;
  list-style: none;
  padding: 0;
  margin: 0;
}
.case-tile {
  --case-accent: var(--danger-ink);
  --case-surface: var(--danger-soft);
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--case-accent) 26%, var(--case-surface));
  border-radius: 8px;
  background: var(--case-surface);
  color: var(--case-accent);
}
.case-tile[data-verdict='accepted'] {
  --case-accent: var(--success-ink);
  --case-surface: var(--success-soft);
}
.case-tile :deep(.verdict) {
  display: block;
  padding: 4px 0;
  font: inherit;
  font-weight: 700;
  white-space: normal;
}
.case-number,
.case-facts dd {
  font-size: 12px;
}
.case-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin: 5px 0 0;
}
.submission-source-code {
  overflow: hidden;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
}
.source-review {
  margin: 0 0 12px;
}
.source-review :deep(h2) {
  font-size: 14px;
}
</style>
