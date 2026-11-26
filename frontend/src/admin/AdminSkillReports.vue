<script setup lang="ts">
import DisclosureHeading from '../components/DisclosureHeading.vue'
import { onUnmounted, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { skillReportReasons, skillReportStates, type SkillReport } from '../api/skillReports'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import PanelCard from '../components/PanelCard.vue'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import { hasMaintenance } from './intents'

type Review = components['schemas']['SkillReportReviewView']
type Action = components['schemas']['ReviewSkillReport']['action']

const { user } = useSession()
const state = ref<SkillReport['state']>('pending')
const page = ref<components['schemas']['SkillReportReviewPage'] | null>(null)
const notes = ref<Record<string, string>>({})
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const status = ref('')
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
      await api.GET('/api/v1/admin/skill-reports', {
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
async function decide(item: Review, action: Action) {
  const note = notes.value[item.report_id]?.trim()
  if (!controller || !hasMaintenance(user.value) || saving.value || !note) return
  const { signal } = controller
  error.value = ''
  status.value = ''
  saving.value = true
  try {
    const result = unwrap(
      await api.POST('/api/v1/admin/skill-reports/{report_id}', {
        params: { path: { report_id: item.report_id } },
        body: { action, note },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (signal.aborted) return
    status.value = result.state === 'confirmed' ? '已确认举报。' : '已记录未采纳。'
    await load()
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = false
  }
}
watch(
  [() => user.value?.id, () => hasMaintenance(user.value), state],
  () => {
    status.value = ''
    void load()
  },
  { immediate: true },
)
onUnmounted(() => controller?.abort())
</script>

<template>
  <header class="admin-page-heading"><h1>技能举报</h1></header>
  <PanelCard title="举报队列" class="native-form">
    <template #actions>
      <label
        >状态
        <select v-model="state" :disabled="saving">
          <option v-for="(label, value) in skillReportStates" :key="value" :value="value">
            {{ label }}
          </option>
        </select>
      </label>
      <el-button :disabled="saving" :loading="loading" @click="load">刷新队列</el-button>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="status" role="status">{{ status }}</p>
    <p v-if="loading" role="status">正在读取举报…</p>
    <p v-else-if="page && !page.items.length">此状态暂无举报。</p>
    <details v-for="item in page?.items ?? []" :key="item.report_id" class="report">
      <DisclosureHeading icon="bell" tone="warning">
        {{ item.release_title }} · v{{ item.version }} · {{ skillReportReasons[item.reason] }}
      </DisclosureHeading>
      <p>提交于 {{ displayTime(item.submitted_at) }}</p>
      <p class="report-text">{{ item.details }}</p>
      <p v-if="item.decision_note" class="report-text">处理说明 · {{ item.decision_note }}</p>
      <div v-if="item.state === 'pending'" class="stack">
        <label
          >处理说明
          <textarea v-model="notes[item.report_id]" :disabled="saving" rows="2" maxlength="500" />
        </label>
        <div>
          <el-button
            type="primary"
            :disabled="saving || !notes[item.report_id]?.trim()"
            @click="decide(item, 'confirm')"
            >确认举报</el-button
          >
          <el-button
            :disabled="saving || !notes[item.report_id]?.trim()"
            @click="decide(item, 'dismiss')"
            >不采纳</el-button
          >
        </div>
      </div>
    </details>
    <p v-if="page?.truncated">当前队列仅显示 50 条。</p>
  </PanelCard>
</template>

<style scoped>
.report {
  padding: 0.8rem 0;
  border-bottom: 1px solid var(--line);
  overflow-wrap: anywhere;
}
.report summary {
  cursor: pointer;
  font-weight: 650;
}
.report-text {
  white-space: pre-wrap;
}
.report label {
  display: grid;
  gap: 0.4rem;
}
</style>
