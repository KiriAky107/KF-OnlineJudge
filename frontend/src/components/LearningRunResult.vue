<script setup lang="ts">
import DisclosureHeading from './DisclosureHeading.vue'
import type { components } from '../api/generated/schema'
import { trainingRankingLabels } from '../api/trainingRanking'
import { displayTime } from '../lib/presentation'
import { comparisonCaseLabels } from '../lib/submissionReview'
import JudgementStatus from './JudgementStatus.vue'
import PanelCard from './PanelCard.vue'
import LearningRunTrace from './LearningRunTrace.vue'
import LearningModelHint from './LearningModelHint.vue'
import LearningModelReasoning from './LearningModelReasoning.vue'
import StatementMarkdown from './StatementMarkdown.vue'
import LearningCodeEvidence from './LearningCodeEvidence.vue'
import LearningRunFeedback from './LearningRunFeedback.vue'
import TrainingFeedbackSource from './TrainingFeedbackSource.vue'
import TrainingGoalCoverage from './TrainingGoalCoverage.vue'
import TrainingPrerequisites from './TrainingPrerequisites.vue'
import TrainingTimingSource from './TrainingTimingSource.vue'

type Run = components['schemas']['AgentRunView']
defineProps<{ run: Run; busy: boolean }>()
defineEmits<{ save: []; cancel: []; refresh: []; feedback: [run: Run] }>()

const labels: Record<Run['state'], string> = {
  queued: '排队中',
  running: '执行中',
  retry_wait: '等待重试',
  cancel_requested: '正在取消',
  cancelled: '已取消',
  succeeded: '已完成',
  failed: '执行失败',
}
const failures: Record<string, string> = {
  evidence_unavailable: '当前账号在可查询范围内找不到这次提交。',
  authorization_unavailable: '原登录会话或技能授权已失效。',
  contest_in_progress: '比赛开始时，这条学习请求已停止。',
  hint_level_unavailable: '当前提示等级超出允许范围，请刷新后选择允许的等级。',
  tool_unavailable: '当前技能配置未授权所需工具。',
  tool_budget_exhausted: '本次运行已达到时间或调用额度。',
  tool_timeout: '学习工具在限定时间内未返回。',
  learning_attempts_exhausted: '运行已用完接续次数。',
}
const focusLabels = {
  wrong_answer: '答案错误',
  compilation_error: '编译失败',
  later_accepted: '同版本后次通过',
}
</script>

<template>
  <article v-if="run.chat || run.review_question" class="chat-reply" aria-label="学习助手回复">
    <div class="chat-reply-content">
      <LearningModelReasoning
        v-if="run.result?.model_hint?.reasoning || run.partial_reasoning"
        :text="run.result?.model_hint?.reasoning ?? run.partial_reasoning ?? ''"
        :kind="run.partial_reasoning ? 'text' : run.result?.model_hint?.reasoning_kind"
        :thinking="run.state === 'running'"
      />
      <LearningRunTrace :key="run.run_id" :run-id="run.run_id" :chat-run="run" />
      <template
        v-if="run.result?.purpose === 'guided_help' || run.result?.purpose === 'submission_review'"
      >
        <StatementMarkdown
          v-if="run.result.model_hint"
          class="chat-answer-text"
          :source="run.result.model_hint.text"
          :links="false"
          highlight-code
        />
        <p v-else role="status">
          {{
            run.result.model_status === 'budget_exhausted'
              ? '模型调用额度已满，请稍后再试。'
              : run.result.model_status === 'output_limit'
                ? '模型服务达到自身的输出上限，未返回完整答复。'
                : run.result.model_status === 'source_model_unavailable'
                  ? '源码分析需要可用的本机模型。'
                  : run.result.model_status === 'context_limit'
                    ? '当前问题和必要证据超出模型上下文预算。'
                    : run.result.model_status === 'insufficient_evidence'
                      ? '当前没有足够的记录生成模型回复。'
                      : '模型暂未返回答复，请稍后重试。'
          }}
        </p>
      </template>
      <p v-else-if="run.failure_code" role="alert">
        {{ failures[run.failure_code] ?? '未取得有效回复。' }}
      </p>
      <p v-else-if="run.state === 'cancelled'" role="status">本次提问已取消。</p>
      <StatementMarkdown
        v-else-if="run.partial_answer && run.state === 'running'"
        class="chat-answer-text streaming-answer"
        :source="run.partial_answer"
        :links="false"
        highlight-code
      />
      <div class="chat-reply-actions">
        <el-button
          v-if="['queued', 'running', 'retry_wait'].includes(run.state)"
          text
          size="small"
          :disabled="busy"
          @click="$emit('cancel')"
          >停止</el-button
        >
        <el-button
          v-if="!run.result && run.state !== 'cancelled'"
          text
          size="small"
          :disabled="busy"
          @click="$emit('refresh')"
          >刷新状态</el-button
        >
      </div>
      <details
        v-if="run.result?.purpose === 'guided_help' && run.result.model_hint"
        class="chat-feedback"
      >
        <DisclosureHeading icon="chat" tone="muted">反馈</DisclosureHeading>
        <LearningRunFeedback
          :key="run.run_id"
          :run-id="run.run_id"
          :purpose="run.result.purpose"
          :saved="run.feedback"
          @saved="$emit('feedback', $event)"
        />
      </details>
    </div>
  </article>
  <PanelCard
    v-if="!run.chat && (!run.review_question || run.result?.purpose === 'submission_review')"
    :title="run.review_question ? '复盘依据' : '运行结果'"
    icon="trainings"
  >
    <template v-if="!run.review_question" #actions
      ><el-button :disabled="busy" @click="$emit('refresh')">刷新</el-button></template
    >
    <div class="stack">
      <p v-if="!run.review_question" role="status">
        {{ labels[run.state] }} · {{ displayTime(run.created_at) }}
      </p>
      <p v-if="run.failure_code" role="alert">
        {{ failures[run.failure_code] ?? '运行未能取得有效结果。' }}
      </p>
      <div v-if="['queued', 'running', 'retry_wait'].includes(run.state)">
        <el-button :disabled="busy" @click="$emit('cancel')">取消运行</el-button>
      </div>
      <template v-if="run.result">
        <template v-if="run.result.purpose === 'submission_review'">
          <p>
            <template v-if="run.result.evidence_as_of">
              评测记录 · {{ displayTime(run.result.since) }} 至
              {{ displayTime(run.result.evidence_as_of) }}
            </template>
            <template v-else>记录起点 {{ displayTime(run.result.since) }}</template>
            <span v-if="run.result.truncated"> · 仅显示部分记录</span>
          </p>
          <p v-if="run.result.anchor_submission_id" class="small muted">
            <RouterLink :to="'/submissions/' + run.result.anchor_submission_id">
              所选提交
            </RouterLink>
            及同题目版本、同语言的更早提交
          </p>
          <ul v-if="run.result.records.length">
            <li v-for="row in run.result.records" :key="row.submission_id">
              <RouterLink class="evidence-link" :to="'/submissions/' + row.submission_id">
                {{ displayTime(row.submitted_at) }}
                <JudgementStatus :state="row.state" :verdict="row.verdict" />
              </RouterLink>
            </li>
          </ul>
          <ul v-if="run.result.comparisons.length">
            <li v-for="pair in run.result.comparisons" :key="pair.later_submission_id">
              <RouterLink :to="'/submissions/' + pair.earlier_submission_id">前次提交</RouterLink>
              →
              <RouterLink :to="'/submissions/' + pair.later_submission_id">后次提交</RouterLink>
              {{
                pair.outcome === 'later_accepted'
                  ? ' · 后次通过'
                  : pair.outcome === 'later_non_accepted'
                    ? ' · 后次仍未通过'
                    : ''
              }}
            </li>
          </ul>
          <p
            v-if="
              run.result.model_status === 'generated' &&
              run.result.model_review_basis === 'evaluations' &&
              !run.result.model_hint?.review_resources
            "
            class="small muted"
          >
            模型建议参考了评测统计，具体记录可在上方打开。
          </p>
          <p
            v-else-if="
              run.result.model_status === 'generated' &&
              run.result.model_review_basis === 'evaluations_and_code_counts'
            "
            class="small muted"
          >
            模型建议参考了评测计数和已授权的代码证据统计。
          </p>
          <p
            v-else-if="
              run.result.model_status === 'generated' &&
              run.result.model_review_basis === 'evaluations_and_control_flow'
            "
            class="small muted"
          >
            模型建议参考了评测统计及已授权的修改区段关键字计数。
          </p>
          <p
            v-else-if="
              run.result.model_status === 'generated' &&
              run.result.model_review_basis === 'evaluations_and_comparison_cases'
            "
            class="small muted"
          >
            模型建议参考了评测统计及已授权的比较条件计数。
          </p>
          <LearningModelHint
            v-if="!run.review_question"
            :hint="run.result.model_hint"
            :status="run.result.model_status"
            :context="run.result.model_review_basis === 'source_excerpt' ? 'source' : 'review'"
            :markdown="Boolean(run.review_question)"
          />
          <p
            v-if="
              run.result.model_hint?.source_line &&
              (run.result.code?.source || run.result.anchor_submission_id)
            "
            class="small review-focus"
          >
            模型引用 · 第 {{ run.result.model_hint.source_line }} 行
            <RouterLink
              :to="
                '/submissions/' +
                (run.result.code?.source.submission_id ?? run.result.anchor_submission_id)
              "
            >
              查看原提交
            </RouterLink>
          </p>
          <p v-if="run.result.model_hint?.review_comparison_case" class="small review-focus">
            模型建议优先核对 ·
            {{ comparisonCaseLabels[run.result.model_hint.review_comparison_case] }}
          </p>
          <p v-if="run.result.model_focus" class="small review-focus">
            已复核的评测 · {{ focusLabels[run.result.model_focus.category] }}
            <RouterLink :to="'/submissions/' + run.result.model_focus.evidence.submission_id">
              查看评测
            </RouterLink>
          </p>
          <p v-else-if="run.result.model_hint?.review_focus" class="small muted">
            评测线索待核对 · {{ focusLabels[run.result.model_hint.review_focus] }}
          </p>
        </template>
        <template v-else-if="run.result.purpose === 'guided_help'">
          <h3>
            {{
              {
                concept: '概念提示',
                location: '位置线索',
                local_change: '局部修改建议',
              }[run.result.delivered_level ?? 'concept']
            }}
          </h3>
          <p>{{ run.result.hint }}</p>
          <pre v-if="run.result.hint_example"><code>{{ run.result.hint_example }}</code></pre>
          <p v-if="run.result.model_hint_basis === 'topic_and_verdict'" class="small muted">
            模型提示参考了所选主题及下方本人评测的判定类别。
          </p>
          <LearningModelHint :hint="run.result.model_hint" :status="run.result.model_status" />
          <RouterLink
            v-if="run.result.evidence"
            class="evidence-link"
            :to="'/submissions/' + run.result.evidence.submission_id"
          >
            查看提示依据
            <JudgementStatus
              :state="run.result.evidence.state"
              :verdict="run.result.evidence.verdict"
            />
          </RouterLink>
          <p v-if="run.result.evidence && run.result.evidence_as_of" class="small muted">
            评测依据截至 {{ displayTime(run.result.evidence_as_of) }}
          </p>
        </template>
        <template v-else>
          <p v-if="run.result.goal">目标 · {{ run.result.goal }}</p>
          <p v-if="run.result.evidence_as_of" class="small muted">
            学习记录截至 {{ displayTime(run.result.evidence_as_of) }}
          </p>
          <p v-if="run.result.ranking_profile" class="small muted">
            排序侧重 · {{ trainingRankingLabels[run.result.ranking_profile] }}
          </p>
          <template v-if="run.result.purpose === 'focused_training'">
            <p>每日 {{ run.result.daily_minutes }} 分钟</p>
            <TrainingGoalCoverage
              v-if="run.result.goal_coverage"
              :coverage="run.result.goal_coverage"
            />
          </template>
          <template v-else>
            <p v-if="run.result.available_minutes != null">
              本次 {{ run.result.available_minutes }} 分钟 · {{ run.result.items.length }} 道题
            </p>
            <p v-if="run.result.tags?.length" class="small muted">
              {{ run.result.tags.join(' · ') }}
            </p>
            <p>周期 {{ run.result.cycle_days }} 天 · 每周 {{ run.result.sessions_per_week }} 次</p>
            <details v-if="run.result.evidence.length">
              <DisclosureHeading icon="reading">复习依据</DisclosureHeading>
              <ul>
                <li v-for="row in run.result.evidence" :key="row.submission_id">
                  <RouterLink :to="'/submissions/' + row.submission_id">
                    {{ displayTime(row.submitted_at) }} · {{ row.verdict ?? row.state }}
                  </RouterLink>
                </li>
              </ul>
            </details>
          </template>
          <ol v-if="run.result.items.length">
            <li v-for="item in run.result.items" :key="item.problem_version_id">
              <RouterLink :to="'/problems/' + item.problem_version_id">{{ item.title }}</RouterLink>
              <p class="small muted">
                {{ item.objective
                }}{{ item.estimated_minutes ? ' · 约 ' + item.estimated_minutes + ' 分钟' : '' }}
              </p>
              <p
                v-if="run.result.purpose === 'focused_training' && run.result.goal_coverage"
                class="small muted"
              >
                {{ item.tags.join(' / ') }}
              </p>
              <TrainingTimingSource v-if="item.timing" :timing="item.timing" />
              <TrainingPrerequisites
                v-if="item.prerequisites?.length"
                :proofs="item.prerequisites"
              />
              <TrainingFeedbackSource
                v-if="item.feedback_evidence"
                :evidence="item.feedback_evidence"
              />
            </li>
          </ol>
          <LearningModelHint
            :hint="run.result.model_hint"
            :status="run.result.model_status"
            context="plan"
          />
          <div v-if="run.result.items.length">
            <el-button type="primary" :disabled="busy" @click="$emit('save')"
              >确认保存训练计划</el-button
            >
          </div>
        </template>
        <LearningCodeEvidence
          v-if="
            (run.result.purpose === 'guided_help' || run.result.purpose === 'submission_review') &&
            run.result.code
          "
          :code="run.result.code"
        />
        <ul v-if="run.result.gaps.length" class="small muted">
          <li v-for="gap in run.result.gaps" :key="gap">{{ gap }}</li>
        </ul>
        <LearningRunFeedback
          v-if="run.state === 'succeeded'"
          :key="run.run_id"
          :run-id="run.run_id"
          :purpose="run.result.purpose"
          :saved="run.feedback"
          @saved="$emit('feedback', $event)"
        />
      </template>
      <LearningRunTrace v-if="!run.review_question" :key="run.run_id" :run-id="run.run_id" />
    </div>
  </PanelCard>
</template>

<style scoped>
.chat-reply-content {
  display: grid;
  gap: 0.4rem;
}
.chat-answer-text {
  animation: answer-enter 200ms ease-out both;
}
.streaming-answer::after {
  content: '';
  display: inline-block;
  width: 0.45em;
  height: 0.85em;
  background: var(--el-color-primary);
  animation: answer-cursor 1s steps(2) infinite;
}
@keyframes answer-cursor {
  50% {
    opacity: 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .streaming-answer::after {
    animation: none;
  }
}
.chat-reply-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}
.chat-reply-actions:empty {
  display: none;
}
.chat-reply-actions :deep(.el-button) {
  margin-inline-start: 0;
  padding-inline: 0.3rem;
}
.chat-feedback summary {
  cursor: pointer;
  color: var(--secondary-ink);
  font-size: 0.85rem;
}
@keyframes answer-enter {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
}
@media (prefers-reduced-motion: reduce) {
  .chat-answer-text {
    animation: none;
  }
}

ol,
ul {
  margin: 0;
  padding-left: 1.25rem;
}
li + li {
  margin-top: 0.35rem;
}
.evidence-link {
  display: inline-flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.review-focus {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  color: var(--el-text-color-regular);
}
a {
  color: var(--el-color-primary-dark-2);
}
</style>
