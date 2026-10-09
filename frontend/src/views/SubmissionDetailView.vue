<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import { boundedPoll } from '../api/polling'
import type { components } from '../api/generated/schema'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import JudgementStatus from '../components/JudgementStatus.vue'
import { preparationName } from '../composables/judgingLanguage'
const route = useRoute()
const id = String(route.params.id)
const submission = ref<components['schemas']['SubmissionDetail'] | null>(null)
const error = ref('')
const notice = ref('')
const source = ref<string | null>(null)
const sourceError = ref('')
const cancelling = ref(false)
const sourceBusy = ref(false)
const request = new AbortController()
let alive = true
const terminal = computed(
  () => !!submission.value && ['finished', 'failed', 'cancelled'].includes(submission.value.state),
)
const poll = boundedPoll({
  load: async (signal) =>
    unwrap(
      await api.GET('/api/v1/submissions/{submission_id}', {
        params: { path: { submission_id: id } },
        signal,
      }),
    ),
  terminal: (s) => ['finished', 'failed', 'cancelled'].includes(s.state),
  result: (s) => {
    submission.value = s
    error.value = ''
  },
  error: (cause) => {
    error.value = errorText(cause)
    if (cause instanceof ApiError && cause.status === 401) {
      submission.value = null
      source.value = null
    }
  },
  exhausted: () => {
    notice.value = '自动查询已停止。评测仍可继续，点击刷新可查看最新状态。'
  },
})
function refresh() {
  error.value = ''
  notice.value = ''
  poll.start()
}
function visibility() {
  poll.visible(document.visibilityState === 'visible')
}
async function cancel() {
  if (cancelling.value || terminal.value) return
  cancelling.value = true
  error.value = ''
  try {
    unwrap(
      await api.POST('/api/v1/submissions/{submission_id}/cancel', {
        params: { path: { submission_id: id } },
        headers: csrfHeaders(),
        signal: request.signal,
      }),
    )
    if (alive) {
      notice.value = '取消已受理，等待执行停止与清理确认。'
      poll.start()
    }
  } catch (cause) {
    if (alive) error.value = errorText(cause)
  } finally {
    if (alive) cancelling.value = false
  }
}
async function readSource() {
  if (sourceBusy.value) return
  sourceBusy.value = true
  sourceError.value = ''
  try {
    const value = unwrap(
      await api.GET('/api/v1/submissions/{submission_id}/source', {
        params: { path: { submission_id: id } },
        signal: request.signal,
      }),
    )
    if (alive) source.value = value.source
  } catch (cause) {
    if (alive) sourceError.value = errorText(cause)
  } finally {
    if (alive) sourceBusy.value = false
  }
}
watch(
  () => useSession().user.value?.id,
  (owner, previousOwner) => {
    if (previousOwner && owner !== previousOwner) {
      poll.dispose()
      submission.value = null
      source.value = null
      error.value = '会话已改变，请重新登录。'
    }
  },
)
onMounted(() => {
  visibility()
  document.addEventListener('visibilitychange', visibility)
  poll.start()
})
onBeforeUnmount(() => {
  alive = false
  poll.dispose()
  request.abort()
  document.removeEventListener('visibilitychange', visibility)
})
</script>
<template>
  <PageHeading :title="'提交 ' + id" description="个人提交的持久化评测状态"
    ><JudgementStatus
      v-if="submission"
      :state="submission.state"
      :verdict="submission.verdict"
      :language="submission.language"
  /></PageHeading>
  <p v-if="error" role="alert">
    {{ error }}
    <RouterLink :to="{ path: '/login', query: { next: route.fullPath } }">重新登录</RouterLink>
  </p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <div class="filters">
    <el-button @click="refresh">刷新状态</el-button
    ><el-button v-if="submission && !terminal" :loading="cancelling" @click="cancel"
      >取消评测</el-button
    ><RouterLink to="/submissions"><el-button>个人提交</el-button></RouterLink>
  </div>
  <template v-if="submission">
    <div class="summary-grid">
      <div class="summary-card">
        <p>运行 CPU 时间</p>
        <strong
          >{{ submission.result?.cpu_time_us == null ? '—' : submission.result.cpu_time_us / 1000 }}
          <small v-if="submission.result?.cpu_time_us != null">ms</small></strong
        >
      </div>
      <div class="summary-card">
        <p>运行内存峰值</p>
        <strong
          >{{
            submission.result?.memory_peak_bytes == null
              ? '—'
              : (submission.result.memory_peak_bytes / 1048576).toFixed(2)
          }}
          <small v-if="submission.result?.memory_peak_bytes != null">MiB</small></strong
        >
      </div>
      <div class="summary-card">
        <p>语言</p>
        <strong>{{ submission.language }}</strong
        ><small>尝试 {{ submission.attempts }} 次</small>
      </div>
      <div class="summary-card">
        <p>提交时间</p>
        <small>{{ submission.created_at }}</small
        ><RouterLink :to="'/problems/' + submission.problem_version_id"
          >返回提交时的题目版本</RouterLink
        >
      </div>
    </div>
    <PanelCard title="评测详情" icon="submissions">
      <p v-if="submission.failure_code" role="alert">系统原因：{{ submission.failure_code }}</p>
      <template v-if="submission.result">
        <p>
          {{ preparationName(submission.language) }}状态：{{ submission.result.compilation_status }}
        </p>
        <pre
          v-if="submission.result.diagnostics"
          class="sample-block"
          data-testid="compiler-diagnostics"
          >{{ submission.result.diagnostics }}</pre>
        <p v-if="submission.result.diagnostics_truncated">
          {{ preparationName(submission.language) }}诊断已截断。
        </p>
        <p v-if="!submission.result.cleanup_complete" role="alert">
          资源清理未确认，服务保留该任务并隔离容量。
        </p>
        <el-table :data="submission.result.cases" empty-text="没有运行测试点"
          ><el-table-column label="测试点"
            ><template #default="{ row }">{{ row.position + 1 }}</template></el-table-column
          ><el-table-column prop="verdict" label="结果" /><el-table-column label="CPU ms"
            ><template #default="{ row }">{{
              row.cpu_time_us == null ? '—' : row.cpu_time_us / 1000
            }}</template></el-table-column
          ><el-table-column label="内存 MiB"
            ><template #default="{ row }">{{
              row.memory_peak_bytes == null ? '—' : (row.memory_peak_bytes / 1048576).toFixed(2)
            }}</template></el-table-column
          ></el-table
        >
      </template>
      <p v-else>
        {{ terminal ? '没有执行报告；终态以持久化状态为准。' : '尚无完整成绩，请等待评测。' }}
      </p>
      <el-button :loading="sourceBusy" @click="readSource">查看我的源码</el-button>
      <p v-if="sourceError" role="alert">{{ sourceError }}</p>
      <pre v-if="source !== null" class="sample-block">{{ source }}</pre>
    </PanelCard>
  </template>
  <p v-else-if="!error">正在读取提交…</p>
</template>
