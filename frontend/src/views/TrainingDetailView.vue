<script setup lang="ts">
import { vLoading } from 'element-plus'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import type { components } from '../api/generated/schema'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'
import PanelCard from '../components/PanelCard.vue'
import PageHeading from '../components/PageHeading.vue'
const route = useRoute()
const { user, refresh } = useSession()
const training = ref<components['schemas']['TrainingDetail'] | null>(null)
const notes = ref<Record<string, string>>({})
const error = ref('')
const notice = ref('')
const loading = ref(false)
const saving = ref<string | null>(null)
const progressLabels = { not_started: '尚未开始', attempted: '已尝试', completed: '已完成（AC）' }
let controller = new AbortController()
let mutation: AbortController | undefined
function clear() {
  controller.abort()
  mutation?.abort()
  controller = new AbortController()
  training.value = null
  notes.value = {}
  error.value = ''
  notice.value = ''
  loading.value = false
  saving.value = null
}
async function load() {
  clear()
  if (!user.value) return
  const { signal } = controller
  loading.value = true
  try {
    const result = unwrap(
      await api.GET('/api/v1/trainings/{training_id}', {
        params: { path: { training_id: String(route.params.id) } },
        signal,
      }),
    )
    if (!signal.aborted) {
      training.value = result
      notes.value = Object.fromEntries(result.items.map((item) => [item.id, item.note]))
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
async function saveNote(itemId: string) {
  if (saving.value || !training.value) return
  mutation = new AbortController()
  const { signal } = mutation
  saving.value = itemId
  error.value = ''
  notice.value = ''
  try {
    const note = notes.value[itemId] ?? ''
    const result = unwrap(
      await api.PATCH('/api/v1/trainings/{training_id}/items/{item_id}', {
        params: { path: { training_id: training.value.id, item_id: itemId } },
        body: { note },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (!signal.aborted) {
      training.value = result
      notice.value = '备注已保存。'
    }
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) saving.value = null
  }
}
watch([() => user.value?.id, () => route.params.id], load, { immediate: true })
onMounted(async () => {
  if (!user.value) {
    try {
      await refresh()
    } catch (cause) {
      error.value = errorText(cause)
    }
  }
})
onBeforeUnmount(clear)
</script>
<template>
  <div class="breadcrumbs">
    <RouterLink to="/trainings">训练计划</RouterLink><span>/</span><span>个人计划</span>
  </div>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <p v-if="!user">
    <RouterLink :to="{ path: '/login', query: { next: route.fullPath } }">登录</RouterLink
    >后查看本人计划。
  </p>
  <template v-else>
    <PageHeading
      :title="training?.title ?? '训练计划'"
      description="计划固定生成时的题目版本，进度来自本人的真实提交。"
      ><el-button :disabled="loading || !!saving" @click="load">刷新进度</el-button></PageHeading
    >
    <div v-if="training" v-loading="loading" class="content-with-aside">
      <PanelCard title="训练题目" icon="problems" subtitle="按顺序练习">
        <article v-for="item in training.items" :key="item.id" class="panel-body">
          <h3>
            {{ item.position + 1 }}.
            <RouterLink v-if="item.available" :to="'/problems/' + item.problem_version_id">{{
              item.title
            }}</RouterLink
            ><span v-else>{{ item.title }}</span>
          </h3>
          <div class="tag-list">
            <span class="tag">{{ progressLabels[item.progress] }}</span
            ><span v-if="!item.available" class="tag">当前不可用</span
            ><span v-if="!item.is_current_version" class="tag">历史版本，不自动替换</span>
          </div>
          <p>{{ item.reasons.join('；') }}</p>
          <p class="small muted">
            生成时标签：{{ item.tags.join(' / ') || '未标注' }}；难度：{{
              item.difficulty || '未标注'
            }}；元数据修订：{{ item.metadata_revision ?? '无' }}
          </p>
          <ul v-if="item.progress_evidence.length">
            <li v-for="record in item.progress_evidence" :key="record.submission_id">
              <RouterLink :to="'/submissions/' + record.submission_id"
                >{{ record.verdict || record.state }} ·
                {{ displayTime(record.submitted_at) }}</RouterLink
              >
            </li>
          </ul>
          <details v-if="item.evidence.length">
            <summary>推荐依据的提交</summary>
            <ul>
              <li v-for="record in item.evidence" :key="record.submission_id">
                <RouterLink :to="'/submissions/' + record.submission_id"
                  >{{ record.verdict || record.state }} ·
                  {{ displayTime(record.submitted_at) }}</RouterLink
                >
              </li>
            </ul>
          </details>
          <el-input
            v-model="notes[item.id]"
            type="textarea"
            :rows="2"
            maxlength="1000"
            show-word-limit
            :aria-label="item.title + '学习备注'"
            :disabled="!!saving"
            placeholder="可选：记录一句学习备注（不改变判题结果）"
          />
          <el-button
            :loading="saving === item.id"
            :disabled="!!saving || notes[item.id] === item.note"
            @click="saveNote(item.id)"
            >保存备注</el-button
          >
        </article>
        <el-empty v-if="!training.items.length" description="此计划没有题目" />
      </PanelCard>
      <div class="stack">
        <PanelCard title="计划信息" icon="trainings"
          ><dl class="details-list">
            <div>
              <dt>创建时间</dt>
              <dd>{{ displayTime(training.created_at) }}</dd>
            </div>
            <div>
              <dt>完成进度</dt>
              <dd>{{ training.completed_count }} / {{ training.item_count }}</dd>
            </div>
            <div>
              <dt>目标知识点</dt>
              <dd>{{ training.parameters.tags?.join(' / ') || '不限' }}</dd>
            </div>
            <div>
              <dt>规则版本</dt>
              <dd>{{ training.rule_version }}</dd>
            </div>
          </dl>
          <p class="small muted">
            只统计计划创建后，本人对固定版本的提交。任一次 AC 即完成，之后非 AC
            不使进度回退；系统失败与等待中不算完成。创建前的 AC 不计入本计划进度。
          </p></PanelCard
        >
      </div>
    </div>
    <p v-else-if="!loading && !error">暂无计划数据。</p>
  </template>
</template>
