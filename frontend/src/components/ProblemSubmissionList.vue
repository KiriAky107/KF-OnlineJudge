<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import { errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import type { components } from '../api/generated/schema'
import JudgementStatus from './JudgementStatus.vue'
import { displayTime } from '../lib/presentation'

const props = defineProps<{ problemVersionId: string }>()
const emit = defineEmits<{ select: [id: string] }>()
const session = useSession()
const language = ref<components['schemas']['SubmissionSummary']['language'] | ''>('')
const page = ref<components['schemas']['SubmissionPage'] | null>(null)
const error = ref('')
const busy = ref(false)
const cursors = ref<(string | undefined)[]>([undefined])
let request: AbortController | undefined
let generation = 0
async function load() {
  request?.abort()
  const current = ++generation
  const controller = new AbortController()
  request = controller
  page.value = null
  error.value = ''
  busy.value = true
  if (!session.user.value) {
    error.value = '登录后可读取本题我的提交。'
    busy.value = false
    return
  }
  try {
    const value = unwrap(
      await api.GET('/api/v1/submissions', {
        params: {
          query: {
            limit: 20,
            after: cursors.value.at(-1),
            problem_version_id: props.problemVersionId,
            language: language.value || undefined,
          },
        },
        signal: controller.signal,
      }),
    )
    if (current === generation) page.value = value
  } catch (cause) {
    if (current === generation) error.value = errorText(cause)
  } finally {
    if (current === generation) busy.value = false
  }
}
function next() {
  if (page.value?.next_after) {
    cursors.value.push(page.value.next_after)
    void load()
  }
}
function previous() {
  if (cursors.value.length > 1) {
    cursors.value.pop()
    void load()
  }
}
watch(
  [() => props.problemVersionId, language, () => session.user.value?.id],
  () => {
    cursors.value = [undefined]
    void load()
  },
  { immediate: true, flush: 'sync' },
)
onBeforeUnmount(() => {
  generation++
  request?.abort()
})
</script>
<template>
  <section aria-label="本题我的提交" data-testid="problem-my-submissions">
    <h2>本题我的提交</h2>
    <p>当前不可变题目版本，仅显示自己的记录。</p>
    <div class="submission-list-controls">
      <label
        >语言
        <select v-model="language" aria-label="提交语言筛选">
          <option value="">全部语言</option>
          <option value="c17">C17</option>
          <option value="cpp20">C++20</option>
          <option value="python313">Python 3.13</option>
          <option value="java21">Java 21</option>
        </select></label
      >
      <el-button :loading="busy" @click="load">刷新列表</el-button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="page && !page.items.length">还没有符合筛选的提交。</p>
    <ol v-if="page" class="my-submission-list">
      <li v-for="item in page.items" :key="item.submission_id">
        <button
          class="my-submission-row"
          :data-submission-id="item.submission_id"
          @click="emit('select', item.submission_id)"
        >
          <JudgementStatus :state="item.state" :verdict="item.verdict" :language="item.language" />
          <span>{{ item.language }}</span
          ><time :datetime="item.created_at">{{ displayTime(item.created_at) }}</time
          ><span class="submission-row-action">查看详情 →</span>
        </button>
      </li>
    </ol>
    <div class="submission-list-controls">
      <el-button :disabled="busy || cursors.length <= 1" @click="previous">上一页</el-button>
      <el-button :disabled="busy || !page?.next_after" @click="next">下一页</el-button>
    </div>
  </section>
</template>
<style scoped>
.submission-list-controls,
.submission-list-controls label {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.submission-list-controls {
  margin: 16px 0;
}
.submission-list-controls select {
  min-height: 36px;
  padding: 6px 10px;
  border: 1px solid var(--el-border-color);
  border-radius: 7px;
  color: var(--el-text-color-primary);
  background: var(--el-bg-color, #fff);
  font: inherit;
}
.submission-row-action {
  margin-left: auto;
  color: #2878ca;
}
.my-submission-row:hover {
  background: #f0f7ff;
  border-color: #a8cff8;
}
:is(.my-submission-row, select):focus-visible {
  outline: 2px solid #409eff;
  outline-offset: 3px;
}
.my-submission-list {
  padding: 0;
  list-style: none;
}
.my-submission-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  width: 100%;
  padding: 14px;
  border: 1px solid var(--el-border-color);
  background: var(--el-bg-color);
  color: var(--el-text-color-primary);
  text-align: left;
  cursor: pointer;
  border-radius: 10px;
  margin: 8px 0;
  overflow-wrap: anywhere;
}
</style>
