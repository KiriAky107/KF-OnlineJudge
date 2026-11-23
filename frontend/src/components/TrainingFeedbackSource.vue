<script setup lang="ts">
import type { components } from '../api/generated/schema'
defineProps<{ evidence: components['schemas']['TrainingFeedbackEvidence']; compact?: boolean }>()
const ratings = { too_easy: '偏简单', appropriate: '合适', too_hard: '偏难' }
const difficulties = { easy: '简单', medium: '中等', hard: '困难' }
</script>

<template>
  <p class="small muted">
    <RouterLink
      :to="`/trainings/${evidence.training_id}#training-item-${evidence.training_item_id}`"
      >{{ compact ? '查看本人自评' : '本人自评：' + ratings[evidence.rating] }}</RouterLink
    >
    <template v-if="!compact">
      · 下轮建议优先练习{{ difficulties[evidence.preferred_difficulty] }}难度</template
    >
  </p>
</template>

<style scoped>
a {
  color: var(--el-color-primary-dark-2);
}
</style>
