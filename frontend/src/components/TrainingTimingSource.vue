<script setup lang="ts">
import { computed } from 'vue'
import type { components } from '../api/generated/schema'
import { displayTime } from '../lib/presentation'

const props = defineProps<{ timing: components['schemas']['TrainingTiming'] }>()
const source = computed(() => {
  const timing = props.timing
  const submission = { label: '上次提交', date: timing.last_attempt_at }
  const sources = {
    review_due: { label: '上次通过', date: timing.last_accepted_at },
    review_later: { label: '建议复习时间', date: timing.review_due_at },
    recent_plan: { label: '上次安排', date: timing.last_planned_at },
    practice: submission,
    recent_attempt: submission,
  }
  return sources[timing.status]
})
</script>

<template>
  <p v-if="source.date" class="small muted">{{ source.label }} · {{ displayTime(source.date) }}</p>
</template>
