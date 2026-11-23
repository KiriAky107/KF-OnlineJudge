<script setup lang="ts">
import type { components } from '../api/generated/schema'
import { comparisonCaseLabels } from '../lib/submissionReview'

defineProps<{ code: components['schemas']['SubmissionCodeEvidence'] }>()
const kinds = {
  replace: '修改',
  insert: '新增',
  delete: '删除',
}
const levels = {
  error: '错误',
  warning: '警告',
  note: '说明',
  location: '诊断位置',
}
const controlKeywords = {
  branch_keywords: '分支',
  loop_keywords: '循环',
  return_keywords: '返回',
}
function lines(start: number, count: number) {
  if (count === 0) return `第 ${start} 行前`
  if (count === 1) return `第 ${start} 行`
  return `第 ${start}–${start + count - 1} 行`
}
function changedCaseSummary(cases: components['schemas']['CodeBoundaryChange']['changed_cases']) {
  if (!cases?.length) return comparisonCaseLabels.equal
  return cases.map((relation) => comparisonCaseLabels[relation]).join('、')
}
</script>

<template>
  <div class="stack code-evidence">
    <h3>本人代码证据</h3>
    <p>
      <RouterLink :to="'/submissions/' + code.source.submission_id">打开本次提交</RouterLink>
      <span v-if="code.line_count != null" class="small muted">
        · 源码 {{ code.line_count }} 行</span
      >
    </p>
    <p v-if="!code.source_available" class="small muted">当前无法读取该次提交的源码。</p>
    <ul v-if="code.compiler_locations.length">
      <li v-for="item in code.compiler_locations" :key="`${item.line}:${item.column}:${item.kind}`">
        {{ levels[item.kind] }} · 第 {{ item.line }} 行{{
          item.column ? `第 ${item.column} 列` : ''
        }}
      </li>
    </ul>
    <p v-if="code.diagnostics_truncated" class="small muted">诊断位置仅展示可核对范围。</p>
    <template v-if="code.comparison">
      <p>
        对比
        <RouterLink :to="'/submissions/' + code.comparison.earlier.submission_id"
          >前次提交</RouterLink
        >
        <span class="small muted"> · 同一题目版本及语言</span>
      </p>
      <ul v-if="code.comparison.regions.length">
        <li
          v-for="region in code.comparison.regions"
          :key="`${region.before_line}:${region.after_line}`"
        >
          {{ kinds[region.kind] }} · 前次 {{ lines(region.before_line, region.before_count) }} →
          本次 {{ lines(region.after_line, region.after_count) }}
        </li>
      </ul>
      <p v-else class="small muted">所比较的行中没有文本变化。</p>
      <section
        v-if="code.comparison.boundary_changes?.length"
        class="code-statistics boundary-changes"
      >
        <h4>比较条件改动</h4>
        <p class="small muted">按普通有序值分析，用公开样例核对标出的取值关系。</p>
        <ul>
          <li v-for="change in code.comparison.boundary_changes" :key="change.after_line">
            前次第 {{ change.before_line }} 行 <code>{{ change.before_operator }}</code> → 本次第
            {{ change.after_line }} 行 <code>{{ change.after_operator }}</code>
            <span class="small muted"> · {{ changedCaseSummary(change.changed_cases) }}</span>
          </li>
        </ul>
      </section>
      <section v-if="code.comparison.control_flow" class="code-statistics control-flow">
        <h4>修改区段的关键字计数</h4>
        <dl class="details-list">
          <div v-for="(label, key) in controlKeywords" :key="key">
            <dt>{{ label }}</dt>
            <dd>
              {{ code.comparison.control_flow.before[key] }} →
              {{ code.comparison.control_flow.after[key] }}
            </dd>
          </div>
        </dl>
      </section>
      <p v-if="code.comparison.truncated" class="small muted">差异超出比较范围，仅展示部分行。</p>
    </template>
  </div>
</template>

<style scoped>
.code-evidence {
  border-top: 1px solid var(--line);
  padding-top: 0.75rem;
}
.code-statistics {
  padding: 0.65rem 0.85rem;
  border-radius: var(--el-border-radius-base);
  background: var(--el-fill-color-lighter);
}
.code-statistics h4 {
  margin: 0;
  color: var(--el-text-color-primary);
}
.boundary-changes p {
  margin: 0.35rem 0;
}
ul {
  margin: 0;
  padding-left: 1.25rem;
}
a {
  color: var(--el-color-primary-dark-2);
}
</style>
