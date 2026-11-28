<script setup lang="ts">
import DisclosureHeading from '../components/DisclosureHeading.vue'
import { ElInputNumber, vLoading } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { trainingIntent } from '../api/trainingIntent'
import { trainingRankingLabels, type TrainingRankingProfile } from '../api/trainingRanking'
import type { components } from '../api/generated/schema'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import PageHeading from '../components/PageHeading.vue'
import UiIcon from '../components/UiIcon.vue'
import PanelCard from '../components/PanelCard.vue'
import TrainingFeedbackSource from '../components/TrainingFeedbackSource.vue'
import TrainingGoalCoverage from '../components/TrainingGoalCoverage.vue'
import TrainingPrerequisites from '../components/TrainingPrerequisites.vue'
import TrainingTimingSource from '../components/TrainingTimingSource.vue'
const { user, refresh } = useSession()
const router = useRouter()
const overview = ref<components['schemas']['LearningOverview'] | null>(null)
const page = ref<components['schemas']['TrainingPage'] | null>(null)
const official = ref<components['schemas']['TrainingPublicationPage'] | null>(null)
const officialError = ref('')
const officialRequest = new AbortController()
const search = ref('')
const progressFilter = ref<'all' | 'active' | 'completed'>('all')
const progressFilters = { all: '全部', active: '待完成', completed: '已完成' } as const
const visiblePlans = computed(() => {
  const name = search.value.trim().toLocaleLowerCase()
  return (
    page.value?.items.filter((plan) => {
      const complete = plan.item_count > 0 && plan.completed_count === plan.item_count
      return (
        plan.title.toLocaleLowerCase().includes(name) &&
        (progressFilter.value === 'all' ||
          (progressFilter.value === 'completed' ? complete : !complete))
      )
    }) ?? []
  )
})
const recommendations = ref<components['schemas']['LearningRecommendations'] | null>(null)
const title = ref('个人训练计划')
const tags = ref('')
const coverEachGoal = ref(false)
const difficulty = ref<'' | 'easy' | 'medium' | 'hard'>('')
const rankingProfile = ref<'' | TrainingRankingProfile>('')
const count = ref(5)
const parameters = computed(() => ({
  tags: [
    ...new Set(
      tags.value
        .split(/[,，]/)
        .map((v) => v.trim())
        .filter(Boolean),
    ),
  ],
  difficulty: difficulty.value || null,
  count: count.value,
  cover_each_goal: coverEachGoal.value,
  ranking_profile: rankingProfile.value || null,
}))
const error = ref('')
const loading = ref(false)
const busy = ref(false)
let controller = new AbortController()
let action: AbortController | undefined
function clear() {
  controller.abort()
  action?.abort()
  controller = new AbortController()
  overview.value = null
  page.value = null
  recommendations.value = null
  loading.value = false
  busy.value = false
  error.value = ''
}
async function load() {
  clear()
  if (!user.value) return
  const { signal } = controller
  loading.value = true
  try {
    const [stats, plans] = await Promise.all([
      api.GET('/api/v1/learning/overview', { signal }).then(unwrap),
      api.GET('/api/v1/trainings', { signal }).then(unwrap),
    ])
    if (!signal.aborted) {
      overview.value = stats
      page.value = plans
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
async function recommend() {
  if (busy.value || !user.value) return
  action = new AbortController()
  const { signal } = action
  busy.value = true
  error.value = ''
  recommendations.value = null
  try {
    const result = unwrap(
      await api.POST('/api/v1/learning/recommendations', {
        body: parameters.value,
        signal,
        headers: csrfHeaders(),
      }),
    )
    if (!signal.aborted) recommendations.value = result
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}
async function create() {
  if (busy.value || !user.value || !recommendations.value?.items.length) return
  action = new AbortController()
  const { signal } = action
  const owner = user.value.id
  busy.value = true
  error.value = ''
  try {
    const input = { ...parameters.value, title: title.value.trim() }
    const intent = trainingIntent(localStorage, owner, JSON.stringify(input))
    const result = unwrap(
      await api.POST('/api/v1/trainings', {
        body: { ...input, idempotency_key: intent.key },
        signal,
        headers: csrfHeaders(),
      }),
    )
    if (!signal.aborted) {
      intent.complete()
      await router.push('/trainings/' + result.id)
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}
watch(parameters, () => {
  recommendations.value = null
})
watch(
  () => user.value?.id,
  () => {
    tags.value = ''
    coverEachGoal.value = false
    difficulty.value = ''
    rankingProfile.value = ''
    title.value = '个人训练计划'
    search.value = ''
    progressFilter.value = 'all'
    void load()
  },
  { immediate: true },
)
onMounted(async () => {
  void api
    .GET('/api/v1/official-trainings', { signal: officialRequest.signal })
    .then(unwrap)
    .then((result) => {
      if (!officialRequest.signal.aborted) official.value = result
    })
    .catch((cause) => {
      if (!officialRequest.signal.aborted) officialError.value = errorText(cause)
    })
  if (!user.value) {
    try {
      await refresh()
    } catch (cause) {
      error.value = errorText(cause)
    }
  }
})
onBeforeUnmount(() => {
  officialRequest.abort()
  clear()
})
</script>
<template>
  <PageHeading title="训练集">
    <RouterLink
      v-if="user"
      class="el-button el-button--primary"
      :to="{ name: 'learning-assistant', query: { assistantTab: 'training' } }"
    >
      <UiIcon name="assistant" /> 让助手建训练集
    </RouterLink>
    <RouterLink class="el-button" to="/trainings/assistant"
      ><UiIcon name="assistant" /> 学习助手</RouterLink
    >
  </PageHeading>
  <p v-if="error" role="alert">{{ error }}</p>
  <section
    v-if="official || officialError"
    class="panel training-catalog official-catalog"
    aria-label="官方训练"
  >
    <header><h2>官方训练</h2></header>
    <p v-if="officialError" role="alert">{{ officialError }}</p>
    <div v-else-if="official?.items.length" class="training-table">
      <table>
        <thead>
          <tr>
            <th scope="col">训练名称</th>
            <th scope="col">题数</th>
            <th scope="col">发布时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="plan in official.items" :key="plan.id">
            <th scope="row">
              <RouterLink :to="{ name: 'official-training-detail', params: { id: plan.id } }">
                {{ plan.title }}
              </RouterLink>
            </th>
            <td>{{ plan.item_count }}</td>
            <td>
              <time :datetime="plan.created_at">{{ displayTime(plan.created_at) }}</time>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="muted">暂无官方训练。</p>
    <p v-if="official?.truncated" class="small muted catalog-limit" role="status">
      显示最近 50 个官方训练。
    </p>
  </section>
  <p v-if="!user">
    <RouterLink to="/login?next=/trainings">登录</RouterLink>后查看本人的学习记录与训练计划。
  </p>
  <div v-else v-loading="loading" class="stack">
    <section class="panel training-filters" aria-label="筛选我的训练集">
      <label class="filter-row">
        <span><UiIcon name="search" /> 搜索</span>
        <el-input v-model="search" clearable aria-label="搜索训练集" placeholder="按名称搜索" />
      </label>
      <div class="filter-row">
        <span>进度</span>
        <div class="filter-options">
          <button
            v-for="(label, key) in progressFilters"
            :key="key"
            type="button"
            :aria-pressed="progressFilter === key"
            @click="progressFilter = key"
          >
            {{ label }}
          </button>
        </div>
      </div>
    </section>
    <section class="panel training-catalog" aria-label="我的训练集">
      <header>
        <h2>我的训练集</h2>
        <span aria-live="polite">{{ visiblePlans.length }} 个</span>
      </header>
      <div v-if="visiblePlans.length" class="training-table">
        <table>
          <thead>
            <tr>
              <th scope="col">训练名称</th>
              <th scope="col">权限</th>
              <th scope="col">进度</th>
              <th scope="col">题数</th>
              <th scope="col">创建时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="plan in visiblePlans" :key="plan.id">
              <th scope="row">
                <RouterLink :to="'/trainings/' + plan.id">{{ plan.title }}</RouterLink>
              </th>
              <td><span class="tag">私有</span></td>
              <td>
                <div class="training-list-progress">
                  <progress
                    :value="plan.completed_count"
                    :max="plan.item_count || 1"
                    aria-label="完成进度"
                  />
                  <span>{{ plan.completed_count }} / {{ plan.item_count }}</span>
                </div>
              </td>
              <td>{{ plan.item_count }}</td>
              <td>
                <time :datetime="plan.created_at">{{ displayTime(plan.created_at) }}</time>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <el-empty
        v-else-if="page"
        :description="page.items.length ? '没有匹配的训练集' : '还没有训练集'"
      />
      <p v-if="page?.truncated" class="small muted catalog-limit" role="status">
        显示最近 {{ page.limit }} 个训练集。
      </p>
    </section>
    <details class="training-extra">
      <DisclosureHeading icon="settings" tone="accent">按条件创建</DisclosureHeading>
      <PanelCard title="按目标生成计划" icon="trainings" subtitle="确定性规则推荐，不是模型分析">
        <el-form label-position="top" :disabled="busy">
          <el-form-item label="计划名称"><el-input v-model="title" maxlength="120" /></el-form-item>
          <el-form-item label="目标知识点（逗号分隔，最多 10 个；可留空）"
            ><el-input v-model="tags"
          /></el-form-item>
          <label class="small">
            <input v-model="coverEachGoal" type="checkbox" :disabled="busy" />
            用不同题目覆盖各知识点
          </label>
          <div class="filters">
            <el-select v-model="difficulty" aria-label="目标难度"
              ><el-option label="不限难度" value="" /><el-option
                label="简单"
                value="easy" /><el-option label="中等" value="medium" /><el-option
                label="困难"
                value="hard" /></el-select
            ><el-input-number v-model="count" :min="1" :max="10" aria-label="题目数量" />
          </div>
          <el-form-item label="排序侧重">
            <el-select v-model="rankingProfile" aria-label="排序侧重">
              <el-option label="常规排序" value="" />
              <el-option
                v-for="(label, profile) in trainingRankingLabels"
                :key="profile"
                :label="label"
                :value="profile"
              />
            </el-select>
          </el-form-item>
          <el-button :loading="busy" @click="recommend">查看推荐候选</el-button>
        </el-form>
        <template v-if="recommendations">
          <p class="small muted">
            本次最多在 {{ recommendations.candidate_limit }} 道候选内排序，不代表全题库排名。{{
              recommendations.candidates_truncated ? '候选已截断，可缩小目标范围后重试。' : ''
            }}
          </p>
          <TrainingGoalCoverage
            v-if="recommendations.goal_coverage"
            :coverage="recommendations.goal_coverage"
          />
          <p v-for="gap in recommendations.gaps" :key="gap" role="status">{{ gap }}</p>
          <el-empty
            v-if="!recommendations.items.length"
            description="没有符合条件的真实题目，请调整目标或补充题目元数据。"
          />
          <ol>
            <li v-for="item in recommendations.items" :key="item.problem_version_id">
              <RouterLink :to="'/problems/' + item.problem_version_id">{{ item.title }}</RouterLink>
              <p>{{ item.reasons.join('；') }}</p>
              <TrainingTimingSource v-if="item.timing" :timing="item.timing" />
              <TrainingPrerequisites
                v-if="item.prerequisites?.length"
                :proofs="item.prerequisites"
              />
              <TrainingFeedbackSource
                v-if="item.feedback_evidence"
                :evidence="item.feedback_evidence"
                compact
              />
              <span class="small muted"
                >{{ item.tags.join(' / ') || '未标注知识点' }} ·
                {{ item.difficulty || '未标注难度' }}</span
              >
            </li>
          </ol>
          <p class="small muted">
            保存时会按当前条件重新核对可用题目并生成，最终内容以保存后的计划为准。规则版本：{{
              recommendations.rule_version
            }}
          </p>
          <el-button
            type="primary"
            :loading="busy"
            :disabled="!recommendations.items.length || !title.trim()"
            @click="create"
            >按当前条件生成并保存</el-button
          >
        </template>
      </PanelCard>
    </details>
    <details class="training-extra">
      <DisclosureHeading icon="rankings">本人学习概况</DisclosureHeading>
      <PanelCard title="本人学习概况" icon="submissions">
        <template #actions
          ><el-button :disabled="loading || busy" @click="load">刷新</el-button></template
        >
        <template v-if="overview">
          <p class="small muted">
            统计范围：{{ displayTime(overview.since) }} 至
            {{ displayTime(overview.generated_at) }}，最多 {{ overview.limit }} 次提交。{{
              overview.truncated ? '已截断，仅展示该范围最近记录。' : ''
            }}
          </p>
          <p v-if="overview.earliest_record_at" class="small muted">
            本次样本最早记录：{{ displayTime(overview.earliest_record_at) }}
          </p>
          <p v-if="!overview.submission_count">此范围内暂无学习记录。可以按目标选择练习。</p>
          <template v-else>
            <div class="tag-list">
              <span class="tag">{{ overview.submission_count }} 次提交</span
              ><span class="tag">{{ overview.ordinary_sample_count }} 次普通判定</span
              ><span class="tag">{{ overview.distinct_problem_count }} 道独立题目</span
              ><span class="tag">{{ overview.accepted_problem_count }} 道题至少一个版本 AC</span>
            </div>
            <p class="small muted">
              以上均限于本次统计范围；某旧版本 AC 不代表新版本通过。普通非 AC 判定不等于知识点缺陷。
            </p>
            <div class="tag-list">
              <span v-for="(value, verdict) in overview.verdict_counts" :key="verdict" class="tag"
                >{{ verdict }}：{{ value }}</span
              >
            </div>
            <p v-for="tag in overview.tags" :key="tag.tag">
              {{ tag.tag }}：{{ tag.sample_count }} 个有效样本，AC
              {{ tag.accepted_count }} 次，普通非 AC {{ tag.non_accepted_count }} 次
            </p>
            <p v-if="overview.missing_metadata_count" class="small muted">
              {{ overview.missing_metadata_count }} 条记录缺少学习元数据，未推测知识点或难度。
            </p>
            <details>
              <DisclosureHeading icon="submissions" tone="muted">最近提交与证据</DisclosureHeading>
              <ul>
                <li v-for="record in overview.recent" :key="record.submission_id">
                  <RouterLink :to="'/submissions/' + record.submission_id"
                    >{{ displayTime(record.submitted_at) }} ·
                    {{ record.verdict || record.state }}</RouterLink
                  >
                  · <RouterLink :to="'/problems/' + record.problem_version_id">题目版本</RouterLink>
                </li>
              </ul>
            </details>
          </template>
        </template>
      </PanelCard>
    </details>
  </div>
</template>

<style scoped>
.official-catalog {
  margin-block-end: 1.15rem;
}
.training-filters {
  display: grid;
  gap: 0.75rem;
  padding: 1rem 1.25rem;
}
.filter-row {
  display: flex;
  gap: 1rem;
  align-items: center;
}
.filter-row > span {
  flex: 0 0 4rem;
  font-weight: 600;
}
.filter-row .el-input {
  max-width: 22rem;
}
.filter-options {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.filter-options button {
  background: var(--surface);
  color: var(--secondary-ink);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0.25rem 0.75rem;
}
.filter-options button[aria-pressed='true'] {
  background: var(--accent-bg);
  color: var(--accent-ink);
  border-color: var(--accent-border);
}
.training-catalog > header {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
  padding: 1rem 1.25rem;
}
.training-catalog > header h2 {
  font-size: 1rem;
}
.training-catalog > header span {
  color: var(--muted);
  font-size: 0.8rem;
}
.training-catalog .training-table {
  padding: 0 0.75rem 0.75rem;
}
.training-catalog .training-table tbody tr:hover {
  background: var(--surface-soft);
}
.training-list-progress {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.training-list-progress progress {
  width: clamp(3rem, 9vw, 9rem);
  accent-color: var(--blue);
}
.catalog-limit {
  padding: 0 1.25rem 1rem;
}

.training-extra > summary {
  cursor: pointer;
  padding: 0.75rem 0;
}
</style>
