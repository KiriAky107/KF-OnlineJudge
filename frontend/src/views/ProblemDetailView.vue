<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { submissionIntent, type SubmissionInput } from '../api/submissionIntent'
import type { components } from '../api/generated/schema'
import { useSession } from '../composables/useSession'
import { useCodeDraft, codeTemplates, languageLabels } from '../composables/useCodeDraft'
import { languageCapability, preparationName } from '../composables/judgingLanguage'
import CodeEditor from '../components/CodeEditor.vue'
import EmptyState from '../components/EmptyState.vue'
import UiIcon from '../components/UiIcon.vue'
const route = useRoute()
const router = useRouter()
const id = String(route.params.id)
const problem = ref<components['schemas']['ProblemDetail'] | null>(null)
const { language, code, state, save } = useCodeDraft(id)
const session = useSession()
const theme = ref('vs')
const fontSize = ref(14)
const plain = ref(false)
const error = ref('')
const submitError = ref('')
const busy = ref(false)
const loading = ref(true)
const testInput = ref('')
const settingsOpen = ref(false)
const split = ref(50)
const workspace = ref<HTMLDivElement>()
const request = new AbortController()
let alive = true
const intents = new Map<string, ReturnType<typeof submissionIntent>>()
const capability = computed(() => languageCapability(problem.value, language.value))
const wireLanguage = computed(() => capability.value?.wire ?? null)
const supported = computed(() => capability.value !== null)
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
function beginDrag(event: PointerEvent) {
  if (!workspace.value || event.button !== 0) return
  event.preventDefault()
  const bounds = workspace.value.getBoundingClientRect()
  function move(e: PointerEvent) {
    split.value = Math.max(30, Math.min(70, ((e.clientX - bounds.left) / bounds.width) * 100))
  }
  function stop() {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stop)
    stopDrag = undefined
  }
  stopDrag?.()
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', stop, { once: true })
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
function sample(value: string) {
  const bytes = Uint8Array.from(atob(value), (c) => c.charCodeAt(0))
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return (
      '字节（十六进制）：' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join(' ')
    )
  }
}
async function copySample(value: string) {
  try {
    await navigator.clipboard.writeText(sample(value))
    ElMessage.success('已复制样例')
  } catch {
    ElMessage.info('请选中样例文本复制')
  }
}
async function submit() {
  if (busy.value || !problem.value || !supported.value || !wireLanguage.value) return
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
    if (alive && session.user.value?.id === owner) await router.push('/submissions/' + submissionId)
  } catch (cause) {
    if (alive) submitError.value = errorText(cause)
  } finally {
    if (alive) busy.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => {
  alive = false
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
        <div class="problem-title-row">
          <div>
            <p class="small muted mono">{{ problem.slug }} · 版本 {{ problem.revision }}</p>
            <h1>{{ problem.title }}</h1>
          </div>
        </div>
        <div class="problem-limits">
          <span>CPU 限制：{{ runLimits ? runLimits.cpu_time_us / 1000 + ' ms' : '未提供' }}</span
          ><span>墙钟限制：{{ runLimits ? runLimits.wall_time_us / 1000 + ' ms' : '未提供' }}</span
          ><span
            >内存限制：{{ runLimits ? runLimits.memory_bytes / 1048576 + ' MiB' : '未提供' }}</span
          ><span>精确字节比较</span>
        </div>
        <div class="detail-prose">
          <h2>题目描述</h2>
          <pre style="white-space: pre-wrap">{{ problem.statement }}</pre>
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
    </section>
    <button
      class="pane-splitter"
      role="separator"
      aria-label="调整题面与编辑器宽度"
      aria-orientation="vertical"
      :aria-valuenow="Math.round(split)"
      aria-valuemin="30"
      aria-valuemax="70"
      @pointerdown="beginDrag"
      @keydown.left.prevent="split = Math.max(30, split - 2)"
      @keydown.right.prevent="split = Math.min(70, split + 2)"
    />
    <section class="panel editor-panel">
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
        <el-button size="small" @click="plain = !plain">{{
          plain ? '代码编辑器' : '纯文本编辑'
        }}</el-button>
        <el-popover v-model:visible="settingsOpen" trigger="click" width="240"
          ><template #reference
            ><el-button size="small" aria-label="编辑器设置"><UiIcon name="settings" /></el-button
          ></template>
          <div class="editor-settings">
            <label>主题</label
            ><el-select v-model="theme" aria-label="编辑器主题"
              ><el-option label="浅色" value="vs" /><el-option
                label="深色"
                value="vs-dark" /></el-select
            ><label>字号</label><el-slider v-model="fontSize" :min="12" :max="22" show-input /></div
        ></el-popover>
      </header>
      <div class="editor-area">
        <textarea
          v-if="plain"
          v-model="code"
          aria-label="源码"
          style="
            width: 100%;
            height: 100%;
            min-height: 320px;
            padding: 18px;
            font-family: monospace;
            resize: none;
          "
          spellcheck="false"
        /><CodeEditor
          v-else
          v-model="code"
          :language="language"
          :theme="theme"
          :font-size="fontSize"
        />
      </div>
      <div class="editor-testing">
        <p v-if="!supported" class="notice-strip">
          当前题目版本不支持此语言，草稿仍会保存。请选择题目公布的可用语言。
        </p>
        <p
          v-else-if="capability?.configuration"
          class="notice-strip"
          data-testid="language-capability"
        >
          {{ languageLabels[language] }} · {{ capability.configuration.runtime_version }} ·
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
        <el-input
          v-model="testInput"
          type="textarea"
          :rows="3"
          aria-label="自测输入"
          placeholder="自测能力暂未开放，输入不会随提交发送"
        />
      </div>
      <footer class="editor-buttons">
        <span class="small muted">{{
          session.user.value ? '提交后评测隐藏测试' : '登录后可提交'
        }}</span
        ><el-button size="small" @click="save">保存草稿</el-button
        ><el-button size="small" type="success" plain disabled>自测运行</el-button
        ><el-button
          size="small"
          type="primary"
          :loading="busy"
          :disabled="!supported"
          @click="submit"
          >提交代码</el-button
        >
      </footer>
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
.editor-area {
  flex: 1;
  min-height: 320px;
  overflow: hidden;
}
.editor-testing {
  padding: 6px 18px 15px;
  background: #f8fafd;
  border-top: 1px solid #e4ebf4;
  min-height: 161px;
}
.editor-testing .el-tabs__header {
  margin-bottom: 12px;
}
.editor-testing .el-tabs__item {
  font-size: 12px;
  height: 38px;
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
  gap: 8px;
  align-items: center;
  padding: 14px 18px;
  min-height: 55px;
}
.editor-buttons > span {
  margin-right: auto;
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
  .editor-buttons > span {
    font-size: 10px;
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
  .editor-area {
    height: 470px;
    flex: none;
  }
  .editor-panel {
    min-height: 700px;
  }
  .editor-buttons {
    flex-wrap: wrap;
  }
  .editor-buttons > span {
    width: 100%;
    font-size: 11px;
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
