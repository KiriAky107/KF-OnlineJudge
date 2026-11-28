<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import { diagnosticCategories } from '../lib/diagnostic'

type Mistake = components['schemas']['PersonalMistake']
const props = defineProps<{ revision: number }>()
const emit = defineEmits<{
  withdrawn: [requestId: string]
  practice: [requestId: string, minutes?: number]
}>()
const path = '/api/v1/learning/agent/mistakes/{request_id}'
const items = ref<Mistake[]>([])
const cursor = ref<string | null>(null)
const loaded = ref(false)
const loading = ref(false)
const busy = ref(false)
const editing = ref('')
const withdrawing = ref('')
const draft = ref('')
const notice = ref('')
const error = ref('')
const life = new AbortController()
const { signal } = life
let request: AbortController | undefined
let reloadAfterMutation = false

async function load(before?: string) {
  if (busy.value) {
    reloadAfterMutation = true
    return
  }
  request?.abort()
  const fetchSignal = (request = new AbortController()).signal
  loading.value = true
  error.value = ''
  try {
    const page = unwrap(
      await api.GET('/api/v1/learning/agent/mistakes', {
        params: { query: { before } },
        signal: fetchSignal,
      }),
    )
    if (fetchSignal.aborted) return
    items.value = before ? [...items.value, ...page.items] : page.items
    cursor.value = page.next_before
    loaded.value = true
  } catch (cause) {
    if (fetchSignal.aborted) return
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) items.value = []
    error.value = errorText(cause)
  } finally {
    if (!fetchSignal.aborted) loading.value = false
  }
}

function edit(item: Mistake) {
  editing.value = item.request_id
  withdrawing.value = ''
  draft.value = item.note
  notice.value = ''
}

async function mutate(requestId: string, remove: boolean) {
  if (busy.value) return
  request?.abort()
  loading.value = false
  busy.value = true
  error.value = ''
  notice.value = ''
  const params = { path: { request_id: requestId } }
  try {
    if (remove) {
      const { response } = await api.DELETE(path, { params, headers: csrfHeaders(), signal })
      if (!response.ok) throw new ApiError(response.status)
      if (signal.aborted) return
      items.value = items.value.filter((item) => item.request_id !== requestId)
      notice.value = '已撤回错因。'
      emit('withdrawn', requestId)
      if (cursor.value === requestId) void load()
    } else {
      const result = unwrap(
        await api.PATCH(path, {
          params,
          headers: csrfHeaders(),
          body: { note: draft.value },
          signal,
        }),
      )
      if (signal.aborted) return
      items.value = items.value.map((item) => (item.request_id === requestId ? result : item))
      notice.value = '已保存备注。'
    }
    editing.value = ''
    withdrawing.value = ''
  } catch (cause) {
    if (!signal.aborted) {
      error.value = errorText(cause)
      if (cause instanceof ApiError && [401, 403].includes(cause.status)) items.value = []
    }
  } finally {
    busy.value = false
    if (reloadAfterMutation && !signal.aborted) {
      reloadAfterMutation = false
      void load()
    }
  }
}

watch(
  () => props.revision,
  () => void load(),
)
onMounted(() => void load())
onBeforeUnmount(() => {
  request?.abort()
  life.abort()
})
</script>

<template>
  <section class="personal-mistakes" aria-label="个人错因记录">
    <p v-if="error" role="alert">{{ error }} <el-button text @click="load()">重试</el-button></p>
    <p v-if="notice" class="small muted" role="status">{{ notice }}</p>
    <p v-if="loading && !loaded" class="small muted" role="status">正在读取个人错因…</p>
    <p v-else-if="loaded && !items.length && !cursor" class="small muted">还没有确认的错因。</p>
    <ul v-if="items.length" class="mistake-list">
      <li v-for="item in items" :key="item.request_id" class="mistake-record">
        <h3>{{ diagnosticCategories[item.finding.category] }}</h3>
        <div class="record-actions small">
          <RouterLink :to="'/submissions/' + item.submission_id" class="text-link"
            >原提交</RouterLink
          >
          <RouterLink :to="'/problems/' + item.problem_version_id" class="text-link">
            原题目版本
          </RouterLink>
          <span class="muted">
            公开样例 {{ item.sample_ordinal + 1 }} · 第 {{ item.finding.line }} 行，第
            {{ item.finding.column }} 列
          </span>
        </div>
        <form v-if="editing === item.request_id" @submit.prevent="mutate(item.request_id, false)">
          <label class="field">
            <span class="small">复盘备注</span>
            <textarea v-model="draft" rows="3" maxlength="600" :disabled="busy" />
          </label>
          <div class="record-actions">
            <el-button native-type="submit" :loading="busy">保存备注</el-button>
            <el-button text :disabled="busy" @click="editing = ''">取消修改</el-button>
          </div>
        </form>
        <template v-else>
          <p v-if="item.note" class="mistake-note">{{ item.note }}</p>
          <div v-if="withdrawing === item.request_id" class="record-actions">
            <el-button type="danger" :loading="busy" @click="mutate(item.request_id, true)">
              确认撤回
            </el-button>
            <el-button text :disabled="busy" @click="withdrawing = ''">取消</el-button>
          </div>
          <div v-else class="record-actions">
            <el-button text :disabled="busy" @click="emit('practice', item.request_id)">
              找同类题
            </el-button>
            <el-button text :disabled="busy" @click="emit('practice', item.request_id, 30)">
              30 分钟补练
            </el-button>
            <el-button text :disabled="busy" @click="edit(item)">修改备注</el-button>
            <el-button text :disabled="busy" @click="withdrawing = item.request_id">撤回</el-button>
          </div>
        </template>
      </li>
    </ul>
    <el-button v-if="cursor" text :loading="loading" @click="load(cursor)">加载更多</el-button>
  </section>
</template>

<style scoped>
.personal-mistakes,
.mistake-list,
.mistake-record,
.mistake-record form {
  display: grid;
  gap: 0.55rem;
}
.personal-mistakes :is(h3, p, ul) {
  margin: 0;
}
.mistake-list {
  list-style: none;
  padding: 0;
}
.mistake-record {
  padding: 0.65rem 0.75rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}
.mistake-record h3 {
  font-size: 1rem;
}
.record-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.55rem;
}
.record-actions :deep(.el-button + .el-button) {
  margin-inline-start: 0;
}
.mistake-note {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
