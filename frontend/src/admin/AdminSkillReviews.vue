<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { skillPurposes } from '../api/skillPurposes'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import PanelCard from '../components/PanelCard.vue'
import SkillDeclaration from '../components/SkillDeclaration.vue'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import { hasMaintenance } from './intents'

type Review = components['schemas']['SkillVersionReview']
type Decision = components['schemas']['ReviewSkillVersion']['action'] | 'disable'
const { user } = useSession()
const state = ref<Review['state']>('pending')
const labels: Record<Review['state'], string> = {
  pending: '待审核',
  published: '已发布',
  archived: '已归档',
  rejected: '已驳回',
  withdrawn: '已撤回',
  disabled: '已停用',
}
const page = ref<components['schemas']['SkillVersionReviewPage'] | null>(null)
const notes = ref<Record<string, string>>({})
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const success = ref('')
let controller: AbortController | undefined

async function load() {
  controller?.abort()
  page.value = null
  notes.value = {}
  error.value = ''
  loading.value = false
  saving.value = false
  if (!hasMaintenance(user.value)) return
  controller = new AbortController()
  const { signal } = controller
  loading.value = true
  try {
    const result = unwrap(
      await api.GET('/api/v1/admin/skill-reviews', {
        params: { query: { state: state.value } },
        signal,
      }),
    )
    if (!signal.aborted) page.value = result
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
async function decide(item: Review, action: Decision) {
  if (!controller || !hasMaintenance(user.value) || saving.value) return
  const { signal } = controller
  const note = notes.value[item.version_id]?.trim() ?? ''
  if ((action === 'reject' || action === 'disable') && !note) return
  saving.value = true
  error.value = ''
  success.value = ''
  try {
    const params = { path: { version_id: item.version_id } }
    const headers = csrfHeaders()
    const result =
      action === 'disable'
        ? unwrap(
            await api.POST('/api/v1/admin/skill-reviews/{version_id}/disable', {
              params,
              headers,
              signal,
              body: { reason: note },
            }),
          )
        : unwrap(
            await api.POST('/api/v1/admin/skill-reviews/{version_id}', {
              params,
              headers,
              signal,
              body: { action, note },
            }),
          )
    if (signal.aborted) return
    success.value =
      result.state === 'disabled'
        ? '版本已停用。'
        : result.state === 'published'
          ? '版本已发布。'
          : '版本已驳回。'
    await load()
  } catch (cause) {
    if (signal.aborted) return
    error.value =
      cause instanceof ApiError && cause.status === 409
        ? '此版本状态已变化，请刷新队列。'
        : errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = false
  }
}
watch(
  [() => user.value?.id, () => hasMaintenance(user.value), state],
  () => {
    success.value = ''
    void load()
  },
  { immediate: true },
)
onUnmounted(() => controller?.abort())
</script>

<template>
  <header class="admin-page-heading"><h1>技能审核</h1></header>
  <PanelCard title="版本队列" class="native-form">
    <template #actions>
      <label>
        状态
        <select v-model="state" :disabled="saving">
          <option v-for="(label, value) in labels" :key="value" :value="value">{{ label }}</option>
        </select>
      </label>
      <el-button :loading="loading" :disabled="saving" @click="load">刷新队列</el-button>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="success" role="status">{{ success }}</p>
    <p v-if="loading" role="status">正在读取版本…</p>
    <p v-else-if="page && !page.items.length">此状态暂无版本。</p>
    <details v-for="item in page?.items ?? []" :key="item.version_id" class="review">
      <summary>{{ item.document.title }} · v{{ item.version }}</summary>
      <p>
        {{ skillPurposes[item.document.purpose] }} · {{ item.author }} ·
        {{ displayTime(item.submitted_at) }}
      </p>
      <SkillDeclaration :declaration="item.document" />
      <p v-if="item.review_note">审核说明 · {{ item.review_note }}</p>
      <p v-if="item.archived_at">作者归档于 {{ displayTime(item.archived_at) }}</p>
      <p v-if="item.disabled_at">停用于 {{ displayTime(item.disabled_at) }}</p>
      <p v-if="item.disable_reason">停用原因 · {{ item.disable_reason }}</p>
      <div v-if="item.state === 'published' || item.state === 'archived'" class="stack">
        <label
          >停用原因
          <textarea v-model="notes[item.version_id]" maxlength="500" rows="2" :disabled="saving" />
        </label>
        <p class="small muted">停用会将该版本移出技能目录，并使该版本的学习运行失去授权。</p>
        <div>
          <el-button
            :disabled="saving || !notes[item.version_id]?.trim()"
            @click="decide(item, 'disable')"
            >停用此版本</el-button
          >
        </div>
      </div>
      <div v-if="item.state === 'pending'" class="stack">
        <label
          >审核说明（驳回必填）
          <textarea v-model="notes[item.version_id]" maxlength="500" rows="2" :disabled="saving" />
        </label>
        <div>
          <el-button type="primary" :disabled="saving" @click="decide(item, 'publish')"
            >批准发布</el-button
          >
          <el-button
            :disabled="saving || !notes[item.version_id]?.trim()"
            @click="decide(item, 'reject')"
            >驳回</el-button
          >
        </div>
      </div>
    </details>
    <p v-if="page?.truncated">显示最早送审的 50 个待审版本，或其他状态最近的 50 个版本。</p>
  </PanelCard>
</template>

<style scoped>
.review {
  padding: 0.8rem 0;
  border-bottom: 1px solid var(--line);
  overflow-wrap: anywhere;
}
.review summary {
  cursor: pointer;
  font-weight: 650;
}
.review label {
  display: grid;
  gap: 0.4rem;
}
</style>
