<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { findProblem, discussions, submissions } from '../data/preview'
import { useCodeDraft, codeTemplates, languageLabels } from '../composables/useCodeDraft'
import CodeEditor from '../components/CodeEditor.vue'
import DifficultyTag from '../components/DifficultyTag.vue'
import VerdictTag from '../components/VerdictTag.vue'
import EmptyState from '../components/EmptyState.vue'
import UiIcon from '../components/UiIcon.vue'
const route = useRoute()
const id = String(route.params.id)
const problem = computed(() => findProblem(id))
const { language, code, state, save } = useCodeDraft(id)
const theme = ref('vs')
const fontSize = ref(14)
const tab = ref('description')
const testTab = ref('input')
const testInput = ref(problem.value?.sampleInput ?? '')
const settingsOpen = ref(false)
const split = ref(50)
const workspace = ref<HTMLDivElement>()
const codeRelated = computed(() => submissions.filter((s) => s.problemId === id))
const relatedDiscussion = computed(() => discussions.filter((d) => d.problemId === id))
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
onBeforeUnmount(() => stopDrag?.())
async function resetCode() {
  try {
    await ElMessageBox.confirm('将当前语言的草稿恢复为初始模板？', '重置代码', {
      confirmButtonText: '重置',
      cancelButtonText: '保留草稿',
      type: 'info',
    })
    code.value = codeTemplates[language.value]
  } catch {
    /* User kept their draft. */
  }
}
function downloadCode() {
  const extensions = { cpp: 'cpp', python: 'py', rust: 'rs' }
  const url = URL.createObjectURL(new Blob([code.value], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'solution-' + id + '.' + extensions[language.value]
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function copySample(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('已复制样例')
  } catch {
    ElMessage.info('浏览器暂不支持复制，请选中样例文本复制')
  }
}
</script>
<template>
  <div
    v-if="problem"
    ref="workspace"
    class="problem-workspace"
    :style="{ '--problem-split': split + '%' }"
  >
    <section class="panel statement-panel">
      <el-tabs v-model="tab" class="statement-tabs"
        ><el-tab-pane label="题目描述" name="description" /><el-tab-pane
          label="提交记录"
          name="submissions" /><el-tab-pane label="题解与讨论" name="discussions" /><el-tab-pane
          label="AI 答疑"
          name="ai"
      /></el-tabs>
      <div class="statement-content">
        <template v-if="tab === 'description'"
          ><div class="problem-title-row">
            <div>
              <p class="small muted mono">题号 {{ problem.id }}</p>
              <h1>{{ problem.title }}</h1>
            </div>
            <DifficultyTag :value="problem.difficulty" />
          </div>
          <div class="tag-list">
            <span v-for="tag in problem.tags" :key="tag" class="tag">{{ tag }}</span
            ><span class="tag">示例题面</span>
          </div>
          <div class="problem-limits">
            <span>时间限制：1000 ms</span><span>内存限制：256 MiB</span
            ><span>标准输入 / 标准输出</span>
          </div>
          <div class="detail-prose">
            <h2>题目描述</h2>
            <p>{{ problem.description }}</p>
            <h2>输入格式</h2>
            <p>{{ problem.input }}</p>
            <h2>输出格式</h2>
            <p>{{ problem.output }}</p>
            <template v-if="problem.sampleInput"
              ><div class="sample-grid">
                <div>
                  <h2>
                    样例输入
                    <el-button link size="small" @click="copySample(problem.sampleInput)"
                      >复制</el-button
                    >
                  </h2>
                  <pre class="sample-block">{{ problem.sampleInput }}</pre>
                </div>
                <div>
                  <h2>
                    样例输出
                    <el-button link size="small" @click="copySample(problem.sampleOutput)"
                      >复制</el-button
                    >
                  </h2>
                  <pre class="sample-block">{{ problem.sampleOutput }}</pre>
                </div>
              </div></template
            >
            <h2>数据范围与提示</h2>
            <p>{{ problem.constraint }}</p>
            <h2>提交说明</h2>
            <p>
              使用标准输入读取数据，只输出题目要求的结果。你可以在右侧编写代码，并在下方准备自测输入。
            </p>
          </div>
        </template>
        <template v-else-if="tab === 'submissions'"
          ><p class="notice-strip">以下为本题的示例评测记录。</p>
          <el-table :data="codeRelated" empty-text="暂无本题的示例记录"
            ><el-table-column label="提交编号" prop="id" width="100" /><el-table-column
              label="用户"
              prop="username"
              min-width="110"
            /><el-table-column label="结果" width="100"
              ><template #default="{ row }"
                ><VerdictTag :value="row.verdict" /></template></el-table-column
            ><el-table-column label="详情" width="80"
              ><template #default="{ row }"
                ><RouterLink :to="'/submissions/' + row.id" class="text-link"
                  >查看</RouterLink
                ></template
              ></el-table-column
            ></el-table
          ></template
        >
        <template v-else-if="tab === 'discussions'"
          ><article
            v-for="discussion in relatedDiscussion"
            :key="discussion.id"
            class="discussion-item"
          >
            <div>
              <h2>
                <RouterLink :to="'/discussions/' + discussion.id">{{
                  discussion.title
                }}</RouterLink>
              </h2>
              <p>{{ discussion.author }} · {{ discussion.date }} · 示例文章</p>
            </div>
          </article>
          <EmptyState v-if="!relatedDiscussion.length" title="暂无本题讨论" /><RouterLink
            to="/discussions"
            class="text-link"
            >进入讨论社区 →</RouterLink
          ></template
        >
        <EmptyState
          v-else
          title="AI 答疑待接入"
          description="后续结合题目、代码和提交记录，提供逐步提示与针对性分析。"
        />
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
        ><el-tooltip content="重置模板"
          ><el-button size="small" aria-label="重置代码模板" @click="resetCode"
            ><UiIcon name="refresh" /></el-button></el-tooltip
        ><span class="draft-label" aria-live="polite">{{ draftText }}</span
        ><el-button size="small" aria-label="下载代码" @click="downloadCode"
          ><UiIcon name="document" /></el-button
        ><el-popover v-model:visible="settingsOpen" trigger="click" width="240"
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
        <CodeEditor v-model="code" :language="language" :theme="theme" :font-size="fontSize" />
      </div>
      <div class="editor-testing">
        <el-tabs v-model="testTab"
          ><el-tab-pane label="自测输入" name="input"
            ><el-input
              v-model="testInput"
              type="textarea"
              :rows="3"
              resize="none"
              placeholder="输入自测数据"
              aria-label="自测输入"
            /><el-button
              v-if="problem.sampleInput"
              link
              type="primary"
              size="small"
              @click="testInput = problem.sampleInput"
              >填入题目样例</el-button
            ></el-tab-pane
          ><el-tab-pane label="运行结果" name="result"
            ><p class="test-result-placeholder">
              尚未运行。判题服务接入后展示标准输出、错误信息与资源使用情况。
            </p></el-tab-pane
          ></el-tabs
        >
      </div>
      <footer class="editor-buttons">
        <span class="small muted">评测服务待接入</span
        ><el-button size="small" @click="save">保存草稿</el-button
        ><el-button size="small" type="success" plain disabled>自测运行</el-button
        ><el-button size="small" type="primary" disabled>提交代码</el-button>
      </footer>
    </section>
  </div>
  <EmptyState v-else title="题目不存在"
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
