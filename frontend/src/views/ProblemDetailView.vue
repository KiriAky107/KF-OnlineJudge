<script setup lang="ts">
import { sample } from '../lib/publicSample'
import { displayRuntimeVersion } from '../lib/presentation'
import {
  editorThemePresets,
  savedEditorTheme,
  type EditorAppearance,
  type EditorThemePreset,
} from '../lib/editorHighlighting'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { submissionIntent, type SubmissionInput } from '../api/submissionIntent'
import { activeJudgementPolling, boundedPoll } from '../api/polling'
import type { components } from '../api/generated/schema'
import { useSession } from '../composables/useSession'
import { useCodeDraft, codeTemplates, languageLabels } from '../composables/useCodeDraft'
import { languageCapability, preparationName } from '../composables/judgingLanguage'
import CodeEditor from '../components/CodeEditor.vue'
import EmptyState from '../components/EmptyState.vue'
import UiIcon from '../components/UiIcon.vue'
import ProblemSubmissionList from '../components/ProblemSubmissionList.vue'
import SubmissionInspector from '../components/SubmissionInspector.vue'
import SelfTestPanel from '../components/SelfTestPanel.vue'
import StatementMarkdown from '../components/StatementMarkdown.vue'
import JudgementStatus from '../components/JudgementStatus.vue'
const route = useRoute()
const router = useRouter()
const id = String(route.params.id)
const problem = ref<components['schemas']['ProblemDetail'] | null>(null)
const { language, code, state, save } = useCodeDraft(id)
const session = useSession()
const statementTab = ref<'statement' | 'submissions'>('statement')
const workbench = ref<'editor' | 'submission'>('editor')
const selectedSubmission = ref<string | null>(null)
const detailPane = ref<HTMLDivElement>()
function selectSubmission(id: string) {
  selectedSubmission.value = id
  workbench.value = 'submission'
  void nextTick(() => detailPane.value?.focus())
}
const preferences = savedEditorTheme()
const themePreset = ref<EditorThemePreset>(preferences.preset)
const appearance = ref<EditorAppearance>(preferences.appearance)
const theme = computed(() => editorThemePresets[themePreset.value][appearance.value])
watch([themePreset, appearance], ([preset, mode]) => {
  try {
    localStorage.setItem('kf-editor-theme', `${preset}:${mode}`)
  } catch {
    // Keep the active selection for this page.
  }
})
const fontSize = ref(14)
const error = ref('')
const submitError = ref('')
const busy = ref(false)
type Submission = components['schemas']['SubmissionDetail']
type RecentSubmission = Pick<Submission, 'submission_id' | 'state' | 'language' | 'verdict'>
const recentSubmission = ref<RecentSubmission | null>(null)
const solved = ref(false)
const submissionFinished = (value: RecentSubmission) =>
  ['finished', 'failed', 'cancelled'].includes(value.state)
const followingSubmission = ref(false)
const submissionNotice = ref('')
let submissionPoll: ReturnType<typeof boundedPoll<Submission>> | undefined
let statusRequest: AbortController | undefined
const loading = ref(true)
const testingOpen = ref(false)
const settingsOpen = ref(false)
const split = ref(50)
const testingSplit = ref(40)
const workspace = ref<HTMLDivElement>()
const editorBody = ref<HTMLDivElement>()
const request = new AbortController()
let alive = true
const intents = new Map<string, ReturnType<typeof submissionIntent>>()
const capability = computed(() => languageCapability(problem.value, language.value))
const wireLanguage = computed(() => capability.value?.wire ?? null)
const supported = computed(() => capability.value !== null)
const executionUnavailable = computed(() => problem.value?.execution_available === false)
const runLimits = computed(() => capability.value?.runLimits)
const draftText = computed(
  () =>
    ({
      initial: '本地草稿',
      saved: '草稿已保存',
      changed: '正在保存…',
      unavailable: '浏览器无法保存草稿',
    })[state.value],
)
let stopDrag: (() => void) | undefined
function resizeTesting(value: number) {
  testingSplit.value = Math.max(25, Math.min(75, value))
}
function beginDrag(event: PointerEvent, testing = false) {
  const container = testing ? editorBody.value : workspace.value
  if (!container || event.button !== 0 || !event.isPrimary) return
  event.preventDefault()
  const splitter = event.currentTarget as HTMLElement
  const bounds = container.getBoundingClientRect()
  const start = testingSplit.value
  const usableHeight = bounds.height - splitter.offsetHeight
  function move(e: PointerEvent) {
    if (testing) resizeTesting(start + ((event.clientY - e.clientY) / usableHeight) * 100)
    else split.value = Math.max(30, Math.min(70, ((e.clientX - bounds.left) / bounds.width) * 100))
  }
  function stop() {
    splitter.removeEventListener('pointermove', move)
    splitter.removeEventListener('lostpointercapture', stop)
    if (splitter.hasPointerCapture(event.pointerId)) splitter.releasePointerCapture(event.pointerId)
    stopDrag = undefined
  }
  stopDrag?.()
  splitter.focus()
  splitter.setPointerCapture(event.pointerId)
  splitter.addEventListener('pointermove', move)
  splitter.addEventListener('lostpointercapture', stop)
  stopDrag = stop
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = unwrap(
      await api.GET('/api/v1/problems/versions/{version_id}', {
        params: { path: { version_id: id } },
        signal: request.signal,
      }),
    )
    if (alive) problem.value = data
  } catch (cause) {
    if (alive) error.value = errorText(cause)
  } finally {
    if (alive) loading.value = false
  }
}
async function resetCode() {
  try {
    await ElMessageBox.confirm('将当前语言的草稿恢复为初始模板？', '重置代码', {
      confirmButtonText: '重置',
      cancelButtonText: '保留草稿',
    })
    code.value = codeTemplates[language.value]
  } catch {
    /* kept */
  }
}
function downloadCode() {
  const url = URL.createObjectURL(new Blob([code.value], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download =
    language.value === 'java'
      ? 'Main.java'
      : language.value === 'python'
        ? 'main.py'
        : 'solution-' + id + '.' + { c: 'c', cpp: 'cpp', rust: 'rs' }[language.value]
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function copySample(value: string) {
  try {
    await navigator.clipboard.writeText(sample(value))
    ElMessage.success('已复制样例')
  } catch {
    ElMessage.info('请选中样例文本复制')
  }
}
function submissionVisibility() {
  submissionPoll?.visible(document.visibilityState === 'visible' && workbench.value === 'editor')
}
watch(workbench, submissionVisibility)
async function restoreSubmissionStatus(versionId: string) {
  statusRequest?.abort()
  const controller = new AbortController()
  statusRequest = controller
  submissionNotice.value = ''
  try {
    const status = unwrap(
      await api.GET('/api/v1/submissions/problem-status/{problem_version_id}', {
        params: { path: { problem_version_id: versionId } },
        signal: controller.signal,
      }),
    )
    if (controller.signal.aborted) return
    solved.value ||= status.solved
    if (status.latest && !recentSubmission.value) {
      recentSubmission.value = status.latest
      if (!submissionFinished(status.latest)) followSubmission(status.latest.submission_id)
    }
  } catch (cause) {
    if (!controller.signal.aborted && !recentSubmission.value) {
      submissionNotice.value = errorText(cause)
    }
  }
}
function followSubmission(submissionId: string) {
  submissionPoll?.dispose()
  followingSubmission.value = true
  submissionNotice.value = ''
  submissionPoll = boundedPoll({
    ...activeJudgementPolling,
    load: async (signal) =>
      unwrap(
        await api.GET('/api/v1/submissions/{submission_id}', {
          params: { path: { submission_id: submissionId } },
          signal,
        }),
      ),
    terminal: submissionFinished,
    result: (value) => {
      recentSubmission.value = value
      solved.value ||= value.state === 'finished' && value.verdict === 'accepted'
      followingSubmission.value = !submissionFinished(value)
    },
    error: (cause) => {
      followingSubmission.value = false
      submissionNotice.value = errorText(cause)
    },
    exhausted: () => {
      followingSubmission.value = false
      submissionNotice.value = '自动查询已停止，可手动刷新。'
    },
  })
  submissionVisibility()
  submissionPoll.start()
}
watch([() => session.user.value?.id, () => problem.value?.version_id], ([owner, versionId]) => {
  selectedSubmission.value = null
  workbench.value = 'editor'
  statusRequest?.abort()
  submissionPoll?.dispose()
  recentSubmission.value = null
  solved.value = false
  followingSubmission.value = false
  submissionNotice.value = ''
  if (owner && versionId) void restoreSubmissionStatus(versionId)
})
async function submit() {
  if (
    busy.value ||
    followingSubmission.value ||
    executionUnavailable.value ||
    !problem.value ||
    !supported.value ||
    !wireLanguage.value
  )
    return
  save()
  busy.value = true
  submitError.value = ''
  try {
    if (!(await session.refresh())) {
      await router.push({ path: '/login', query: { next: route.fullPath } })
      return
    }
    const owner = session.user.value!.id
    const input: SubmissionInput = {
      problem_version_id: problem.value.version_id,
      language: wireLanguage.value,
      source: code.value,
    }
    save()
    let intent = intents.get(owner)
    if (!intent) {
      intent = submissionIntent(localStorage, owner)
      intents.set(owner, intent)
    }
    const submissionId = await intent.submit(
      input,
      async (key) =>
        unwrap(
          await api.POST('/api/v1/submissions', {
            body: { ...input, idempotency_key: key },
            headers: csrfHeaders(),
            signal: request.signal,
          }),
        ).submission_id,
    )
    if (alive && session.user.value?.id === owner) {
      recentSubmission.value = {
        submission_id: submissionId,
        language: input.language,
        state: 'queued',
        verdict: null,
      }
      followSubmission(submissionId)
    }
  } catch (cause) {
    if (alive) submitError.value = errorText(cause)
  } finally {
    if (alive) busy.value = false
  }
}
onMounted(() => {
  document.addEventListener('visibilitychange', submissionVisibility)
  void load()
})
onBeforeUnmount(() => {
  alive = false
  document.removeEventListener('visibilitychange', submissionVisibility)
  statusRequest?.abort()
  submissionPoll?.dispose()
  request.abort()
  stopDrag?.()
})
</script>
<template>
  <div
    v-if="problem"
    ref="workspace"
    class="problem-workspace"
    :style="{ '--problem-split': split + '%' }"
  >
    <section class="panel statement-panel">
      <div class="statement-content">
        <nav class="workspace-tabs" aria-label="题目内容">
          <el-button
            :aria-pressed="statementTab === 'statement'"
            @click="statementTab = 'statement'"
            >题面</el-button
          >
          <el-button
            :aria-pressed="statementTab === 'submissions'"
            @click="statementTab = 'submissions'"
            >本题我的提交</el-button
          >
        </nav>
        <ProblemSubmissionList
          v-if="statementTab === 'submissions'"
          :problem-version-id="id"
          @select="selectSubmission"
        />
        <div v-show="statementTab === 'statement'">
          <div class="problem-title-row">
            <div>
              <p class="small muted mono">{{ problem.slug }} · 版本 {{ problem.revision }}</p>
              <h1>{{ problem.title }}</h1>
            </div>
          </div>
          <div class="problem-limits">
            <span>CPU 限制：{{ runLimits ? runLimits.cpu_time_us / 1000 + ' ms' : '未提供' }}</span
            ><span
              >墙钟限制：{{ runLimits ? runLimits.wall_time_us / 1000 + ' ms' : '未提供' }}</span
            ><span
              >内存限制：{{
                runLimits ? runLimits.memory_bytes / 1048576 + ' MiB' : '未提供'
              }}</span
            ><span>精确字节比较</span>
          </div>
          <div class="detail-prose">
            <h2>题目描述</h2>
            <StatementMarkdown :source="problem.statement" />
            <div v-for="(item, index) in problem.samples" :key="index" class="sample-grid">
              <div>
                <h2>
                  样例输入 {{ index + 1 }}
                  <el-button link @click="copySample(item.input)">复制</el-button>
                </h2>
                <pre class="sample-block">{{ sample(item.input) }}</pre>
              </div>
              <div>
                <h2>
                  样例输出 {{ index + 1 }}
                  <el-button link @click="copySample(item.output)">复制</el-button>
                </h2>
                <pre class="sample-block">{{ sample(item.output) }}</pre>
              </div>
            </div>
            <h2>提交说明</h2>
            <p>
              使用标准输入 / 标准输出。支持语言：{{
                problem.languages.join(' / ')
              }}。隐藏测试数据不会公开。
            </p>
            <RouterLink to="/submissions" class="text-link">查看个人提交记录</RouterLink>
          </div>
        </div>
      </div>
    </section>
    <button
      class="pane-splitter"
      role="separator"
      aria-label="调整题面与编辑器宽度"
      aria-orientation="vertical"
      :aria-valuenow="Math.round(split)"
      aria-valuemin="30"
      aria-valuemax="70"
      @pointerdown="beginDrag($event)"
      @keydown.left.prevent="split = Math.max(30, split - 2)"
      @keydown.right.prevent="split = Math.min(70, split + 2)"
    />
    <section class="panel workbench-panel">
      <nav class="workspace-tabs" aria-label="工作台">
        <el-button :aria-pressed="workbench === 'editor'" @click="workbench = 'editor'">
          {{ workbench === 'editor' ? '代码编辑器' : '返回代码编辑器' }}
        </el-button>
        <el-button :aria-pressed="workbench === 'submission'" @click="workbench = 'submission'"
          >提交详情</el-button
        >
      </nav>
      <div v-show="workbench === 'editor'" class="editor-panel">
        <header class="editor-toolbar">
          <span class="small muted">语言</span
          ><el-select v-model="language" aria-label="编程语言" size="small"
            ><el-option
              v-for="(label, value) in languageLabels"
              :key="value"
              :label="label"
              :value="value" /></el-select
          ><el-button size="small" aria-label="重置代码模板" @click="resetCode"
            ><UiIcon name="refresh" /></el-button
          ><span class="draft-label" aria-live="polite">{{ draftText }}</span
          ><el-button size="small" aria-label="下载代码" @click="downloadCode"
            ><UiIcon name="document"
          /></el-button>
          <el-popover v-model:visible="settingsOpen" trigger="click" width="264">
            <template #reference>
              <el-button size="small" aria-label="编辑器设置"><UiIcon name="settings" /></el-button>
            </template>
            <div class="editor-settings">
              <label>主题预设</label>
              <el-select v-model="themePreset" aria-label="主题预设">
                <el-option
                  v-for="(preset, value) in editorThemePresets"
                  :key="value"
                  :label="preset.label"
                  :value="value"
                />
              </el-select>
              <label>主题外观</label>
              <el-select v-model="appearance" aria-label="主题外观">
                <el-option label="亮色" value="light" />
                <el-option label="暗色" value="dark" />
              </el-select>
              <label>字号</label>
              <el-slider v-model="fontSize" :min="12" :max="22" show-input />
            </div>
          </el-popover>
        </header>
        <div
          ref="editorBody"
          class="editor-body"
          :class="{ 'editor-body--collapsed': !testingOpen }"
          :style="{
            '--code-size': `${100 - testingSplit}fr`,
            '--testing-size': `${testingSplit}fr`,
          }"
        >
          <div class="editor-area">
            <CodeEditor v-model="code" :language="language" :theme="theme" :font-size="fontSize" />
          </div>
          <button
            v-show="testingOpen"
            class="testing-splitter"
            type="button"
            role="separator"
            aria-label="调整自测与提交区高度"
            title="上下拖动调整自测与提交区高度"
            aria-orientation="horizontal"
            :aria-valuenow="Math.round(testingSplit)"
            aria-valuemin="25"
            aria-valuemax="75"
            @pointerdown="beginDrag($event, true)"
            @keydown.up.prevent="resizeTesting(testingSplit + 5)"
            @keydown.down.prevent="resizeTesting(testingSplit - 5)"
            @keydown.home.prevent="resizeTesting(25)"
            @keydown.end.prevent="resizeTesting(75)"
          />
          <div v-show="testingOpen" id="self-test-panel" class="testing-panel">
            <div class="editor-testing">
              <SelfTestPanel
                :problem="problem"
                :language="executionUnavailable ? null : wireLanguage"
                :source="code"
                @run="save"
              />
            </div>
          </div>
        </div>
        <div
          v-if="executionUnavailable || !supported || capability?.configuration || submitError"
          class="workbench-notes"
        >
          <p v-if="executionUnavailable" class="notice-strip" role="status">
            当前站点未启用自测和提交评测。代码草稿仍可编辑和保存。
          </p>
          <p v-if="!supported" class="notice-strip">
            当前题目版本不支持此语言，草稿仍会保存。请选择题目公布的可用语言。
          </p>
          <p
            v-else-if="!executionUnavailable && capability?.configuration"
            class="notice-strip"
            data-testid="language-capability"
          >
            {{ languageLabels[language] }} ·
            {{ displayRuntimeVersion(capability.configuration.runtime_version) }} ·
            {{ preparationName(capability.wire) }}后逐点运行。
            <template v-if="capability.wire === 'java21'">
              默认包 Main.java，入口为 public static void main(String[])。
            </template>
            <template v-else-if="capability.wire === 'python313'">
              单文件 main.py，仅标准库。
            </template>
          </p>
          <p v-if="submitError" role="alert">
            {{ submitError }}
            <RouterLink
              v-if="!session.user.value"
              :to="{ path: '/login', query: { next: route.fullPath } }"
              >重新登录</RouterLink
            >
          </p>
        </div>
        <footer class="editor-buttons" aria-label="评测状态与操作">
          <div class="workbench-status">
            <span v-if="recentSubmission" class="status-label" role="status">
              状态
              <button
                type="button"
                class="status-tag"
                title="查看提交详情和编译器输出"
                @click="selectSubmission(recentSubmission.submission_id)"
              >
                <JudgementStatus
                  :state="recentSubmission.state"
                  :verdict="recentSubmission.verdict"
                  :language="recentSubmission.language"
                />
              </button>
            </span>
            <span
              v-if="solved && recentSubmission?.verdict !== 'accepted'"
              class="solved-badge verdict-ac"
              role="status"
            >
              <span aria-hidden="true">✓</span> 你已通过本题
            </span>
            <span v-if="!recentSubmission && !solved && !submissionNotice" class="small muted">{{
              executionUnavailable
                ? '仅保存草稿'
                : session.user.value
                  ? '提交后评测隐藏测试'
                  : '登录后可提交'
            }}</span>
            <el-button
              v-if="recentSubmission || submissionNotice"
              size="small"
              link
              :disabled="followingSubmission"
              @click="
                recentSubmission
                  ? followSubmission(recentSubmission.submission_id)
                  : restoreSubmissionStatus(problem.version_id)
              "
            >
              刷新状态
            </el-button>
            <span v-if="submissionNotice" class="status-notice" role="status">{{
              submissionNotice
            }}</span>
          </div>
          <el-button size="small" @click="save">保存草稿</el-button>
          <el-button
            size="small"
            :aria-expanded="testingOpen"
            aria-controls="self-test-panel"
            @click="testingOpen = !testingOpen"
          >
            {{ testingOpen ? '收起自测' : '在线自测' }}
          </el-button>
          <el-button
            size="small"
            type="primary"
            :loading="busy"
            :disabled="executionUnavailable || !supported || followingSubmission"
            @click="submit"
            >提交代码</el-button
          >
        </footer>
      </div>
      <div
        v-if="workbench === 'submission'"
        ref="detailPane"
        class="workbench-detail"
        aria-label="提交详情"
        role="region"
        tabindex="-1"
        data-testid="workbench-detail"
      >
        <SubmissionInspector
          v-if="selectedSubmission"
          :submission-id="selectedSubmission"
          :theme="theme"
          embedded
        />
        <p v-else>从“本题我的提交”选择一条记录。</p>
      </div>
    </section>
  </div>
  <EmptyState v-else :title="loading ? '正在加载题目…' : '题目暂时无法读取'" :description="error"
    ><el-button v-if="!loading" @click="load">重试</el-button
    ><RouterLink to="/problems"><el-button>返回题库</el-button></RouterLink></EmptyState
  >
</template>
<style>
.app-main:has(.problem-workspace) {
  width: calc(100% - 48px);
  max-width: 1900px;
  margin: 20px auto;
}
.app-shell:has(.problem-workspace) .app-footer {
  display: none;
}
.problem-workspace {
  display: grid;
  grid-template-columns: minmax(0, var(--problem-split)) 12px minmax(0, 1fr);
  height: calc(100dvh - 117px);
  min-height: 600px;
}
.statement-panel,
.editor-panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-radius: 14px;
}
.statement-tabs {
  padding: 8px 20px 0;
  background: #fff;
  border-bottom: 1px solid #edf0f5;
}
.statement-tabs .el-tabs__header {
  margin: 0;
}
.statement-tabs .el-tabs__active-bar,
.statement-tabs .el-tabs__nav-wrap::after {
  display: none;
}
.statement-tabs .el-tabs__item {
  font-size: 12px;
  height: 36px;
  padding: 0 12px;
  margin: 0 4px 8px 0;
  border-radius: 7px;
}
.statement-tabs .el-tabs__item.is-active {
  background: #edf6ff;
}
.statement-content {
  padding: 30px;
  overflow: auto;
  flex: 1;
}
.problem-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 15px;
}
.problem-title-row h1 {
  font-size: 27px;
  font-weight: 650;
  letter-spacing: -0.6px;
  margin-top: 6px;
}
.problem-limits {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  margin: 20px 0;
  padding: 16px 18px;
  background: #f3f7fc;
  border: 0;
  border-radius: 10px;
  font-size: 12px;
  color: #8b9cb2;
}
.statement-content .detail-prose h2 {
  font-size: 16px;
  color: #455e80;
  border-left: 3px solid #b5d5f7;
  padding-left: 12px;
  margin: 25px 0 12px;
}
.statement-content .detail-prose {
  font-size: 13px;
  line-height: 1.9;
}
.sample-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.sample-grid h2 {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.pane-splitter {
  background: none;
  border: 0;
  cursor: col-resize;
  position: relative;
  padding: 0;
  touch-action: none;
}
.pane-splitter::after {
  content: '';
  width: 3px;
  height: 32px;
  background: #9eb7d7;
  border-radius: 3px;
  position: absolute;
  left: 4px;
  top: 48%;
}
.pane-splitter:hover {
  background: #e0eaff;
}
.editor-toolbar {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
  padding: 12px 18px;
  background: #f8fafd;
  border-bottom: 1px solid #e4ebf4;
  min-height: 61px;
}
.editor-toolbar .el-select {
  width: 120px;
}
.draft-label {
  font-size: 11px;
  color: #9aabc0;
  margin-left: auto;
}
.editor-body {
  display: grid;
  grid-template-rows: minmax(140px, var(--code-size)) 20px minmax(115px, var(--testing-size));
  flex: 1;
  min-height: 0;
}
.editor-body--collapsed {
  grid-template-rows: minmax(140px, 1fr);
}
.editor-area {
  min-height: 0;
  overflow: hidden;
}
.testing-splitter {
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-block: 1px solid var(--el-border-color);
  background: #f3f7fc;
  color: #587799;
  cursor: row-resize;
  touch-action: none;
}
.testing-splitter::after {
  content: '';
  width: 36px;
  height: 4px;
  border-radius: 4px;
  background: currentColor;
}
.testing-splitter:hover,
.testing-splitter:focus-visible {
  background: #e4effd;
  color: #35678f;
}
.testing-splitter:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: -2px;
}
.testing-panel {
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #f8fafd;
}
.editor-testing {
  flex: 1;
  min-height: 0;
}
.test-result-placeholder {
  font-size: 12px;
  color: #93a4ba;
  padding: 16px 0;
  min-height: 90px;
}
.editor-buttons {
  border-top: 1px solid #edf0f5;
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 14px 18px;
  min-height: 55px;
}
.editor-buttons .el-button + .el-button {
  margin-inline-start: 0;
}
.editor-settings {
  display: flex;
  flex-direction: column;
  gap: 12px;
  font-size: 12px;
}
@media (max-width: 960px) {
  .draft-label {
    display: none;
  }
  .statement-content {
    padding: 22px;
  }
  .problem-title-row h1 {
    font-size: 24px;
  }
  .editor-toolbar {
    padding: 10px;
  }
  .editor-buttons {
    padding: 10px;
    gap: 5px;
  }
  .sample-grid {
    grid-template-columns: 1fr;
  }
  .statement-tabs .el-tabs__item {
    padding: 0 10px;
  }
}
@media (max-width: 760px) {
  .problem-workspace {
    display: flex;
    flex-direction: column;
    height: auto;
    gap: 16px;
    min-height: 0;
  }
  .pane-splitter {
    display: none;
  }
  .statement-content {
    overflow: visible;
  }
  .editor-body {
    height: clamp(36rem, 86dvh, 48rem);
    flex: none;
  }
  .editor-buttons {
    flex-wrap: wrap;
  }
  .editor-buttons > .workbench-status {
    width: 100%;
  }
  .sample-grid {
    grid-template-columns: 1fr 1fr;
  }
  .statement-content {
    padding: 22px;
  }
  .app-main:has(.problem-workspace) {
    width: calc(100% - 24px);
    margin-top: 14px;
  }
}
</style>

<style scoped>
.workbench-panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.workspace-tabs {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--el-border-color);
}
.workbench-panel > .editor-panel {
  flex: 1;
  overflow: hidden;
}
.workbench-detail {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px;
}
.workbench-notes {
  flex-shrink: 0;
  padding: 4px 18px;
  font-size: 13px;
}
.workbench-notes .notice-strip {
  margin: 0;
  padding: 5px 10px;
}
.solved-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  width: auto;
  padding: 5px 9px;
  border-radius: 5px;
  background: var(--verdict-background);
  color: var(--verdict-text);
  white-space: nowrap;
  font-size: 12px;
  font-weight: 700;
}
.workbench-status,
.status-label,
.status-tag {
  display: flex;
  align-items: center;
  gap: 6px;
}
.workbench-status {
  flex-wrap: wrap;
  margin-inline-end: auto;
  min-width: 0;
  font-size: 12px;
}
.status-label {
  font-weight: 600;
}
.status-tag {
  border: 0;
  border-radius: 5px;
  padding: 0;
  background: transparent;
}
.status-tag:hover {
  filter: brightness(0.96);
}
.status-tag:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
.status-notice {
  flex-basis: 100%;
}
</style>
