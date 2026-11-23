<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { agentRunIntent } from '../api/agentRunIntent'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { boundedPoll } from '../api/polling'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'
import { useSession } from '../composables/useSession'
import {
  hasTrainingCandidateAccess,
  trainingCandidateTool,
  trainingChatSkill,
  trainingTurn,
  type TrainingSetConditions,
} from '../lib/trainingSetChat'

type Run = components['schemas']['AgentRunView']
type Skill = components['schemas']['SkillVersionView']
const route = useRoute()
const router = useRouter()
const { user, refresh } = useSession()
const skills = ref<Skill[]>([])
const input = ref('')
const awaitingTopic = ref<TrainingSetConditions>()
const messages = ref<{ from: '你' | '学习助手'; text: string }[]>([])
const run = ref<Run>()
const active = ref<string>()
const busy = ref(false)
const error = ref('')
const notice = ref('')
let request = new AbortController()

const skill = computed(() => trainingChatSkill(skills.value, run.value?.skill_version_id))
const authorized = computed(() => !!skill.value && hasTrainingCandidateAccess(skill.value))
const draft = computed(() =>
  run.value?.result?.purpose === 'focused_training' ? run.value.result : undefined,
)
const terminal = (value: Run) => ['succeeded', 'failed', 'cancelled'].includes(value.state)
const pending = computed(() => !!active.value && (!run.value || !terminal(run.value)))
const difficulty = { easy: '简单', medium: '中等', hard: '困难' } as const
const monitor = boundedPoll<Run>({
  load: async (signal) =>
    unwrap(
      await api.GET('/api/v1/learning/agent/runs/{run_id}', {
        params: { path: { run_id: active.value! } },
        signal,
      }),
    ),
  terminal,
  result: (value) => {
    run.value = value
    if (value.state === 'failed') notice.value = '这次草案生成失败。可以调整条件后重试。'
    if (value.state === 'cancelled') notice.value = '这次草案已取消。'
  },
  error: (cause) => (error.value = errorText(cause)),
  exhausted: () => (notice.value = '已暂停自动刷新，可继续查看草案状态。'),
  maxRequests: 90,
  maxTime: 180_000,
})

async function action(work: (signal: AbortSignal) => Promise<void>) {
  if (busy.value) return
  const { signal } = request
  busy.value = true
  error.value = notice.value = ''
  try {
    await work(signal)
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}

function selectRun(id: string | undefined) {
  if (active.value === id) return
  monitor.pause()
  active.value = id
  if (run.value?.run_id !== id) run.value = undefined
  if (!id) {
    messages.value = []
    awaitingTopic.value = undefined
  }
  if (id && (!run.value || !terminal(run.value))) monitor.start()
}

watch(
  () => user.value?.id,
  (owner) => {
    request.abort()
    request = new AbortController()
    busy.value = false
    error.value = notice.value = ''
    input.value = ''
    messages.value = []
    awaitingTopic.value = undefined
    skills.value = []
    selectRun(undefined)
    if (!owner) return
    void action(async (signal) => {
      const catalog = unwrap(await api.GET('/api/v1/learning/agent/skills', { signal }))
      if (!signal.aborted) skills.value = catalog.versions
    })
  },
  { immediate: true },
)
watch(
  [() => user.value?.id, () => route.query.run],
  ([owner, id]) =>
    selectRun(owner && typeof id === 'string' && /^[0-9a-f-]{36}$/.test(id) ? id : undefined),
  { immediate: true },
)
onMounted(() => {
  if (!user.value) void refresh().catch((cause) => (error.value = errorText(cause)))
})
onBeforeUnmount(() => {
  request.abort()
  monitor.dispose()
})

function enable() {
  const selected = skill.value
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

function send() {
  const owner = user.value?.id
  const selected = skill.value
  const text = input.value.trim()
  if (!owner || !selected || !authorized.value || pending.value || busy.value || !text) return
  const options = draft.value?.training_set
  const source = options ? run.value : undefined
  const turn = trainingTurn(
    text,
    source && options ? { runId: source.run_id, options } : undefined,
    awaitingTopic.value,
  )
  messages.value.push({ from: '你', text })
  if ('reply' in turn) {
    if (turn.awaitingTopic) awaitingTopic.value = turn.awaitingTopic
    messages.value.push({ from: '学习助手', text: turn.reply })
    input.value = ''
    return
  }
  void action(async (signal) => {
    const intent = agentRunIntent(
      localStorage,
      owner,
      JSON.stringify([selected.version_id, source?.session_id, turn.draft]),
    )
    let session = source?.session_id ?? intent.sessionId
    if (!session) {
      const created = unwrap(
        await api.POST('/api/v1/learning/agent/sessions', {
          headers: csrfHeaders(),
          body: { purpose: 'focused_training' },
          signal,
        }),
      )
      if (signal.aborted) return
      session = created.session_id
      intent.bind(session)
    }
    const receipt = unwrap(
      await api.POST('/api/v1/learning/agent/sessions/{session_id}/runs', {
        headers: csrfHeaders(),
        params: { path: { session_id: session } },
        body: {
          request_id: intent.requestId,
          skill_version_id: selected.version_id,
          training_set: turn.draft,
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
    input.value = ''
    awaitingTopic.value = undefined
    run.value = receipt
    selectRun(receipt.run_id)
    await router.replace({ query: { run: receipt.run_id } })
  })
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
  if (!id || !draft.value?.training_set || !draft.value.items.length) return
  void action(async (signal) => {
    const saved = unwrap(
      await api.POST('/api/v1/learning/agent/runs/{run_id}/training', {
        headers: csrfHeaders(),
        params: { path: { run_id: id } },
        signal,
      }),
    )
    if (!signal.aborted && run.value?.run_id === id) {
      run.value = { ...run.value, training_id: saved.id }
      notice.value = '训练集已创建。'
    }
  })
}
</script>

<template>
  <PageHeading title="聊天创建训练集" eyebrow="TRAINING">
    <RouterLink v-if="active && !pending" class="el-button" to="/trainings/create">
      新建训练集
    </RouterLink>
    <RouterLink class="el-button" to="/trainings">返回训练集</RouterLink>
  </PageHeading>
  <p v-if="error" role="alert">
    {{ error }}
    <el-button v-if="active && pending" size="small" @click="monitor.start()">重试读取</el-button>
  </p>
  <p v-if="!user">
    <RouterLink :to="{ path: '/login', query: { next: route.fullPath } }">登录</RouterLink
    >后可创建训练集。
  </p>
  <div v-else class="training-chat">
    <p v-if="!pending && skill && !authorized">
      创建训练集需要启用专项训练的题库查询。
      <el-button :disabled="busy" @click="enable">启用题库查询</el-button>
    </p>
    <p v-else-if="run && !pending && !skill">
      继续修改这份草案需要原技能版本。
      <RouterLink to="/trainings/assistant">管理学习技能</RouterLink>
    </p>
    <div class="message-list" aria-label="训练集对话">
      <div
        v-for="(message, index) in messages"
        :key="index"
        class="chat-message"
        :data-from="message.from"
      >
        <strong>{{ message.from }}</strong>
        <p>{{ message.text }}</p>
      </div>
      <p v-if="pending" role="status">
        正在生成草案…
        <el-button size="small" :disabled="busy" @click="cancel">取消</el-button>
      </p>
      <p v-if="notice" role="status">
        {{ notice }}
        <el-button v-if="pending" size="small" @click="monitor.start()">刷新状态</el-button>
      </p>
      <section v-if="draft?.training_set" class="draft-card" aria-label="训练集草案">
        <header>
          <div>
            <h2>{{ draft.training_set.title }}</h2>
            <p>{{ draft.items.length }} / {{ draft.training_set.count }} 道题</p>
          </div>
          <RouterLink
            v-if="run?.training_id"
            class="el-button el-button--primary"
            :to="'/trainings/' + run.training_id"
            >打开训练集</RouterLink
          >
          <el-button
            v-else
            type="primary"
            :disabled="busy || pending || !draft.items.length"
            @click="save"
            >确认创建</el-button
          >
        </header>
        <p
          v-if="draft.training_set.tags.length || draft.training_set.difficulty"
          class="small muted"
        >
          {{ draft.training_set.tags.join('、') }}
          <template v-if="draft.training_set.difficulty">
            · {{ difficulty[draft.training_set.difficulty] }}</template
          >
        </p>
        <ol>
          <li v-for="item in draft.items" :key="item.problem_version_id">
            <RouterLink :to="'/problems/' + item.problem_version_id">{{ item.title }}</RouterLink>
            <span v-if="item.difficulty" class="muted"> · {{ difficulty[item.difficulty] }}</span>
          </li>
        </ol>
        <p v-for="gap in draft.gaps" :key="gap" class="small muted">{{ gap }}</p>
      </section>
    </div>
    <form class="chat-form" @submit.prevent="send">
      <el-input
        v-model="input"
        maxlength="500"
        type="textarea"
        :rows="2"
        aria-label="训练集要求"
        placeholder="建一套二分查找训练集，4 道入门题"
        :disabled="!authorized || pending || busy"
      />
      <el-button
        native-type="submit"
        type="primary"
        :disabled="!authorized || pending || busy || !input.trim()"
        >发送</el-button
      >
    </form>
  </div>
</template>

<style scoped>
.training-chat {
  display: grid;
  gap: 1rem;
  max-width: 54rem;
  margin: 0 auto;
}
.message-list {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.75rem;
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
}
.chat-message p {
  margin: 0.25rem 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.draft-card {
  align-self: stretch;
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
}
</style>
