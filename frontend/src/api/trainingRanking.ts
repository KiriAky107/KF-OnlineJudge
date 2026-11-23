import type { components } from './generated/schema'

export type TrainingRankingProfile = NonNullable<
  components['schemas']['LearningParameters']['ranking_profile']
>

export const trainingRankingLabels: Record<TrainingRankingProfile, string> = {
  feedback_first: '难度自评优先',
  unresolved_first: '待巩固知识点优先',
}
