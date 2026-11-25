<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { adminProblems as api, maintenanceError } from '../api/adminProblems'
import type { components } from '../api/generated/schema'
import { ApiError, unwrap } from '../api/transport'
import { api as platform } from '../api/client'
import { useSession } from '../composables/useSession'
import { sha256Fingerprint } from '../lib/requestCrypto'
import StatementAuthor from './StatementAuthor.vue'
import { loadIntent, mutationIntent, saveIntent, type MutationIntent } from './intents'

const problemId = String(useRoute().params.id)
const session = useSession()
const controller = new AbortController()
const encoder = new TextEncoder()
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })
const editor = ref<components['schemas']['ProblemEditor']>()
const loading = ref(false)
const busy = ref(false)
const conflict = ref(false)
const error = ref('')
const receipt = ref<components['schemas']['MaintenanceReceipt']>()
const title = ref('')
const statement = ref('')
const difficulty = ref('')
const levels = ref<components['schemas']['DifficultyLevel'][]>([])
const tags = ref('')
const prerequisites = ref('')
type SampleField = { text: string; binary?: string }
const samples = ref<{ key: number; input: SampleField; output: SampleField }[]>([])
let sampleKey = 0
let intent = loadIntent<MutationIntent>('revision')

const editable = computed(
  () => editor.value?.enabled && !conflict.value && !loading.value && !busy.value,
)
const languageNames = { c17: 'C17', cpp20: 'C++20', python313: 'Python 3.13', java21: 'Java 21' }
const limits = computed(() => {
  const detail = editor.value?.detail
  if (!detail) return []
  return detail.run_limits
    ? [
        {
          language: detail.languages.map((name) => languageNames[name]).join(' / '),
          run: detail.run_limits,
        },
      ]
    : (detail.language_configurations ?? []).map((item) => ({
        language: languageNames[item.language],
        run: item.run_limits,
      }))
})

function readSample(value: string): SampleField {
  try {
    return { text: decoder.decode(Uint8Array.from(atob(value), (char) => char.charCodeAt(0))) }
  } catch {
    return { text: '', binary: value }
  }
}
function encodeSample(field: SampleField) {
  if (field.binary !== undefined) return field.binary
  const bytes = encoder.encode(field.text)
  if (bytes.length > 65536) throw new Error('单个样例输入或输出最多 64 KiB。')
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}
function addSample() {
  samples.value.push({ key: sampleKey++, input: { text: '' }, output: { text: '' } })
}
function labels(input: string) {
  return [
    ...new Set(
      input
        .split(/[,，]/)
        .map((value) => value.trim())
        .filter(Boolean),
    ),
  ]
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [value, settings] = await Promise.all([
      api.editor(problemId, controller.signal),
      platform
        .GET('/api/v1/problems/difficulty-levels', { signal: controller.signal })
        .then(unwrap),
    ])
    levels.value = settings
    editor.value = value
    title.value = value.detail.title
    statement.value = value.detail.statement
    difficulty.value = value.metadata.difficulty_key ?? value.metadata.difficulty ?? ''
    tags.value = value.metadata.tags.join(', ')
    prerequisites.value = (value.metadata.prerequisite_tags ?? []).join(', ')
    samples.value = value.detail.samples.map((sample) => ({
      key: sampleKey++,
      input: readSample(sample.input),
      output: readSample(sample.output),
    }))
    conflict.value = false
  } catch (cause) {
    if (!controller.signal.aborted) error.value = maintenanceError(cause)
  } finally {
    loading.value = false
  }
}

async function publish() {
  const current = editor.value
  const user = session.user.value
  if (!current || !user || !editable.value) return
  busy.value = true
  error.value = ''
  receipt.value = undefined
  try {
    if (
      !title.value.trim() ||
      !statement.value.trim() ||
      encoder.encode(statement.value).length > 262144
    ) {
      throw new Error('请输入标题和题面，题面最多 256 KiB。')
    }
    const body = {
      schema_version: 1 as const,
      expected_active_version_id: current.detail.version_id,
      expected_metadata_revision: current.metadata.revision,
      title: title.value.trim(),
      statement: statement.value,
      samples: samples.value.map(({ input, output }) => ({
        encoding: 'base64' as const,
        input: encodeSample(input),
        output: encodeSample(output),
      })),
      metadata: {
        tags:
          tags.value === current.metadata.tags.join(', ')
            ? current.metadata.tags
            : labels(tags.value),
        difficulty: levels.value.find((level) => level.key === difficulty.value)?.tier ?? null,
        difficulty_key: difficulty.value || undefined,
        prerequisite_tags:
          prerequisites.value === (current.metadata.prerequisite_tags ?? []).join(', ')
            ? (current.metadata.prerequisite_tags ?? [])
            : labels(prerequisites.value),
      },
    }
    const fingerprint = sha256Fingerprint(encoder.encode(JSON.stringify({ problemId, body })))
    intent = mutationIntent(intent, user.id, fingerprint)
    saveIntent('revision', intent)
    receipt.value = await api.revise(
      problemId,
      { ...body, operation_id: intent.operation_id },
      controller.signal,
    )
    await load()
  } catch (cause) {
    if (controller.signal.aborted) return
    if (cause instanceof ApiError && cause.status === 409) {
      conflict.value = true
      error.value = '当前版本或学习元数据已变更。请先复制需要保留的修改，再载入最新版本。'
    } else {
      error.value = cause instanceof ApiError ? maintenanceError(cause) : (cause as Error).message
    }
  } finally {
    busy.value = false
  }
}

onMounted(load)
onUnmounted(() => controller.abort())
</script>

<template>
  <div class="admin-management">
    <header class="admin-page-heading">
      <RouterLink to="/problems" class="text-link">返回题目维护</RouterLink>
      <h1>编辑题目</h1>
      <p v-if="editor">{{ editor.detail.slug }} · 当前版本 {{ editor.detail.revision }}</p>
    </header>
    <p v-if="loading" role="status">正在读取题目…</p>
    <p v-if="error" class="admin-feedback" role="alert">{{ error }}</p>
    <p v-if="receipt" class="admin-feedback" role="status">
      已发布版本 {{ receipt.problem.revision }}。
      <a
        v-if="receipt.published_version_id"
        :href="'/problems/' + receipt.published_version_id"
        class="text-link"
      >
        查看新题面
      </a>
    </p>
    <button
      v-if="conflict || (!editor && error)"
      type="button"
      :disabled="loading || busy"
      @click="load"
    >
      载入最新版本
    </button>
    <p v-if="editor && !editor.enabled" class="admin-feedback">请先恢复题目，再发布新版本。</p>
    <form v-if="editor" class="revision-form" @submit.prevent="publish">
      <fieldset :disabled="busy || !editor.enabled">
        <section class="admin-real-panel">
          <h2>基本信息</h2>
          <label>标题<input v-model="title" maxlength="160" required /></label>
          <div class="revision-grid">
            <label
              >难度
              <select v-model="difficulty">
                <option value="">未标注</option>
                <option v-for="level in levels" :key="level.key" :value="level.key">
                  {{ level.label }} · {{ level.score }}
                </option>
              </select>
            </label>
            <RouterLink class="text-link" to="/problem-difficulties">管理难度档位</RouterLink>
            <label>知识点（逗号分隔）<input v-model="tags" /></label>
            <label>先修知识点（逗号分隔）<input v-model="prerequisites" /></label>
          </div>
        </section>
        <section class="admin-real-panel">
          <h2>题目描述</h2>
          <StatementAuthor v-model="statement" publishing />
        </section>
        <section class="admin-real-panel">
          <h2>公开样例</h2>
          <div v-for="(sample, index) in samples" :key="sample.key" class="revision-sample">
            <div class="admin-actions">
              <strong>样例 {{ index + 1 }}</strong>
              <button type="button" @click="samples.splice(index, 1)">移除此样例</button>
            </div>
            <div class="sample-grid">
              <label
                >样例输入
                <textarea
                  v-model="sample.input.text"
                  :readonly="sample.input.binary !== undefined"
                  :placeholder="sample.input.binary !== undefined ? '二进制数据' : ''"
                  rows="4"
                />
              </label>
              <label
                >样例输出
                <textarea
                  v-model="sample.output.text"
                  :readonly="sample.output.binary !== undefined"
                  :placeholder="sample.output.binary !== undefined ? '二进制数据' : ''"
                  rows="4"
                />
              </label>
            </div>
          </div>
          <button type="button" :disabled="samples.length >= 10" @click="addSample">
            添加样例
          </button>
        </section>
      </fieldset>
      <section class="admin-real-panel">
        <h2>判题配置</h2>
        <p>{{ editor.case_count }} 个隐藏测试点。发布后继续使用当前测试数据和语言限额。</p>
        <ul>
          <li v-for="{ language, run } in limits" :key="language">
            {{ language }} · CPU {{ run.cpu_time_us / 1000 }} ms · 内存
            {{ run.memory_bytes / 1048576 }} MiB
          </li>
        </ul>
      </section>
      <div class="admin-actions revision-submit">
        <button class="admin-primary" type="submit" :disabled="!editable">
          {{ busy ? '正在发布…' : '发布新版本' }}
        </button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.revision-form fieldset {
  min-width: 0;
  border: 0;
  margin: 0;
  padding: 0;
}
.revision-form label {
  display: grid;
  gap: 0.4rem;
}
.revision-grid,
.sample-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 13rem), 1fr));
  gap: 0.8rem;
}
.revision-grid {
  margin-block-start: 0.8rem;
}
.revision-sample {
  border-bottom: 1px solid var(--el-border-color);
  margin-block: 0.85rem;
  padding-block-end: 0.85rem;
}
.sample-grid textarea {
  width: 100%;
  resize: vertical;
  padding: 0.65rem;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  font-family: monospace;
}
.revision-submit {
  justify-content: flex-end;
  padding-block: 0.85rem;
}
</style>
