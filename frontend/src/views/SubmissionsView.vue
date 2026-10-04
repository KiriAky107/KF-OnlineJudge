<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { submissions, verdictLabels, type Verdict } from '../data/preview'
import { usePagination } from '../composables/usePagination'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import SubmissionTable from '../components/SubmissionTable.vue'
const route = useRoute()
const username = ref(typeof route.query.user === 'string' ? route.query.user : '')
const problemId = ref('')
const language = ref('')
const verdict = ref<Verdict | ''>('')
const filtered = computed(() =>
  submissions.filter(
    (s) =>
      (!username.value || s.username.toLowerCase().includes(username.value.trim().toLowerCase())) &&
      (!problemId.value || s.problemId.includes(problemId.value.trim())) &&
      (!language.value || s.language === language.value) &&
      (!verdict.value || s.verdict === verdict.value),
  ),
)
const { page, pageSize, paged, total } = usePagination(filtered)
function reset() {
  username.value = ''
  problemId.value = ''
  language.value = ''
  verdict.value = ''
}
</script>
<template>
  <PageHeading
    title="评测记录"
    eyebrow="SUBMISSIONS"
    description="查看提交状态、运行情况与评测结果。"
  /><PanelCard title="提交列表" icon="submissions" subtitle="示例评测记录"
    ><div class="filters">
      <el-input
        v-model="username"
        placeholder="用户名"
        aria-label="筛选用户名"
        clearable
        style="width: 180px"
      /><el-input
        v-model="problemId"
        placeholder="题号"
        aria-label="筛选题号"
        clearable
        style="width: 150px"
      /><el-select v-model="verdict" aria-label="评测结果" placeholder="全部结果" clearable
        ><el-option
          v-for="(label, value) in verdictLabels"
          :key="value"
          :label="label"
          :value="value" /></el-select
      ><el-select v-model="language" aria-label="语言" placeholder="全部语言" clearable
        ><el-option
          v-for="l in ['C++', 'Python', 'Rust']"
          :key="l"
          :label="l"
          :value="l" /></el-select
      ><el-button @click="reset">重置</el-button>
    </div>
    <SubmissionTable :items="paged" />
    <div class="pagination-row">
      <span>{{ total }} 条记录</span
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[5, 10, 20]"
        :total="total"
        layout="prev, pager, next, sizes"
      /></div
  ></PanelCard>
</template>
