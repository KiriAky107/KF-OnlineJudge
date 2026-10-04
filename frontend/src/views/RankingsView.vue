<script setup lang="ts">
import { computed, ref } from 'vue'
import { rankings } from '../data/preview'
import PanelCard from '../components/PanelCard.vue'
import PageHeading from '../components/PageHeading.vue'
const search = ref('')
const order = ref('solved')
const items = computed(() =>
  [...rankings]
    .sort((a, b) => (order.value === 'solved' ? b.solved - a.solved : b.rating - a.rating))
    .map((user, index) => ({ ...user, rank: index + 1 }))
    .filter((u) => u.username.toLowerCase().includes(search.value.trim().toLowerCase())),
)
</script>
<template>
  <PageHeading title="总排名" eyebrow="RANKINGS" description="保持练习，让每一道题都成为积累。" />
  <div class="summary-grid">
    <div v-for="(user, index) in rankings.slice(0, 3)" :key="user.username" class="summary-card">
      <p>NO. {{ index + 1 }} · 示例过题榜</p>
      <strong style="font-size: 23px"
        ><RouterLink :to="'/users/' + user.username">{{ user.username }}</RouterLink></strong
      ><small>{{ user.solved }} 道已通过 · {{ user.motto }}</small>
    </div>
    <div class="summary-card">
      <p>榜单规则</p>
      <strong style="font-size: 21px">持续进步</strong><small>默认按照通过题目数排序</small>
    </div>
  </div>
  <PanelCard title="排行榜" icon="rankings" subtitle="示例排名"
    ><div class="filters">
      <el-input
        v-model="search"
        placeholder="搜索用户"
        aria-label="搜索排名用户"
        clearable
      /><el-radio-group v-model="order"
        ><el-radio-button value="solved">过题数</el-radio-button
        ><el-radio-button value="rating">竞赛积分</el-radio-button></el-radio-group
      >
    </div>
    <el-table :data="items" stripe empty-text="没有找到用户"
      ><el-table-column label="排名" width="80" align="center"
        ><template #default="{ row }"
          ><span class="number-medal" :class="'top-' + row.rank">{{ row.rank }}</span></template
        ></el-table-column
      ><el-table-column label="用户" min-width="170"
        ><template #default="{ row }"
          ><RouterLink :to="'/users/' + row.username" class="user-inline table-link"
            ><span class="avatar" :style="{ background: row.color }">{{ row.username[0] }}</span
            >{{ row.username }}</RouterLink
          ></template
        ></el-table-column
      ><el-table-column label="个人签名" prop="motto" min-width="200" /><el-table-column
        label="通过题目"
        prop="solved"
        width="120"
        align="right" /><el-table-column
        label="总提交"
        prop="submissions"
        width="110"
        align="right" /><el-table-column
        label="竞赛积分"
        prop="rating"
        width="110"
        align="right" /></el-table
  ></PanelCard>
</template>
