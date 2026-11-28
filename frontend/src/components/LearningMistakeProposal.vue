<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'

const props = defineProps<{ requestId: string; disabled: boolean; withdrawnId?: string }>()
const emit = defineEmits<{ saved: []; open: [] }>()
const note = ref('')
const saved = ref(false)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const life = new AbortController()
const { signal } = life
const params = { path: { request_id: props.requestId } }

async function load() {
  loading.value = true
  error.value = ''
  try {
    unwrap(await api.GET('/api/v1/learning/agent/mistakes/{request_id}', { params, signal }))
    if (!signal.aborted) saved.value = true
  } catch (cause) {
    if (signal.aborted) return
    if (cause instanceof ApiError && cause.status === 404) saved.value = false
    else error.value = errorText(cause)
  } finally {
    loading.value = false
  }
}

async function confirm() {
  if (props.disabled || saving.value || loading.value || saved.value) return
  saving.value = true
  try {
    unwrap(
      await api.PUT('/api/v1/learning/agent/diagnostics/{request_id}/mistake', {
        params,
        headers: csrfHeaders(),
        body: { confirmed: true, note: note.value },
        signal,
      }),
    )
    if (signal.aborted) return
    saved.value = true
    note.value = ''
    error.value = ''
    emit('saved')
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    saving.value = false
  }
}

watch(
  () => props.withdrawnId,
  (request) => {
    if (request === props.requestId) void load()
  },
)
onMounted(() => void load())
onBeforeUnmount(() => life.abort())
</script>

<template>
  <form class="mistake-proposal" @submit.prevent="confirm">
    <h4>记录这个错因</h4>
    <p v-if="loading" class="small muted" role="status">正在读取个人记录…</p>
    <p v-if="error" class="small" role="alert">{{ error }}</p>
    <div v-if="saved" class="proposal-actions">
      <span class="small" role="status">已加入个人错因</span>
      <el-button text @click="emit('open')">查看个人错因</el-button>
    </div>
    <template v-else-if="!loading">
      <p class="small muted">核对原提交和样例后，可将上面的线索保存为个人错因。</p>
      <label class="field">
        <span class="small">复盘备注（选填）</span>
        <textarea v-model="note" rows="2" maxlength="600" :disabled="saving || disabled" />
      </label>
      <div class="proposal-actions">
        <el-button native-type="submit" :loading="saving" :disabled="disabled">
          确认并保存错因
        </el-button>
      </div>
    </template>
  </form>
</template>

<style scoped>
.mistake-proposal {
  display: grid;
  gap: 0.45rem;
  padding-block-start: 0.65rem;
  border-block-start: 1px solid var(--line);
}
.mistake-proposal :is(h4, p) {
  margin: 0;
}
.mistake-proposal h4 {
  font-size: 0.875rem;
}
.proposal-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.65rem;
}
</style>
