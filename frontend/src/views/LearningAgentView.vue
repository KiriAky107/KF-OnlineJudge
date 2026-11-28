<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { agentRunIntent } from '../api/agentRunIntent'
import { api } from '../api/client'
import { trainingRankingLabels } from '../api/trainingRanking'
import type { components } from '../api/generated/schema'
import { boundedPoll } from '../api/polling'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import UiIcon from '../components/UiIcon.vue'
import TrainingSetChatView from './TrainingSetChatView.vue'
import PageHeading from '../components/PageHeading.vue'
import LearningRunResult from '../components/LearningRunResult.vue'
import LearningChatProgress from '../components/LearningChatProgress.vue'
import AgentTimelineTurn from '../components/AgentTimelineTurn.vue'
import { sha256Fingerprint } from '../lib/requestCrypto'
import LearningSourceExcerpt from '../components/LearningSourceExcerpt.vue'
import LearningReviewSubmission from '../components/LearningReviewSubmission.vue'
import LearningPublicDiagnostic from '../components/LearningPublicDiagnostic.vue'
import LearningBatchDiagnostic from '../components/LearningBatchDiagnostic.vue'
import LearningPersonalMistakes from '../components/LearningPersonalMistakes.vue'
import LearningMistakePractice from '../components/LearningMistakePractice.vue'
import SkillVersionComparison from '../components/SkillVersionComparison.vue'
import { useSession } from '../composables/useSession'
import { useContestAgentAccess } from '../composables/useContestAgentAccess'
import { useAssistantTimeline } from '../composables/useAssistantTimeline'
import { displayTime } from '../lib/presentation'
import { assistantPage } from '../lib/assistantNavigation'
import { hintIndex, hintLevels, type HintLevel } from '../lib/hintLevels'
import {
  hasTrainingCandidateAccess,
  isProblemSearchQuestion,
  isTrainingMessage,
  trainingChatSkill,
} from '../lib/trainingSetChat'
import { mistakePracticeTurn } from '../lib/mistakePractice'
import { isSubmissionReviewQuestion } from '../lib/assistantReview'
import { periodicReviewTurn, type PeriodicReviewRequest } from '../lib/assistantPeriodicReview'

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

const props = defineProps<{
  embedded?: boolean
  floating?: boolean
  contextSubmission?: string
  contextProblemVersion?: string
  contextTrainingId?: string
}>()
const trainingVisited = ref(false)
const trainingChat = ref<InstanceType<typeof TrainingSetChatView>>()
const trainingRecord = ref<Run>()
const switchingComposer = ref(false)
const settingsOpen = ref(false)
const historyOpen = ref(false)
const mistakesOpen = ref(false)
const mistakeRevision = ref(0)
const withdrawnMistakeId = ref<string>()
const practiceResult = ref<components['schemas']['MistakePractice']>()
const practiceSource = ref<components['schemas']['DiagnosticMistakeEvidence']>()
const practiceQuestion = ref('')
const practiceNotice = ref('')
const practiceBusy = ref(false)
const waitingForPracticeTime = ref(false)
const practicePanel = ref<HTMLElement>()
let practiceRequest: AbortController | undefined
const questionDraft = ref('')
const questionLimit = 1200
const pendingQuestion = ref<{ key: string | undefined; text: string }>()
const conversationEnd = ref<HTMLElement>()
const chatIncludeDetails = ref(false)
const chatSourceExcerpt = ref<components['schemas']['SourceLineRange'] | null>(null)
const chosenHintLevel = ref<HintLevel | null>(null)
const pendingPeriodicQuestion = ref('')
const periodicNotice = ref('')
const promptLabel = ref('')
const queryRun = () => (props.embedded ? 'helpRun' : 'run')
const route = useRoute()
const router = useRouter()
const contextProblem = computed(() => {
  if (props.contextProblemVersion) return props.contextProblemVersion
  if (!props.embedded && isUuid(route.query.problem)) return route.query.problem
  return undefined
})
const contextTraining = computed(() => {
  if (props.contextTrainingId) return props.contextTrainingId
  if (!props.embedded && isUuid(route.query.training)) return route.query.training
  return undefined
})
const fullPage = computed(() =>
  assistantPage(route.query, contextProblem.value, contextTraining.value),
)
type AssistantTab = 'agent' | 'training'
const activeChat = computed<AssistantTab>(() => {
  const tab = route.query.assistantTab
  return tab === 'training' || (tab !== 'agent' && isUuid(route.query.trainingRun))
    ? 'training'
    : 'agent'
})
watch(
  activeChat,
  (tab) => {
    if (tab === 'training') trainingVisited.value = true
  },
  { immediate: true },
)
const { user, refresh } = useSession()
const { blocked: contestBlocked, recheck } = useContestAgentAccess()
const catalog = ref<Skill[]>([])
const settingsCatalog = computed(() =>
  props.embedded ? catalog.value.filter((skill) => skill.purpose === 'guided_help') : catalog.value,
)
const history = ref<History | null>(null)
const historyLoading = ref(false)
const historyError = ref('')
const historyVisible = computed(() => historyOpen.value && !!user.value && !contestBlocked.value)
let historyRequest: AbortController | undefined
const run = ref<Run | null>(null)
const hintProblem = computed(() => contextProblem.value ?? run.value?.chat?.problem_version_id)
const hintTraining = computed(
  () =>
    contextTraining.value ??
    (run.value?.chat?.problem_version_id === hintProblem.value
      ? (run.value?.chat?.training_id ?? undefined)
      : undefined),
)
const lastHintTurn = computed(() =>
  hintProblem.value &&
  run.value?.chat?.problem_version_id === hintProblem.value &&
  (run.value?.chat?.training_id ?? undefined) === hintTraining.value
    ? run.value
    : null,
)
const hintCeiling = ref<HintLevel | null>(null)
const hintCeilingError = ref(false)
const hintCeilingRefresh = ref(0)
watch(
  [hintProblem, hintTraining, () => user.value?.id, contestBlocked, hintCeilingRefresh],
  async ([problem, training, owner, blocked], _previous, cleanup) => {
    hintCeiling.value = null
    hintCeilingError.value = false
    if (!owner || !problem || blocked) return
    const controller = new AbortController()
    cleanup(() => controller.abort())
    try {
      const policy = unwrap(
        await api.GET('/api/v1/learning/agent/hint-ceiling', {
          params: { query: { problem_version_id: problem, training_id: training } },
          signal: controller.signal,
        }),
      )
      if (!controller.signal.aborted) hintCeiling.value = policy.max_hint_level
    } catch {
      if (!controller.signal.aborted) hintCeilingError.value = true
    }
  },
  { immediate: true },
)
const lastHintIndex = computed(() => hintIndex(lastHintTurn.value?.chat?.hint_level))
const unlockedHintIndex = computed(() => {
  const previous = lastHintTurn.value
  const completed =
    previous?.chat?.hint_level &&
    previous.state === 'succeeded' &&
    previous.result?.purpose === 'guided_help' &&
    previous.result.model_status === 'generated'
  return Math.min(hintIndex(hintCeiling.value), lastHintIndex.value + (completed ? 1 : 0))
})
const hintLevel = computed({
  get: () =>
    hintLevels[
      Math.min(
        unlockedHintIndex.value,
        chosenHintLevel.value === null ? lastHintIndex.value : hintIndex(chosenHintLevel.value),
      )
    ]?.value ?? 'L0',
  set: (value: HintLevel) => {
    chosenHintLevel.value = value
  },
})
watch([hintProblem, hintTraining, () => lastHintTurn.value?.run_id], () => {
  chosenHintLevel.value = null
})
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
  settingsCatalog.value.find((entry) => entry.version_id === selected.value),
)
const continuingSkillId = computed(() => (run.value?.chat ? run.value.skill_version_id : null))
const chatSkill = computed(() => {
  const guided = catalog.value.filter((skill) => skill.purpose === 'guided_help')
  if (continuingSkillId.value) {
    return guided.find((skill) => skill.version_id === continuingSkillId.value)
  }
  return (
    guided.find((skill) => skill.version_id === selected.value && skill.binding?.enabled) ??
    guided.find((skill) => skill.binding?.enabled) ??
    guided.find((skill) => skill.origin === 'builtin' && skill.binding) ??
    guided.find((skill) => skill.origin === 'builtin')
  )
})
const reviewSkill = computed(() => {
  const reviews = catalog.value.filter((skill) => skill.purpose === 'submission_review')
  return (
    reviews.find(
      (skill) =>
        skill.binding?.enabled &&
        skill.binding.authorized_tools.some(({ name }) => name === 'submission_history'),
    ) ?? reviews.find((skill) => skill.origin === 'builtin')
  )
})
const reviewEnabled = computed(
  () =>
    reviewSkill.value?.binding?.enabled &&
    reviewSkill.value.binding.authorized_tools.some(({ name }) => name === 'submission_history'),
)
const practiceSkill = computed(() => trainingChatSkill(catalog.value))
const practiceEnabled = computed(
  () => practiceSkill.value && hasTrainingCandidateAccess(practiceSkill.value),
)
const periodicSkill = computed(() => {
  const versions = catalog.value.filter((skill) => skill.purpose === 'periodic_review')
  return (
    versions.find(
      (skill) =>
        skill.binding?.enabled &&
        skill.binding.authorized_tools.some(({ name }) => name === 'training_candidates'),
    ) ?? versions.find((skill) => skill.origin === 'builtin')
  )
})
const periodicEnabled = computed(
  () =>
    periodicSkill.value?.binding?.enabled &&
    periodicSkill.value.binding.authorized_tools.some(({ name }) => name === 'training_candidates'),
)
const authorizedChatTarget = computed(() => isUuid(submission.value.trim()))
const composerSkill = computed(() =>
  toPeriodic.value ? undefined : toReview.value ? reviewSkill.value : chatSkill.value,
)
const chatCanUseDetails = computed(
  () =>
    !toReview.value &&
    authorizedChatTarget.value &&
    (hintLevel.value === 'L2' || hintLevel.value === 'L3') &&
    composerSkill.value?.binding?.authorized_tools?.some(
      (tool) => tool.name === 'submission_history',
    ),
)
const chatCanUseSource = computed(
  () =>
    authorizedChatTarget.value &&
    (toReview.value || hintLevel.value === 'L3') &&
    composerSkill.value?.binding?.authorized_tools?.some((tool) => tool.name === 'source_evidence'),
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
const runId = computed(() => {
  const value = route.query[queryRun()]
  return isUuid(value) ? value : ''
})

function onMistakeSaved() {
  withdrawnMistakeId.value = undefined
  mistakeRevision.value += 1
}

const trainingId = computed(() => (isUuid(route.query.trainingRun) ? route.query.trainingRun : ''))
watch(
  trainingId,
  (id) => {
    if (id) trainingVisited.value = true
  },
  { immediate: true },
)
const conversationSession = computed(() => {
  if (!user.value || contestBlocked.value) return undefined
  const chat = run.value?.run_id === runId.value ? run.value : null
  const training =
    trainingRecord.value?.run_id === trainingId.value ? trainingRecord.value : undefined
  return (activeChat.value === 'training' ? (training ?? chat) : (chat ?? training))?.session_id
})
const displayedRun = computed(() =>
  activeChat.value === 'training' ? trainingId.value : runId.value,
)
const { timeline, conversationId, earlier, loadingTimeline, timelineError, loadTimeline } =
  useAssistantTimeline(
    () => conversationSession.value,
    () => displayedRun.value || undefined,
    feedback,
  )
watch(timeline, (turns) => {
  if (trainingId.value && runId.value) return
  const latest = turns.slice().reverse()
  const training = trainingId.value ? undefined : latest.find((turn) => turn.training_request)
  const guided = runId.value
    ? undefined
    : latest.find((turn) => turn.purpose === 'guided_help' && turn.question)
  if (!training && !guided) return
  const query = { ...route.query }
  query.assistantTab = activeChat.value
  if (training) query.trainingRun = training.run_id
  if (guided) query[queryRun()] = guided.run_id
  void router.replace({ query })
})
const timelineParts = computed(() => {
  const rows = timeline.value
  const selectedId = displayedRun.value
  const index = rows.findIndex(({ run_id }) => run_id === selectedId)
  if (index >= 0) return { before: rows.slice(0, index), after: rows.slice(index + 1) }
  const focused = activeChat.value === 'training' ? trainingRecord.value : run.value
  if (!focused || focused.run_id !== selectedId) return { before: rows, after: [] }
  const created = Date.parse(focused.created_at)
  const firstLater = rows.findIndex(
    ({ created_at, run_id }) =>
      Date.parse(created_at) > created ||
      (Date.parse(created_at) === created && run_id > selectedId),
  )
  return firstLater < 0
    ? { before: rows, after: [] }
    : { before: rows.slice(0, firstLater), after: rows.slice(firstLater) }
})
function draftKey(id = runId.value || trainingId.value) {
  const owner = user.value?.id
  if (!owner) return undefined
  const entry = props.embedded && !props.floating ? 'workbench' : 'assistant'
  const problem = contextProblem.value ?? 'general'
  const scope = contextTraining.value ? `${problem}:${contextTraining.value}` : problem
  return `kf-oj:agent-composer:v1:${owner}:${entry}:${scope}:${id || 'new'}`
}
const composerKey = computed(() => draftKey())
function saveQuestionDraft(key: string | undefined, text: string) {
  const value = text.slice(0, questionLimit)
  if (key === composerKey.value) questionDraft.value = value
  if (!key) return
  try {
    if (value) sessionStorage.setItem(key, value)
    else sessionStorage.removeItem(key)
  } catch {
    // The composer remains usable without browser storage.
  }
}
const composer = computed({
  get: () => questionDraft.value,
  set: (value) => saveQuestionDraft(composerKey.value, value),
})
const sendingQuestion = computed(() => {
  const pending = pendingQuestion.value
  return pending?.key === composerKey.value ? (pending?.text ?? '') : ''
})
watch(sendingQuestion, async (question) => {
  if (!question) return
  await nextTick()
  conversationEnd.value?.scrollIntoView({ block: 'nearest' })
})
watch(
  composerKey,
  (key) => {
    questionDraft.value = ''
    chatIncludeDetails.value = false
    chatSourceExcerpt.value = null
    if (!key) return
    try {
      questionDraft.value = (sessionStorage.getItem(key) ?? '').slice(0, questionLimit)
    } catch {
      // The composer remains usable without browser storage.
    }
  },
  { immediate: true, flush: 'sync' },
)
const currentPurpose = computed(
  () =>
    run.value?.result?.purpose ??
    history.value?.items.find((item) => item.run_id === runId.value)?.purpose,
)
const loginPath = computed(() => ({ path: '/login', query: { next: route.fullPath } }))
const terminal = (value: Run) => ['cancelled', 'failed', 'succeeded'].includes(value.state)
const chatPending = computed(
  () =>
    Boolean(runId.value && !run.value && !runError.value) ||
    Boolean(
      run.value &&
      (run.value.chat || run.value.review_question || run.value.periodic_review) &&
      !terminal(run.value),
    ),
)
let loadingController: AbortController | undefined
let actionController: AbortController | undefined

function feedback(cause: unknown) {
  if (cause instanceof ApiError && cause.status === 403) void recheck()
  if (cause instanceof ApiError && cause.status === 422) return '请检查技能参数、标识及授权设置。'
  if (cause instanceof ApiError && cause.status === 409) return '当前运行或题目状态已变化，请刷新。'
  return errorText(cause)
}

function applyRun(value: Run) {
  if (!user.value || contestBlocked.value || value.run_id !== runId.value) return
  if (value.training_request) {
    const query = { ...route.query }
    query.assistantTab = 'training'
    query.trainingRun = value.run_id
    delete query[queryRun()]
    void router.replace({ query })
    return
  }
  run.value = value
  if (!submission.value && value.result?.purpose === 'submission_review') {
    submission.value = value.result.anchor_submission_id ?? ''
  }
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
  interval: 1200,
  maxRequests: 150,
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
  monitor.visible(Boolean(user.value && !contestBlocked.value && runId.value && !document.hidden))
}

function refreshRun() {
  runError.value = ''
  pollPaused.value = false
  if (user.value && !contestBlocked.value && runId.value) monitor.start()
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
    const skills = unwrap(
      await api.GET('/api/v1/learning/agent/skills', {
        params: { query: { version_id: requestedVersion } },
        signal,
      }),
    )
    if (signal.aborted) return
    catalog.value = skills.versions
    if (!selectedSkill.value) {
      const requested =
        props.embedded ||
        (isUuid(route.query.submission) && route.query.purpose !== 'submission_review')
          ? 'guided_help'
          : route.query.purpose
      selected.value =
        settingsCatalog.value.find((skill) => skill.version_id === requestedVersion)?.version_id ??
        settingsCatalog.value.find((skill) => skill.purpose === requested && skill.binding)
          ?.version_id ??
        settingsCatalog.value.find((skill) => skill.purpose === requested)?.version_id ??
        settingsCatalog.value.find((skill) => skill.binding?.enabled)?.version_id ??
        settingsCatalog.value[0]?.version_id ??
        ''
    }
  } catch (cause) {
    if (!signal.aborted) error.value = feedback(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

async function loadHistory() {
  historyRequest?.abort()
  if (!historyVisible.value) return
  const { signal } = (historyRequest = new AbortController())
  historyLoading.value = true
  historyError.value = ''
  try {
    const page = unwrap(
      await api.GET('/api/v1/learning/agent/conversations', {
        params: { query: { problem_version_id: contextProblem.value } },
        signal,
      }),
    )
    if (signal.aborted) return
    const existing = page.items.find((item) => item.run_id === run.value?.run_id)
    if (existing && run.value) existing.state = run.value.state
    history.value = page
  } catch (cause) {
    if (!signal.aborted) historyError.value = feedback(cause)
  } finally {
    if (!signal.aborted) historyLoading.value = false
  }
}
watch([historyVisible, () => user.value?.id, contextProblem], () => {
  void loadHistory()
})

async function act(operation: (signal: AbortSignal) => Promise<void>) {
  if (!user.value || contestBlocked.value || busy.value) return
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
    if (!signal.aborted) {
      busy.value = false
      pendingQuestion.value = undefined
    }
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

function saveSkill(mode: 'settings' | 'review' | 'periodic' | 'practice' = 'settings') {
  const quick = mode !== 'settings'
  const skill =
    mode === 'review'
      ? reviewSkill.value
      : mode === 'periodic'
        ? periodicSkill.value
        : mode === 'practice'
          ? practiceSkill.value
          : selectedSkill.value
  if (!skill) return
  void act(async (signal) => {
    const input = {
      ...(quick ? (skill.binding?.parameters ?? skill.default_parameters) : parameters.value),
    }
    if (!quick && skill.purpose === 'submission_review')
      input.problem_ids = splitList(problemIds.value)
    if (!quick && skill.purpose === 'focused_training') input.tags = splitList(tags.value)
    const authorizedTools = skill.available_tools.filter((tool) =>
      quick
        ? (mode === 'review' && tool.name === 'submission_history') ||
          ((mode === 'periodic' || mode === 'practice') && tool.name === 'training_candidates') ||
          skill.binding?.authorized_tools.some((granted) => toolsKey(granted) === toolsKey(tool))
        : allowedTools.value.includes(toolsKey(tool)),
    )
    const binding = unwrap(
      await api.PUT('/api/v1/learning/agent/skills/{version_id}', {
        params: { path: { version_id: skill.version_id } },
        headers: csrfHeaders(),
        signal,
        body: {
          enabled: quick || enabled.value,
          parameters: input,
          authorized_tools: authorizedTools,
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

function start(question?: string, workflow?: 'review' | PeriodicReviewRequest) {
  const review = workflow === 'review'
  const periodic = typeof workflow === 'object' ? workflow : undefined
  const freeQuestion = Boolean(question && !workflow)
  const previous = run.value
  const problemVersion = contextProblem.value ?? previous?.chat?.problem_version_id ?? null
  const chatBinding = chatSkill.value?.binding
  const practiceGranted = Boolean(
    chatBinding?.enabled &&
    chatBinding.authorized_tools.some(({ name }) => name === 'submission_history'),
  )
  const practiceToolOnly = freeQuestion && !problemVersion && practiceGranted
  const ownPracticeQuestion =
    !!question &&
    /(?:我(?:的|最近)(?:做题|刷题|练习|学习|提交|通过|做了)|(?:my|my recent) practice)/i.test(
      question,
    )
  const usesChatEvidence =
    (chatCanUseDetails.value && chatIncludeDetails.value) ||
    (chatCanUseSource.value && !!chatSourceExcerpt.value) ||
    practiceToolOnly ||
    (ownPracticeQuestion && practiceGranted)
  const skill = review
    ? reviewSkill.value
    : periodic
      ? periodicSkill.value
      : freeQuestion
        ? usesChatEvidence
          ? chatSkill.value
          : undefined
        : selectedSkill.value
  const owner = user.value?.id
  if (!owner || ((!freeQuestion || usesChatEvidence) && !skill?.binding?.enabled)) return
  const purpose = skill?.purpose ?? 'guided_help'
  const skillVersionId = skill?.version_id
  if (question && !workflow && problemVersion && hintCeiling.value === null) return
  const parent =
    question &&
    !workflow &&
    previous?.chat &&
    (previous.chat.problem_version_id ?? null) === problemVersion &&
    (previous.chat.training_id ?? undefined) === hintTraining.value &&
    (!skillVersionId || previous.skill_version_id === skillVersionId) &&
    terminal(previous)
      ? previous
      : null
  const chat =
    question && !workflow
      ? {
          question,
          problem_version_id: problemVersion,
          previous_run_id: parent?.run_id ?? null,
          include_submission_details: Boolean(chatCanUseDetails.value && chatIncludeDetails.value),
          ...(practiceToolOnly ? { practice_tool_only: true } : {}),
          ...(problemVersion ? { hint_level: hintLevel.value } : {}),
          ...(hintTraining.value ? { training_id: hintTraining.value } : {}),
        }
      : undefined
  const submittedDraftKey = question ? composerKey.value : undefined
  const anchor = conversationSession.value
  const topic = !question && purpose === 'guided_help' ? modelTopic.value : ''
  const useModelVerdict = Boolean(
    !question && canUseModelVerdict.value && includeModelVerdict.value,
  )
  const sourceExcerpt = periodic
    ? null
    : question
      ? chatCanUseSource.value
        ? chatSourceExcerpt.value
        : null
      : purpose === 'submission_review' && canUseCode.value
        ? modelSourceExcerpt.value
        : null
  const useModelReview =
    purpose === 'submission_review' &&
    (review || includeModelReview.value || Boolean(sourceExcerpt))
  const useCode = question
    ? Boolean(sourceExcerpt)
    : Boolean(!topic && canUseCode.value && (includeCode.value || sourceExcerpt))
  const useModelCodeContext =
    !question && !sourceExcerpt && useModelReview && useCode && includeModelCodeContext.value
  const useModelControlFlow = useModelCodeContext && includeModelControlFlow.value
  const useModelComparisonCases = useModelCodeContext && includeModelComparisonCases.value
  const useEvidenceFocus =
    !question && !sourceExcerpt && useModelReview && includeModelEvidenceFocus.value
  const explainPlan = !question && isTrainingSkill.value && includeModelPlanExplanation.value
  const target = (
    question
      ? review || chat?.include_submission_details || sourceExcerpt
      : useModelVerdict ||
        (!topic && (purpose === 'guided_help' || purpose === 'submission_review'))
  )
    ? submission.value.trim()
    : ''
  if (props.embedded && !question && !topic && !target) {
    error.value = '针对本题答疑需要一条本人提交。你也可以在技能设置中选择概念提示。'
    return
  }
  if ((target || useModelVerdict) && !isUuid(target)) {
    error.value = '请输入有效的提交标识。'
    return
  }
  void act(async (signal) => {
    if (question) pendingQuestion.value = { key: submittedDraftKey, text: question }
    const intent = agentRunIntent(
      sessionStorage,
      owner,
      (skillVersionId ?? 'basic-chat') +
        ':' +
        (anchor ?? '') +
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
        (sourceExcerpt ? `:source:${sourceExcerpt.start_line}-${sourceExcerpt.end_line}` : '') +
        (chat ? ':chat:' + sha256Fingerprint(new TextEncoder().encode(JSON.stringify(chat))) : '') +
        (review && question
          ? ':review:' + sha256Fingerprint(new TextEncoder().encode(question))
          : '') +
        (periodic
          ? ':periodic:' + sha256Fingerprint(new TextEncoder().encode(JSON.stringify(periodic)))
          : ''),
    )
    let session = parent?.session_id ?? intent.sessionId
    if (!session) {
      const created = unwrap(
        await api.POST('/api/v1/learning/agent/sessions', {
          body: { purpose, conversation_session_id: anchor },
          signal,
          headers: csrfHeaders(),
        }),
      )
      if (signal.aborted) return
      session = created.session_id
    }
    intent.bind(session)
    const submitted = unwrap(
      await api.POST('/api/v1/learning/agent/sessions/{session_id}/runs', {
        params: { path: { session_id: session } },
        body: {
          request_id: intent.requestId,
          skill_version_id: skillVersionId,
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
          chat,
          review_question: review ? question : undefined,
          periodic_review: periodic,
        },
        signal,
        headers: csrfHeaders(),
      }),
    )
    if (signal.aborted) return
    modelSourceExcerpt.value = null
    const currentDraft = !question || submittedDraftKey === composerKey.value
    if (question) {
      saveQuestionDraft(submittedDraftKey, '')
      if (currentDraft) {
        chatIncludeDetails.value = false
        chatSourceExcerpt.value = null
      }
    }
    intent.complete()
    if (!currentDraft) {
      void loadHistory()
      return
    }
    promptLabel.value = purposes[purpose]
    const query = { ...route.query }
    query.assistantTab = 'agent'
    query[queryRun()] = submitted.run_id
    if (workflow) delete query.purpose
    await router.replace({ query })
    if (signal.aborted) return
    pendingQuestion.value = undefined
    if (!run.value) applyRun(submitted)
    void loadHistory()
  })
}

function selectHistory(purpose: Purpose) {
  historyOpen.value = false
  promptLabel.value = purposes[purpose]
}

function turnLocation(id: string, training: boolean, currentConversation: boolean) {
  const query = { ...route.query }
  if (!currentConversation) {
    delete query[queryRun()]
    delete query.trainingRun
  }
  query.assistantTab = training ? 'training' : 'agent'
  query[training ? 'trainingRun' : queryRun()] = id
  return { query }
}

const mistakeSubmission = computed(() => {
  if (currentPurpose.value !== 'submission_review' && route.query.purpose !== 'submission_review')
    return
  const id = submission.value.trim()
  return isUuid(id) ? id : undefined
})
const mistakeTurn = computed(() =>
  mistakePracticeTurn(
    composer.value,
    Boolean(mistakeSubmission.value || practiceSource.value || waitingForPracticeTime.value),
  ),
)

function clearPractice() {
  practiceRequest?.abort()
  practiceBusy.value = false
  practiceNotice.value = practiceQuestion.value = ''
  practiceResult.value = undefined
  practiceSource.value = undefined
  waitingForPracticeTime.value = false
}

function onMistakeWithdrawn(id: string) {
  withdrawnMistakeId.value = id
  if (practiceBusy.value || practiceSource.value?.request_id === id) clearPractice()
}

async function showPractice(question: string, requestId?: string, minutes?: number) {
  if (!user.value || contestBlocked.value || practiceBusy.value) return false
  const linkedSubmission = requestId ? undefined : mistakeSubmission.value
  const selected = practiceSource.value
  const sourceId =
    requestId ||
    (!linkedSubmission || selected?.submission_id === linkedSubmission
      ? selected?.request_id
      : undefined)
  clearPractice()
  practiceQuestion.value = question
  const skill = practiceSkill.value
  if (!skill || !practiceEnabled.value) {
    practiceNotice.value = '启用已授权找题的专项训练技能后，可按错因找题。'
    await nextTick()
    practicePanel.value?.scrollIntoView({ block: 'nearest' })
    return false
  }
  const { signal } = (practiceRequest = new AbortController())
  practiceBusy.value = true
  try {
    const result = unwrap(
      await api.POST('/api/v1/learning/agent/mistakes/practice', {
        headers: csrfHeaders(),
        body: {
          skill_version_id: skill.version_id,
          request_id: sourceId,
          submission_id: linkedSubmission,
          available_minutes: minutes,
        },
        signal,
      }),
    )
    if (signal.aborted) return false
    practiceResult.value = result
    practiceSource.value = result.evidence
    return true
  } catch (cause) {
    if (!signal.aborted)
      practiceNotice.value =
        cause instanceof ApiError && cause.status === 404
          ? '没有可用于这次请求的已确认错因。可以在「错因」里核对记录后重试。'
          : feedback(cause)
    return false
  } finally {
    if (!signal.aborted) {
      practiceBusy.value = false
      await nextTick()
      practicePanel.value?.scrollIntoView({ block: 'nearest' })
    }
  }
}

function practiceFromRecord(id: string, minutes?: number) {
  void showPractice(minutes ? `${minutes} 分钟补练` : '找同类题', id, minutes)
}

watch([() => user.value?.id, contestBlocked, displayedRun, mistakeSubmission], clearPractice)

const periodicTurn = computed(() => {
  const question = composer.value
  if (mistakeTurn.value) return
  if (/训练集|训练题单/.test(question) || isProblemSearchQuestion(question, false)) return
  return periodicReviewTurn(question, pendingPeriodicQuestion.value || undefined)
})
const toPeriodic = computed(() => Boolean(periodicTurn.value))
const toTraining = computed(
  () =>
    !mistakeTurn.value &&
    !toPeriodic.value &&
    isTrainingMessage(
      composer.value,
      Boolean(trainingId.value),
      Boolean(trainingChat.value?.awaitingTopic),
      Boolean(trainingChat.value?.searchActive),
    ),
)
const toReview = computed(
  () =>
    !mistakeTurn.value &&
    !toTraining.value &&
    !toPeriodic.value &&
    !hintTraining.value &&
    (route.query.purpose === 'submission_review' ||
      isSubmissionReviewQuestion(
        composer.value,
        authorizedChatTarget.value || !!contextProblem.value,
      )),
)
const reviewReady = computed(() => reviewEnabled.value && authorizedChatTarget.value)
watch([toReview, toPeriodic], () => {
  chatIncludeDetails.value = false
  chatSourceExcerpt.value = null
})
watch([chatCanUseDetails, chatCanUseSource], ([details, source]) => {
  if (!details) chatIncludeDetails.value = false
  if (!source) chatSourceExcerpt.value = null
})
const conversationBusy = computed(() =>
  Boolean(
    busy.value ||
    practiceBusy.value ||
    chatPending.value ||
    switchingComposer.value ||
    trainingChat.value?.busy ||
    trainingChat.value?.pending,
  ),
)

async function send() {
  const question = composer.value.trim()
  if (conversationBusy.value || !question) return
  const practice = mistakeTurn.value
  if (practice) {
    const key = composerKey.value
    if ('clarification' in practice) {
      const source = practiceSource.value
      clearPractice()
      practiceSource.value = source
      waitingForPracticeTime.value = true
      practiceQuestion.value = question
      practiceNotice.value = practice.clarification
      saveQuestionDraft(key, '')
    } else if (await showPractice(question, undefined, practice.minutes)) {
      saveQuestionDraft(key, '')
    }
    return
  }
  clearPractice()
  const timed = periodicTurn.value
  if (timed) {
    if (!periodicEnabled.value) {
      start(question)
      return
    }
    if ('clarification' in timed) {
      pendingPeriodicQuestion.value ||= question
      periodicNotice.value = timed.clarification
      saveQuestionDraft(composerKey.value, '')
      return
    }
    pendingPeriodicQuestion.value = periodicNotice.value = ''
    start(timed.request.question, timed.request)
    return
  }
  pendingPeriodicQuestion.value = periodicNotice.value = ''
  if (!toTraining.value || !practiceEnabled.value) {
    start(question, toReview.value && reviewReady.value ? 'review' : undefined)
    return
  }
  if (chatIncludeDetails.value || chatSourceExcerpt.value) {
    chatIncludeDetails.value = false
    chatSourceExcerpt.value = null
  }
  const key = composerKey.value
  switchingComposer.value = true
  try {
    if (activeChat.value !== 'training') {
      await router.replace({ query: { ...route.query, assistantTab: 'training' } })
    }
    await nextTick()
    if (await trainingChat.value?.sendText(question)) saveQuestionDraft(key, '')
  } finally {
    switchingComposer.value = false
  }
}

function composerEnter(event: KeyboardEvent) {
  if (event.shiftKey || event.isComposing) return
  event.preventDefault()
  void send()
}

async function newChat() {
  clearPractice()
  saveQuestionDraft(draftKey(''), '')
  trainingChat.value?.resetLocalComposer()
  pendingPeriodicQuestion.value = periodicNotice.value = ''
  chatIncludeDetails.value = false
  chatSourceExcerpt.value = null
  promptLabel.value = ''
  const query = { ...route.query }
  delete query[queryRun()]
  delete query.trainingRun
  delete query.assistantTab
  if (query.purpose === 'submission_review') delete query.purpose
  await router.replace({ query })
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

watch(contextProblem, () => {
  history.value = null
  void load()
})
watch(selectedSkill, hydrate)
watch(submission, () => {
  chatIncludeDetails.value = false
  chatSourceExcerpt.value = null
})
watch(
  () => props.contextSubmission,
  (next, previous) => {
    if (props.embedded && (!submission.value || submission.value === previous))
      submission.value = next ?? ''
  },
)
watch(submission, () => (modelSourceExcerpt.value = null), { flush: 'sync' })
watch(
  () => route.query.submission,
  (value) => {
    if (!props.embedded) submission.value = isUuid(value) ? value : ''
  },
)
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
    trainingRecord.value = undefined
    promptLabel.value = ''
    pendingQuestion.value = undefined
    pendingPeriodicQuestion.value = periodicNotice.value = ''
    chatIncludeDetails.value = false
    chatSourceExcerpt.value = null
    submission.value = props.embedded
      ? (props.contextSubmission ?? '')
      : isUuid(route.query.submission)
        ? route.query.submission
        : ''
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
watch(contestBlocked, (blocked) => {
  if (blocked) monitor.pause()
  syncVisibility()
  if (blocked) {
    run.value = null
    return
  }
  void load()
  refreshRun()
})
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
  practiceRequest?.abort()
  monitor.dispose()
  document.removeEventListener('visibilitychange', syncVisibility)
  loadingController?.abort()
  historyRequest?.abort()
  actionController?.abort()
})
</script>

<template>
  <PageHeading v-if="!embedded" class="assistant-heading" title="学习助手">
    <RouterLink class="el-button" to="/trainings/skill-drafts">技能草稿</RouterLink>
    <RouterLink class="el-button" to="/trainings/skill-library">技能目录</RouterLink>
    <RouterLink class="el-button" to="/trainings">返回训练集</RouterLink>
  </PageHeading>
  <p v-if="contestBlocked" class="agent-error" role="status">
    比赛进行中，学习助手将在比赛结束后恢复。
  </p>
  <div
    v-else
    class="agent-layout native-form"
    :class="{
      'agent-layout--page': !embedded,
      'agent-layout--conversing': !embedded && user,
      'agent-layout--embedded': embedded,
      'agent-layout--floating': floating,
    }"
  >
    <header class="agent-toolbar">
      <span class="agent-modes"
        ><UiIcon name="assistant" /> {{ contextProblem ? '题目答疑' : '学习对话' }}</span
      >
      <div v-if="user" class="agent-modes" aria-label="对话选项">
        <RouterLink
          v-if="embedded && !floating"
          :to="fullPage"
          class="text-link"
          title="在完整页面继续对话"
        >
          <UiIcon name="expand" /> 全页
        </RouterLink>
        <button
          v-if="runId || trainingId || activeChat === 'training'"
          type="button"
          :disabled="conversationBusy"
          @click="newChat"
        >
          新对话
        </button>
        <button type="button" :aria-expanded="historyOpen" @click="historyOpen = !historyOpen">
          <UiIcon name="clock" /> 历史
        </button>
        <button type="button" :aria-expanded="mistakesOpen" @click="mistakesOpen = !mistakesOpen">
          错因
        </button>
        <button type="button" :aria-expanded="settingsOpen" @click="settingsOpen = !settingsOpen">
          <UiIcon name="settings" /> 设置
        </button>
      </div>
    </header>
    <div class="agent-conversation">
      <p v-if="error" role="alert" class="agent-error">{{ error }}</p>
      <p v-if="!user" class="agent-error">
        <RouterLink class="text-link" :to="loginPath">登录</RouterLink>后使用学习助手。
      </p>
      <template v-else>
        <aside v-if="mistakesOpen" :key="user.id" class="agent-mistakes" aria-label="个人错因">
          <LearningPersonalMistakes
            :revision="mistakeRevision"
            @withdrawn="onMistakeWithdrawn"
            @practice="practiceFromRecord"
          />
        </aside>
        <aside v-show="settingsOpen" class="agent-settings" aria-label="技能配置">
          <p v-if="loading && !settingsCatalog.length" role="status">正在读取技能…</p>
          <el-button v-else-if="!settingsCatalog.length" @click="load">重试读取</el-button>
          <template v-else>
            <label class="field">
              <span>技能与版本</span>
              <select v-model="selected" :disabled="busy">
                <option
                  v-for="skill in settingsCatalog"
                  :key="skill.version_id"
                  :value="skill.version_id"
                >
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
            <form v-if="selectedSkill" class="stack settings" @submit.prevent="saveSkill()">
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
            <div v-if="selectedSkill?.binding?.enabled && !hintTraining" class="stack launch">
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
                <input v-model="includeCode" type="checkbox" :disabled="busy" />
                本次使用本人代码证据
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
                  !embedded &&
                  (selectedSkill.purpose === 'submission_review' ||
                    (canUseModelVerdict && includeModelVerdict) ||
                    (!modelTopic && selectedSkill.purpose === 'guided_help'))
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
              <el-button :disabled="busy" @click="start()">
                {{
                  selectedSkill.purpose === 'guided_help'
                    ? '获取单次提示'
                    : '开始' + purposes[selectedSkill.purpose]
                }}
              </el-button>
            </div>
          </template>
        </aside>
        <aside v-show="historyOpen" class="agent-history" aria-label="最近对话与学习任务">
          <p v-if="historyLoading" role="status">正在读取历史…</p>
          <p v-else-if="historyError" role="alert">
            {{ historyError }}
            <el-button text size="small" @click="loadHistory">重试读取</el-button>
          </p>
          <p v-else-if="history && !history.items.length" class="small muted">暂无历史记录。</p>
          <ul v-else class="run-list">
            <li v-for="item in history?.items" :key="item.conversation_id ?? item.run_id">
              <RouterLink
                :to="turnLocation(item.run_id, Boolean(item.has_training_request), false)"
                :aria-current="
                  displayedRun === item.run_id ||
                  (conversationId && conversationId === item.conversation_id)
                    ? 'true'
                    : undefined
                "
                @click="selectHistory(item.purpose)"
              >
                <span class="history-title">
                  {{ item.conversation_title ?? purposes[item.purpose] }}
                </span>
                <span class="muted small"
                  >{{ states[item.state] }} · {{ displayTime(item.created_at) }}</span
                >
              </RouterLink>
            </li>
          </ul>
          <p v-if="history?.truncated && !historyLoading && !historyError" class="small muted">
            显示最近 30 个对话。
          </p>
        </aside>
        <div class="conversation-feed">
          <section
            v-if="timelineParts.before.length || earlier || timelineError"
            class="timeline"
            aria-label="当前对话的历史消息"
          >
            <p v-if="timelineError" role="alert">
              {{ timelineError }} <el-button text @click="loadTimeline()">重试读取</el-button>
            </p>
            <el-button
              v-else-if="earlier"
              text
              :loading="loadingTimeline"
              @click="loadTimeline(earlier)"
            >
              加载更早的对话
            </el-button>
            <AgentTimelineTurn
              v-for="turn in timelineParts.before"
              :key="turn.run_id"
              :turn="turn"
              :location="turnLocation(turn.run_id, Boolean(turn.training_request), true)"
              :label="purposes[turn.purpose]"
              :status="states[turn.state]"
            />
          </section>
          <div v-if="trainingVisited" v-show="activeChat === 'training'" class="agent-training">
            <TrainingSetChatView
              ref="trainingChat"
              v-model:skills="catalog"
              embedded
              shared-composer
              :floating="floating"
              :conversation-session-id="conversationSession"
              :tab-visible="activeChat === 'training'"
              @run-change="trainingRecord = $event"
            />
          </div>
          <section
            v-if="activeChat === 'agent' && (!practiceQuestion || run || runId || sendingQuestion)"
            class="conversation"
            :class="{ 'conversation--with-practice': practiceQuestion }"
            aria-label="学习对话"
          >
            <p v-if="runError" role="alert">
              {{ runError }} <button type="button" @click="refreshRun">重试读取</button>
            </p>
            <p v-if="pollPaused" role="status">
              已暂停自动刷新。<button type="button" @click="refreshRun">继续查看</button>
            </p>
            <div
              v-if="!run && !runId && !sendingQuestion && !practiceQuestion"
              class="agent-welcome"
            >
              <UiIcon name="assistant-face" />
              <h2>{{ contextProblem ? '需要一点解题提示？' : '今天想练什么？' }}</h2>
              <p>
                {{
                  contextProblem
                    ? '直接问读题、解题思路或边界条件。'
                    : '问解题思路，或试试「建一套二分查找入门训练集」。'
                }}
              </p>
            </div>
            <p v-else-if="!run && runId && !sendingQuestion" role="status">正在读取对话…</p>
            <template v-else-if="run">
              <p
                :key="run.run_id"
                class="agent-request"
                :class="{ 'message-enter': run.chat || run.review_question || run.periodic_review }"
              >
                {{
                  run.chat?.question ||
                  run.review_question ||
                  run.periodic_review?.question ||
                  promptLabel ||
                  (currentPurpose ? purposes[currentPurpose] : '已发起的学习任务')
                }}
              </p>
              <LearningRunResult
                :run="run"
                :busy="busy"
                @cancel="cancel"
                @refresh="refreshRun"
                @save="savePlan"
                @feedback="applyFeedback"
              />
              <LearningBatchDiagnostic
                v-if="
                  user &&
                  run.state === 'succeeded' &&
                  run.result?.purpose === 'submission_review' &&
                  run.result.anchor_submission_id
                "
                :key="run.session_id + ':' + run.result.anchor_submission_id"
                :account-id="user.id"
                :submission-id="run.result.anchor_submission_id"
                :review-session-id="run.session_id"
                :review-run-id="run.run_id"
                :suggested="run.result.model_hint?.review_resources?.includes('batch_diagnostics')"
                :disabled="conversationBusy"
              />
            </template>
            <template v-if="sendingQuestion">
              <p class="agent-request message-enter">{{ sendingQuestion }}</p>
              <LearningChatProgress phase="sending" />
            </template>
            <span ref="conversationEnd" aria-hidden="true" class="conversation-end" />
          </section>
          <section v-if="timelineParts.after.length" class="timeline" aria-label="后续消息">
            <AgentTimelineTurn
              v-for="turn in timelineParts.after"
              :key="turn.run_id"
              :turn="turn"
              :location="turnLocation(turn.run_id, Boolean(turn.training_request), true)"
              :label="purposes[turn.purpose]"
              :status="states[turn.state]"
            />
          </section>
          <div v-if="practiceQuestion" ref="practicePanel" class="agent-practice-result">
            <p class="agent-request">{{ practiceQuestion }}</p>
            <p v-if="practiceBusy" role="status">正在按已确认的错因找题…</p>
            <p v-if="practiceNotice" class="small" role="status">{{ practiceNotice }}</p>
            <el-button v-if="practiceSkill && !practiceEnabled" text @click="saveSkill('practice')">
              启用专项训练
            </el-button>
            <LearningMistakePractice v-if="practiceResult" :result="practiceResult" />
          </div>
        </div>
        <LearningReviewSubmission
          v-if="toReview && reviewEnabled"
          v-model="submission"
          :problem-version="contextProblem"
          :disabled="conversationBusy"
        />
        <LearningPublicDiagnostic
          v-if="user && toReview && authorizedChatTarget"
          :key="user.id + ':' + submission.trim()"
          :account-id="user.id"
          :submission-id="submission.trim()"
          :review="run?.result?.purpose === 'submission_review' ? run.result : undefined"
          :review-run-id="run?.state === 'succeeded' ? run.run_id : undefined"
          :disabled="conversationBusy"
          :withdrawn-mistake-id="withdrawnMistakeId"
          @mistake-saved="onMistakeSaved"
          @open-mistakes="mistakesOpen = true"
        />
        <LearningSourceExcerpt
          v-if="chatCanUseSource && !toTraining"
          :key="composerKey + ':' + submission"
          v-model="chatSourceExcerpt"
          :submission-id="submission.trim()"
          :disabled="busy || chatPending"
          chat
          class="chat-source"
        />
        <form class="agent-composer" @submit.prevent="send">
          <div class="composer-field">
            <label class="field">
              <textarea
                v-model="composer"
                aria-label="输入你的问题"
                rows="2"
                :maxlength="questionLimit"
                :placeholder="
                  contextProblem ? '关于这道题，你想问什么？' : '提问，或说说你想练什么…'
                "
                :disabled="busy || switchingComposer"
                @keydown.enter="composerEnter"
              />
            </label>
            <p v-if="toReview && !reviewSkill" class="small muted" role="status">
              当前没有可用的提交复盘技能。
            </p>
            <p v-if="toPeriodic && !periodicSkill" class="small muted" role="status">
              当前没有可用的阶段复习技能。
            </p>
            <p v-if="periodicNotice" class="small muted" role="status">{{ periodicNotice }}</p>
            <label v-if="hintProblem && !toReview && !toTraining && !toPeriodic" class="hint-level">
              <span>提示等级</span>
              <select v-model="hintLevel" :disabled="conversationBusy || hintCeiling === null">
                <option
                  v-for="({ value, label }, index) in hintLevels"
                  :key="value"
                  :value="value"
                  :disabled="index > unlockedHintIndex"
                >
                  {{ value }} · {{ label }}
                </option>
              </select>
              <small v-if="hintCeilingError" role="status">
                无法读取提示上限。
                <button type="button" @click="hintCeilingRefresh++">重试</button>
              </small>
              <small v-else-if="hintCeiling && hintCeiling !== 'L3'">最高 {{ hintCeiling }}</small>
            </label>
            <label v-if="chatCanUseDetails && !toTraining" class="check small">
              <input v-model="chatIncludeDetails" type="checkbox" :disabled="busy || chatPending" />
              本轮附上所选本人提交的评测类别
            </label>
            <small v-if="chatIncludeDetails || chatSourceExcerpt" class="muted">
              附件仅供本轮使用。
            </small>
            <small v-if="mistakeTurn" class="muted"> 使用本人已确认的错因与题库知识点。 </small>
            <small v-else-if="toReview && reviewEnabled" class="muted">
              复盘会读取所选提交的评测和可用代码证据。远端模型可查阅公开题面、评测统计及获准的代码摘要。源码片段经单独确认后由本机模型分析。
            </small>
            <small v-else-if="toPeriodic && periodicEnabled" class="muted">
              根据本次可用时间和你的练习进度给出复习题单。
            </small>
            <small v-else-if="!toTraining && !toReview && !toPeriodic" class="muted">
              {{
                contextProblem ? '问题、近期对话和公开题面' : '问题和近期对话'
              }}会发送给已配置的模型。
            </small>
          </div>
          <el-button
            v-if="toPeriodic && periodicSkill && !periodicEnabled"
            text
            :disabled="conversationBusy"
            @click="saveSkill('periodic')"
          >
            启用阶段复习
          </el-button>
          <el-button
            v-if="toReview && reviewSkill && !reviewEnabled"
            text
            :disabled="conversationBusy"
            @click="saveSkill('review')"
          >
            启用提交复盘
          </el-button>
          <el-button
            native-type="submit"
            type="primary"
            :disabled="
              conversationBusy ||
              !composer.trim() ||
              (!mistakeTurn &&
                !!hintProblem &&
                hintCeiling === null &&
                !(toReview && reviewReady) &&
                !(toTraining && practiceEnabled) &&
                !(toPeriodic && periodicEnabled))
            "
            :loading="busy || switchingComposer"
          >
            <UiIcon name="send" /> 发送
          </el-button>
        </form>
      </template>
    </div>
  </div>
</template>

<style scoped>
.assistant-heading {
  gap: 0.4rem 0.75rem;
  margin-bottom: 0.55rem;
}
.assistant-heading :deep(h1) {
  margin: 0;
  font-size: 1.3rem;
}
.assistant-heading :deep(.el-button) {
  height: 1.8rem;
  padding-inline: 0.6rem;
  font-size: 0.8rem;
}
.agent-layout {
  display: flex;
  flex-direction: column;
  min-height: min(40rem, 88svh);
  margin: 0 auto;
  max-width: 66rem;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
}
.agent-layout--page {
  max-width: none;
  min-height: 0;
}
.agent-layout--conversing {
  height: max(26rem, calc(100dvh - 6.6rem));
  overflow: hidden;
}
.agent-conversation {
  display: contents;
}
.agent-layout--embedded {
  min-height: var(--assistant-min-height, clamp(32rem, 74svh, 48rem));
  border: 0;
  border-radius: 0;
}
.agent-layout--floating {
  height: 100%;
  min-height: 0;
}
:is(.agent-layout--floating, .agent-layout--conversing) :is(.agent-toolbar, .agent-composer) {
  flex: none;
}
:is(.agent-layout--floating, .agent-layout--conversing) .conversation-feed {
  min-height: 0;
  flex: 1;
  overflow: auto;
  overscroll-behavior: contain;
}
:is(.agent-layout--floating, .agent-layout--conversing)
  :is(.agent-settings, .agent-history, .agent-mistakes, .chat-source) {
  max-height: 13rem;
  overflow: auto;
}
.agent-toolbar,
.agent-modes {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  flex-wrap: wrap;
}
.agent-toolbar {
  justify-content: space-between;
  padding: 0.6rem 0.7rem;
  border-bottom: 1px solid var(--line);
}
.agent-modes button {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.35rem 0.5rem;
  border: 0;
  border-radius: 6px;
  color: var(--secondary-ink);
  background: transparent;
  font-size: 0.82rem;
}
.agent-modes button:hover,
.agent-modes button[aria-pressed='true'],
.agent-modes button[aria-expanded='true'] {
  color: var(--accent-ink);
  background: var(--accent-bg);
}
.agent-error {
  padding: 0.65rem 1rem;
}
.conversation-feed {
  display: flex;
  flex: 1;
  flex-direction: column;
}
.timeline {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
  padding: 0.85rem 1rem 0;
}
.timeline > .el-button {
  align-self: center;
}
.agent-practice-result {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
  padding: 1rem;
}
.agent-practice-result > .el-button {
  align-self: flex-start;
}
.agent-settings,
.agent-history,
.agent-mistakes {
  padding: 0.8rem 1rem;
  background: var(--surface-soft);
  border-bottom: 1px solid var(--line);
  max-height: 65svh;
  overflow: auto;
}
.conversation {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 0.75rem;
  min-height: 13rem;
  flex: 1;
  padding: 1rem;
}
.conversation--with-practice {
  min-height: 0;
  flex: none;
}
.conversation-end {
  height: 0;
  margin-top: -0.75rem;
}
.agent-welcome {
  padding: 0.8rem 0;
  text-align: center;
  color: var(--secondary-ink);
  margin: auto;
}
.agent-layout--embedded .agent-welcome {
  margin: clamp(3rem, 11svh, 6rem) auto auto;
}
.agent-layout--page .agent-welcome {
  margin: clamp(0.75rem, 3vh, 2rem) auto auto;
}
.agent-welcome > .el-icon {
  color: var(--accent-ink);
  font-size: 1.6rem;
}
.agent-welcome h2 {
  margin-block: 0.5rem;
  font-size: 1.1rem;
  color: var(--strong-ink);
}
:deep(.agent-request) {
  align-self: flex-end;
  border-radius: 9px;
  background: var(--accent-bg);
  color: var(--accent-ink);
  padding: 0.45rem 0.65rem;
}
.agent-composer {
  position: sticky;
  bottom: 0;
  background: var(--surface);
  padding: 0.7rem 0.75rem;
  border-top: 1px solid var(--line);
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 0.55rem;
}
.agent-layout--embedded .agent-composer {
  position: static;
}
.composer-field {
  display: grid;
  flex: 1 1 12rem;
  min-width: 0;
  gap: 0.5rem;
}
.composer-field textarea {
  width: 100%;
  min-height: 3rem;
  max-height: 12rem;
  resize: vertical;
}
.hint-level {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  color: var(--secondary-ink);
  font-size: 0.82rem;
}
.hint-level select {
  width: auto;
  max-width: 100%;
}
:deep(.agent-request) {
  max-width: min(90%, 44rem);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.message-enter {
  animation: message-enter 180ms ease-out both;
}
@keyframes message-enter {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
}
@media (prefers-reduced-motion: reduce) {
  .message-enter {
    animation: none;
  }
}
.chat-source {
  padding: 0 0.65rem;
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
.history-title {
  min-width: 0;
  overflow-wrap: anywhere;
}
.run-list a[aria-current='true'] {
  border-radius: var(--el-border-radius-base);
  background: var(--el-color-primary-light-9);
}
</style>
