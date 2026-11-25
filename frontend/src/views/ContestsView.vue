<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'
import PanelCard from '../components/PanelCard.vue'
import PageHeading from '../components/PageHeading.vue'
import { displayTime } from '../lib/presentation'

type Contest = components['schemas']['ContestSchedule']
const contests = ref<Contest[]>([])
const status = ref('全部')
const query = ref('')
const error = ref('')
const loading = ref(true)
const controller = new AbortController()
const now = Date.now()
function contestStatus(contest: Contest) {
  if (Date.parse(contest.ends_at) <= now) return '已结束'
  return Date.parse(contest.starts_at) <= now ? '进行中' : '即将开始'
}
const items = computed(() =>
  contests.value
    .map((contest) => ({ ...contest, status: contestStatus(contest) }))
    .filter(
      (contest) =>
        (status.value === '全部' || contest.status === status.value) &&
        contest.title.includes(query.value.trim()),
    ),
)

onMounted(async () => {
  try {
    contests.value = unwrap(await api.GET('/api/v1/contests', { signal: controller.signal }))
  } catch (cause) {
    if (!controller.signal.aborted) error.value = errorText(cause)
  } finally {
    loading.value = false
  }
})
onBeforeUnmount(() => controller.abort())
</script>

<template>
  <PageHeading title="比赛" />
  <PanelCard title="比赛列表" icon="contests">
    <div class="filters">
      <el-radio-group v-model="status">
        <el-radio-button
          v-for="item in ['全部', '即将开始', '进行中', '已结束']"
          :key="item"
          :value="item"
        >
          {{ item }}
        </el-radio-button>
      </el-radio-group>
      <el-input v-model="query" placeholder="搜索比赛" aria-label="搜索比赛" clearable />
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="loading" role="status">正在读取赛程…</p>
    <el-table v-else :data="items" stripe empty-text="没有符合条件的比赛">
      <el-table-column label="比赛名称" min-width="230">
        <template #default="{ row }">
          <RouterLink class="table-link" :to="'/contests/' + row.key">{{ row.title }}</RouterLink>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="112" prop="status" />
      <el-table-column label="开始时间" min-width="172">
        <template #default="{ row }">{{ displayTime(row.starts_at) }}</template>
      </el-table-column>
      <el-table-column label="结束时间" min-width="172">
        <template #default="{ row }">{{ displayTime(row.ends_at) }}</template>
      </el-table-column>
    </el-table>
  </PanelCard>
</template>
