<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { csrfHeaders, errorText, unwrap } from '../api/transport'

const event = ref({ key: '', title: '', starts: '', ends: '' })
const scheduled = ref<components['schemas']['ContestSchedule']>()
const scheduleBusy = ref(false)
const scheduleError = ref('')
const policyKey = ref('')
const loadedPolicyKey = ref('')
const policyEnabled = ref(true)
const policyVersions = ref('')
const policyBusy = ref(false)
const policyError = ref('')
const policySaved = ref(false)
const key = ref('')
const title = ref('')
const ended = ref('')
const standings = ref('')
const result = ref<components['schemas']['ContestSettlement']>()
const error = ref('')
const busy = ref(false)
const controller = new AbortController()
onBeforeUnmount(() => controller.abort())

async function schedule() {
  if (scheduleBusy.value) return
  scheduleBusy.value = true
  scheduleError.value = ''
  scheduled.value = undefined
  try {
    scheduled.value = unwrap(
      await api.POST('/api/v1/admin/contest-schedules', {
        body: {
          key: event.value.key.trim(),
          title: event.value.title.trim(),
          starts_at: new Date(event.value.starts).toISOString(),
          ends_at: new Date(event.value.ends).toISOString(),
          semantic_completion_enabled: true,
        },
        headers: csrfHeaders(),
        signal: controller.signal,
      }),
    )
    policyKey.value = scheduled.value.key
    await loadEditorPolicy()
  } catch (cause) {
    if (!controller.signal.aborted) scheduleError.value = errorText(cause)
  } finally {
    scheduleBusy.value = false
  }
}

async function loadEditorPolicy() {
  const key = policyKey.value.trim()
  if (!key || policyBusy.value) return
  policyBusy.value = true
  policyError.value = ''
  policySaved.value = false
  loadedPolicyKey.value = ''
  try {
    const saved = unwrap(
      await api.GET('/api/v1/admin/contest-schedules/{key}/editor-policy', {
        params: { path: { key } },
        signal: controller.signal,
      }),
    )
    policyEnabled.value = saved.semantic_completion_enabled
    policyVersions.value = saved.version_ids.join('\n')
    loadedPolicyKey.value = key
  } catch (cause) {
    if (!controller.signal.aborted) policyError.value = errorText(cause)
  } finally {
    policyBusy.value = false
  }
}

async function saveEditorPolicy() {
  const key = policyKey.value.trim()
  if (!key || key !== loadedPolicyKey.value || policyBusy.value) return
  policyBusy.value = true
  policySaved.value = false
  policyError.value = ''
  try {
    unwrap(
      await api.PUT('/api/v1/admin/contest-schedules/{key}/editor-policy', {
        params: { path: { key } },
        headers: csrfHeaders(),
        signal: controller.signal,
        body: {
          semantic_completion_enabled: policyEnabled.value,
          version_ids: policyVersions.value.split(/\s+/).filter(Boolean),
        },
      }),
    )
    policySaved.value = true
  } catch (cause) {
    if (!controller.signal.aborted) policyError.value = errorText(cause)
  } finally {
    policyBusy.value = false
  }
}

async function settle() {
  if (busy.value) return
  error.value = ''
  result.value = undefined
  busy.value = true
  try {
    const rows = standings.value
      .trim()
      .split(/\r?\n/)
      .filter((line) => line.trim())
    const entrants = rows.map((line) => {
      const match = /^\s*([a-z0-9_]{3,24})\s*[,，\t ]\s*([1-9]\d{0,2})\s*$/.exec(line)
      if (!match) throw new Error('每行填写本站用户名和最终名次，例如 team_member, 1。')
      return { username: match[1]!, rank: Number(match[2]) }
    })
    result.value = unwrap(
      await api.POST('/api/v1/admin/contest-settlements', {
        body: {
          key: key.value.trim(),
          title: title.value.trim(),
          ended_at: new Date(ended.value).toISOString(),
          standings: entrants,
        },
        headers: csrfHeaders(),
        signal: controller.signal,
      }),
    )
  } catch (cause) {
    if (!controller.signal.aborted) error.value = errorText(cause)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="admin-management">
    <header class="admin-page-heading"><h1>比赛管理</h1></header>
    <form class="admin-real-panel native-form stack" @submit.prevent="schedule">
      <h2>发布比赛时间</h2>
      <label
        >赛事编号
        <input v-model="event.key" required maxlength="80" pattern="[a-z0-9][a-z0-9_-]{0,79}"
      /></label>
      <label>比赛名称 <input v-model="event.title" required maxlength="120" /></label>
      <label
        >开始时间（设备时区）<input v-model="event.starts" type="datetime-local" required
      /></label>
      <label
        >结束时间（设备时区）<input v-model="event.ends" type="datetime-local" required
      /></label>
      <div>
        <el-button type="primary" native-type="submit" :loading="scheduleBusy">发布赛程</el-button>
      </div>
      <p v-if="scheduleError" role="alert">{{ scheduleError }}</p>
      <p v-if="scheduled" role="status">「{{ scheduled.title }}」已发布，可确认参赛。</p>
    </form>
    <form class="admin-real-panel native-form stack" @submit.prevent="saveEditorPolicy">
      <h2>比赛题目与补全</h2>
      <label>赛事编号 <input v-model="policyKey" required maxlength="80" /></label>
      <div><el-button :loading="policyBusy" @click="loadEditorPolicy">读取设置</el-button></div>
      <template v-if="loadedPolicyKey && loadedPolicyKey === policyKey.trim()">
        <label>
          比赛题目版本 ID（每行一个，题目页地址末尾）
          <textarea v-model="policyVersions" rows="4" maxlength="900" />
        </label>
        <div>
          <label class="contest-completion-toggle">
            <input v-model="policyEnabled" type="checkbox" /> 允许语义补全
          </label>
          <p class="muted">关闭后，参赛者在比赛题目中仍可使用基础补全。开赛后题目不再变更。</p>
        </div>
        <div>
          <el-button type="primary" native-type="submit" :loading="policyBusy">保存设置</el-button>
        </div>
      </template>
      <p v-if="policyError" role="alert">{{ policyError }}</p>
      <p v-if="policySaved && loadedPolicyKey === policyKey.trim()" role="status">设置已保存。</p>
    </form>
    <form class="admin-real-panel native-form stack" @submit.prevent="settle">
      <h2>比赛评分结算</h2>
      <p>录入本站已结束正式计分赛的最终名次。并列名次使用 1、2、2、4 的格式。</p>
      <label
        >赛事编号 <input v-model="key" required maxlength="80" pattern="[a-z0-9][a-z0-9_-]{0,79}"
      /></label>
      <label>比赛名称 <input v-model="title" required maxlength="120" /></label>
      <label>结束时间（设备时区）<input v-model="ended" type="datetime-local" required /></label>
      <label
        >最终名次（每行「用户名, 名次」）<textarea
          v-model="standings"
          required
          rows="7"
          maxlength="20000"
        />
      </label>
      <div>
        <el-button type="primary" native-type="submit" :loading="busy">结算比赛评分</el-button>
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="result" role="status">
        「{{ result.title }}」已结算 {{ result.participants }} 位选手。
      </p>
    </form>
  </div>
</template>

<style scoped>
form label {
  display: grid;
  gap: 0.4rem;
  max-width: 40rem;
}
.contest-completion-toggle {
  display: flex;
  align-items: center;
}
</style>
