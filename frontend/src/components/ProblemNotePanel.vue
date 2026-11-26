<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import TrainingNoteEditor from './TrainingNoteEditor.vue'

const props = defineProps<{ versionId: string; title: string }>()
const { user } = useSession()
const route = useRoute()
const login = computed(() => ({ path: '/login', query: { next: route.fullPath } }))
const note = ref('')
const saved = ref('')
const loading = ref(false)
const ready = ref(false)
const saving = ref(false)
const error = ref('')
let request: AbortController | undefined

async function load() {
  const signal = request?.signal
  if (!user.value || !signal || loading.value) return
  loading.value = true
  error.value = ''
  try {
    const result = unwrap(
      await api.GET('/api/v1/learning/problem-notes/{problem_version_id}', {
        params: { path: { problem_version_id: props.versionId } },
        signal,
      }),
    )
    if (!signal.aborted) {
      saved.value = note.value = result.note
      ready.value = true
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

async function save() {
  const signal = request?.signal
  if (!user.value || !signal || !ready.value || saving.value || note.value === saved.value) return
  saving.value = true
  error.value = ''
  try {
    const result = unwrap(
      await api.PUT('/api/v1/learning/problem-notes/{problem_version_id}', {
        params: { path: { problem_version_id: props.versionId } },
        headers: csrfHeaders(),
        body: { note: note.value },
        signal,
      }),
    )
    if (!signal.aborted) saved.value = note.value = result.note
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = false
  }
}

watch(
  [() => user.value?.id, () => props.versionId],
  ([owner], _previous, onCleanup) => {
    const controller = new AbortController()
    request = controller
    onCleanup(() => controller.abort())
    saved.value = note.value = error.value = ''
    loading.value = ready.value = saving.value = false
    if (owner) void load()
  },
  { flush: 'sync', immediate: true },
)
</script>

<template>
  <div class="problem-note">
    <p v-if="!user" class="note-status">
      <RouterLink :to="login" class="text-link">登录</RouterLink>后可保存本人笔记。
    </p>
    <p v-else-if="loading" class="note-status" role="status">正在读取笔记…</p>
    <p v-if="error" class="note-status" role="alert">
      {{ error }}
      <el-button v-if="!ready" text size="small" @click="load">重试读取</el-button>
    </p>
    <TrainingNoteEditor
      v-if="ready"
      v-model="note"
      :item-id="versionId"
      :title="title"
      :saved="saved"
      :saving="saving"
      :disabled="saving"
      @save="save"
    />
  </div>
</template>

<style scoped>
.problem-note {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.problem-note > :deep(.training-note-editor) {
  flex: 1;
  height: auto;
}
.note-status {
  margin: 0.75rem 0.85rem;
}
</style>
