<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { errorText, unwrap } from '../api/transport'
import type { components } from '../api/generated/schema'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
const items = ref<components['schemas']['ProblemSummary'][]>([])
const query = ref('')
const error = ref('')
const busy = ref(false)
const next = ref<string | null>(null)
const current = ref<string | undefined>()
const previous = ref<(string | undefined)[]>([])
let request: AbortController | undefined
let generation = 0
const filtered = computed(() =>
  items.value.filter((p) =>
    (p.slug + p.title).toLowerCase().includes(query.value.trim().toLowerCase()),
  ),
)
async function load(after?: string) {
  request?.abort()
  const epoch = ++generation
  const controller = new AbortController()
  request = controller
  error.value = ''
  busy.value = true
  try {
    const page = unwrap(
      await api.GET('/api/v1/problems', {
        params: { query: { after, limit: 20 } },
        signal: controller.signal,
      }),
    )
    if (epoch !== generation) return
    items.value = page.items
    next.value = page.next_after
    current.value = after
  } catch (cause) {
    if (epoch === generation) {
      error.value = errorText(cause)
      items.value = []
      next.value = null
    }
  } finally {
    if (epoch === generation) busy.value = false
  }
}
function forward() {
  if (!next.value) return
  previous.value.push(current.value)
  void load(next.value)
}
function back() {
  void load(previous.value.pop())
}
onMounted(() => load())
onBeforeUnmount(() => {
  generation++
  request?.abort()
})
</script>
<template>
  <PageHeading
    title="题库"
    eyebrow="PROBLEM BANK"
    description="选择已发布的题目，使用支持的语言提交。"
  />
  <PanelCard title="已发布题目" icon="problems">
    <p v-if="error" role="alert">{{ error }} <el-button @click="load(current)">重试</el-button></p>
    <div class="filters">
      <el-input
        v-model="query"
        placeholder="筛选当前页题号或标题"
        aria-label="搜索题目"
        clearable
      />
    </div>
    <el-table :data="filtered" :empty-text="busy ? '正在加载…' : '当前页没有题目'">
      <el-table-column prop="slug" label="题号" min-width="160" />
      <el-table-column label="题目" min-width="220"
        ><template #default="{ row }"
          ><RouterLink :to="'/problems/' + row.version_id" class="text-link">{{
            row.title
          }}</RouterLink></template
        ></el-table-column
      >
      <el-table-column label="支持语言"
        ><template #default="{ row }">{{ row.languages.join(' / ') }}</template></el-table-column
      >
    </el-table>
    <div class="pagination-row">
      <span>当前页 {{ items.length }} 道题目</span
      ><el-button :disabled="busy || !previous.length" @click="back">上一页</el-button
      ><el-button :disabled="busy || !next" @click="forward">下一页</el-button>
    </div>
  </PanelCard>
</template>
