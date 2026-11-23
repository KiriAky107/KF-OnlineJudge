<script setup lang="ts">
import type { components } from '../api/generated/schema'

defineProps<{ coverage: components['schemas']['TrainingGoalCoverage'] }>()
</script>

<template>
  <div class="small">
    <p v-if="coverage.covered.length">这组题目覆盖 {{ coverage.covered.join('、') }}</p>
    <p v-if="coverage.missing.length" class="muted">还需安排 {{ coverage.missing.join('、') }}</p>
  </div>
</template>
