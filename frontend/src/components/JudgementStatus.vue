<script setup lang="ts">
import { computed } from 'vue'
import { preparingStatus, type WireLanguage } from '../composables/judgingLanguage'
const props = defineProps<{
  state: string
  verdict: string | null
  language?: WireLanguage
  plain?: boolean
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
const verdictTones: Record<string, string> = {
  accepted: 'verdict-ac',
  wrong_answer: 'verdict-wa',
  compilation_error: 'verdict-ce',
  runtime_error: 'verdict-re',
  time_limit_exceeded: 'verdict-tle',
  memory_limit_exceeded: 'verdict-tle',
  output_limit_exceeded: 'verdict-tle',
  system_error: 'verdict-re',
}
const phases: Record<string, 'queued' | 'compiling' | 'running'> = {
  queued: 'queued',
  claimed: 'compiling',
  compiling: 'compiling',
  running: 'running',
  cancelling: 'queued',
}
const phase = computed(() => (props.verdict ? undefined : phases[props.state]))
const tone = computed(() => {
  if (props.plain) return undefined
  if (props.verdict) return verdictTones[props.verdict] ?? 'verdict-pending'
  if (props.state === 'failed') return 'verdict-re'
  return phase.value === 'compiling' || phase.value === 'running'
    ? 'verdict-active'
    : 'verdict-pending'
})
const label = computed(() =>
  props.verdict
    ? (grades[props.verdict] ?? props.verdict)
    : props.state === 'compiling'
      ? preparingStatus(props.language)
      : (states[props.state] ?? 'Status Unavailable'),
)
</script>
<template>
  <span
    class="verdict judgement-tag"
    :class="tone"
    :data-phase="phase"
    lang="en"
    data-testid="judgement-status"
  >
    <span v-if="phase" class="phase-indicator" aria-hidden="true"></span>{{ label }}
  </span>
</template>
<style scoped>
.judgement-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-family: inherit;
  font-size: 12px;
  font-weight: 650;
}
.phase-indicator {
  display: inline-block;
  width: 8px;
  height: 8px;
  flex: none;
  border-radius: 50%;
  background: currentColor;
  animation: judgement-pulse 1.25s ease-in-out infinite;
}
[data-phase='compiling'] .phase-indicator {
  width: 10px;
  height: 10px;
  border: 1.5px solid currentColor;
  border-right-color: transparent;
  background: transparent;
  animation: judgement-turn 0.9s linear infinite;
}
[data-phase='queued'] .phase-indicator {
  animation-duration: 1.8s;
}
@keyframes judgement-pulse {
  50% {
    opacity: 0.45;
    transform: scale(0.76);
  }
}
@keyframes judgement-turn {
  to {
    transform: rotate(1turn);
  }
}
@media (prefers-reduced-motion: reduce) {
  .phase-indicator,
  [data-phase='compiling'] .phase-indicator {
    animation: none;
  }
}
</style>
