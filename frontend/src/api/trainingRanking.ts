import type { components } from './generated/schema'

export type TrainingRankingProfile = NonNullable<
  components['schemas']['LearningParameters']['ranking_profile']
>

export const trainingRankingLabels: Record<TrainingRankingProfile, string> = {
  feedback_first: '难度自评优先',
  unresolved_first: '待巩固知识点优先',
}

export const trainingFeedbackRatingLabels = {
  too_easy: '偏简单',
  appropriate: '合适',
  too_hard: '偏难',
}
