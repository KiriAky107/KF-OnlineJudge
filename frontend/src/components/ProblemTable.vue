<script setup lang="ts">
import { acceptance, type Problem } from '../data/preview'
import DifficultyTag from './DifficultyTag.vue'
defineProps<{ items: Problem[]; compact?: boolean }>()
</script>
<template>
  <el-table :data="items" stripe style="width: 100%" empty-text="没有符合条件的题目">
    <el-table-column prop="id" label="题号" width="90"
      ><template #default="{ row }"
        ><span class="mono muted">{{ row.id }}</span></template
      ></el-table-column
    >
    <el-table-column label="题目" min-width="190"
      ><template #default="{ row }"
        ><RouterLink class="table-link" :to="`/problems/${row.id}`">{{
          row.title
        }}</RouterLink></template
      ></el-table-column
    >
    <el-table-column v-if="!compact" label="标签" min-width="180"
      ><template #default="{ row }"
        ><div class="tag-list">
          <span v-for="tag in row.tags" :key="tag" class="tag">{{ tag }}</span>
        </div></template
      ></el-table-column
    >
    <el-table-column label="难度" width="90"
      ><template #default="{ row }"><DifficultyTag :value="row.difficulty" /></template
    ></el-table-column>
    <el-table-column label="通过率" width="100" align="right"
      ><template #default="{ row }"
        ><span class="muted tabular"
          >{{ acceptance({ accepted: row.accepted, submissions: row.submissions }) }}%</span
        ></template
      ></el-table-column
    >
    <el-table-column v-if="!compact" prop="updated" label="更新日期" width="125" />
  </el-table>
</template>
