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
import SubmissionHeatmap from '../components/SubmissionHeatmap.vue'

const route = useRoute()
const session = useSession()
const owner = computed(() => {
  const user = session.user.value
  return user?.username === route.params.username ? user : null
})
const page = ref<components['schemas']['SubmissionPage'] | null>(null)
const titles = ref<Record<string, string>>({})
const error = ref('')
const activity = ref<components['schemas']['SubmissionActivity']>()
const activityError = ref('')
const rating = ref<components['schemas']['ContestRating']>()
const ratingError = ref('')
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
function problemTitle(version: string) {
  return titles.value[version] ?? `题目版本 ${version.slice(0, 8)}`
}

watch(
  owner,
  async (user, _, onCleanup) => {
    page.value = null
    titles.value = {}
    error.value = ''
    activity.value = undefined
    activityError.value = ''
    rating.value = undefined
    ratingError.value = ''
    if (!user) return

    const request = new AbortController()
    onCleanup(() => request.abort())
    const { signal } = request
    const loadRating = api
      .GET('/api/v1/contests/my-rating', { signal })
      .then(unwrap)
      .then((result) => {
        if (!signal.aborted) rating.value = result
      })
      .catch((cause) => {
        if (!signal.aborted) ratingError.value = errorText(cause)
      })
    const loadActivity = api
      .GET('/api/v1/submissions/activity', { signal })
      .then(unwrap)
      .then((result) => {
        if (!signal.aborted) activity.value = result
      })
      .catch((cause) => {
        if (!signal.aborted) activityError.value = errorText(cause)
      })
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
    } finally {
      await Promise.all([loadActivity, loadRating])
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
    <PanelCard class="profile-calendar" title="交题热力图" icon="rankings">
      <SubmissionHeatmap v-if="activity" :activity="activity" />
      <p v-else-if="activityError" role="alert">{{ activityError }}</p>
      <p v-else role="status">正在读取交题活动…</p>
    </PanelCard>
    <PanelCard class="profile-calendar" title="比赛评分" icon="contests">
      <p v-if="ratingError" role="alert">{{ ratingError }}</p>
      <p v-else-if="!rating" role="status">正在读取比赛评分…</p>
      <p v-else-if="rating.rating === null" class="muted">
        未定级 · 完成首场正式计分赛并结算后显示评分。
      </p>
      <template v-else>
        <dl class="rating-summary">
          <div>
            <dt>当前评分</dt>
            <dd>{{ rating.rating }}</dd>
          </div>
          <div>
            <dt>最高评分</dt>
            <dd>{{ rating.highest_rating }}</dd>
          </div>
          <div>
            <dt>计分比赛</dt>
            <dd>{{ rating.rated_contests }}</dd>
          </div>
        </dl>
        <div class="training-table">
          <table>
            <thead>
              <tr>
                <th scope="col">比赛</th>
                <th scope="col">时间</th>
                <th scope="col">名次</th>
                <th scope="col">赛前 → 赛后</th>
                <th scope="col">变化</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="entry in rating.history" :key="entry.contest_key">
                <th scope="row">{{ entry.title }}</th>
                <td>
                  <time :datetime="entry.ended_at">{{ displayTime(entry.ended_at) }}</time>
                </td>
                <td>{{ entry.rank }}</td>
                <td>{{ entry.old_rating }} → {{ entry.new_rating }}</td>
                <td
                  :class="
                    entry.new_rating > entry.old_rating
                      ? 'rating-rise'
                      : entry.new_rating < entry.old_rating
                        ? 'rating-fall'
                        : ''
                  "
                >
                  {{ entry.new_rating > entry.old_rating ? '+' : ''
                  }}{{ entry.new_rating - entry.old_rating }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="rating.rated_contests > rating.history.length" class="small muted">
          显示最近 {{ rating.history.length }} 场。
        </p>
      </template>
    </PanelCard>
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
.profile-calendar {
  margin-block: 0.9rem;
}
.rating-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem 2rem;
  margin: 0 0 0.8rem;
}
.rating-summary dt {
  color: var(--muted);
  font-size: 0.8rem;
}
.rating-summary dd {
  margin: 0.15rem 0 0;
  font-weight: 700;
  font-size: 1.25rem;
  font-variant-numeric: tabular-nums;
}
.rating-rise {
  color: var(--success-ink);
}
.rating-fall {
  color: var(--danger-ink);
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
