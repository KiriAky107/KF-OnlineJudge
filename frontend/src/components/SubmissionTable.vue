<script setup lang="ts">
import { findProblem, type submissions } from '../data/preview'
import VerdictTag from './VerdictTag.vue'
defineProps<{ items: typeof submissions; compact?: boolean }>()
</script>
<template>
  <el-table :data="items" stripe empty-text="暂无符合条件的评测记录"
    ><el-table-column label="提交编号" width="100"
      ><template #default="{ row }"
        ><RouterLink class="table-link mono" :to="'/submissions/' + row.id"
          >#{{ row.id }}</RouterLink
        ></template
      ></el-table-column
    ><el-table-column label="题目" min-width="180"
      ><template #default="{ row }"
        ><RouterLink class="table-link" :to="'/problems/' + row.problemId">{{
          findProblem(row.problemId)?.title
        }}</RouterLink></template
      ></el-table-column
    ><el-table-column v-if="!compact" label="用户" min-width="120"
      ><template #default="{ row }"
        ><RouterLink :to="'/users/' + row.username">{{ row.username }}</RouterLink></template
      ></el-table-column
    ><el-table-column label="结果" width="110"
      ><template #default="{ row }"><VerdictTag :value="row.verdict" /></template></el-table-column
    ><el-table-column label="语言" prop="language" width="90" /><el-table-column
      v-if="!compact"
      label="用时"
      width="90"
      align="right"
      ><template #default="{ row }">{{
        row.time === null ? '—' : row.time + ' ms'
      }}</template></el-table-column
    ><el-table-column v-if="!compact" label="内存" width="100" align="right"
      ><template #default="{ row }">{{
        row.memory === null ? '—' : row.memory + ' KiB'
      }}</template></el-table-column
    ><el-table-column label="提交时间" prop="created" min-width="175"
  /></el-table>
</template>
