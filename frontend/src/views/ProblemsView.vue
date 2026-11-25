<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'
import DifficultyTag from '../components/DifficultyTag.vue'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'

type Problem = components['schemas']['CatalogProblem']
type Difficulty = NonNullable<Problem['difficulty']>
const difficultyLabels: Record<Difficulty, string> = { easy: '简单', medium: '中等', hard: '困难' }
const difficultyOptions = Object.entries(difficultyLabels) as [Difficulty, string][]
const items = ref<Problem[]>([])
const search = ref('')
const difficulty = ref<string>()
const levels = ref<components['schemas']['DifficultyLevel'][]>([])
const levelRequest = new AbortController()
const tag = ref<string>()
const showTags = ref(true)
const pointedVersion = ref<string>()
const focusedVersion = ref<string>()
const activeProblem = computed(() =>
  items.value.find((item) => item.version_id === (pointedVersion.value ?? focusedVersion.value)),
)
const scoreLevels = computed(() => {
  const published = new Map(levels.value.map((level) => [level.key, level]))
  for (const { difficulty_level: level } of items.value) {
    if (level) published.set(level.key, level)
  }
  return [...published.values()].sort((a, b) => a.score - b.score)
})
const error = ref('')
const busy = ref(false)
const next = ref<string | null>(null)
const current = ref<string | undefined>()
const previous = ref<(string | undefined)[]>([])
let request: AbortController | undefined
let generation = 0

const tags = computed(() =>
  [...new Set(items.value.flatMap((problem) => problem.tags ?? []))].sort((a, b) =>
    a.localeCompare(b, 'zh-CN'),
  ),
)
const distribution = computed(() => {
  const counts = { easy: 0, medium: 0, hard: 0, unrated: 0 }
  for (const item of items.value) counts[item.difficulty ?? 'unrated']++
  return [
    ...difficultyOptions.map(([key, label]) => ({ label, count: counts[key], color: key })),
    { label: '未标注', count: counts.unrated, color: 'unrated' },
  ]
})

async function load(after?: string) {
  request?.abort()
  const epoch = ++generation
  const controller = new AbortController()
  request = controller
  error.value = ''
  busy.value = true
  current.value = after
  pointedVersion.value = focusedVersion.value = undefined
  try {
    const page = unwrap(
      await api.GET('/api/v1/problems', {
        params: {
          query: {
            after,
            limit: 20,
            search: search.value.trim() || undefined,
            difficulty_key: difficulty.value,
            tag: tag.value,
          },
        },
        signal: controller.signal,
      }),
    )
    if (epoch !== generation) return
    items.value = page.items
    next.value = page.next_after
  } catch (cause) {
    if (epoch === generation) {
      error.value = errorText(cause)
      items.value = []
      next.value = null
    }
  } finally {
    if (epoch === generation) busy.value = false
  }
}

function forward() {
  if (!next.value) return
  previous.value.push(current.value)
  void load(next.value)
}
function back() {
  void load(previous.value.pop())
}
function chooseTag(value: string) {
  tag.value = tag.value === value ? undefined : value
}
function passRate(accepted: number, judged: number) {
  return judged > 0 ? Math.min((100 * accepted) / judged, 100) : 0
}
const verdictLabels = [
  ['accepted', 'AC', 'Accepted', 'success'],
  ['wrong_answer', 'WA', 'Wrong Answer', 'danger'],
  ['compilation_error', 'CE', 'Compilation Error', 'warning'],
  ['runtime_error', 'RE', 'Runtime Error', 'danger'],
  ['time_limit_exceeded', 'TLE', 'Time Limit Exceeded', 'warning'],
  ['memory_limit_exceeded', 'MLE', 'Memory Limit Exceeded', 'warning'],
  ['output_limit_exceeded', 'OLE', 'Output Limit Exceeded', 'warning'],
] as const

function pointAt(row: Problem) {
  pointedVersion.value = row.version_id
}

function leaveCell(row: Problem, _column: unknown, cell: HTMLElement, event: MouseEvent) {
  if (
    pointedVersion.value === row.version_id &&
    !(event.relatedTarget instanceof Node && cell.parentElement?.contains(event.relatedTarget))
  ) {
    pointedVersion.value = undefined
  }
}

watch(
  [search, difficulty, tag],
  ([nextSearch], [lastSearch], cleanup) => {
    request?.abort()
    generation++
    previous.value = []
    current.value = undefined
    next.value = null
    items.value = []
    error.value = ''
    busy.value = true
    const timer = setTimeout(() => void load(), nextSearch === lastSearch ? 0 : 200)
    cleanup(() => clearTimeout(timer))
  },
  { flush: 'sync' },
)
onMounted(() => {
  void load()
  void api
    .GET('/api/v1/problems/difficulty-levels', { signal: levelRequest.signal })
    .then(({ data }) => {
      if (data) levels.value = data
    })
    .catch(() => {})
})
onBeforeUnmount(() => {
  generation++
  request?.abort()
  levelRequest.abort()
})
</script>

<template>
  <PageHeading title="题库" />
  <div class="catalog">
    <PanelCard title="题目列表" icon="problems">
      <div class="catalog-search">
        <el-input
          v-model="search"
          placeholder="搜索题号或题目"
          aria-label="搜索题目"
          :maxlength="80"
          clearable
        />
        <label class="catalog-tag-toggle"
          ><input v-model="showTags" type="checkbox" />显示标签</label
        >
      </div>
      <div class="catalog-filters" role="group" aria-label="按难度筛选">
        <span class="muted">难度</span>
        <el-button
          size="small"
          :type="difficulty === undefined ? 'primary' : 'default'"
          :aria-pressed="difficulty === undefined"
          @click="difficulty = undefined"
          >全部</el-button
        >
        <el-button
          v-for="{ key, label } in levels"
          :key="key"
          size="small"
          :type="difficulty === key ? 'primary' : 'default'"
          :aria-pressed="difficulty === key"
          @click="difficulty = key"
          >{{ label }}</el-button
        >
        <el-tag v-if="tag" closable @close="tag = undefined">{{ tag }}</el-tag>
      </div>
      <p v-if="error" role="alert">
        {{ error }} <el-button size="small" @click="load(current)">重试</el-button>
      </p>
      <el-table
        :data="items"
        stripe
        :empty-text="busy ? '正在加载…' : '没有符合条件的题目'"
        @cell-mouse-enter="pointAt"
        @cell-mouse-leave="leaveCell"
        @mouseleave="pointedVersion = undefined"
      >
        <el-table-column prop="slug" label="题号" min-width="156">
          <template #default="{ row }"
            ><span class="mono muted">{{ row.slug }}</span></template
          >
        </el-table-column>
        <el-table-column label="题目" min-width="160">
          <template #default="{ row }">
            <RouterLink
              :to="'/problems/' + row.version_id"
              class="text-link"
              @focus="focusedVersion = row.version_id"
              @blur="focusedVersion = undefined"
            >
              {{ row.title }}
            </RouterLink>
          </template>
        </el-table-column>
        <el-table-column label="难度" min-width="125">
          <template #default="{ row }">
            <DifficultyTag
              v-if="row.difficulty"
              :value="difficultyLabels[row.difficulty as Difficulty]"
              :level="row.difficulty_level"
            />
            <span v-else class="muted">未标注</span>
          </template>
        </el-table-column>
        <el-table-column v-if="showTags" label="标签" min-width="164">
          <template #default="{ row }">
            <div class="catalog-tags">
              <el-button
                v-for="label in row.tags ?? []"
                :key="label"
                size="small"
                text
                @click="chooseTag(label)"
              >
                {{ label }}
              </el-button>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="total_submissions" label="提交总数" width="96" align="right">
          <template #default="{ row }">
            <span class="tabular">{{ row.total_submissions ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="通过率" min-width="156">
          <template #default="{ row }">
            <div
              class="pass-rate"
              tabindex="0"
              :aria-label="
                row.title +
                '通过率，' +
                (row.judged_submissions > 0
                  ? passRate(row.accepted_submissions, row.judged_submissions).toFixed(1) + '%'
                  : '暂无评测')
              "
              @focus="focusedVersion = row.version_id"
              @blur="focusedVersion = undefined"
            >
              <template v-if="row.judged_submissions > 0">
                <div
                  class="pass-rate-fill"
                  :style="{
                    width: passRate(row.accepted_submissions, row.judged_submissions) + '%',
                  }"
                  aria-hidden="true"
                />
                <span class="pass-rate-label tabular">
                  {{ passRate(row.accepted_submissions, row.judged_submissions).toFixed(1) }}%
                </span>
              </template>
              <span v-else class="pass-rate-label muted">暂无评测</span>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-row">
        <span aria-live="polite">第 {{ previous.length + 1 }} 页 · {{ items.length }} 道题</span>
        <el-button :disabled="busy || !previous.length" @click="back">上一页</el-button>
        <el-button :disabled="busy || !next" @click="forward">下一页</el-button>
      </div>
    </PanelCard>

    <aside class="catalog-side" aria-label="题库概览">
      <PanelCard :title="activeProblem ? '题目判定分布' : '本页难度'" class="catalog-distribution">
        <template v-if="activeProblem">
          <p class="distribution-title">{{ activeProblem.title }}</p>
          <template v-if="activeProblem.judged_submissions">
            <p class="small muted">{{ activeProblem.judged_submissions }} 次有效评测</p>
            <div
              v-for="[verdict, label, fullLabel, tone] in verdictLabels"
              :key="verdict"
              class="distribution-row verdict-row"
              :data-tone="tone"
            >
              <abbr :title="fullLabel">{{ label }}</abbr>
              <div class="distribution-track" aria-hidden="true">
                <div
                  :style="{
                    width:
                      passRate(
                        activeProblem.verdict_counts?.[verdict] ?? 0,
                        activeProblem.judged_submissions,
                      ) + '%',
                  }"
                />
              </div>
              <span class="tabular small">
                {{ activeProblem.verdict_counts?.[verdict] ?? 0 }} ·
                {{
                  passRate(
                    activeProblem.verdict_counts?.[verdict] ?? 0,
                    activeProblem.judged_submissions,
                  ).toFixed(1)
                }}%
              </span>
            </div>
          </template>
          <p v-else class="muted">暂无有效评测</p>
        </template>
        <template v-else>
          <div
            v-for="row in distribution"
            :key="row.label"
            class="distribution-row"
            :data-level="row.color"
          >
            <span>{{ row.label }}</span>
            <div class="distribution-track" aria-hidden="true">
              <div :style="{ width: passRate(row.count, items.length) + '%' }" />
            </div>
            <span class="tabular">{{ row.count }}</span>
          </div>
        </template>
      </PanelCard>
      <PanelCard title="本页标签">
        <div v-if="tags.length" class="catalog-tags">
          <el-button
            v-for="label in tags"
            :key="label"
            size="small"
            :type="tag === label ? 'primary' : 'default'"
            :aria-pressed="tag === label"
            @click="chooseTag(label)"
            >{{ label }}</el-button
          >
        </div>
        <span v-else class="muted">本页暂无标签</span>
      </PanelCard>
      <PanelCard title="难度分">
        <div v-if="activeProblem" class="score-selection">
          <span>{{ activeProblem.title }}</span>
          <strong v-if="activeProblem.difficulty_level" class="tabular">
            {{ activeProblem.difficulty_level.score }}
          </strong>
          <span v-else class="muted">未标注</span>
        </div>
        <dl v-if="scoreLevels.length" class="details-list score-list">
          <div v-for="level in scoreLevels" :key="level.key">
            <dt><DifficultyTag :value="level.label" :level="level" /></dt>
            <dd class="tabular">{{ level.score }}</dd>
          </div>
        </dl>
        <p v-else-if="!activeProblem" class="muted">暂无难度分</p>
      </PanelCard>
    </aside>
  </div>
</template>

<style scoped>
.catalog {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(15rem, 19rem);
  align-items: start;
  gap: 1rem;
}
.catalog-side {
  display: grid;
  gap: 1rem;
  min-width: 0;
  position: sticky;
  top: var(--site-header-height, 3.8rem);
}
.catalog-search,
.catalog-filters,
.catalog-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}
.catalog-search {
  justify-content: space-between;
  margin-bottom: 0.75rem;
}
.catalog-search .el-input {
  width: min(100%, 25rem);
}
.catalog-tag-toggle {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  cursor: pointer;
}
.catalog-tag-toggle input {
  accent-color: var(--el-color-primary);
}
.catalog-filters {
  margin-bottom: 0.75rem;
}
.catalog-filters .el-button + .el-button,
.catalog-tags .el-button + .el-button {
  margin-left: 0;
}
.catalog-tags .el-button.is-text {
  padding-inline: 0.25rem;
}
.distribution-row {
  display: grid;
  grid-template-columns: 3.5rem minmax(0, 1fr) auto;
  align-items: center;
  gap: 0.5rem;
  margin-block: 0.55rem;
}
.catalog-distribution {
  min-height: 14rem;
}
.distribution-title {
  font-weight: 650;
  overflow-wrap: anywhere;
}
.verdict-row {
  grid-template-columns: 2.35rem minmax(0, 1fr) 5.2rem;
}
.verdict-row abbr {
  font-size: 0.76rem;
  font-weight: 650;
}
.score-selection {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
  justify-content: space-between;
  padding-bottom: 0.55rem;
  border-bottom: 1px solid var(--line);
}
.score-list {
  margin: 0;
}
.distribution-row .tabular {
  text-align: right;
}
.pass-rate {
  text-align: right;
  position: relative;
  overflow: hidden;
  border-radius: 999px;
  background: var(--el-fill-color);
  line-height: 1.35rem;
  padding-inline: 0.65rem;
  font-size: 0.75rem;
}
.pass-rate-fill {
  position: absolute;
  inset: 0 auto 0 0;
  border-radius: inherit;
  background: var(--el-color-primary-light-5);
}
.pass-rate-label {
  position: relative;
}
.distribution-track {
  height: 0.45rem;
  overflow: hidden;
  border-radius: 999px;
  background: var(--el-fill-color);
}
.distribution-track > div {
  height: 100%;
  border-radius: inherit;
  background: var(--el-text-color-secondary);
}
[data-level='easy'] .distribution-track > div,
[data-tone='success'] .distribution-track > div {
  background: var(--el-color-success);
}
[data-level='medium'] .distribution-track > div,
[data-tone='warning'] .distribution-track > div {
  background: var(--el-color-warning);
}
[data-level='hard'] .distribution-track > div,
[data-tone='danger'] .distribution-track > div {
  background: var(--el-color-danger);
}
@media (max-width: 920px) {
  .catalog {
    grid-template-columns: minmax(0, 1fr);
  }
  .catalog-side {
    position: static;
  }
}
</style>
