<script setup lang="ts">
import { vLoading } from 'element-plus'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { trainingIntent } from '../api/trainingIntent'
import { trainingRankingLabels } from '../api/trainingRanking'
import type { components } from '../api/generated/schema'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import PanelCard from '../components/PanelCard.vue'
import PageHeading from '../components/PageHeading.vue'
import TrainingFeedbackSource from '../components/TrainingFeedbackSource.vue'
import TrainingGoalCoverage from '../components/TrainingGoalCoverage.vue'
import TrainingPrerequisites from '../components/TrainingPrerequisites.vue'
import TrainingTimingSource from '../components/TrainingTimingSource.vue'
type Rating = NonNullable<components['schemas']['UpdateTrainingFeedback']['rating']>
type FollowUpPace = Omit<components['schemas']['CreateTrainingFollowUp'], 'idempotency_key'>
const route = useRoute()
const router = useRouter()
const { user, refresh } = useSession()
const training = ref<components['schemas']['TrainingDetail'] | null>(null)
const notes = ref<Record<string, string>>({})
const ratings = ref<Record<string, Rating | ''>>({})
const ratingChoices: Record<Rating, string> = {
  too_easy: '偏简单',
  appropriate: '合适',
  too_hard: '偏难',
}
const error = ref('')
const notice = ref('')
const loading = ref(false)
const saving = ref<string | null>(null)
const dailyMinutes = ref<number | ''>('')
const cycleDays = ref<number | ''>('')
const sessionsPerWeek = ref<number | ''>('')
const progressLabels = { not_started: '尚未开始', attempted: '已尝试', completed: '已完成（AC）' }
let controller = new AbortController()
let mutation: AbortController | undefined
function clear() {
  controller.abort()
  mutation?.abort()
  controller = new AbortController()
  training.value = null
  notes.value = {}
  ratings.value = {}
  error.value = ''
  notice.value = ''
  loading.value = false
  saving.value = null
}
async function load() {
  clear()
  if (!user.value) return
  const { signal } = controller
  loading.value = true
  try {
    const result = unwrap(
      await api.GET('/api/v1/trainings/{training_id}', {
        params: { path: { training_id: String(route.params.id) } },
        signal,
      }),
    )
    if (!signal.aborted) {
      training.value = result
      const schedule = result.schedule
      dailyMinutes.value = schedule?.purpose === 'focused_training' ? schedule.daily_minutes : ''
      cycleDays.value = schedule?.purpose === 'periodic_review' ? schedule.cycle_days : ''
      sessionsPerWeek.value =
        schedule?.purpose === 'periodic_review' ? schedule.sessions_per_week : ''
      notes.value = Object.fromEntries(result.items.map((item) => [item.id, item.note]))
      ratings.value = Object.fromEntries(
        result.items.map((item) => [item.id, item.feedback?.rating ?? '']),
      )
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
async function saveItem(itemId: string, kind: 'note' | 'feedback') {
  if (saving.value || !training.value) return
  mutation = new AbortController()
  const { signal } = mutation
  saving.value = kind + ':' + itemId
  error.value = ''
  notice.value = ''
  try {
    const options = {
      params: { path: { training_id: training.value.id, item_id: itemId } },
      headers: csrfHeaders(),
      signal,
    }
    const result = unwrap(
      kind === 'note'
        ? await api.PATCH('/api/v1/trainings/{training_id}/items/{item_id}', {
            ...options,
            body: { note: notes.value[itemId] ?? '' },
          })
        : await api.PUT('/api/v1/trainings/{training_id}/items/{item_id}/feedback', {
            ...options,
            body: { rating: ratings.value[itemId] || null },
          }),
    )
    if (!signal.aborted) {
      training.value = result
      notice.value = kind === 'note' ? '备注已保存。' : '难度自评已保存。'
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = null
  }
}
async function followUp(pace: FollowUpPace = {}) {
  const owner = user.value?.id
  const source = training.value?.id
  if (!owner || !source || saving.value || loading.value) return
  mutation = new AbortController()
  const { signal } = mutation
  saving.value = 'follow-up'
  error.value = ''
  notice.value = ''
  try {
    const fingerprint = Object.keys(pace).length ? source + ':' + JSON.stringify(pace) : source
    const intent = trainingIntent(localStorage, `follow-up:${owner}:${source}`, fingerprint)
    const result = unwrap(
      await api.POST('/api/v1/trainings/{training_id}/follow-ups', {
        params: { path: { training_id: source } },
        body: { idempotency_key: intent.key, ...pace },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (!signal.aborted) {
      intent.complete()
      await router.push('/trainings/' + result.id)
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = null
  }
}
function followUpAtNewPace() {
  const schedule = training.value?.schedule
  if (schedule?.purpose === 'focused_training' && dailyMinutes.value !== '') {
    void followUp(
      dailyMinutes.value === schedule.daily_minutes ? {} : { daily_minutes: dailyMinutes.value },
    )
  }
  if (
    schedule?.purpose === 'periodic_review' &&
    cycleDays.value !== '' &&
    sessionsPerWeek.value !== ''
  ) {
    const pace: FollowUpPace = {}
    if (cycleDays.value !== schedule.cycle_days) pace.cycle_days = cycleDays.value
    if (sessionsPerWeek.value !== schedule.sessions_per_week)
      pace.sessions_per_week = sessionsPerWeek.value
    void followUp(pace)
  }
}
watch([() => user.value?.id, () => route.params.id], load, { immediate: true })
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
  <div class="breadcrumbs">
    <RouterLink to="/trainings">训练计划</RouterLink><span>/</span><span>个人计划</span>
  </div>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="!user">
    <RouterLink :to="{ path: '/login', query: { next: route.fullPath } }">登录</RouterLink
    >后查看本人计划。
  </p>
  <template v-else>
    <PageHeading
      class="training-heading"
      :title="training?.title ?? '训练计划'"
      description="计划固定生成时的题目版本，进度来自本人的真实提交。"
    >
      <el-button :disabled="loading || !!saving" @click="load">刷新进度</el-button>
      <el-button
        v-if="training"
        type="primary"
        :disabled="loading || !!saving"
        :loading="saving === 'follow-up'"
        @click="followUp()"
      >
        根据最新记录生成下一轮
      </el-button>
    </PageHeading>
    <div v-if="training" v-loading="loading" class="content-with-aside">
      <PanelCard title="训练题目" icon="problems" subtitle="按顺序练习">
        <TrainingGoalCoverage v-if="training.goal_coverage" :coverage="training.goal_coverage" />
        <article
          v-for="item in training.items"
          :id="'training-item-' + item.id"
          :key="item.id"
          class="panel-body"
        >
          <h3>
            {{ item.position + 1 }}.
            <RouterLink v-if="item.available" :to="'/problems/' + item.problem_version_id">{{
              item.title
            }}</RouterLink
            ><span v-else>{{ item.title }}</span>
          </h3>
          <div class="tag-list">
            <span class="tag">{{ progressLabels[item.progress] }}</span
            ><span v-if="!item.available" class="tag">当前不可用</span
            ><span v-if="!item.is_current_version" class="tag">历史版本，不自动替换</span>
          </div>
          <p>{{ item.reasons.join('；') }}</p>
          <p v-if="item.estimated_minutes" class="small muted">
            建议用时约 {{ item.estimated_minutes }} 分钟
          </p>
          <TrainingTimingSource v-if="item.timing" :timing="item.timing" />
          <TrainingPrerequisites v-if="item.prerequisites?.length" :proofs="item.prerequisites" />
          <TrainingFeedbackSource
            v-if="item.feedback_evidence"
            :evidence="item.feedback_evidence"
            compact
          />
          <p class="small muted">
            生成时标签：{{ item.tags.join(' / ') || '未标注' }}；难度：{{
              item.difficulty || '未标注'
            }}；元数据修订：{{ item.metadata_revision ?? '无' }}
          </p>
          <ul v-if="item.progress_evidence.length">
            <li v-for="record in item.progress_evidence" :key="record.submission_id">
              <RouterLink :to="'/submissions/' + record.submission_id"
                >{{ record.verdict || record.state }} ·
                {{ displayTime(record.submitted_at) }}</RouterLink
              >
            </li>
          </ul>
          <details v-if="item.evidence.length">
            <summary>推荐依据的提交</summary>
            <ul>
              <li v-for="record in item.evidence" :key="record.submission_id">
                <RouterLink :to="'/submissions/' + record.submission_id"
                  >{{ record.verdict || record.state }} ·
                  {{ displayTime(record.submitted_at) }}</RouterLink
                >
              </li>
            </ul>
          </details>
          <el-input
            v-model="notes[item.id]"
            type="textarea"
            :rows="2"
            maxlength="1000"
            show-word-limit
            :aria-label="item.title + '学习备注'"
            :disabled="!!saving"
            placeholder="可选：记录一句学习备注（不改变判题结果）"
          />
          <el-button
            :loading="saving === 'note:' + item.id"
            :disabled="!!saving || notes[item.id] === item.note"
            @click="saveItem(item.id, 'note')"
            >保存备注</el-button
          >
          <div class="rating-form">
            <el-select
              v-model="ratings[item.id]"
              :aria-label="item.title + '难度自评'"
              :disabled="!!saving"
            >
              <el-option label="未评价" value="" />
              <el-option
                v-for="(label, rating) in ratingChoices"
                :key="rating"
                :label="label"
                :value="rating"
              />
            </el-select>
            <el-button
              :loading="saving === 'feedback:' + item.id"
              :disabled="!!saving || (ratings[item.id] ?? '') === (item.feedback?.rating ?? '')"
              @click="saveItem(item.id, 'feedback')"
              >保存难度自评</el-button
            >
          </div>
          <p v-if="item.feedback" class="small muted">
            本人自评：{{ ratingChoices[item.feedback.rating] }} ·
            {{ displayTime(item.feedback.rated_at) }}
          </p>
        </article>
        <el-empty v-if="!training.items.length" description="此计划没有题目" />
      </PanelCard>
      <div class="stack">
        <PanelCard title="计划信息" icon="trainings"
          ><dl class="details-list">
            <div>
              <dt>创建时间</dt>
              <dd>{{ displayTime(training.created_at) }}</dd>
            </div>
            <div v-if="training.previous_plan_id">
              <dt>计划来源</dt>
              <dd>
                <RouterLink class="text-link" :to="'/trainings/' + training.previous_plan_id">
                  上一轮计划
                </RouterLink>
              </dd>
            </div>
            <div>
              <dt>完成进度</dt>
              <dd>{{ training.completed_count }} / {{ training.item_count }}</dd>
            </div>
            <div>
              <dt>目标知识点</dt>
              <dd>{{ training.parameters.tags?.join(' / ') || '不限' }}</dd>
            </div>
            <div v-if="training.schedule?.goal">
              <dt>训练目标</dt>
              <dd>{{ training.schedule.goal }}</dd>
            </div>
            <div v-if="training.schedule">
              <dt>训练节奏</dt>
              <dd v-if="training.schedule.purpose === 'focused_training'">
                每日 {{ training.schedule.daily_minutes }} 分钟
              </dd>
              <dd v-else>
                周期 {{ training.schedule.cycle_days }} 天 · 每周
                {{ training.schedule.sessions_per_week }} 次
              </dd>
            </div>
            <div>
              <dt>规则版本</dt>
              <dd>{{ training.rule_version }}</dd>
            </div>
            <div v-if="training.parameters.ranking_profile">
              <dt>排序侧重</dt>
              <dd>{{ trainingRankingLabels[training.parameters.ranking_profile] }}</dd>
            </div>
          </dl>
          <details v-if="training.schedule" class="follow-up-pace native-form">
            <summary>调整下一轮节奏</summary>
            <form class="stack" @submit.prevent="followUpAtNewPace">
              <label v-if="training.schedule.purpose === 'focused_training'">
                每日分钟数
                <input
                  v-model.number="dailyMinutes"
                  type="number"
                  min="5"
                  max="180"
                  required
                  :disabled="!!saving"
                />
              </label>
              <template v-else>
                <label>
                  复习周期（天）
                  <input
                    v-model.number="cycleDays"
                    type="number"
                    min="7"
                    max="90"
                    required
                    :disabled="!!saving"
                  />
                </label>
                <label>
                  每周次数
                  <input
                    v-model.number="sessionsPerWeek"
                    type="number"
                    min="1"
                    max="7"
                    required
                    :disabled="!!saving"
                  />
                </label>
              </template>
              <el-button
                native-type="submit"
                :loading="saving === 'follow-up'"
                :disabled="!!saving"
              >
                按新节奏生成下一轮
              </el-button>
            </form>
          </details>
          <p class="small muted">
            只统计计划创建后，本人对固定版本的提交。任一次 AC 即完成，之后非 AC
            不使进度回退；系统失败与等待中不算完成。创建前的 AC 不计入本计划进度。
          </p></PanelCard
        >
      </div>
    </div>
    <p v-else-if="!loading && !error">暂无计划数据。</p>
  </template>
</template>

<style scoped>
.training-heading :deep(.page-heading-actions) {
  flex-wrap: wrap;
}
.rating-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.8rem;
}
.rating-form .el-select {
  width: 11rem;
}
.follow-up-pace {
  border-top: 1px solid var(--line);
  padding-block: 0.75rem 0.5rem;
}
.follow-up-pace > summary {
  margin-bottom: 0.75rem;
  cursor: pointer;
  color: var(--el-text-color-primary);
}
.follow-up-pace label {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(4rem, 0.75fr);
  align-items: center;
  gap: 0.6rem;
}
.follow-up-pace button {
  justify-self: start;
}
</style>
