<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { csrfHeaders, errorText, unwrap } from '../api/transport'

type Summary = components['schemas']['SkillVersionRating']
const props = defineProps<{ versionId: string }>()
const path = '/api/v1/learning/agent/skill-library/{version_id}/rating'
const score = ref<number | ''>('')
const summary = ref<Summary | null>(null)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const controller = new AbortController()
const { signal } = controller

async function request(action: 'load' | 'save' | 'remove') {
  if (busy.value || (action === 'save' && score.value === '')) return
  const params = { path: { version_id: props.versionId } }
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    let data: Summary
    if (action === 'save' && score.value !== '') {
      data = unwrap(
        await api.PUT(path, {
          body: { score: score.value },
          headers: csrfHeaders(),
          params,
          signal,
        }),
      )
    } else if (action === 'remove') {
      data = unwrap(await api.DELETE(path, { headers: csrfHeaders(), params, signal }))
    } else {
      data = unwrap(await api.GET(path, { params, signal }))
    }
    if (signal.aborted) return
    summary.value = data
    score.value = data.own_score ?? ''
    if (action === 'save') notice.value = '评分已保存。'
    if (action === 'remove') notice.value = '评分已撤回。'
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) busy.value = false
  }
}
onMounted(() => void request('load'))
onUnmounted(() => controller.abort())
</script>

<template>
  <section class="rating" aria-label="版本评价">
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="summary">
      <p v-if="summary.count">
        平均 {{ summary.average?.toFixed(1) }} / 5 · {{ summary.count }} 人评分
      </p>
      <p v-else>该版本暂无评分。</p>
      <form class="native-form" @submit.prevent="request('save')">
        <label
          >我的评分
          <select v-model="score" :disabled="busy">
            <option value="" disabled>选 1–5 分</option>
            <option v-for="points in 5" :key="points" :value="points">{{ points }} 分</option>
          </select>
        </label>
        <el-button
          native-type="submit"
          :disabled="busy || score === '' || score === summary.own_score"
        >
          保存评分
        </el-button>
        <el-button v-if="summary.own_score" :disabled="busy" @click="request('remove')"
          >撤回评分</el-button
        >
      </form>
    </template>
    <p v-else-if="busy" role="status">正在读取评分…</p>
    <el-button v-else @click="request('load')">重新读取评分</el-button>
  </section>
</template>

<style scoped>
.rating {
  margin-top: 0.85rem;
  padding-top: 0.7rem;
  border-top: 1px solid var(--line);
}
.rating p {
  margin: 0 0 0.65rem;
}
.rating form,
.rating label {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.55rem;
}
</style>
