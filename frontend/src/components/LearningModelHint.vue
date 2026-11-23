<script setup lang="ts">
import type { components } from '../api/generated/schema'

type Hint = components['schemas']['ModelHint']
type State = components['schemas']['GuidedHelpResult']['model_status']

type Context = 'concept' | 'review' | 'plan' | 'source'
withDefaults(defineProps<{ hint?: Hint | null; status?: State; context?: Context }>(), {
  hint: null,
  status: null,
  context: 'concept',
})

const labels = {
  concept: { title: '模型提示', fallback: '规则提示', insufficient: '缺少可用的提示依据。' },
  review: {
    title: '模型建议 · 待核对',
    fallback: '规则复盘',
    insufficient: '当前没有足够的评测记录生成模型建议。',
  },
  source: {
    title: '本机模型分析 · 待核对',
    fallback: '规则复盘',
    insufficient: '缺少可用的本人源码片段。',
  },
  plan: {
    title: '模型训练说明',
    fallback: '规则训练草案',
    insufficient: '当前没有可安排的题目供模型解释。',
  },
}
</script>

<template>
  <section v-if="hint" :aria-label="labels[context].title" class="model-hint">
    <h4>{{ labels[context].title }}</h4>
    <p>{{ hint.text }}</p>
    <p class="small muted">
      {{ hint.model }}
      <template v-if="hint.usage">
        · 输入 {{ hint.usage.input_tokens }} token · 输出 {{ hint.usage.output_tokens }} token
      </template>
    </p>
  </section>
  <p v-else-if="status === 'insufficient_evidence'" class="small muted">
    {{ labels[context].insufficient }}
  </p>
  <p v-else-if="status === 'source_model_unavailable'" class="small muted">
    片段分析需要配置本机模型。已保留规则复盘。
  </p>
  <p v-else-if="status === 'unavailable'" class="small muted">
    模型服务未返回可用内容，已保留{{ labels[context].fallback }}。
  </p>
  <p v-else-if="status === 'budget_exhausted'" class="small muted">
    本次模型调用额度已满，已保留{{ labels[context].fallback }}。
  </p>
  <p v-else-if="status === 'uncertain'" class="small muted">
    上次模型响应未能确认，已保留{{ labels[context].fallback }}。
  </p>
</template>

<style scoped>
.model-hint {
  display: grid;
  gap: 0.35rem;
  padding: 0.85rem 1rem;
  border-inline-start: 3px solid var(--el-color-primary);
  border-radius: var(--el-border-radius-base);
  background: var(--el-color-primary-light-9);
  overflow-wrap: anywhere;
}
.model-hint h4 {
  margin: 0;
  color: var(--el-text-color-primary);
  font-size: 0.95rem;
  font-weight: 650;
}
.model-hint p {
  white-space: pre-wrap;
}
</style>
