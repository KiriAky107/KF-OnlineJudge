<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import { notifyContestRegistration } from '../composables/useContestAgentAccess'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'

const route = useRoute()
const { user } = useSession()
const key = computed(() => String(route.params.id))
const contest = ref<components['schemas']['ContestSchedule']>()
const confirmed = ref(false)
const acknowledging = ref(false)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const workbenchProblems = ref<components['schemas']['ContestEditorProblem'][]>([])
const workbenchError = ref('')
const ended = computed(() => contest.value && Date.parse(contest.value.ends_at) <= Date.now())
let request = new AbortController()

watch(
  [key, () => user.value?.id],
  async ([contestKey, account], _, cleanup) => {
    request.abort()
    request = new AbortController()
    const current = request
    cleanup(() => current.abort())
    const signal = current.signal
    loading.value = true
    busy.value = false
    error.value = ''
    confirmed.value = false
    acknowledging.value = false
    contest.value = undefined
    try {
      const params = { path: { key: contestKey } }
      const schedule = unwrap(await api.GET('/api/v1/contests/{key}', { params, signal }))
      if (signal.aborted) return
      contest.value = schedule
      if (account) {
        const registration = unwrap(
          await api.GET('/api/v1/contests/{key}/registration', { params, signal }),
        )
        if (!signal.aborted) confirmed.value = registration.confirmed
      }
    } catch (cause) {
      if (!signal.aborted) error.value = errorText(cause)
    } finally {
      if (!signal.aborted) loading.value = false
    }
  },
  { immediate: true },
)

watch([contest, confirmed], ([schedule, joined], _, cleanup) => {
  workbenchProblems.value = []
  workbenchError.value = ''
  if (!schedule || !joined) return
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  cleanup(() => {
    controller.abort()
    clearTimeout(timer)
  })
  async function load() {
    if (!schedule || controller.signal.aborted) return
    const delay = Date.parse(schedule.starts_at) - Date.now()
    if (delay > 0) {
      timer = setTimeout(load, Math.min(delay + 250, 2_147_483_647))
      return
    }
    try {
      const rows = unwrap(
        await api.GET('/api/v1/contests/{key}/editor-problems', {
          params: { path: { key: schedule.key } },
          signal: controller.signal,
        }),
      )
      if (!controller.signal.aborted) workbenchProblems.value = rows
    } catch (cause) {
      if (!controller.signal.aborted) workbenchError.value = errorText(cause)
    }
  }
  void load()
})

async function confirm() {
  if (!acknowledging.value || busy.value) return
  const { signal } = request
  busy.value = true
  error.value = ''
  try {
    const registration = unwrap(
      await api.PUT('/api/v1/contests/{key}/registration', {
        params: { path: { key: key.value } },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (signal.aborted) return
    confirmed.value = registration.confirmed
    acknowledging.value = false
    if (registration.confirmed) notifyContestRegistration()
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}
</script>

<template>
  <PageHeading :title="contest?.title ?? '比赛'">
    <RouterLink class="el-button" to="/contests">返回比赛列表</RouterLink>
  </PageHeading>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="loading" role="status">正在读取比赛…</p>
  <PanelCard v-else-if="contest" title="比赛时间与参赛确认" icon="contests">
    <dl class="details-list">
      <div>
        <dt>开始时间</dt>
        <dd>{{ displayTime(contest.starts_at) }}</dd>
      </div>
      <div>
        <dt>结束时间</dt>
        <dd>{{ displayTime(contest.ends_at) }}</dd>
      </div>
    </dl>
    <p>确认参赛后，上述时段内暂时停用此账号在全站的学习助手。比赛结束后自动恢复。</p>
    <p v-if="contest.semantic_completion_enabled === false">比赛期间，比赛题目仅提供基础补全。</p>
    <p v-if="confirmed" role="status"><el-tag type="success">已确认参赛</el-tag></p>
    <p v-else-if="ended">比赛已结束。</p>
    <RouterLink v-else-if="!user" :to="{ path: '/login', query: { next: route.fullPath } }">
      登录后参赛
    </RouterLink>
    <form v-else class="stack native-form" @submit.prevent="confirm">
      <label><input v-model="acknowledging" type="checkbox" /> 我确认参加这场比赛</label>
      <div>
        <el-button type="primary" native-type="submit" :disabled="!acknowledging" :loading="busy">
          确认参赛
        </el-button>
      </div>
    </form>
  </PanelCard>
  <p v-if="workbenchError" role="alert">{{ workbenchError }}</p>
  <PanelCard v-if="workbenchProblems.length" title="比赛题目" icon="problems">
    <ol>
      <li v-for="item in workbenchProblems" :key="item.version_id">
        <RouterLink :to="'/problems/' + item.version_id">{{ item.title }}</RouterLink>
      </li>
    </ol>
  </PanelCard>
</template>
