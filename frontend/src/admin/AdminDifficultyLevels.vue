<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import DifficultyTag from '../components/DifficultyTag.vue'

type Level = components['schemas']['DifficultyLevel']
const emptyLevel = (): Level => ({
  key: '',
  label: '',
  score: 1000,
  color: '#2d8cf0',
  tier: 'medium',
})
const draft = ref<Level>(emptyLevel())
const levels = ref<Level[]>([])
const error = ref('')
const notice = ref('')
const busy = ref(false)
const existing = ref(false)
const tiers = { easy: '简单', medium: '中等', hard: '困难' }
const controller = new AbortController()
function edit(level?: Level) {
  existing.value = !!level
  draft.value = level ? { ...level } : emptyLevel()
  notice.value = error.value = ''
}
async function load() {
  try {
    levels.value = unwrap(
      await api.GET('/api/v1/problems/difficulty-levels', { signal: controller.signal }),
    )
  } catch (cause) {
    if (!controller.signal.aborted) error.value = errorText(cause)
  }
}
async function save() {
  if (busy.value) return
  busy.value = true
  error.value = notice.value = ''
  try {
    const { key, ...body } = draft.value
    const stored = unwrap(
      await api.PUT('/api/v1/admin/problem-difficulty-levels/{key}', {
        params: { path: { key } },
        headers: csrfHeaders(),
        body,
        signal: controller.signal,
      }),
    )
    edit(stored)
    notice.value = '难度档位已保存。'
    await load()
  } catch (cause) {
    if (!controller.signal.aborted) error.value = errorText(cause)
  } finally {
    busy.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => controller.abort())
</script>

<template>
  <div class="admin-management">
    <header class="admin-page-heading"><h1>题目难度档位</h1></header>
    <section class="admin-real-panel stack">
      <div v-for="level in levels" :key="level.key" class="level-row">
        <DifficultyTag :level="level" :value="level.label" />
        <span>{{ level.key }} · {{ tiers[level.tier] }}</span>
        <el-button size="small" :disabled="busy" @click="edit(level)">编辑</el-button>
      </div>
      <div><el-button :disabled="busy" @click="edit()">添加档位</el-button></div>
    </section>
    <form class="admin-real-panel native-form stack" @submit.prevent="save">
      <label
        >档位编号
        <input
          v-model="draft.key"
          :readonly="existing"
          required
          maxlength="32"
          pattern="[a-z0-9][a-z0-9_-]{0,31}"
      /></label>
      <label>显示名称 <input v-model="draft.label" required maxlength="24" /></label>
      <label
        >难度分 <input v-model.number="draft.score" type="number" required min="1" max="10000"
      /></label>
      <label>标签颜色 <input v-model="draft.color" type="color" /></label>
      <label
        >训练推荐分组
        <select v-model="draft.tier" :disabled="existing">
          <option v-for="(label, tier) in tiers" :key="tier" :value="tier">{{ label }}</option>
        </select>
      </label>
      <div><el-button type="primary" native-type="submit" :loading="busy">保存档位</el-button></div>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>
    </form>
  </div>
</template>

<style scoped>
form label {
  display: grid;
  gap: 0.4rem;
  max-width: 24rem;
}
.level-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
}
</style>
