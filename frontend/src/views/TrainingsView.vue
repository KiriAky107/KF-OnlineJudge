<script setup lang="ts">
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
import PanelCard from '../components/PanelCard.vue'
import TrainingFeedbackSource from '../components/TrainingFeedbackSource.vue'
import TrainingGoalCoverage from '../components/TrainingGoalCoverage.vue'
import TrainingPrerequisites from '../components/TrainingPrerequisites.vue'
import TrainingTimingSource from '../components/TrainingTimingSource.vue'
const { user, refresh } = useSession()
const router = useRouter()
const overview = ref<components['schemas']['LearningOverview'] | null>(null)
const page = ref<components['schemas']['TrainingPage'] | null>(null)
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
    void load()
  },
  { immediate: true },
)
onMounted(async () => {
  if (!user.value) {
    try {
      await refresh()
    } catch (cause) {
      error.value = errorText(cause)
    }
  }
})
onBeforeUnmount(clear)
</script>
<template>
  <PageHeading
    title="训练计划"
    eyebrow="TRAINING"
    description="从自己的真实提交记录出发，按目标安排练习。"
  >
    <RouterLink class="el-button el-button--primary" to="/trainings/assistant">学习助手</RouterLink>
  </PageHeading>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="!user">
    <RouterLink to="/login?next=/trainings">登录</RouterLink>后查看本人的学习记录与训练计划。
  </p>
  <div v-else v-loading="loading" class="stack">
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
            {{ tag.tag }}：{{ tag.sample_count }} 个有效样本，AC {{ tag.accepted_count }} 次，普通非
            AC {{ tag.non_accepted_count }} 次
          </p>
          <p v-if="overview.missing_metadata_count" class="small muted">
            {{ overview.missing_metadata_count }} 条记录缺少学习元数据，未推测知识点或难度。
          </p>
          <details>
            <summary>最近提交与证据</summary>
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
            <TrainingPrerequisites v-if="item.prerequisites?.length" :proofs="item.prerequisites" />
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
    <PanelCard title="我的计划" icon="trainings">
      <p v-if="page?.truncated" role="status">
        仅列出最近 {{ page.limit }} 个计划；更早计划仍可通过原详情链接访问。
      </p>
      <div class="card-grid">
        <article v-for="plan in page?.items" :key="plan.id" class="training-card">
          <div>
            <h2>
              <RouterLink :to="'/trainings/' + plan.id">{{ plan.title }}</RouterLink>
            </h2>
            <p>{{ plan.completed_count }} / {{ plan.item_count }} 道已完成</p>
            <time>{{ displayTime(plan.created_at) }}</time>
          </div>
        </article>
      </div>
      <el-empty v-if="page && !page.items.length" description="还没有保存的训练计划" />
    </PanelCard>
  </div>
</template>
