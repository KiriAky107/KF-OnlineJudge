<script setup lang="ts">
import { ref, watch } from 'vue'
import ProblemBoundaryDrafts from './ProblemBoundaryDrafts.vue'
import { reviewProblemDraft, type AuthoringSample } from '../lib/problemAuthoringReview'

const props = defineProps<{ statement: string; samples: readonly AuthoringSample[] }>()
const report = ref<ReturnType<typeof reviewProblemDraft>>()
const sectionLabels = { input: '输入格式', output: '输出格式', constraints: '数据范围' } as const
const sampleLabels = {
  matched: '与题面一致',
  different: '与题面内容不同',
  missing: '题面中未找到可对照的代码块',
  ambiguous: '题面有多段同名代码块',
  binary: '二进制样例，需人工核对',
} as const

watch(
  () => [props.statement, props.samples],
  () => (report.value = undefined),
  { deep: true },
)

function checkDraft() {
  report.value = reviewProblemDraft(props.statement, props.samples)
}
</script>

<template>
  <section class="admin-real-panel authoring-review" aria-label="录题体检">
    <h2>录题体检</h2>
    <button type="button" :disabled="!statement.trim()" @click="checkDraft">检查题面与样例</button>
    <div v-if="report" class="review-results" aria-live="polite">
      <dl class="statement-sections">
        <div v-for="(label, section) in sectionLabels" :key="section">
          <dt>{{ label }}</dt>
          <dd>{{ report.sections[section] ? '找到小节' : '未找到小节，核对题面' }}</dd>
        </div>
      </dl>
      <div v-if="report.constraints.length">
        <h3>题面中的数据范围</h3>
        <ul>
          <li v-for="(line, index) in report.constraints" :key="index">{{ line }}</li>
        </ul>
      </div>
      <div v-if="report.samples.length">
        <h3>公开样例对照</h3>
        <ol>
          <li v-for="(sample, index) in report.samples" :key="index">
            <strong>样例 {{ index + 1 }}</strong>
            <div>输入 · {{ sampleLabels[sample.input] }}</div>
            <div>输出 · {{ sampleLabels[sample.output] }}</div>
          </li>
        </ol>
      </div>
      <p v-else>本题暂无独立登记的公开样例。</p>
      <p v-if="report.extraSampleLabels.length">
        题面中的样例 {{ report.extraSampleLabels.join('、') }} 缺少对应的公开样例。
      </p>
    </div>
    <ProblemBoundaryDrafts :samples="samples" />
  </section>
</template>

<style scoped>
.review-results {
  display: grid;
  gap: 0.8rem;
  margin-block-start: 0.9rem;
}
.statement-sections {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
  gap: 0.65rem;
  margin: 0;
}
.statement-sections > div,
.review-results li {
  overflow-wrap: anywhere;
}
.statement-sections dt,
.review-results li strong {
  font-weight: 650;
}
.statement-sections dd {
  margin: 0.2rem 0 0;
}
.review-results :is(ol, ul) {
  padding-inline-start: 1.25rem;
}
.review-results li + li {
  margin-block-start: 0.4rem;
}
</style>
