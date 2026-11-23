<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { agentRunIntent } from '../api/agentRunIntent'
import { api } from '../api/client'
import { trainingRankingLabels } from '../api/trainingRanking'
import type { components } from '../api/generated/schema'
import { boundedPoll } from '../api/polling'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import LearningRunResult from '../components/LearningRunResult.vue'
import LearningSourceExcerpt from '../components/LearningSourceExcerpt.vue'
import SkillVersionComparison from '../components/SkillVersionComparison.vue'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'

type Skill = components['schemas']['SkillVersionView']
type Run = components['schemas']['AgentRunView']
type History = components['schemas']['AgentHistoryPage']
type Purpose = components['schemas']['AgentPurpose']
type ModelTopic = NonNullable<components['schemas']['StartAgentRun']['model_topic']>

const purposes: Record<Purpose, string> = {
  submission_review: '提交复盘',
  guided_help: '渐进式答疑',
  focused_training: '专项训练',
  periodic_review: '阶段复习',
}
const states: Record<Run['state'], string> = {
  queued: '排队中',
  running: '执行中',
  retry_wait: '等待重试',
  cancel_requested: '正在取消',
  cancelled: '已取消',
  succeeded: '已完成',
  failed: '执行失败',
}
const toolNames: Record<string, string> = {
  submission_history: '读取本人提交记录',
  source_evidence: '读取本人源码的差异和诊断位置',
  training_candidates: '查询训练候选题及本人的通过记录',
}
const toolsKey = (tool: Skill['available_tools'][number]) => `${tool.name}:${tool.version}`
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const isUuid = (value: unknown): value is string =>
  typeof value === 'string' && uuidPattern.test(value)
const difficulties = [
  { value: 'easy', label: '简单' },
  { value: 'medium', label: '中等' },
  { value: 'hard', label: '困难' },
]

const route = useRoute()
const router = useRouter()
const { user, refresh } = useSession()
const catalog = ref<Skill[]>([])
const history = ref<History | null>(null)
const run = ref<Run | null>(null)
const selected = ref('')
const parameters = ref<Record<string, unknown>>({})
const enabled = ref(true)
const allowedTools = ref<string[]>([])
const problemIds = ref('')
const tags = ref('')
const submission = ref('')
const modelSourceExcerpt = ref<components['schemas']['SourceLineRange'] | null>(null)
const includeCode = ref(false)
const includeModelReview = ref(false)
const includeModelCodeContext = ref(false)
const includeModelControlFlow = ref(false)
const includeModelComparisonCases = ref(false)
const includeModelEvidenceFocus = ref(false)
const includeModelPlanExplanation = ref(false)
const modelTopic = ref<ModelTopic | ''>('')
const includeModelVerdict = ref(false)
const error = ref('')
const runError = ref('')
const changesSaved = ref(false)
const pollPaused = ref(false)
const loading = ref(false)
const busy = ref(false)
const selectedSkill = computed(() =>
  catalog.value.find((entry) => entry.version_id === selected.value),
)
const previousVersion = computed(() => {
  const current = selectedSkill.value
  if (current?.origin !== 'published') return undefined
  return catalog.value.find(
    (skill) =>
      skill.slug === current.slug && skill.version_id !== current.version_id && skill.binding,
  )
})
const saveLabel = computed(() => {
  const current = selectedSkill.value
  const previous = previousVersion.value
  if (!current || !previous) return '保存配置'
  return current.release > previous.release ? '保存并升级' : '保存并回退'
})
const canUseCode = computed(() =>
  selectedSkill.value?.binding?.authorized_tools?.some((tool) => tool.name === 'source_evidence'),
)
const canUseModelVerdict = computed(
  () =>
    modelTopic.value &&
    selectedSkill.value?.purpose === 'guided_help' &&
    selectedSkill.value.binding?.authorized_tools?.some(
      (tool) => tool.name === 'submission_history',
    ),
)
const canUseModelCodeContext = computed(
  () =>
    selectedSkill.value?.purpose === 'submission_review' &&
    canUseCode.value &&
    includeCode.value &&
    includeModelReview.value,
)
const isTrainingSkill = computed(
  () =>
    selectedSkill.value?.purpose === 'focused_training' ||
    selectedSkill.value?.purpose === 'periodic_review',
)
watch(canUseModelCodeContext, (available) => {
  if (!available) includeModelCodeContext.value = false
})
watch(includeModelCodeContext, (available) => {
  if (!available) {
    includeModelControlFlow.value = false
    includeModelComparisonCases.value = false
  }
})
watch(includeModelReview, (available) => {
  if (!available) includeModelEvidenceFocus.value = false
})
watch(canUseModelVerdict, (available) => {
  if (!available) includeModelVerdict.value = false
})
const runId = computed(() => (isUuid(route.query.run) ? route.query.run : ''))
const loginPath = computed(() => ({ path: '/login', query: { next: route.fullPath } }))
const terminal = (value: Run) => ['cancelled', 'failed', 'succeeded'].includes(value.state)
let loadingController: AbortController | undefined
let actionController: AbortController | undefined

function feedback(cause: unknown) {
  if (cause instanceof ApiError && cause.status === 422) return '请检查技能参数、标识及授权设置。'
  if (cause instanceof ApiError && cause.status === 409) return '当前运行或题目状态已变化，请刷新。'
  return errorText(cause)
}

function applyRun(value: Run) {
  if (!user.value || value.run_id !== runId.value) return
  run.value = value
  const known = history.value?.items.find((item) => item.run_id === value.run_id)
  if (known) known.state = value.state
}

const monitor = boundedPoll<Run>({
  load: async (signal) =>
    unwrap(
      await api.GET('/api/v1/learning/agent/runs/{run_id}', {
        params: { path: { run_id: runId.value } },
        signal,
      }),
    ),
  terminal,
  result: applyRun,
  error: (cause) => (runError.value = feedback(cause)),
  exhausted: () => (pollPaused.value = true),
  maxRequests: 90,
  maxTime: 180_000,
})

function applyFeedback(value: Run) {
  if (!user.value || value.run_id !== runId.value) return
  monitor.pause()
  runError.value = ''
  pollPaused.value = false
  applyRun(value)
}

function syncVisibility() {
  monitor.visible(Boolean(user.value && runId.value && !document.hidden))
}

function refreshRun() {
  runError.value = ''
  pollPaused.value = false
  if (user.value && runId.value) monitor.start()
}

function hydrate(skill: Skill | undefined) {
  modelSourceExcerpt.value = null
  includeCode.value = false
  includeModelReview.value = false
  includeModelCodeContext.value = false
  includeModelControlFlow.value = false
  includeModelComparisonCases.value = false
  includeModelEvidenceFocus.value = false
  includeModelPlanExplanation.value = false
  includeModelVerdict.value = false
  modelTopic.value = ''
  parameters.value = { ...(skill?.binding?.parameters ?? skill?.default_parameters) }
  if (skill?.purpose === 'focused_training' || skill?.purpose === 'periodic_review') {
    parameters.value.ranking_profile ??= null
  }
  enabled.value = skill?.binding?.enabled ?? true
  const defaultTools = skill?.origin === 'builtin' ? skill.available_tools : []
  allowedTools.value = (skill?.binding?.authorized_tools ?? defaultTools).map(toolsKey)
  const ids = parameters.value.problem_ids
  const typedTags = parameters.value.tags
  problemIds.value = Array.isArray(ids) ? ids.join(', ') : ''
  tags.value = Array.isArray(typedTags) ? typedTags.join(', ') : ''
}

async function load() {
  loadingController?.abort()
  if (!user.value) return
  loadingController = new AbortController()
  const { signal } = loadingController
  loading.value = true
  error.value = ''
  try {
    const requestedVersion = isUuid(route.query.skill) ? route.query.skill : undefined
    const [skills, activity] = await Promise.all([
      api
        .GET('/api/v1/learning/agent/skills', {
          params: { query: { version_id: requestedVersion } },
          signal,
        })
        .then(unwrap),
      api.GET('/api/v1/learning/agent/runs', { signal }).then(unwrap),
    ])
    if (signal.aborted) return
    catalog.value = skills.versions
    history.value = activity
    if (!selectedSkill.value) {
      const requested =
        isUuid(route.query.submission) && route.query.purpose !== 'submission_review'
          ? 'guided_help'
          : route.query.purpose
      selected.value =
        skills.versions.find((skill) => skill.version_id === requestedVersion)?.version_id ??
        skills.versions.find((skill) => skill.purpose === requested && skill.binding)?.version_id ??
        skills.versions.find((skill) => skill.purpose === requested)?.version_id ??
        skills.versions.find((skill) => skill.binding?.enabled)?.version_id ??
        skills.versions[0]?.version_id ??
        ''
    }
    const existing = activity.items.find((item) => item.run_id === run.value?.run_id)
    if (existing && run.value) existing.state = run.value.state
  } catch (cause) {
    if (!signal.aborted) error.value = feedback(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

async function act(operation: (signal: AbortSignal) => Promise<void>) {
  if (!user.value || busy.value) return
  actionController = new AbortController()
  const { signal } = actionController
  busy.value = true
  error.value = ''
  changesSaved.value = false
  try {
    await operation(signal)
  } catch (cause) {
    if (!signal.aborted) error.value = feedback(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}

function splitList(value: string) {
  return [
    ...new Set(
      value
        .split(/[,，\s]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ]
}

function saveSkill() {
  const skill = selectedSkill.value
  if (!skill) return
  void act(async (signal) => {
    const input = { ...parameters.value }
    if (skill.purpose === 'submission_review') input.problem_ids = splitList(problemIds.value)
    if (skill.purpose === 'focused_training') input.tags = splitList(tags.value)
    const binding = unwrap(
      await api.PUT('/api/v1/learning/agent/skills/{version_id}', {
        params: { path: { version_id: skill.version_id } },
        headers: csrfHeaders(),
        signal,
        body: {
          enabled: enabled.value,
          parameters: input,
          authorized_tools: skill.available_tools.filter((tool) =>
            allowedTools.value.includes(toolsKey(tool)),
          ),
        },
      }),
    )
    if (signal.aborted) return
    catalog.value = catalog.value.map((entry) =>
      entry.slug === skill.slug
        ? { ...entry, binding: entry.version_id === skill.version_id ? binding : null }
        : entry,
    )
    if (skill.origin === 'published') {
      await router.replace({ query: { ...route.query, skill: skill.version_id } })
    }
    if (!signal.aborted) changesSaved.value = true
  })
}

function start() {
  const skill = selectedSkill.value
  const owner = user.value?.id
  if (!owner || !skill?.binding?.enabled) return
  const topic = skill.purpose === 'guided_help' ? modelTopic.value : ''
  const useModelVerdict = Boolean(canUseModelVerdict.value && includeModelVerdict.value)
  const sourceExcerpt =
    skill.purpose === 'submission_review' && canUseCode.value ? modelSourceExcerpt.value : null
  const useModelReview =
    skill.purpose === 'submission_review' && (includeModelReview.value || Boolean(sourceExcerpt))
  const useCode = Boolean(!topic && canUseCode.value && (includeCode.value || sourceExcerpt))
  const useModelCodeContext =
    !sourceExcerpt && useModelReview && useCode && includeModelCodeContext.value
  const useModelControlFlow = useModelCodeContext && includeModelControlFlow.value
  const useModelComparisonCases = useModelCodeContext && includeModelComparisonCases.value
  const useEvidenceFocus = !sourceExcerpt && useModelReview && includeModelEvidenceFocus.value
  const explainPlan = isTrainingSkill.value && includeModelPlanExplanation.value
  const target =
    useModelVerdict ||
    (!topic && (skill.purpose === 'guided_help' || skill.purpose === 'submission_review'))
      ? submission.value.trim()
      : ''
  if ((target || useModelVerdict) && !isUuid(target)) {
    error.value = '请输入有效的提交标识。'
    return
  }
  void act(async (signal) => {
    const intent = agentRunIntent(
      sessionStorage,
      owner,
      skill.version_id +
        ':' +
        target +
        (useCode ? ':code' : '') +
        (topic ? ':' + topic : '') +
        (useModelVerdict ? ':model-verdict' : '') +
        (useModelReview ? ':review-model' : '') +
        (useModelCodeContext ? ':review-code-counts' : '') +
        (useModelControlFlow ? ':review-control-flow' : '') +
        (useModelComparisonCases ? ':review-comparison-cases' : '') +
        (useEvidenceFocus ? ':review-evidence-focus' : '') +
        (explainPlan ? ':plan-explanation' : '') +
        (sourceExcerpt ? `:source:${sourceExcerpt.start_line}-${sourceExcerpt.end_line}` : ''),
    )
    let session = intent.sessionId
    if (!session) {
      const created = unwrap(
        await api.POST('/api/v1/learning/agent/sessions', {
          body: { purpose: skill.purpose },
          signal,
          headers: csrfHeaders(),
        }),
      )
      if (signal.aborted) return
      session = created.session_id
      intent.bind(session)
    }
    const submitted = unwrap(
      await api.POST('/api/v1/learning/agent/sessions/{session_id}/runs', {
        params: { path: { session_id: session } },
        body: {
          request_id: intent.requestId,
          skill_version_id: skill.version_id,
          submission_id: target || null,
          include_source_evidence: useCode,
          model_topic: topic || null,
          include_model_verdict: useModelVerdict,
          include_model_review: useModelReview,
          include_model_code_context: useModelCodeContext,
          include_model_control_flow: useModelControlFlow,
          include_model_comparison_cases: useModelComparisonCases,
          include_model_evidence_focus: useEvidenceFocus,
          include_model_plan_explanation: explainPlan,
          model_source_excerpt: sourceExcerpt ?? undefined,
        },
        signal,
        headers: csrfHeaders(),
      }),
    )
    if (signal.aborted) return
    modelSourceExcerpt.value = null
    intent.complete()
    await router.replace({ query: { ...route.query, run: submitted.run_id } })
    if (signal.aborted) return
    if (!run.value) applyRun(submitted)
    void load()
  })
}

function cancel() {
  const id = runId.value
  if (!id) return
  void act(async (signal) => {
    const cancelled = unwrap(
      await api.POST('/api/v1/learning/agent/runs/{run_id}/cancel', {
        params: { path: { run_id: id } },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (signal.aborted) return
    applyRun(cancelled)
    refreshRun()
  })
}

function savePlan() {
  const id = runId.value
  if (!id) return
  void act(async (signal) => {
    const plan = unwrap(
      await api.POST('/api/v1/learning/agent/runs/{run_id}/training', {
        params: { path: { run_id: id } },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (!signal.aborted) await router.push('/trainings/' + plan.id)
  })
}

watch(selectedSkill, hydrate)
watch(submission, () => (modelSourceExcerpt.value = null), { flush: 'sync' })
watch(
  () => user.value?.id,
  () => {
    actionController?.abort()
    loadingController?.abort()
    busy.value = false
    loading.value = false
    error.value = ''
    changesSaved.value = false
    catalog.value = []
    selected.value = ''
    history.value = null
    submission.value = isUuid(route.query.submission) ? route.query.submission : ''
    void load()
  },
  { immediate: true },
)
watch(
  [() => user.value?.id, runId],
  () => {
    run.value = null
    runError.value = ''
    syncVisibility()
    refreshRun()
  },
  { immediate: true },
)
onMounted(async () => {
  document.addEventListener('visibilitychange', syncVisibility)
  if (!user.value) {
    try {
      await refresh()
    } catch (cause) {
      error.value = feedback(cause)
    }
  }
})
onBeforeUnmount(() => {
  monitor.dispose()
  document.removeEventListener('visibilitychange', syncVisibility)
  loadingController?.abort()
  actionController?.abort()
})
</script>

<template>
  <PageHeading title="学习助手">
    <RouterLink class="el-button" to="/trainings/skill-drafts">技能草稿</RouterLink>
    <RouterLink class="el-button" to="/trainings/skill-library">技能目录</RouterLink>
    <RouterLink class="el-button" to="/trainings">返回训练计划</RouterLink>
  </PageHeading>
  <p v-if="error" role="alert" class="agent-error">{{ error }}</p>
  <p v-if="!user"><RouterLink :to="loginPath">登录</RouterLink>后查看和运行自己的学习技能。</p>
  <div v-else class="agent-layout native-form">
    <div class="stack">
      <PanelCard title="学习技能" icon="trainings">
        <p v-if="loading && !catalog.length" role="status">正在读取技能…</p>
        <el-button v-else-if="!catalog.length" @click="load">重试读取</el-button>
        <template v-else>
          <label class="field">
            <span>技能与版本</span>
            <select v-model="selected" :disabled="busy">
              <option v-for="skill in catalog" :key="skill.version_id" :value="skill.version_id">
                {{ skill.title }} ·
                {{
                  skill.origin === 'trial'
                    ? '修订 ' + (skill.release - 1) + '（私有试用）'
                    : 'v' + skill.release
                }}{{ skill.origin === 'published' ? '（社区）' : ''
                }}{{ skill.archived_at ? '（已归档）' : ''
                }}{{ skill.binding?.enabled ? '（已启用）' : '' }}
              </option>
            </select>
          </label>
          <SkillVersionComparison
            v-if="previousVersion && selectedSkill"
            :key="previousVersion.version_id + selectedSkill.version_id"
            :saved="previousVersion"
            :selected="selectedSkill"
          />
          <form v-if="selectedSkill" class="stack settings" @submit.prevent="saveSkill">
            <p class="small muted">{{ selectedSkill.description }}</p>
            <p v-if="selectedSkill.origin === 'trial'" class="small muted">
              此修订仅你可见。保存你为本次试用选择的参数和工具权限后即可运行。
            </p>
            <fieldset :disabled="busy">
              <div v-if="selectedSkill.purpose === 'submission_review'" class="fields">
                <label class="field"
                  >最近天数<input
                    v-model.number="parameters.days"
                    type="number"
                    min="1"
                    max="90"
                    required
                /></label>
                <label class="field"
                  >详细程度
                  <select v-model="parameters.detail">
                    <option value="brief">简要</option>
                    <option value="normal">标准</option>
                    <option value="detailed">详细</option>
                  </select>
                </label>
                <label class="field wide"
                  >题目标识（可选，用逗号分隔）<input v-model="problemIds" maxlength="760"
                /></label>
              </div>
              <div v-else-if="selectedSkill.purpose === 'guided_help'" class="fields">
                <label class="field"
                  >提示等级<select v-model="parameters.hint_level">
                    <option value="concept">概念提醒</option>
                    <option value="location">位置线索</option>
                    <option value="local_change">局部修改建议</option>
                  </select></label
                >
                <label class="check"
                  ><input
                    v-model="parameters.allow_code_examples"
                    type="checkbox"
                  />可用时允许代码示例</label
                >
              </div>
              <div v-else-if="selectedSkill.purpose === 'focused_training'" class="fields">
                <label class="field wide"
                  >目标知识点（用逗号分隔）<input v-model="tags" maxlength="650"
                /></label>
                <label class="check wide">
                  <input v-model="parameters.cover_each_goal" type="checkbox" />
                  用不同题目覆盖各知识点
                </label>
                <label class="field wide"
                  >训练目标<input v-model="parameters.goal" maxlength="200"
                /></label>
                <label class="field wide"
                  >每日分钟数<input
                    v-model.number="parameters.daily_minutes"
                    type="number"
                    min="5"
                    max="180"
                    required
                /></label>
                <label class="field"
                  >最低难度<select v-model="parameters.minimum_difficulty">
                    <option v-for="level in difficulties" :key="level.value" :value="level.value">
                      {{ level.label }}
                    </option>
                  </select></label
                >
                <label class="field"
                  >最高难度<select v-model="parameters.maximum_difficulty">
                    <option v-for="level in difficulties" :key="level.value" :value="level.value">
                      {{ level.label }}
                    </option>
                  </select></label
                >
              </div>
              <div v-else class="fields">
                <label class="field"
                  >复习周期（天）<input
                    v-model.number="parameters.cycle_days"
                    type="number"
                    min="7"
                    max="90"
                    required
                /></label>
                <label class="field"
                  >每周训练次数<input
                    v-model.number="parameters.sessions_per_week"
                    type="number"
                    min="1"
                    max="7"
                    required
                /></label>
                <label class="field wide"
                  >阶段目标<input v-model="parameters.stage_goal" maxlength="200"
                /></label>
              </div>
              <label v-if="isTrainingSkill" class="field ranking-field">
                排序侧重
                <select v-model="parameters.ranking_profile">
                  <option :value="null">常规排序</option>
                  <option
                    v-for="(label, profile) in trainingRankingLabels"
                    :key="profile"
                    :value="profile"
                  >
                    {{ label }}
                  </option>
                </select>
              </label>
            </fieldset>
            <fieldset class="stack" :disabled="busy">
              <legend>工具授权</legend>
              <label
                v-for="tool in selectedSkill.available_tools"
                :key="toolsKey(tool)"
                class="check"
              >
                <input v-model="allowedTools" type="checkbox" :value="toolsKey(tool)" />
                {{ toolNames[tool.name] ?? tool.name }}
              </label>
              <label class="check"><input v-model="enabled" type="checkbox" /> 启用此版本</label>
            </fieldset>
            <div class="controls">
              <el-button native-type="submit" :disabled="busy">{{ saveLabel }}</el-button>
              <span v-if="changesSaved" role="status" class="small muted">已保存</span>
            </div>
          </form>
          <div v-if="selectedSkill?.binding?.enabled" class="stack launch">
            <template v-if="selectedSkill.purpose === 'submission_review' && !modelSourceExcerpt">
              <label class="check">
                <input v-model="includeModelReview" type="checkbox" :disabled="busy" />
                本次生成模型复盘建议
              </label>
              <small v-if="includeModelReview" class="muted"
                >模型会收到评测计数和同题版本的结果变化次数。</small
              >
              <label v-if="includeModelReview" class="check">
                <input v-model="includeModelEvidenceFocus" type="checkbox" :disabled="busy" />
                让模型选择一类线索供本次复核
              </label>
              <small v-if="includeModelEvidenceFocus" class="muted">
                模型会收到可选类别名称，学习助手据此核对一条本人评测。
              </small>
            </template>
            <template v-if="isTrainingSkill">
              <label class="check">
                <input v-model="includeModelPlanExplanation" type="checkbox" :disabled="busy" />
                本次生成模型训练说明
              </label>
              <small v-if="includeModelPlanExplanation" class="muted">
                模型只会收到题量、难度分布、已设训练时间和目标覆盖数量。
              </small>
            </template>
            <label v-if="selectedSkill.purpose === 'guided_help'" class="field">
              模型概念提示
              <select v-model="modelTopic" :disabled="busy">
                <option value="">仅使用规则提示</option>
                <option value="binary_search">二分查找</option>
                <option value="graph_traversal">图遍历</option>
                <option value="dynamic_programming">动态规划</option>
              </select>
              <small v-if="modelTopic && !includeModelVerdict" class="muted"
                >本次只将所选主题发送给模型服务。</small
              >
            </label>
            <label v-if="canUseModelVerdict" class="check">
              <input v-model="includeModelVerdict" type="checkbox" :disabled="busy" />
              同时发送指定本人提交的评测类别
            </label>
            <small v-if="includeModelVerdict" class="muted">
              模型会收到所选主题和一次已完成的评测类别，例如答案错误。
            </small>
            <label v-if="canUseCode && !modelTopic" class="check">
              <input v-model="includeCode" type="checkbox" :disabled="busy" /> 本次使用本人代码证据
            </label>
            <label v-if="canUseModelCodeContext && !modelSourceExcerpt" class="check">
              <input v-model="includeModelCodeContext" type="checkbox" :disabled="busy" />
              发送代码改动计数和编译问题类别
            </label>
            <label
              v-if="canUseModelCodeContext && includeModelCodeContext && !modelSourceExcerpt"
              class="check"
            >
              <input v-model="includeModelControlFlow" type="checkbox" :disabled="busy" />
              同时发送修改区段的分支、循环和返回关键字计数
            </label>
            <label
              v-if="canUseModelCodeContext && includeModelCodeContext && !modelSourceExcerpt"
              class="check"
            >
              <input v-model="includeModelComparisonCases" type="checkbox" :disabled="busy" />
              同时发送比较条件变化的计数
            </label>
            <small v-if="includeModelComparisonCases && !modelSourceExcerpt" class="muted">
              比较条件只发送小于、等于、大于三种关系受影响的次数。
            </small>
            <label
              v-if="
                selectedSkill.purpose === 'submission_review' ||
                (canUseModelVerdict && includeModelVerdict) ||
                (!modelTopic && selectedSkill.purpose === 'guided_help')
              "
              class="field"
            >
              {{ includeModelVerdict ? '本人提交标识' : '提交标识（可选）' }}
              <input
                v-model="submission"
                :placeholder="
                  selectedSkill.purpose === 'submission_review'
                    ? '留空复盘配置范围内的记录'
                    : includeModelVerdict
                      ? '填写要引用的本人提交标识'
                      : '留空使用最近一次提交'
                "
                maxlength="36"
                :disabled="busy"
              />
            </label>
            <LearningSourceExcerpt
              v-if="
                selectedSkill.purpose === 'submission_review' && canUseCode && isUuid(submission)
              "
              :key="user.id + ':' + selectedSkill.version_id + ':' + submission"
              v-model="modelSourceExcerpt"
              :submission-id="submission"
              :disabled="busy"
            />
            <div>
              <el-button type="primary" :disabled="busy" @click="start">
                {{ selectedSkill.origin === 'trial' ? '运行私有试用' : '用已保存配置启动' }}
              </el-button>
            </div>
          </div>
        </template>
      </PanelCard>
    </div>
    <div class="stack">
      <p v-if="runError" role="alert">
        {{ runError }} <button type="button" @click="refreshRun">重试读取</button>
      </p>
      <p v-if="pollPaused" role="status">
        已暂停自动刷新。<button type="button" @click="refreshRun">继续查看</button>
      </p>
      <LearningRunResult
        v-if="run"
        :run="run"
        :busy="busy"
        @cancel="cancel"
        @refresh="refreshRun"
        @save="savePlan"
        @feedback="applyFeedback"
      />
      <PanelCard title="最近运行" icon="submissions">
        <p v-if="loading && !history" role="status">正在读取运行…</p>
        <p v-else-if="history && !history.items.length" class="small muted">暂无运行记录。</p>
        <ul v-else class="run-list">
          <li v-for="item in history?.items" :key="item.run_id">
            <RouterLink
              :to="{ query: { ...route.query, run: item.run_id } }"
              :aria-current="runId === item.run_id ? 'true' : undefined"
            >
              <span>{{ purposes[item.purpose] }}</span>
              <span class="muted small"
                >{{ states[item.state] }} · {{ displayTime(item.created_at) }}</span
              >
            </RouterLink>
          </li>
        </ul>
        <p v-if="history?.truncated" class="small muted">显示最近 30 次运行。</p>
      </PanelCard>
    </div>
  </div>
</template>

<style scoped>
.agent-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 1rem;
  align-items: start;
}
.agent-error {
  margin-bottom: 1rem;
}
.settings,
.launch {
  margin-top: 1rem;
}
.launch {
  border-top: 1px solid var(--line);
  padding-top: 1rem;
}
.fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}
.field {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
}
.ranking-field {
  margin-top: 0.75rem;
}
.wide {
  grid-column: 1 / -1;
}
input:not([type='checkbox']),
select {
  width: 100%;
}
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
legend {
  margin-bottom: 0.4rem;
  font-weight: 600;
}
.check,
.controls {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.run-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.run-list a {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0.35rem;
  padding: 0.55rem;
  border-bottom: 1px solid var(--line);
}
.run-list a[aria-current='true'] {
  border-radius: var(--el-border-radius-base);
  background: var(--el-color-primary-light-9);
}
@media (max-width: 800px) {
  .agent-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
