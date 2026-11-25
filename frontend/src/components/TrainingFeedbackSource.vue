<script setup lang="ts">
import type { components } from '../api/generated/schema'
import { trainingFeedbackRatingLabels } from '../api/trainingRanking'
defineProps<{ evidence: components['schemas']['TrainingFeedbackEvidence']; compact?: boolean }>()
const difficulties = { easy: '简单', medium: '中等', hard: '困难' }
</script>

<template>
  <p class="small muted">
    本人自评 · {{ trainingFeedbackRatingLabels[evidence.rating] }}
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
