<script setup lang="ts">
import DisclosureHeading from '../components/DisclosureHeading.vue'
import { vLoading } from 'element-plus'
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { trainingIntent } from '../api/trainingIntent'
import { trainingRankingLabels } from '../api/trainingRanking'
import type { components } from '../api/generated/schema'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import { hintLevels, type HintLevel } from '../lib/hintLevels'
import PanelCard from '../components/PanelCard.vue'
import UiIcon from '../components/UiIcon.vue'
import DifficultyTag from '../components/DifficultyTag.vue'
import TrainingGoalCoverage from '../components/TrainingGoalCoverage.vue'
import TrainingNoteEditor from '../components/TrainingNoteEditor.vue'
type FollowUpPace = Omit<components['schemas']['CreateTrainingFollowUp'], 'idempotency_key'>
const route = useRoute()
const router = useRouter()
const { user, refresh } = useSession()
const training = ref<components['schemas']['TrainingDetail'] | null>(null)
const hintCeiling = ref<HintLevel>('L3')
watch(
  () => training.value?.max_hint_level,
  (level) => {
    hintCeiling.value = level ?? 'L3'
  },
)
const ProblemWorkspaceView = defineAsyncComponent(() => import('./ProblemDetailView.vue'))
const showTags = ref(false)
const tab = computed(() => {
  if (route.query.tab === 'problems') return 'problems'
  return route.query.tab === 'notes' || route.hash.startsWith('#training-item-')
    ? 'notes'
    : 'overview'
})
const selectedNote = computed(() => {
  const id = route.query.note ?? route.hash.slice('#training-item-'.length)
  return training.value?.items.find((item) => item.id === id) ?? training.value?.items[0]
})
const openNote = (id?: string) => ({ path: route.path, query: { tab: 'notes', note: id } })
const selectedProblem = computed(() =>
  tab.value === 'problems'
    ? training.value?.items.find(
        (item) => item.available && item.problem_version_id === route.query.problem,
      )
    : undefined,
)
const problemList = computed(() => ({ path: route.path, query: { tab: 'problems' } }))
const openProblem = (version: string) => ({
  path: route.path,
  query: { tab: 'problems', problem: version },
})
const rates = ref<Record<string, number | null>>({})
const rateSnapshot = computed(() => {
  if (tab.value !== 'problems' || selectedProblem.value) return ''
  return JSON.stringify(
    training.value?.items
      .filter((item) => item.available && item.is_current_version)
      .map((item) => [item.problem_version_id, item.slug])
      .slice(0, 10) ?? [],
  )
})
watch(
  rateSnapshot,
  async (snapshot, _previous, cleanup) => {
    if (!snapshot) return
    const request = new AbortController()
    cleanup(() => request.abort())
    const versions: [string, string][] = JSON.parse(snapshot)
    if (versions.every(([version]) => Object.hasOwn(rates.value, version))) return
    const entries = await Promise.all(
      versions.map(async ([version, slug]) => {
        try {
          const page = unwrap(
            await api.GET('/api/v1/problems', {
              params: { query: { search: slug, limit: 10 } },
              signal: request.signal,
            }),
          )
          const result = page.items.find((item) => item.version_id === version)
          const rate = result?.judged_submissions
            ? (result.accepted_submissions / result.judged_submissions) * 100
            : null
          return [version, rate] as const
        } catch {
          return [version, null] as const
        }
      }),
    )
    if (!request.signal.aborted) rates.value = Object.fromEntries(entries)
  },
  { immediate: true },
)

const notes = ref<Record<string, string>>({})
const trainingDifficulties = { easy: '简单', medium: '中等', hard: '困难' } as const
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
  rates.value = {}
  notes.value = {}
  error.value = ''
  notice.value = ''
  loading.value = false
  saving.value = null
}
async function load(keepView = false) {
  if (keepView) {
    controller.abort()
    controller = new AbortController()
    error.value = ''
    notice.value = ''
  } else clear()
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
      const pendingNotes = new Map<string, string>()
      if (training.value?.id === result.id) {
        for (const item of training.value.items) {
          const note = notes.value[item.id]
          if (note !== undefined && note !== item.note) pendingNotes.set(item.id, note)
        }
      }
      training.value = result
      const schedule = result.schedule
      dailyMinutes.value = schedule?.purpose === 'focused_training' ? schedule.daily_minutes : ''
      cycleDays.value = schedule?.purpose === 'periodic_review' ? schedule.cycle_days : ''
      sessionsPerWeek.value =
        schedule?.purpose === 'periodic_review' ? schedule.sessions_per_week : ''
      notes.value = Object.fromEntries(
        result.items.map((item) => [item.id, pendingNotes.get(item.id) ?? item.note]),
      )
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
async function saveNote(itemId: string) {
  if (saving.value || !training.value) return
  mutation = new AbortController()
  const { signal } = mutation
  saving.value = 'note:' + itemId
  error.value = ''
  notice.value = ''
  try {
    const result = unwrap(
      await api.PATCH('/api/v1/trainings/{training_id}/items/{item_id}', {
        params: { path: { training_id: training.value.id, item_id: itemId } },
        headers: csrfHeaders(),
        body: { note: notes.value[itemId] ?? '' },
        signal,
      }),
    )
    if (!signal.aborted) {
      training.value = result
      notes.value[itemId] = result.items.find((item) => item.id === itemId)?.note ?? ''
      notice.value = '笔记已保存。'
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = null
  }
}
async function saveHintCeiling() {
  const id = training.value?.id
  if (!id || saving.value || hintCeiling.value === (training.value?.max_hint_level ?? 'L3')) return
  mutation?.abort()
  mutation = new AbortController()
  const { signal } = mutation
  saving.value = 'hint-ceiling'
  error.value = ''
  notice.value = ''
  try {
    const policy = unwrap(
      await api.PUT('/api/v1/trainings/{training_id}/hint-ceiling', {
        params: { path: { training_id: id } },
        headers: csrfHeaders(),
        body: { max_hint_level: hintCeiling.value },
        signal,
      }),
    )
    if (!signal.aborted && training.value?.id === id) {
      training.value = { ...training.value, ...policy }
      notice.value = '题单提示上限已保存。'
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
watch([() => user.value?.id, () => route.params.id], () => void load(), { immediate: true })
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
  <div v-if="!selectedProblem" class="breadcrumbs">
    <RouterLink to="/trainings">训练集</RouterLink><span>/</span><span>详情</span>
  </div>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="!user">
    <RouterLink :to="{ path: '/login', query: { next: route.fullPath } }">登录</RouterLink
    >后查看本人计划。
  </p>
  <template v-else>
    <div
      v-if="training"
      v-loading="loading"
      class="training-detail"
      :class="{ 'training-detail--practice': selectedProblem }"
    >
      <header class="panel training-hero">
        <div class="training-title">
          <div>
            <span class="tag">私有训练</span>
            <h1>{{ training.title }}</h1>
          </div>
          <el-button :disabled="loading || !!saving" @click="load(true)"
            ><UiIcon name="refresh" /> 刷新进度</el-button
          >
        </div>
        <div class="training-progress">
          <progress
            :value="training.completed_count"
            :max="training.item_count || 1"
            aria-label="训练集完成进度"
          />
          <span>{{ training.completed_count }} / {{ training.item_count }} 道已完成</span>
        </div>
      </header>
      <nav class="panel training-nav" aria-label="训练集详情">
        <RouterLink
          :to="{ path: route.path }"
          :aria-current="tab === 'overview' ? 'page' : undefined"
        >
          <UiIcon name="reading" /> 训练简介
        </RouterLink>
        <RouterLink :to="problemList" :aria-current="tab === 'problems' ? 'page' : undefined">
          <UiIcon name="problems" /> 题目列表
        </RouterLink>
        <RouterLink
          :to="openNote(selectedProblem?.id)"
          :aria-current="tab === 'notes' ? 'page' : undefined"
        >
          <UiIcon name="document" /> 训练笔记
        </RouterLink>
      </nav>
      <section v-if="selectedProblem" class="training-practice" aria-label="训练练习">
        <div class="practice-toolbar">
          <RouterLink class="text-link" :to="problemList">返回题目列表</RouterLink>
          <span class="small muted"
            >第 {{ selectedProblem.position + 1 }} / {{ training.item_count }} 题</span
          >
        </div>
        <ProblemWorkspaceView
          :key="selectedProblem.problem_version_id"
          :version-id="selectedProblem.problem_version_id"
          :training-id="training.id"
        >
          <template #notes>
            <TrainingNoteEditor
              :model-value="notes[selectedProblem.id] ?? ''"
              :item-id="selectedProblem.id"
              :title="selectedProblem.title"
              :saved="selectedProblem.note"
              :disabled="!!saving"
              :saving="saving === 'note:' + selectedProblem.id"
              @update:model-value="notes[selectedProblem.id] = $event"
              @save="saveNote(selectedProblem.id)"
            />
          </template>
        </ProblemWorkspaceView>
      </section>
      <PanelCard v-else-if="tab === 'problems'" title="训练题单" icon="problems">
        <template #actions>
          <el-button text size="small" :aria-pressed="showTags" @click="showTags = !showTags">{{
            showTags ? '隐藏知识点' : '显示知识点'
          }}</el-button>
        </template>
        <p v-if="route.query.problem" role="status" class="small muted">
          该题目当前无法打开，请从下方重新选择。
        </p>
        <div v-if="training.items.length" class="training-table">
          <table>
            <thead>
              <tr>
                <th scope="col">进度</th>
                <th scope="col">题目标识</th>
                <th scope="col">题目</th>
                <th scope="col">难度</th>
                <th v-if="showTags" scope="col">知识点</th>
                <th scope="col">通过率</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in training.items" :key="item.id">
                <td>
                  <span :class="{ 'completed-status': item.progress === 'completed' }">{{
                    progressLabels[item.progress]
                  }}</span>
                </td>
                <td class="mono small">{{ item.slug }}</td>
                <th scope="row">
                  <RouterLink v-if="item.available" :to="openProblem(item.problem_version_id)">{{
                    item.title
                  }}</RouterLink>
                  <span v-else>{{ item.title }}（当前不可用）</span>
                </th>
                <td>
                  <DifficultyTag
                    :value="item.difficulty ? trainingDifficulties[item.difficulty] : '未标注'"
                  />
                </td>
                <td v-if="showTags">{{ item.tags.join('、') || '未标注' }}</td>
                <td>
                  <div v-if="rates[item.problem_version_id] != null" class="problem-rate">
                    <progress
                      :value="rates[item.problem_version_id] ?? 0"
                      max="100"
                      :aria-label="item.title + '通过率'"
                    />
                    {{ rates[item.problem_version_id]?.toFixed(1) }}%
                  </div>
                  <span v-else class="small muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <el-empty v-else description="此训练集没有题目" />
      </PanelCard>
      <PanelCard v-else-if="tab === 'notes'" title="训练笔记" icon="document">
        <div v-if="selectedNote" class="training-notes">
          <nav class="note-list" aria-label="按题查看笔记">
            <RouterLink
              v-for="item in training.items"
              :key="item.id"
              :to="openNote(item.id)"
              :aria-current="item.id === selectedNote.id ? 'page' : undefined"
            >
              <span>{{ item.title }}</span>
              <span v-if="item.note" class="small muted">已记录</span>
            </RouterLink>
          </nav>
          <TrainingNoteEditor
            :model-value="notes[selectedNote.id] ?? ''"
            :item-id="selectedNote.id"
            :title="selectedNote.title"
            :saved="selectedNote.note"
            :disabled="!!saving"
            :saving="saving === 'note:' + selectedNote.id"
            @update:model-value="notes[selectedNote.id] = $event"
            @save="saveNote(selectedNote.id)"
          />
        </div>
        <el-empty v-else description="此训练集没有题目" />
      </PanelCard>
      <div v-else class="training-overview">
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
                <template v-if="training.schedule.available_minutes != null">
                  · 本次安排 {{ training.schedule.available_minutes }} 分钟
                </template>
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
        </PanelCard>
        <PanelCard title="训练简介" icon="reading">
          <p v-if="training.schedule?.goal">{{ training.schedule.goal }}</p>
          <TrainingGoalCoverage v-if="training.goal_coverage" :coverage="training.goal_coverage" />
          <p class="small muted">通过本计划所选题目版本即记为完成，只统计计划创建后的本人评测。</p>
          <form class="overview-actions training-hint-setting" @submit.prevent="saveHintCeiling">
            <label>
              训练提示上限
              <select v-model="hintCeiling" :disabled="!!saving">
                <option v-for="{ value, label } in hintLevels" :key="value" :value="value">
                  {{ value }} · {{ label }}
                </option>
              </select>
            </label>
            <el-button
              native-type="submit"
              :disabled="!!saving || hintCeiling === (training.max_hint_level ?? 'L3')"
              :loading="saving === 'hint-ceiling'"
              >保存上限</el-button
            >
          </form>
          <div class="overview-actions">
            <RouterLink class="el-button el-button--primary" :to="problemList">打开题单</RouterLink>
            <el-button
              :disabled="loading || !!saving"
              :loading="saving === 'follow-up'"
              @click="followUp()"
              >生成下一轮</el-button
            >
          </div>
          <details v-if="training.schedule" class="follow-up-pace native-form">
            <DisclosureHeading icon="clock" tone="accent">调整下一轮节奏</DisclosureHeading>
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
        </PanelCard>
      </div>
    </div>
    <p v-else-if="!loading && !error">暂无计划数据。</p>
  </template>
</template>

<style>
.app-main:has(.training-detail--practice) {
  margin: 12px auto;
}
.app-shell:has(.training-detail--practice) .app-footer {
  display: none;
}
</style>

<style scoped>
.training-detail {
  display: grid;
  gap: 0.9rem;
}
.training-hero {
  padding: 1.2rem 1.4rem;
}
.training-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}
.training-title h1 {
  margin-top: 0.5rem;
  color: var(--strong-ink);
  font-size: clamp(1.35rem, 2.2vw, 1.85rem);
}
.training-progress {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  margin-top: 1rem;
  color: var(--secondary-ink);
  font-size: 0.82rem;
}
.training-progress progress {
  flex: 1;
  min-width: 0;
  height: 0.8rem;
  accent-color: var(--el-color-success);
}
.training-progress progress::-webkit-progress-value {
  background: var(--el-color-success);
}
.training-progress progress::-moz-progress-bar {
  background: var(--el-color-success);
}
.training-detail--practice {
  gap: 0.55rem;
}
.training-detail--practice .training-hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(12rem, 30%);
  align-items: center;
  gap: 1rem;
  padding: 0.55rem 0.85rem;
}
.training-detail--practice .training-title h1 {
  margin: 0;
  font-size: 1.2rem;
}
.training-detail--practice .training-title .tag {
  display: none;
}
.training-detail--practice .training-progress {
  margin-top: 0;
}
@media (max-width: 700px) {
  .training-detail--practice .training-hero {
    grid-template-columns: minmax(0, 1fr);
    gap: 0.5rem;
  }
}
.training-nav {
  display: flex;
  align-items: stretch;
  gap: 0.4rem;
  padding-inline: 0.7rem;
}
.training-nav a {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.7rem 0.75rem;
  border-bottom: 2px solid transparent;
}
.training-nav a[aria-current='page'] {
  color: var(--accent-ink);
  border-color: var(--blue);
}
.training-overview {
  display: grid;
  grid-template-columns: minmax(14rem, 1fr) minmax(0, 2fr);
  gap: 0.9rem;
  align-items: start;
}
.overview-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-block: 1rem;
}
.overview-actions .el-button {
  margin: 0;
}
.training-hint-setting,
.training-hint-setting label {
  align-items: center;
}
.training-hint-setting label {
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem;
  font-size: 0.86rem;
}
.training-hint-setting select {
  min-height: 2rem;
  padding: 0.2rem 0.55rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--body-ink);
  font: inherit;
}
.practice-toolbar {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  margin-bottom: 0.55rem;
}
.problem-rate {
  display: flex;
  gap: 0.45rem;
  align-items: center;
  white-space: nowrap;
}
.problem-rate progress {
  width: clamp(3rem, 8vw, 8rem);
  accent-color: var(--blue);
}
.completed-status {
  color: var(--success-ink);
  font-weight: 600;
}
.training-notes {
  display: grid;
  grid-template-columns: minmax(10rem, 14rem) minmax(0, 1fr);
  gap: 1rem;
  align-items: start;
}
.note-list {
  display: grid;
  gap: 0.35rem;
  border-inline-end: 1px solid var(--line);
  padding-inline-end: 0.75rem;
}
.note-list a {
  display: flex;
  justify-content: space-between;
  gap: 0.45rem;
  padding: 0.55rem;
  border-radius: var(--el-border-radius-base);
  overflow-wrap: anywhere;
}
.note-list a[aria-current='page'] {
  background: var(--accent-bg);
  color: var(--accent-ink);
}
@media (max-width: 700px) {
  .training-overview,
  .training-notes {
    grid-template-columns: minmax(0, 1fr);
  }
  .note-list {
    border-inline-end: 0;
    border-bottom: 1px solid var(--line);
    padding: 0 0 0.5rem;
  }
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
