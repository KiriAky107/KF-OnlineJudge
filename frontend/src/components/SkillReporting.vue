<script setup lang="ts">
import DisclosureHeading from './DisclosureHeading.vue'
import { computed, onUnmounted, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { skillReportReasons, skillReportStates, type SkillReport } from '../api/skillReports'
import { trainingIntent } from '../api/trainingIntent'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import { sha256Fingerprint } from '../lib/requestCrypto'

const props = defineProps<{ versionId: string }>()
const path = '/api/v1/learning/agent/skill-library/{version_id}/reports'
const { user } = useSession()
const page = ref<components['schemas']['SkillReportPage'] | null>(null)
const reason = ref<SkillReport['reason']>('incorrect_content')
const details = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const opened = ref(false)
const pending = computed(() => page.value?.items.some((item) => item.state === 'pending'))
const controller = new AbortController()
const { signal } = controller

async function perform(action: 'load' | 'submit') {
  const owner = user.value?.id
  if (!owner || busy.value) return
  const params = { path: { version_id: props.versionId } }
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    if (action === 'submit') {
      const value = { reason: reason.value, details: details.value.trim() }
      if (!value.details || pending.value) return
      const fingerprint = sha256Fingerprint(new TextEncoder().encode(JSON.stringify(value)))
      if (signal.aborted) return
      const intent = trainingIntent(
        sessionStorage,
        'skill-report:' + owner + ':' + props.versionId,
        fingerprint,
      )
      const report = unwrap(
        await api.POST(path, {
          body: { ...value, request_id: intent.key },
          headers: csrfHeaders(),
          params,
          signal,
        }),
      )
      if (signal.aborted) return
      intent.complete()
      const next = [
        report,
        ...(page.value?.items ?? []).filter((item) => item.report_id !== report.report_id),
      ]
      page.value = {
        items: next.slice(0, 20),
        truncated: !!page.value?.truncated || next.length > 20,
      }
      details.value = ''
      notice.value = '举报已提交。'
    } else {
      const result = unwrap(await api.GET(path, { params, signal }))
      if (!signal.aborted) page.value = result
    }
  } catch (cause) {
    if (signal.aborted) return
    error.value =
      cause instanceof ApiError && cause.status === 409
        ? '该版本已有待处理举报，请重新读取。'
        : errorText(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}
function read(event: Event) {
  if (!(event.currentTarget as HTMLDetailsElement).open || opened.value) return
  opened.value = true
  void perform('load')
}
onUnmounted(() => controller.abort())
</script>

<template>
  <details class="reporting native-form" @toggle="read">
    <DisclosureHeading icon="bell" tone="warning">举报此版本</DisclosureHeading>
    <p v-if="error" role="alert">{{ error }}</p>
    <el-button v-if="error && page && !pending" :disabled="busy" @click="perform('load')"
      >重新读取</el-button
    >
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="busy && !page" role="status">正在读取举报…</p>
    <el-button v-else-if="!page" @click="perform('load')">重新读取</el-button>
    <template v-if="page">
      <div v-if="pending">
        <p class="small muted">该版本已有你提交的待处理举报。</p>
        <el-button :disabled="busy" @click="perform('load')">更新处理状态</el-button>
      </div>
      <form v-else class="stack" @submit.prevent="perform('submit')">
        <label>
          举报原因
          <select v-model="reason" :disabled="busy">
            <option v-for="(label, key) in skillReportReasons" :key="key" :value="key">
              {{ label }}
            </option>
          </select>
        </label>
        <label class="explanation">
          说明
          <textarea v-model="details" required maxlength="500" rows="3" :disabled="busy" />
        </label>
        <div>
          <el-button native-type="submit" :disabled="busy || !details.trim()">提交举报</el-button>
        </div>
      </form>
      <div v-for="report in page.items" :key="report.report_id" class="report-history">
        <p>
          <strong>{{ skillReportStates[report.state] }}</strong> ·
          {{ skillReportReasons[report.reason] }} · {{ displayTime(report.submitted_at) }}
        </p>
        <p>{{ report.details }}</p>
        <p v-if="report.decision_note">处理说明 · {{ report.decision_note }}</p>
      </div>
      <p v-if="page.truncated" class="small muted">仅显示最近 20 条举报。</p>
    </template>
  </details>
</template>

<style scoped>
.reporting {
  border-top: 1px solid var(--line);
  margin-top: 0.75rem;
  padding-top: 0.75rem;
}
.reporting summary {
  cursor: pointer;
  margin-bottom: 0.65rem;
}
.explanation {
  display: grid;
  gap: 0.4rem;
}
.report-history {
  border-top: 1px solid var(--line);
  margin-top: 0.65rem;
  padding-top: 0.35rem;
  overflow-wrap: anywhere;
}
.report-history p {
  white-space: pre-wrap;
  margin: 0.4rem 0;
}
</style>
