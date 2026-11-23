<script setup lang="ts">
import { computed } from 'vue'
import { preparingStatus, type WireLanguage } from '../composables/judgingLanguage'
const props = defineProps<{
  state: string
  verdict: string | null
  language?: WireLanguage
}>()
const grades: Record<string, string> = {
  accepted: 'Accepted',
  wrong_answer: 'Wrong Answer',
  compilation_error: 'Compilation Error',
  runtime_error: 'Runtime Error',
  time_limit_exceeded: 'Time Limit Exceeded',
  memory_limit_exceeded: 'Memory Limit Exceeded',
  output_limit_exceeded: 'Output Limit Exceeded',
  cancelled: 'Cancelled',
  system_error: 'System Error',
}
const states: Record<string, string> = {
  queued: 'Queued',
  claimed: 'Preparing',
  running: 'Running',
  cancelling: 'Cancelling',
  cancelled: 'Cancelled',
  failed: 'Judging Failed',
  finished: 'Judging Complete',
  unobserved: 'Not Yet Judged',
}
const label = computed(() =>
  props.verdict
    ? (grades[props.verdict] ?? props.verdict)
    : props.state === 'compiling'
      ? preparingStatus(props.language)
      : (states[props.state] ?? 'Status Unavailable'),
)
</script>
<template>
  <span class="verdict" lang="en" data-testid="judgement-status">{{ label }}</span>
</template>
