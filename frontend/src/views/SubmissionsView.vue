<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { ApiError, errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import { boundedPoll } from '../api/polling'
import type { components } from '../api/generated/schema'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import JudgementStatus from '../components/JudgementStatus.vue'
const page = ref<components['schemas']['SubmissionPage'] | null>(null)
const current = ref<string | undefined>()
const previous = ref<(string | undefined)[]>([])
const error = ref('')
const notice = ref('')
let poll: ReturnType<typeof boundedPoll<components['schemas']['SubmissionPage']>> | undefined
function load(after?: string) {
  poll?.dispose()
  current.value = after
  error.value = ''
  notice.value = ''
  poll = boundedPoll({
    load: async (signal) =>
      unwrap(
        await api.GET('/api/v1/submissions', { params: { query: { after, limit: 20 } }, signal }),
      ),
    terminal: (value) =>
      value.items.every((s) => ['finished', 'failed', 'cancelled'].includes(s.state)),
    result: (value) => {
      page.value = value
    },
    error: (cause) => {
      error.value = errorText(cause)
      if (cause instanceof ApiError && cause.status === 401) page.value = null
    },
    exhausted: () => {
      notice.value = '自动查询已停止，请手动刷新查看最新状态。'
    },
  })
  visibility()
  poll.start()
}
function visibility() {
  poll?.visible(document.visibilityState === 'visible')
}
function forward() {
  if (!page.value?.next_after) return
  previous.value.push(current.value)
  load(page.value.next_after)
}
function back() {
  load(previous.value.pop())
}
watch(
  () => useSession().user.value?.id,
  (owner, previousOwner) => {
    if (previousOwner && owner !== previousOwner) {
      poll?.dispose()
      page.value = null
      error.value = '会话已改变，请重新登录。'
    }
  },
)
onMounted(() => {
  document.addEventListener('visibilitychange', visibility)
  load()
})
onBeforeUnmount(() => {
  poll?.dispose()
  document.removeEventListener('visibilitychange', visibility)
})
</script>
<template>
  <PageHeading
    title="个人提交"
    eyebrow="SUBMISSIONS"
    description="仅显示当前登录账号的持久化提交记录。"
  />
  <PanelCard title="我的提交" icon="submissions"
    ><p v-if="error" role="alert">{{ error }} <RouterLink to="/login">重新登录</RouterLink></p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <el-table :data="page?.items ?? []" empty-text="当前页没有提交">
      <el-table-column label="提交" min-width="230"
        ><template #default="{ row }"
          ><RouterLink :to="'/submissions/' + row.submission_id" class="text-link">{{
            row.submission_id
          }}</RouterLink></template
        ></el-table-column
      >
      <el-table-column prop="language" label="语言" width="100" /><el-table-column
        label="状态"
        min-width="130"
        ><template #default="{ row }"
          ><JudgementStatus
            :state="row.state"
            :verdict="row.verdict"
            :language="row.language" /></template></el-table-column
      ><el-table-column prop="created_at" label="提交时间" min-width="180" />
    </el-table>
    <div class="pagination-row">
      <el-button @click="load(current)">刷新列表</el-button
      ><el-button :disabled="!previous.length" @click="back">上一页</el-button
      ><el-button :disabled="!page?.next_after" @click="forward">下一页</el-button>
    </div>
  </PanelCard>
</template>
