<script setup lang="ts">
import type { RouteLocationRaw } from 'vue-router'
import type { components } from '../api/generated/schema'
import LearningModelReasoning from './LearningModelReasoning.vue'
import StatementMarkdown from './StatementMarkdown.vue'

defineProps<{
  turn: components['schemas']['AgentConversationRun']
  location: RouteLocationRaw
  label: string
  status: string
}>()
</script>

<template>
  <div class="timeline-turn">
    <p class="agent-request">{{ turn.question ?? label }}</p>
    <LearningModelReasoning
      v-if="turn.reasoning"
      :text="turn.reasoning"
      :kind="turn.reasoning_kind"
    />
    <StatementMarkdown
      v-if="turn.answer"
      class="timeline-answer"
      :source="turn.answer"
      :links="false"
      highlight-code
    />
    <RouterLink
      v-else-if="turn.training_id"
      :to="'/trainings/' + turn.training_id"
      class="text-link"
    >
      {{ turn.training_request?.title ?? '打开训练集' }}
    </RouterLink>
    <RouterLink v-else :to="location" class="text-link">
      {{
        turn.training_request?.title ??
        (turn.purpose === 'periodic_review' && turn.state === 'succeeded' ? '查看复习题单' : status)
      }}
    </RouterLink>
    <RouterLink
      v-if="turn.answer && turn.purpose === 'submission_review'"
      :to="location"
      class="text-link small"
    >
      查看复盘依据
    </RouterLink>
  </div>
</template>

<style scoped>
.timeline-turn {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.timeline-answer {
  padding-inline: 0.65rem;
}
.timeline-turn > a {
  align-self: flex-start;
  margin: 0.25rem 0.65rem;
}
</style>
