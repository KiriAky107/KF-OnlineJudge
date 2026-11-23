<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { skillPurposes as purposes } from '../api/skillPurposes'
import { createSkillDraft, type CreateSkillDraft } from '../api/skillDrafts'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import SkillDraftPublication from '../components/SkillDraftPublication.vue'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'

type Document = components['schemas']['SkillDraftDocument']
type Draft = components['schemas']['SkillDraftView']
type DraftPage = components['schemas']['SkillDraftPage']
type Skill = components['schemas']['SkillVersionView']
const MAX_DECLARATION_BYTES = 16_384

const toolNames: Record<string, string> = {
  submission_history: '读取本人提交记录',
  source_evidence: '读取本人代码证据',
  training_candidates: '查询训练候选题',
}
const key = (tool: Skill['available_tools'][number]) => `${tool.name}:${tool.version}`
const { user, refresh } = useSession()
const route = useRoute()
const router = useRouter()
const drafts = ref<DraftPage | null>(null)
const catalog = ref<Skill[]>([])
const sourceId = ref('')
const current = ref<Draft | null>(null)
const document = ref<Document | null>(null)
const parameters = ref('{}')
const requested = ref<string[]>([])
const unsaved = computed(() => {
  const saved = current.value?.document
  return (
    !!saved &&
    (JSON.stringify(document.value) !== JSON.stringify(saved) ||
      parameters.value !== JSON.stringify(saved.parameters ?? {}, null, 2) ||
      requested.value.join() !== (saved.requested_tools ?? []).map(key).join())
  )
})
const selectedTools = computed(() => {
  const tools = catalog.value
    .filter((skill) => skill.purpose === document.value?.purpose)
    .flatMap((skill) => skill.available_tools)
  return [...new Map(tools.map((tool) => [key(tool), tool])).values()]
})
const busy = ref(false)
const loading = ref(false)
const notice = ref('')
const error = ref('')
let pageController: AbortController | undefined
let actionController: AbortController | undefined

function message(cause: unknown) {
  if (cause instanceof ApiError && cause.status === 409) return '草稿已更新，请重新打开后再编辑。'
  if (cause instanceof ApiError && cause.status === 422)
    return '请检查声明、参考资料路径及摘要、参数和工具范围。'
  return errorText(cause)
}
function show(value: Draft) {
  current.value = value
  document.value = {
    ...value.document,
    references: value.document.references?.map((reference) => ({ ...reference })),
  }
  parameters.value = JSON.stringify(value.document.parameters ?? {}, null, 2)
  requested.value = (value.document.requested_tools ?? []).map(key)
}
function newDraft() {
  current.value = null
  document.value = {
    schema_version: 1,
    runtime_version: 1,
    purpose: 'guided_help',
    title: '',
    description: '',
    method: '',
    requested_tools: [],
  }
  parameters.value = '{}'
  requested.value = []
  error.value = ''
  notice.value = ''
}
function changePurpose() {
  parameters.value = '{}'
  requested.value = []
}
function addReference() {
  const editor = document.value
  if (!editor) return
  editor.references = [...(editor.references ?? []), { path: 'references/', content: '' }]
}
function removeReference(index: number) {
  const editor = document.value
  if (!editor?.references) return
  editor.references.splice(index, 1)
  if (!editor.references.length) delete editor.references
}
async function load() {
  pageController?.abort()
  if (!user.value) return
  pageController = new AbortController()
  const { signal } = pageController
  loading.value = true
  try {
    const [mine, skills] = await Promise.all([
      api.GET('/api/v1/learning/agent/skill-drafts', { signal }).then(unwrap),
      api.GET('/api/v1/learning/agent/skills', { signal }).then(unwrap),
    ])
    if (signal.aborted) return
    drafts.value = mine
    catalog.value = skills.versions.filter((skill) => skill.origin === 'builtin')
    if (!sourceId.value) sourceId.value = catalog.value[0]?.version_id ?? ''
  } catch (cause) {
    if (!signal.aborted) error.value = message(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
async function act(operation: (signal: AbortSignal, owner: string) => Promise<void>) {
  if (!user.value || busy.value) return
  actionController = new AbortController()
  const { signal } = actionController
  const owner = user.value.id
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await operation(signal, owner)
  } catch (cause) {
    if (!signal.aborted) error.value = message(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}
function open(id: string) {
  void act(async (signal) => {
    const result = unwrap(
      await api.GET('/api/v1/learning/agent/skill-drafts/{draft_id}', {
        params: { path: { draft_id: id } },
        signal,
      }),
    )
    if (!signal.aborted) show(result)
  })
}
async function create(body: CreateSkillDraft, signal: AbortSignal, owner: string) {
  const result = await createSkillDraft(body, owner, signal)
  if (signal.aborted) return
  show(result)
  notice.value = '草稿已创建。'
  void load()
}
function copy() {
  const source = sourceId.value
  if (!source) return
  void act((signal, owner) => create({ source_version_id: source }, signal, owner))
}
function importFile(event: Event) {
  const element = event.target as HTMLInputElement
  const file = element.files?.[0]
  element.value = ''
  if (!file) return
  void act(async (signal, owner) => {
    if (file.size > MAX_DECLARATION_BYTES) throw new Error('请选择不超过 16 KiB 的 JSON 声明。')
    let declaration: unknown
    try {
      declaration = JSON.parse(await file.text())
    } catch {
      throw new Error('文件不是有效的 JSON。')
    }
    if (!declaration || typeof declaration !== 'object' || Array.isArray(declaration))
      throw new Error('JSON 文件须包含一个技能声明对象。')
    if (signal.aborted) return
    await create({ document: declaration as Document }, signal, owner)
  })
}
function save() {
  if (!document.value) return
  const draft = current.value
  const editor = document.value
  void act(async (signal, owner) => {
    let settings: unknown
    try {
      settings = JSON.parse(parameters.value)
    } catch {
      throw new Error('默认参数需使用有效的 JSON。')
    }
    if (!settings || typeof settings !== 'object' || Array.isArray(settings))
      throw new Error('默认参数需使用 JSON 对象。')
    const declaration = {
      ...editor,
      parameters: settings as Record<string, unknown>,
      requested_tools: selectedTools.value.filter((tool) => requested.value.includes(key(tool))),
      references: editor.references?.map(({ path, content }) => ({ path, content })),
    }
    if (!draft) return await create({ document: declaration }, signal, owner)
    const saved = unwrap(
      await api.PUT('/api/v1/learning/agent/skill-drafts/{draft_id}', {
        params: { path: { draft_id: draft.draft_id } },
        headers: csrfHeaders(),
        signal,
        body: { revision: draft.revision, document: declaration },
      }),
    )
    if (signal.aborted) return
    show(saved)
    notice.value = '草稿已保存。'
    void load()
  })
}
function startTrial() {
  const draft = current.value
  if (!draft || unsaved.value) return
  void act(async (signal) => {
    const trial = unwrap(
      await api.POST('/api/v1/learning/agent/skill-drafts/{draft_id}/trials', {
        params: { path: { draft_id: draft.draft_id } },
        body: { revision: draft.revision },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (!signal.aborted) {
      await router.push({ path: '/trainings/assistant', query: { skill: trial.version_id } })
    }
  })
}
function download() {
  if (!current.value) return
  const json = current.value.document
  const options = { type: 'application/json' }
  const readable = new Blob([JSON.stringify(json, null, 2) + '\n'], options)
  const file =
    readable.size <= MAX_DECLARATION_BYTES
      ? readable
      : new Blob([JSON.stringify(json) + '\n'], options)
  const location = URL.createObjectURL(file)
  const a = window.document.createElement('a')
  a.href = location
  a.download = 'kf-skill-draft.json'
  a.click()
  setTimeout(() => URL.revokeObjectURL(location), 1000)
}
watch(
  () => user.value?.id,
  () => {
    pageController?.abort()
    actionController?.abort()
    drafts.value = null
    catalog.value = []
    current.value = null
    document.value = null
    sourceId.value = ''
    busy.value = false
    loading.value = false
    notice.value = ''
    error.value = ''
    void load()
  },
  { immediate: true },
)
watch(
  [() => user.value?.id, () => route.query.draft],
  ([owner, draft]) => {
    if (owner && typeof draft === 'string' && draft !== current.value?.draft_id) open(draft)
  },
  { immediate: true },
)
onMounted(async () => {
  if (!user.value) {
    try {
      await refresh()
    } catch (cause) {
      error.value = message(cause)
    }
  }
})
onBeforeUnmount(() => {
  pageController?.abort()
  actionController?.abort()
})
</script>

<template>
  <PageHeading title="技能草稿">
    <RouterLink class="el-button" to="/trainings/assistant">返回学习助手</RouterLink>
    <RouterLink class="el-button" to="/trainings/skill-library">技能目录</RouterLink>
  </PageHeading>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="!user">
    <RouterLink :to="{ path: '/login', query: { next: '/trainings/skill-drafts' } }"
      >登录</RouterLink
    >后编辑自己的草稿。
  </p>
  <div v-else class="draft-layout native-form">
    <PanelCard title="我的草稿" icon="trainings">
      <template #actions>
        <el-button type="primary" :disabled="busy" @click="newDraft">新建草稿</el-button>
      </template>
      <div class="stack">
        <p v-if="loading" role="status">正在读取草稿…</p>
        <p v-else-if="!drafts?.items.length" class="small muted">暂无草稿。</p>
        <ul v-else class="draft-list">
          <li v-for="item in drafts.items" :key="item.draft_id">
            <button type="button" :disabled="busy" @click="open(item.draft_id)">
              <strong>{{ item.title }}</strong>
              <span class="small muted"
                >{{ purposes[item.purpose] }} · {{ displayTime(item.updated_at) }}</span
              >
            </button>
          </li>
        </ul>
        <p v-if="drafts?.truncated" class="small muted">显示最近 50 份草稿。</p>
        <div v-if="catalog.length" class="stack">
          <label class="field"
            >官方版本
            <select v-model="sourceId" :disabled="busy">
              <option v-for="skill in catalog" :key="skill.version_id" :value="skill.version_id">
                {{ skill.title }} · v{{ skill.release }}
              </option>
            </select>
          </label>
          <div>
            <el-button :disabled="busy || !sourceId" @click="copy">复制为私有草稿</el-button>
          </div>
        </div>
        <label class="field"
          >导入技能声明（JSON，最多 16 KiB）
          <input
            type="file"
            accept=".json,application/json"
            :disabled="busy"
            @change="importFile"
          />
        </label>
      </div>
    </PanelCard>
    <PanelCard v-if="document" :title="current ? '编辑草稿' : '新建草稿'">
      <template #actions>
        <el-button v-if="current" :disabled="busy || unsaved" @click="startTrial"
          >试用已保存修订</el-button
        >
        <el-button v-if="current" :disabled="busy" @click="download">导出已保存 JSON</el-button>
      </template>
      <form class="stack" @submit.prevent="save">
        <p v-if="current?.source_version_id" class="small muted">
          复制来源 · {{ current.source_version_id }}
        </p>
        <fieldset class="stack" :disabled="busy">
          <label class="field"
            >用途
            <select v-model="document.purpose" @change="changePurpose">
              <option v-for="(label, value) in purposes" :key="value" :value="value">
                {{ label }}
              </option>
            </select>
          </label>
          <label class="field"
            >名称<input v-model="document.title" maxlength="80" required
          /></label>
          <label class="field"
            >说明<textarea v-model="document.description" rows="2" maxlength="500" required />
          </label>
          <label class="field"
            >教学方法<textarea v-model="document.method" rows="5" maxlength="4000" required />
          </label>
          <fieldset class="stack reference-editor">
            <legend>参考资料</legend>
            <p class="small muted">
              最多 4 份。使用 references/名称.md 或 .txt，名称用小写字母、数字、下划线或连字符。
            </p>
            <div
              v-for="(reference, index) in document.references ?? []"
              :key="index"
              class="stack reference-fields"
            >
              <label class="field">
                资料路径<input v-model="reference.path" maxlength="75" required />
              </label>
              <label class="field">
                资料正文<textarea v-model="reference.content" rows="4" maxlength="3000" required />
              </label>
              <div>
                <button type="button" class="el-button" @click="removeReference(index)">
                  移除资料
                </button>
              </div>
            </div>
            <div>
              <button
                type="button"
                class="el-button"
                :disabled="(document.references?.length ?? 0) >= 4"
                @click="addReference"
              >
                添加参考资料
              </button>
            </div>
          </fieldset>
          <label class="field">许可<input v-model="document.license" maxlength="80" /></label>
          <label class="field"
            >来源与署名<input v-model="document.attribution" maxlength="256"
          /></label>
          <label class="field"
            >默认参数（JSON 对象）<textarea v-model="parameters" rows="4" spellcheck="false" />
          </label>
          <fieldset v-if="selectedTools.length" class="stack">
            <legend>申请的工具范围</legend>
            <label v-for="tool in selectedTools" :key="key(tool)" class="check">
              <input v-model="requested" type="checkbox" :value="key(tool)" />
              {{ toolNames[tool.name] ?? tool.name }}
            </label>
          </fieldset>
        </fieldset>
        <div>
          <el-button native-type="submit" type="primary" :loading="busy">保存草稿</el-button>
          <span v-if="unsaved" class="small muted">保存修改后可试用这次修订。</span>
        </div>
      </form>
      <SkillDraftPublication
        v-if="current"
        :key="current.draft_id + ':' + current.revision"
        :draft-id="current.draft_id"
        :revision="current.revision"
        :license="current.document.license"
      />
    </PanelCard>
  </div>
</template>

<style scoped>
.draft-layout {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(0, 2fr);
  gap: 1rem;
  align-items: start;
}
.field {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
}
input:not([type='checkbox']),
select,
textarea {
  width: 100%;
}
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.reference-editor {
  padding: 0.85rem 1rem 1rem;
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--el-border-radius-base);
  background: var(--el-fill-color-lighter);
}
.reference-editor legend {
  padding: 0 0.35rem;
  font-weight: 650;
}
.reference-fields + .reference-fields {
  padding-top: 0.85rem;
  border-top: 1px solid var(--el-border-color-light);
}
.draft-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.draft-list button {
  display: grid;
  text-align: left;
  width: 100%;
  background: none;
  border: 0;
  border-bottom: 1px solid var(--line);
  padding: 0.5rem;
  color: inherit;
}
.check {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
@media (max-width: 750px) {
  .draft-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
