import type { components } from './generated/schema'

export const skillPurposes: Record<components['schemas']['AgentPurpose'], string> = {
  submission_review: '提交复盘',
  guided_help: '渐进式答疑',
  focused_training: '专项训练',
  periodic_review: '阶段复习',
}
