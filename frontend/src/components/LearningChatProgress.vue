<script setup lang="ts">
import type { components } from '../api/generated/schema'

type Phase = components['schemas']['AgentRunView']['state'] | 'sending'
defineProps<{ phase: Phase }>()
const labels: Partial<Record<Phase, string>> = {
  sending: '正在发送',
  queued: '等待助手处理',
  running: '正在处理问题',
  retry_wait: '等待重试',
  cancel_requested: '正在停止',
}
</script>

<template>
  <div v-if="labels[phase]" class="chat-progress" role="status" aria-live="polite">
    <span class="progress-dots" aria-hidden="true"><i /><i /><i /></span>
    <span>{{ labels[phase] }}</span>
  </div>
</template>

<style scoped>
.chat-progress {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  color: var(--secondary-ink);
  font-size: 0.875rem;
  min-height: 1.5rem;
}
.progress-dots {
  display: inline-flex;
  gap: 4px;
  align-items: center;
}
.progress-dots i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent-ink);
  animation: chat-pulse 1.2s ease-in-out infinite;
}
.progress-dots i:nth-child(2) {
  animation-delay: 0.14s;
}
.progress-dots i:nth-child(3) {
  animation-delay: 0.28s;
}
@keyframes chat-pulse {
  0%,
  70%,
  100% {
    transform: translateY(0);
    opacity: 0.45;
  }
  35% {
    transform: translateY(-3px);
    opacity: 1;
  }
}
@media (prefers-reduced-motion: reduce) {
  .progress-dots i {
    animation: none;
  }
}
</style>
