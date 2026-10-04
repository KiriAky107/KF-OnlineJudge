<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { problems, problemTags } from '../data/preview'
import { usePagination } from '../composables/usePagination'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import ProblemTable from '../components/ProblemTable.vue'
const route = useRoute()
const query = ref('')
const difficulty = ref('')
const tag = ref(typeof route.query.tag === 'string' ? route.query.tag : '')
const filtered = computed(() =>
  problems.filter(
    (p) =>
      (!query.value || (p.id + p.title).toLowerCase().includes(query.value.trim().toLowerCase())) &&
      (!difficulty.value || p.difficulty === difficulty.value) &&
      (!tag.value || p.tags.includes(tag.value)),
  ),
)
const { page, pageSize, paged, total } = usePagination(filtered)
function reset() {
  query.value = ''
  difficulty.value = ''
  tag.value = ''
}
</script>
<template>
  <PageHeading title="题库" eyebrow="PROBLEM BANK" description="挑选一道题，开始今天的练习。"
    ><RouterLink to="/trainings"><el-button>按题单练习</el-button></RouterLink></PageHeading
  >
  <PanelCard title="全部题目" icon="problems" subtitle="示例题库">
    <div class="filters">
      <el-input
        v-model="query"
        placeholder="搜索题号或题目名称"
        aria-label="搜索题目"
        clearable
      /><el-select v-model="difficulty" placeholder="全部难度" aria-label="难度" clearable
        ><el-option
          v-for="d in ['入门', '简单', '中等', '困难']"
          :key="d"
          :label="d"
          :value="d" /></el-select
      ><el-select v-model="tag" placeholder="全部标签" aria-label="题目标签" clearable filterable
        ><el-option v-for="t in problemTags" :key="t" :label="t" :value="t" /></el-select
      ><el-button @click="reset">重置</el-button
      ><span class="filter-label">{{ total }} 道题目</span>
    </div>
    <ProblemTable :items="paged" />
    <div class="pagination-row">
      <span class="muted">共 {{ total }} 道题目</span
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[5, 10, 20]"
        :total="total"
        layout="prev, pager, next, sizes"
      />
    </div>
  </PanelCard>
</template>
