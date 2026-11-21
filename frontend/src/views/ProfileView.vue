<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { errorText, unwrap } from '../api/transport'
import type { components } from '../api/generated/schema'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import EmptyState from '../components/EmptyState.vue'
import JudgementStatus from '../components/JudgementStatus.vue'
import PanelCard from '../components/PanelCard.vue'

const route = useRoute()
const session = useSession()
const owner = computed(() => {
  const user = session.user.value
  return user?.username === route.params.username ? user : null
})
const page = ref<components['schemas']['SubmissionPage'] | null>(null)
const titles = ref<Record<string, string>>({})
const error = ref('')
const limit = 100
const languages = { c17: 'C17', cpp20: 'C++20', python313: 'Python 3.13', java21: 'Java 21' }
const records = computed(() => page.value?.items ?? [])
const judged = computed(() =>
  records.value.filter((item) => item.state === 'finished' && item.verdict !== null),
)
const accepted = computed(() => judged.value.filter((item) => item.verdict === 'accepted'))
const solved = computed(() => [...new Set(accepted.value.map((item) => item.problem_version_id))])
const rate = computed(() =>
  judged.value.length
    ? new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 }).format(
        accepted.value.length / judged.value.length,
      )
    : '—',
)
const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' })
const dayKey = (date: Date) => [date.getFullYear(), date.getMonth(), date.getDate()].join('-')
const activity = computed(() => {
  const counts = new Map<string, number>()
  for (const row of records.value) {
    const day = dayKey(new Date(row.created_at))
    counts.set(day, (counts.get(day) ?? 0) + 1)
  }
  const today = new Date()
  return Array.from({ length: 28 }, (_, index) => {
    const date = new Date(today)
    date.setDate(today.getDate() - 27 + index)
    return {
      key: dayKey(date),
      label: dateFormat.format(date),
      count: counts.get(dayKey(date)) ?? 0,
    }
  })
})
function problemTitle(version: string) {
  return titles.value[version] ?? `题目版本 ${version.slice(0, 8)}`
}

watch(
  owner,
  async (user, _, onCleanup) => {
    page.value = null
    titles.value = {}
    error.value = ''
    if (!user) return

    const request = new AbortController()
    onCleanup(() => request.abort())
    const { signal } = request
    try {
      const result = unwrap(
        await api.GET('/api/v1/submissions', { params: { query: { limit } }, signal }),
      )
      if (signal.aborted) return
      page.value = result
      if (result.items.length) {
        const catalog = await api.GET('/api/v1/problems', {
          params: { query: { limit } },
          signal,
        })
        if (!signal.aborted && catalog.data) {
          titles.value = Object.fromEntries(
            catalog.data.items.map((item) => [item.version_id, item.title]),
          )
        }
      }
    } catch (cause) {
      if (!signal.aborted && page.value === null) error.value = errorText(cause)
    }
  },
  { immediate: true },
)
</script>

<template>
  <template v-if="owner">
    <div class="profile-banner">
      <span class="avatar profile-avatar" aria-hidden="true">{{
        owner.username[0]?.toUpperCase()
      }}</span>
      <div>
        <p>个人主页</p>
        <h1>{{ owner.username }}</h1>
        <span v-if="owner.capabilities?.problem_maintenance" class="tag">管理员</span>
      </div>
      <div class="profile-actions">
        <RouterLink class="el-button" to="/trainings">我的训练</RouterLink>
        <RouterLink class="el-button el-button--primary" to="/submissions">全部提交</RouterLink>
      </div>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!page" role="status">正在读取提交记录…</p>
    <template v-else>
      <p v-if="page.next_after" class="small muted">统计范围为最近 {{ limit }} 条提交。</p>
      <div class="summary-grid profile-stats">
        <div class="summary-card">
          <p>提交数</p>
          <strong>{{ records.length }}</strong>
        </div>
        <div class="summary-card">
          <p>通过的题目版本</p>
          <strong>{{ solved.length }}</strong>
        </div>
        <div class="summary-card">
          <p>已判提交通过率</p>
          <strong>{{ rate }}</strong>
        </div>
      </div>
      <div class="content-with-aside">
        <div class="stack">
          <PanelCard title="最近提交" icon="submissions">
            <el-table :data="records.slice(0, 8)" empty-text="暂无提交">
              <el-table-column label="题目" min-width="140">
                <template #default="{ row }">
                  <RouterLink :to="'/submissions/' + row.submission_id" class="text-link">
                    {{ problemTitle(row.problem_version_id) }}
                  </RouterLink>
                </template>
              </el-table-column>
              <el-table-column label="语言" width="105">
                <template #default="{ row }">{{
                  languages[row.language as keyof typeof languages]
                }}</template>
              </el-table-column>
              <el-table-column label="状态" min-width="95">
                <template #default="{ row }">
                  <JudgementStatus
                    :state="row.state"
                    :verdict="row.verdict"
                    :language="row.language"
                  />
                </template>
              </el-table-column>
              <el-table-column label="提交时间" min-width="155">
                <template #default="{ row }">
                  <time :datetime="row.created_at">{{ displayTime(row.created_at) }}</time>
                </template>
              </el-table-column>
            </el-table>
          </PanelCard>
        </div>
        <div class="stack">
          <PanelCard title="最近 28 天提交活动" icon="rankings">
            <div class="heatmap profile-activity" role="group" aria-label="最近 28 天每日提交次数">
              <span
                v-for="day in activity"
                :key="day.key"
                class="heat-cell"
                :data-level="Math.min(day.count, 4)"
                :title="`${day.label} · ${day.count} 次提交`"
                :aria-label="`${day.label} · ${day.count} 次提交`"
              />
            </div>
            <div class="detail-meta">
              <span>{{ activity[0]?.label }}</span
              ><span>{{ activity.at(-1)?.label }}</span>
            </div>
          </PanelCard>
          <PanelCard title="通过的题目版本" icon="problems">
            <p v-if="!solved.length" class="small muted">暂无通过记录</p>
            <ul v-else class="solved-versions">
              <li v-for="version in solved" :key="version">
                <RouterLink :to="'/problems/' + version" class="text-link">{{
                  problemTitle(version)
                }}</RouterLink>
              </li>
            </ul>
          </PanelCard>
        </div>
      </div>
    </template>
  </template>
  <EmptyState v-else title="公开个人资料尚未开放">
    <RouterLink
      v-if="session.user.value"
      :to="{ name: 'profile', params: { username: session.user.value.username } }"
      class="el-button"
    >
      返回我的主页
    </RouterLink>
  </EmptyState>
</template>

<style scoped>
.profile-avatar {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary-dark-2);
}
.profile-stats {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.profile-activity {
  grid-template-columns: repeat(7, minmax(0, 1fr));
}
.solved-versions {
  display: grid;
  gap: 8px;
  list-style: none;
  padding: 0;
  margin: 0;
}
@media (max-width: 560px) {
  .profile-stats {
    grid-template-columns: 1fr;
  }
}
</style>
