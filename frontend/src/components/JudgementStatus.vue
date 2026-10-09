<script setup lang="ts">
import { computed } from 'vue'
import type { components } from '../api/generated/schema'
const props = defineProps<{
  state: components['schemas']['SubmissionSummary']['state']
  verdict: components['schemas']['SubmissionSummary']['verdict']
}>()
const grades: Record<string, string> = {
  accepted: 'AC',
  wrong_answer: 'WA',
  compilation_error: 'CE',
  runtime_error: 'RE',
  time_limit_exceeded: 'TLE',
  memory_limit_exceeded: 'MLE',
  output_limit_exceeded: 'OLE',
  cancelled: '已取消',
  system_error: '系统错误',
}
const states: Record<string, string> = {
  queued: '等待评测',
  claimed: '已领取',
  compiling: '正在编译',
  running: '正在运行',
  cancelling: '正在停止并清理',
  cancelled: '已取消',
  failed: '评测失败',
  finished: '评测完成',
}
const label = computed(() =>
  props.verdict ? (grades[props.verdict] ?? props.verdict) : states[props.state],
)
</script>
<template>
  <span class="verdict" data-testid="judgement-status">{{ label }}</span>
</template>
