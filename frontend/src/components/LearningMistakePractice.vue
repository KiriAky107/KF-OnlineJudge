<script setup lang="ts">
import type { components } from '../api/generated/schema'
import { diagnosticCategories } from '../lib/diagnostic'

defineProps<{ result: components['schemas']['MistakePractice'] }>()
</script>

<template>
  <section class="mistake-practice" aria-label="错因补练建议">
    <h3>{{ result.available_minutes ? `${result.available_minutes} 分钟补练` : '同类题' }}</h3>
    <p class="small">
      已确认错因 · {{ diagnosticCategories[result.evidence.finding.category] }}
      <RouterLink :to="'/submissions/' + result.evidence.submission_id" class="text-link">
        查看原提交
      </RouterLink>
    </p>
    <p>{{ result.focus }}</p>
    <p v-if="result.review_minutes">
      {{ result.review_minutes }} 分钟 · 用原题的公开样例复核此错因。
    </p>
    <RouterLink
      v-if="result.source_available"
      class="text-link"
      :to="{
        name: 'problem-workspace',
        params: { id: result.evidence.problem_version_id },
        query: { selfTestSample: String(result.evidence.sample_ordinal + 1) },
      }"
    >
      {{ result.source_title }} · 将公开样例 {{ result.evidence.sample_ordinal + 1 }} 填入自测
    </RouterLink>
    <ol v-if="result.items.length" class="practice-items">
      <li v-for="item in result.items" :key="item.problem_version_id">
        <span v-if="item.estimated_minutes">{{ item.estimated_minutes }} 分钟 · </span>
        <RouterLink :to="'/problems/' + item.problem_version_id" class="text-link">
          {{ item.title }}
        </RouterLink>
        <p class="small muted">与原题共有知识点 {{ item.shared_tags.join('、') }}</p>
      </li>
    </ol>
    <p v-if="result.verification_minutes">
      {{ result.verification_minutes }} 分钟 · 用边界输入验证修改，简记检查方法。
    </p>
    <p v-for="gap in result.gaps" :key="gap" class="small muted">{{ gap }}</p>
  </section>
</template>

<style scoped>
.mistake-practice,
.practice-items {
  display: grid;
  gap: 0.55rem;
}
.mistake-practice :is(h3, ol, p) {
  margin: 0;
}
.practice-items {
  padding-inline-start: 1.45rem;
}
.mistake-practice h3 {
  font-size: 1rem;
}
</style>
