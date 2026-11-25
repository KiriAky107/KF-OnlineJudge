<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { agentRunIntent } from '../api/agentRunIntent'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { boundedPoll } from '../api/polling'
import { trainingRankingLabels } from '../api/trainingRanking'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import UiIcon from '../components/UiIcon.vue'
import PageHeading from '../components/PageHeading.vue'
import { useSession } from '../composables/useSession'
import { useContestAgentAccess } from '../composables/useContestAgentAccess'
import { useConversationHistory } from '../composables/useConversationHistory'
import { sha256Fingerprint } from '../lib/requestCrypto'
import {
  hasTrainingCandidateAccess,
  isProblemSearchQuestion,
  trainingCandidateTool,
  trainingChatSkill,
  trainingDifficultyLabels,
  trainingSearchTurn,
  trainingTurn,
  type TrainingSetConditions,
  type TrainingSetDraft,
} from '../lib/trainingSetChat'

type Run = components['schemas']['AgentRunView']
type Skill = components['schemas']['SkillVersionView']
type History = components['schemas']['AgentHistoryPage']
type Message = { from: '你' | '学习助手'; text: string }
type SavedComposer = { input?: string; messages?: Message[]; awaitingTopic?: TrainingSetConditions }
const props = withDefaults(
  defineProps<{
    embedded?: boolean
    floating?: boolean
    sharedComposer?: boolean
    tabVisible?: boolean
    conversationSessionId?: string
  }>(),
  {
    tabVisible: true,
    conversationSessionId: undefined,
  },
)
const sharedSkills = defineModel<Skill[]>('skills')
const emit = defineEmits<{ 'run-change': [run: Run | undefined] }>()
const route = useRoute()
const router = useRouter()
const runQuery = () => (props.embedded ? 'trainingRun' : 'run')
const { user, refresh } = useSession()
const { blocked: contestBlocked, recheck } = useContestAgentAccess()
const localSkills = ref<Skill[]>([])
const skills = computed({
  get: () => sharedSkills.value ?? localSkills.value,
  set: (value: Skill[]) => {
    if (props.sharedComposer) sharedSkills.value = value
    else localSkills.value = value
  },
})
const input = ref('')
const messagesPane = ref<HTMLElement>()
const awaitingTopic = ref<TrainingSetConditions>()
const messages = ref<Message[]>([])
const run = ref<Run>()
watch(run, (value) => emit('run-change', value))
const active = ref<string>()
const confirmationConflict = ref<string>()
const busy = ref(false)
const error = ref('')
const notice = ref('')
const recentId = useId()
const recentOpen = ref(false)
const recent = ref<History>()
const recentLoading = ref(false)
const recentError = ref('')
let recentRequest: AbortController | undefined
let request = new AbortController()
let loadedComposerKey: string | undefined

function composerKey(owner: string, runId = 'new') {
  return `kf-oj:training-composer:v1:${owner}:${runQuery()}:${runId}`
}
const draftKey = computed(() => {
  const owner = user.value?.id
  return owner ? composerKey(owner, active.value) : undefined
})
const canStartOver = computed(() =>
  Boolean(active.value || messages.value.length || input.value.trim()),
)

watch(
  draftKey,
  (key) => {
    loadedComposerKey = undefined
    let saved: SavedComposer = {}
    if (key) {
      try {
        saved = JSON.parse(sessionStorage.getItem(key) ?? 'null') ?? {}
      } catch {
        // Draft storage is optional.
      }
    }
    input.value = typeof saved.input === 'string' ? saved.input.slice(0, 500) : ''
    messages.value = Array.isArray(saved.messages)
      ? saved.messages.filter(
          (message) =>
            message &&
            (message.from === '你' || message.from === '学习助手') &&
            typeof message.text === 'string',
        )
      : []
    const awaiting = saved.awaitingTopic
    awaitingTopic.value =
      awaiting && typeof awaiting.title === 'string' && Number.isInteger(awaiting.count)
        ? awaiting
        : undefined
    loadedComposerKey = key
  },
  { immediate: true },
)

watch(
  [input, messages, awaitingTopic],
  () => {
    const key = loadedComposerKey
    if (!key || key !== draftKey.value) return
    try {
      if (!input.value && !messages.value.length && !awaitingTopic.value) {
        sessionStorage.removeItem(key)
      } else {
        sessionStorage.setItem(
          key,
          JSON.stringify({
            input: input.value,
            messages: messages.value.slice(-16),
            awaitingTopic: awaitingTopic.value,
          }),
        )
      }
    } catch {
      // The composer still works when draft storage is unavailable.
    }
  },
  { flush: 'sync', deep: true },
)

const skill = computed(() => trainingChatSkill(skills.value, run.value?.skill_version_id))
const authorized = computed(() => !!skill.value && hasTrainingCandidateAccess(skill.value))
const draft = computed(() =>
  run.value?.result?.purpose === 'focused_training' ? run.value.result : undefined,
)
const trainingRequest = computed(() => run.value?.training_request ?? draft.value?.training_set)
const searchActive = computed(() => !!trainingRequest.value?.search)
const searchFilters = computed(() => {
  const search = trainingRequest.value?.search
  if (!search) return ''
  const { minimum_score: min, maximum_score: max, only_unsolved: unsolved } = search
  const score =
    min != null && max != null
      ? `${min}–${max} 分`
      : min != null
        ? `${min} 分以上`
        : max != null
          ? `${max} 分以下`
          : ''
  return [score, unsolved ? '仅未通过' : ''].filter(Boolean).join(' · ')
})
const { earlierMessages, earlierCursor, earlierLoading, earlierError, loadEarlier } =
  useConversationHistory(
    () => {
      if (
        props.sharedComposer ||
        !user.value ||
        contestBlocked.value ||
        !run.value ||
        !trainingRequest.value
      )
        return null
      const previous =
        trainingRequest.value.revision?.run_id ?? trainingRequest.value.previous_run_id
      return previous ? run.value.run_id : null
    },
    (cause) => {
      if (cause instanceof ApiError && cause.status === 403) void recheck()
      return errorText(cause)
    },
  )
const terminal = (value: Run) => ['succeeded', 'failed', 'cancelled'].includes(value.state)
const pending = computed(() => !!active.value && (!run.value || !terminal(run.value)))
const canRebuild = computed(() => {
  const current = run.value
  return Boolean(
    current &&
    terminal(current) &&
    !current.training_id &&
    !searchActive.value &&
    trainingRequest.value &&
    (confirmationConflict.value === current.run_id ||
      !authorized.value ||
      current.state !== 'succeeded' ||
      draft.value?.items.length === 0),
  )
})
const rebuildSkill = computed(() =>
  authorized.value ? skill.value : trainingChatSkill(skills.value),
)
const rebuildAuthorized = computed(
  () => !!rebuildSkill.value && hasTrainingCandidateAccess(rebuildSkill.value),
)
const difficulty = trainingDifficultyLabels
async function readRun(id: string, signal: AbortSignal) {
  return unwrap(
    await api.GET('/api/v1/learning/agent/runs/{run_id}', {
      params: { path: { run_id: id } },
      signal,
    }),
  )
}

const monitor = boundedPoll<Run>({
  load: (signal) => readRun(active.value!, signal),
  terminal,
  result: (value) => {
    run.value = value
    if (value.state === 'failed') notice.value = '这次草案生成失败。可以调整条件后重试。'
    if (value.state === 'cancelled') notice.value = '这次草案已取消。'
  },
  error: (cause) => {
    if (cause instanceof ApiError && cause.status === 403) void recheck()
    error.value = errorText(cause)
  },
  exhausted: () => (notice.value = '已暂停自动刷新，可继续查看草案状态。'),
  maxRequests: 90,
  maxTime: 180_000,
})

async function action(work: (signal: AbortSignal) => Promise<void>) {
  if (busy.value || contestBlocked.value) return
  const { signal } = request
  busy.value = true
  error.value = notice.value = ''
  try {
    await work(signal)
  } catch (cause) {
    if (!signal.aborted) {
      if (cause instanceof ApiError && cause.status === 403) void recheck()
      error.value = errorText(cause)
    }
  } finally {
    if (!signal.aborted) busy.value = false
  }
}

async function readSkills(signal: AbortSignal) {
  const catalog = unwrap(await api.GET('/api/v1/learning/agent/skills', { signal }))
  if (!signal.aborted) skills.value = catalog.versions
}

function loadSkills() {
  if (!props.sharedComposer) void action(readSkills)
}

async function loadRecent() {
  recentRequest?.abort()
  const { signal } = (recentRequest = new AbortController())
  recentLoading.value = true
  recentError.value = ''
  try {
    const page = unwrap(
      await api.GET('/api/v1/learning/agent/runs', {
        params: { query: { training_drafts: true } },
        signal,
      }),
    )
    if (!signal.aborted) recent.value = page
  } catch (cause) {
    if (!signal.aborted) {
      if (cause instanceof ApiError && cause.status === 403) void recheck()
      recentError.value = errorText(cause)
    }
  } finally {
    if (!signal.aborted) recentLoading.value = false
  }
}

watch([recentOpen, () => props.tabVisible], ([open, visible]) => {
  if (open && visible && user.value && !contestBlocked.value) void loadRecent()
  else recentRequest?.abort()
})

function openRecent(id: string) {
  recentOpen.value = false
  error.value = notice.value = ''
  void router.push({ query: { ...route.query, [runQuery()]: id } })
}

function recentStatus(item: History['items'][number]) {
  if (item.training_id) return '已创建'
  if (item.state === 'succeeded') return '未创建'
  if (item.state === 'failed') return '生成失败'
  if (item.state === 'cancelled') return '已取消'
  return item.state === 'cancel_requested' ? '正在取消' : '生成中'
}

function selectRun(id: string | undefined) {
  if (active.value === id) return
  monitor.pause()
  active.value = id
  if (run.value?.run_id !== id) run.value = undefined
  if (id && !contestBlocked.value && (!run.value || !terminal(run.value))) monitor.start()
}

watch(
  () => user.value?.id,
  (owner) => {
    request.abort()
    recentRequest?.abort()
    recentOpen.value = false
    recent.value = undefined
    request = new AbortController()
    busy.value = false
    confirmationConflict.value = undefined
    error.value = notice.value = ''
    localSkills.value = []
    selectRun(undefined)
    if (!owner) return
    loadSkills()
  },
  { immediate: true },
)
watch(
  [() => user.value?.id, () => route.query[runQuery()]],
  ([owner, id]) =>
    selectRun(owner && typeof id === 'string' && /^[0-9a-f-]{36}$/.test(id) ? id : undefined),
  { immediate: true },
)
watch(contestBlocked, (blocked) => {
  if (blocked) {
    recentRequest?.abort()
    recentOpen.value = false
    recent.value = undefined
    monitor.pause()
  } else if (user.value) {
    loadSkills()
    if (active.value && (!run.value || !terminal(run.value))) monitor.start()
  }
})
onMounted(() => {
  if (!user.value) void refresh().catch((cause) => (error.value = errorText(cause)))
})
onBeforeUnmount(() => {
  request.abort()
  recentRequest?.abort()
  monitor.dispose()
})

function enable(selected = skill.value) {
  if (!selected) return
  const candidate = trainingCandidateTool(selected)
  if (!candidate) return
  const tools = selected.binding?.authorized_tools ?? []
  const granted = tools.some(
    (tool) => tool.name === candidate.name && tool.version === candidate.version,
  )
  void action(async (signal) => {
    const binding = unwrap(
      await api.PUT('/api/v1/learning/agent/skills/{version_id}', {
        headers: csrfHeaders(),
        params: { path: { version_id: selected.version_id } },
        body: {
          enabled: true,
          parameters: selected.binding?.parameters ?? selected.default_parameters,
          authorized_tools: granted ? tools : [...tools, candidate],
        },
        signal,
      }),
    )
    if (!signal.aborted) {
      skills.value = skills.value.map((entry) =>
        entry.version_id === selected.version_id ? { ...entry, binding } : entry,
      )
    }
  })
}

async function sendText(value: string) {
  const owner = user.value?.id
  const selected = skill.value
  const text = value.trim()
  if (
    !owner ||
    !selected ||
    !authorized.value ||
    pending.value ||
    busy.value ||
    !text ||
    contestBlocked.value
  )
    return false
  if (text.length > 500) {
    error.value = '训练要求请控制在 500 字以内。'
    return false
  }
  const options = draft.value?.training_set
  const source = options ? run.value : undefined
  const search = isProblemSearchQuestion(text, searchActive.value)
  const turn = search
    ? trainingSearchTurn(text, options?.search ? options : undefined)
    : trainingTurn(
        text,
        source && options && !options.search
          ? { runId: source.run_id, options, items: draft.value?.items }
          : undefined,
        awaitingTopic.value,
      )
  if ('reply' in turn) {
    messages.value.push({ from: '你', text })
    if (turn.awaitingTopic) awaitingTopic.value = turn.awaitingTopic
    messages.value.push({ from: '学习助手', text: turn.reply })
    input.value = ''
    return true
  }
  return submitDraft(
    owner,
    selected,
    {
      ...turn.draft,
      message: text,
      previous_run_id: turn.draft.revision ? undefined : source?.run_id,
    },
    source?.session_id,
  )
}

function send() {
  void sendText(input.value)
}

function rebuild() {
  const owner = user.value?.id
  const selected = rebuildSkill.value
  const previous = trainingRequest.value
  if (!owner || !selected || !previous || !canRebuild.value || !rebuildAuthorized.value) return
  submitDraft(owner, selected, {
    title: previous.title,
    count: previous.count,
    tags: previous.tags,
    difficulty: previous.difficulty,
    cover_each_goal: previous.cover_each_goal,
    ranking_profile: previous.ranking_profile,
    message: '按原筛选条件重新生成',
  })
}

async function submitDraft(
  owner: string,
  selected: Skill,
  draft: TrainingSetDraft,
  sessionId?: string,
) {
  const submittedKey = draftKey.value
  const anchor = props.conversationSessionId
  const serialized = JSON.stringify([selected.version_id, sessionId ?? anchor, draft])
  let accepted = false
  await action(async (signal) => {
    const intent = agentRunIntent(
      localStorage,
      owner,
      sha256Fingerprint(new TextEncoder().encode(serialized)),
    )
    let session = sessionId ?? intent.sessionId
    if (!session) {
      const created = unwrap(
        await api.POST('/api/v1/learning/agent/sessions', {
          headers: csrfHeaders(),
          body: { purpose: 'focused_training', conversation_session_id: anchor },
          signal,
        }),
      )
      if (signal.aborted) return
      session = created.session_id
    }
    intent.bind(session)
    const receipt = unwrap(
      await api.POST('/api/v1/learning/agent/sessions/{session_id}/runs', {
        headers: csrfHeaders(),
        params: { path: { session_id: session } },
        body: {
          request_id: intent.requestId,
          skill_version_id: selected.version_id,
          training_set: draft,
          include_source_evidence: false,
          include_model_review: false,
          include_model_code_context: false,
          include_model_plan_explanation: false,
        },
        signal,
      }),
    )
    if (signal.aborted) return
    intent.complete()
    accepted = true
    if (submittedKey !== draftKey.value) {
      if (submittedKey) {
        try {
          sessionStorage.removeItem(submittedKey)
        } catch {
          // Storage can be unavailable when the draft is submitted.
        }
      }
      return
    }
    input.value = ''
    awaitingTopic.value = undefined
    messages.value = []
    run.value = receipt
    selectRun(receipt.run_id)
    await router.replace({ query: { ...route.query, [runQuery()]: receipt.run_id } })
  })
  return accepted
}

function startEasyTraining() {
  input.value = '给我创建一个简单题的训练集'
  send()
}

function submitOnEnter(event: Event) {
  if (!(event instanceof KeyboardEvent) || event.isComposing) return
  event.preventDefault()
  send()
}
watch([() => messages.value.length, draft], async () => {
  await nextTick()
  const pane = messagesPane.value
  if (pane) pane.scrollTop = pane.scrollHeight
})

function resetLocalComposer() {
  if (user.value) {
    try {
      sessionStorage.removeItem(composerKey(user.value.id))
    } catch {
      // Continue with an empty composer in memory.
    }
  }
  if (!active.value) {
    input.value = ''
    messages.value = []
    awaitingTopic.value = undefined
  }
  notice.value = error.value = ''
}

defineExpose({ sendText, pending, busy, awaitingTopic, searchActive, resetLocalComposer })

async function newTraining() {
  const query = { ...route.query }
  delete query[runQuery()]
  resetLocalComposer()
  await router.replace({ query })
  selectRun(undefined)
}

function cancel() {
  const id = active.value
  if (!id) return
  void action(async (signal) => {
    const result = unwrap(
      await api.POST('/api/v1/learning/agent/runs/{run_id}/cancel', {
        headers: csrfHeaders(),
        params: { path: { run_id: id } },
        signal,
      }),
    )
    if (!signal.aborted) {
      run.value = result
      if (terminal(result)) monitor.pause()
    }
  })
}

function save() {
  const id = run.value?.run_id
  if (
    !id ||
    !authorized.value ||
    confirmationConflict.value === id ||
    searchActive.value ||
    !draft.value?.training_set ||
    !draft.value.items.length
  )
    return
  void action(async (signal) => {
    let trainingId: string | null | undefined
    try {
      const saved = unwrap(
        await api.POST('/api/v1/learning/agent/runs/{run_id}/training', {
          headers: csrfHeaders(),
          params: { path: { run_id: id } },
          signal,
        }),
      )
      trainingId = saved.id
    } catch (cause) {
      if (!(cause instanceof ApiError) || ![404, 409, 422].includes(cause.status)) throw cause
      const current = await readRun(id, signal)
      if (signal.aborted || run.value?.run_id !== id) return
      run.value = current
      trainingId = current.training_id
      if (!trainingId) {
        confirmationConflict.value = id
        notice.value = '当前草案无法确认，可以按原条件重新选题。'
        await readSkills(signal)
      }
    }
    if (trainingId && !signal.aborted && run.value?.run_id === id) {
      run.value = { ...run.value, training_id: trainingId }
      notice.value = '训练集已创建。'
      const entry = recent.value?.items.find((item) => item.run_id === id)
      if (entry) entry.training_id = trainingId
    }
  })
}
</script>

<template>
  <PageHeading v-if="!embedded" title="聊天创建训练集">
    <el-button v-if="canStartOver && !pending && !contestBlocked" @click="newTraining"
      >新建训练集</el-button
    >
    <RouterLink class="el-button" to="/trainings">返回训练集</RouterLink>
  </PageHeading>
  <p v-if="contestBlocked" role="status">比赛进行中，学习助手将在比赛结束后恢复。</p>
  <p v-else-if="error" role="alert">
    {{ error }}
    <el-button v-if="active && pending" size="small" @click="monitor.start()">重试读取</el-button>
  </p>
  <p v-if="!user">
    <RouterLink :to="{ path: '/login', query: { next: route.fullPath } }">登录</RouterLink
    >后可创建训练集。
  </p>
  <div
    v-else-if="!contestBlocked"
    class="training-chat"
    :class="{
      'training-chat--embedded': embedded,
      'training-chat--floating': floating,
      'training-chat--shared': sharedComposer,
    }"
  >
    <header v-if="!sharedComposer" class="chat-header">
      <span><UiIcon name="chat" /> 训练对话</span>
      <div class="chat-header-actions">
        <el-button
          text
          size="small"
          :aria-expanded="recentOpen"
          :aria-controls="recentId"
          @click="recentOpen = !recentOpen"
        >
          <UiIcon name="clock" /> 最近草案
        </el-button>
        <el-button
          v-if="embedded && canStartOver && !pending"
          text
          size="small"
          @click="newTraining"
          >新建训练集</el-button
        >
      </div>
    </header>
    <aside v-if="recentOpen" :id="recentId" class="recent-drafts" aria-label="最近草案">
      <p v-if="recentLoading" role="status">正在读取最近草案…</p>
      <p v-else-if="recentError" role="alert">
        {{ recentError }} <el-button text size="small" @click="loadRecent">重试读取</el-button>
      </p>
      <ul v-else-if="recent?.items.length">
        <li v-for="item in recent.items" :key="item.session_id">
          <button
            type="button"
            class="recent-draft"
            :aria-current="item.run_id === active ? 'true' : undefined"
            :disabled="busy"
            @click="openRecent(item.run_id)"
          >
            <strong>{{ item.conversation_title ?? '训练集草案' }}</strong>
            <span class="tag" :class="{ 'draft-created': item.training_id }">
              {{ recentStatus(item) }}
            </span>
          </button>
          <RouterLink
            v-if="item.training_id"
            class="text-link small"
            :to="'/trainings/' + item.training_id"
            >查看训练集</RouterLink
          >
        </li>
      </ul>
      <p v-else class="muted">暂无训练集草案。</p>
      <p v-if="recent?.truncated && !recentLoading && !recentError" class="small muted">
        显示最近 30 个训练对话。
      </p>
    </aside>
    <p v-if="!canRebuild && !pending && skill && !authorized">
      创建训练集需要启用专项训练的题库查询。
      <el-button :disabled="busy" @click="enable()">启用题库查询</el-button>
    </p>
    <p v-else-if="!canRebuild && run && !pending && !skill && !run.training_id">
      继续修改这份草案需要原技能版本。
      <RouterLink to="/trainings/assistant">管理学习技能</RouterLink>
    </p>
    <div ref="messagesPane" class="message-list" aria-label="训练集对话">
      <div v-if="!messages.length && !run && !pending" class="chat-empty">
        <UiIcon name="trainings" />
        <h2>想练哪些题？</h2>
        <p>告诉我知识点、难度和题数，也可以说「优先巩固薄弱知识点」。</p>
        <button
          v-if="!sharedComposer"
          type="button"
          :disabled="!authorized || busy"
          @click="startEasyTraining"
        >
          选 5 道简单题
        </button>
      </div>
      <div v-if="earlierCursor || earlierError" class="older-training-turns">
        <p v-if="earlierError" role="alert">{{ earlierError }}</p>
        <el-button :loading="earlierLoading" @click="loadEarlier">
          {{ earlierError ? '重试读取' : '加载更早的建集记录' }}
        </el-button>
      </div>
      <template v-for="turn in earlierMessages" :key="turn.run_id">
        <div v-if="turn.question" class="chat-message" data-from="你">
          <strong>你</strong>
          <p>{{ turn.question }}</p>
        </div>
        <div v-if="turn.training_request" class="chat-message">
          <strong>{{ turn.training_request.title }}</strong>
          <p class="small muted">
            {{ turn.training_request.count }} 道题
            <template v-if="turn.training_request.tags.length">
              · {{ turn.training_request.tags.join('、') }}
            </template>
            <template v-if="turn.training_request.difficulty">
              · {{ difficulty[turn.training_request.difficulty] }}
            </template>
          </p>
          <RouterLink
            :to="{ query: { ...route.query, [runQuery()]: turn.run_id } }"
            class="text-link"
          >
            {{ turn.training_request.search ? '查看找题结果' : '查看这份草案' }}
          </RouterLink>
        </div>
      </template>
      <div v-if="trainingRequest?.message" class="chat-message" data-from="你">
        <strong>你</strong>
        <p>{{ trainingRequest.message }}</p>
      </div>
      <p v-if="pending" role="status">
        {{ searchActive ? '正在查找题目…' : '正在生成草案…' }}
        <el-button size="small" :disabled="busy" @click="cancel">取消</el-button>
      </p>
      <p v-if="notice" role="status">
        {{ notice }}
        <el-button v-if="pending" size="small" @click="monitor.start()">刷新状态</el-button>
      </p>
      <section
        v-if="draft?.training_set"
        class="draft-card"
        :aria-label="searchActive ? '找题结果' : '训练集草案'"
      >
        <header>
          <div>
            <h2>{{ draft.training_set.title }}</h2>
            <p>
              {{
                searchActive
                  ? `找到 ${draft.items.length} 道题`
                  : `${draft.items.length} / ${draft.training_set.count} 道题`
              }}
            </p>
          </div>
          <RouterLink
            v-if="run?.training_id"
            class="el-button el-button--primary"
            :to="'/trainings/' + run.training_id"
            >打开训练集</RouterLink
          >
          <el-button
            v-else-if="!searchActive"
            type="primary"
            :disabled="
              busy ||
              pending ||
              !authorized ||
              confirmationConflict === run?.run_id ||
              !draft.items.length
            "
            @click="save"
            >确认创建</el-button
          >
        </header>
        <p
          v-if="
            draft.training_set.tags.length ||
            draft.training_set.difficulty ||
            draft.training_set.ranking_profile ||
            searchFilters
          "
          class="small muted"
        >
          {{
            [
              draft.training_set.tags.join('、'),
              draft.training_set.difficulty && difficulty[draft.training_set.difficulty],
              draft.training_set.ranking_profile &&
                trainingRankingLabels[draft.training_set.ranking_profile],
              searchFilters,
            ]
              .filter(Boolean)
              .join(' · ')
          }}
        </p>
        <ol>
          <li v-for="item in draft.items" :key="item.problem_version_id">
            <RouterLink :to="'/problems/' + item.problem_version_id">{{ item.title }}</RouterLink>
            <span v-if="item.difficulty" class="muted"> · {{ difficulty[item.difficulty] }}</span>
            <span v-if="searchActive && item.difficulty_score != null" class="muted">
              · {{ item.difficulty_score }} 分</span
            >
            <span v-if="searchActive" class="muted">
              · {{ item.previously_accepted ? '已通过' : '未通过' }}</span
            >
            <span
              v-if="draft.replacement && item.position === draft.replacement.position"
              class="draft-replacement"
            >
              <span class="tag">已替换</span>
              <span class="small muted">原题：{{ draft.replacement.previous_title }}</span>
            </span>
          </li>
        </ol>
        <p v-for="gap in draft.gaps" :key="gap" class="small muted">{{ gap }}</p>
      </section>
      <section v-if="canRebuild" class="draft-card" aria-label="重建草案">
        <p v-if="!authorized">原草案的技能当前不可用。可以用现有技能，按原条件另建草案。</p>
        <p v-else>沿用原有的题数、知识点和难度，从当前题库重新选题。</p>
        <el-button v-if="rebuildAuthorized" :disabled="busy" @click="rebuild">
          按原条件重建
        </el-button>
        <el-button v-else-if="rebuildSkill" :disabled="busy" @click="enable(rebuildSkill)">
          启用题库查询
        </el-button>
        <RouterLink v-else class="text-link" to="/trainings/assistant">管理学习技能</RouterLink>
      </section>
      <div
        v-for="(message, index) in messages"
        :key="index"
        class="chat-message"
        :data-from="message.from"
      >
        <strong>{{ message.from }}</strong>
        <p>{{ message.text }}</p>
      </div>
    </div>
    <form v-if="!sharedComposer" class="chat-form" @submit.prevent="send">
      <el-input
        v-model="input"
        maxlength="500"
        type="textarea"
        :rows="2"
        aria-label="训练集要求"
        placeholder="建一套二分查找训练集，4 道入门题"
        :disabled="!authorized || pending || busy"
        @keydown.enter.exact="submitOnEnter"
      />
      <el-button
        native-type="submit"
        type="primary"
        :disabled="!authorized || pending || busy || !input.trim()"
        ><UiIcon name="send" /> 发送</el-button
      >
    </form>
  </div>
</template>

<style scoped>
.training-chat {
  display: flex;
  flex-direction: column;
  max-width: 60rem;
  min-height: min(40rem, 88svh);
  margin: 0 auto;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
}
.training-chat--embedded {
  min-height: var(--assistant-min-height, 32rem);
  border: 0;
  border-radius: 0;
}
.training-chat--floating {
  height: 100%;
  min-height: 0;
}
.training-chat--shared,
.training-chat--shared .message-list {
  min-height: 0;
  height: auto;
  max-height: none;
  overflow: visible;
}
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.8rem 1rem;
  border-bottom: 1px solid var(--line);
  font-weight: 600;
}
.chat-header-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.recent-drafts {
  max-height: min(17rem, 40svh);
  overflow: auto;
  padding: 0.55rem 1rem;
  border-bottom: 1px solid var(--line);
  background: var(--surface-soft);
}
.recent-drafts p {
  margin: 0.45rem 0;
}
.recent-drafts ul {
  display: grid;
  gap: 0.25rem;
  padding: 0;
  margin: 0;
  list-style: none;
}
.recent-drafts li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.55rem;
}
.recent-draft {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.45rem 0.55rem;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--strong-ink);
  text-align: start;
  font: inherit;
  cursor: pointer;
}
.recent-draft:hover,
.recent-draft[aria-current='true'] {
  border-color: var(--line);
  background: var(--surface);
}
.recent-draft strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.recent-draft .tag,
.recent-drafts .text-link {
  flex: none;
}
.recent-draft .draft-created {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
}
.training-chat > p {
  margin: 0.6rem 1rem 0;
}
.chat-empty {
  margin: auto;
  max-width: 26rem;
  text-align: center;
  color: var(--secondary-ink);
}
.chat-empty h2 {
  margin-block: 0.5rem;
  color: var(--strong-ink);
  font-size: 1.1rem;
}
.chat-empty > .el-icon {
  color: var(--accent-ink);
  font-size: 1.5rem;
}
.chat-empty button {
  margin: 0.8rem 0;
  border: 1px solid var(--accent-border);
  border-radius: 7px;
  padding: 0.45rem 0.7rem;
  background: var(--accent-bg);
  color: var(--accent-ink);
}

.message-list {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.75rem;
  flex: 1;
  padding: 1rem;
  max-height: 75svh;
  overflow: auto;
}
.older-training-turns {
  align-self: center;
}
.chat-message,
.draft-card {
  padding: 0.8rem 1rem;
  border: 1px solid var(--line);
  border-radius: 0.75rem;
  background: var(--el-bg-color);
}
.chat-message[data-from='你'] {
  align-self: flex-end;
  max-width: 85%;
  background: var(--accent-bg);
}
.chat-message p {
  margin: 0.25rem 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.draft-card {
  align-self: stretch;
}
.draft-replacement {
  display: inline-flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin-inline-start: 0.5rem;
}
.draft-replacement .tag {
  color: var(--accent-ink);
  background: var(--accent-bg);
}
.draft-card header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
}
.draft-card h2 {
  margin: 0;
}
.draft-card header p {
  margin: 0.25rem 0 0;
}
.draft-card li {
  margin-block: 0.4rem;
}
.chat-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 0.75rem;
  align-items: end;
  padding: 0.8rem 1rem;
  border-top: 1px solid var(--line);
}
.training-chat--embedded .chat-form {
  grid-template-columns: minmax(0, 1fr);
}
.training-chat--embedded .chat-form .el-button {
  justify-self: end;
}
</style>
