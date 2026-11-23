import type { components } from './generated/schema'

export type SkillReport = components['schemas']['SkillReportView']

export const skillReportReasons: Record<SkillReport['reason'], string> = {
  incorrect_content: '内容有误',
  privacy: '涉及个人信息',
  licensing: '许可或署名问题',
  other: '其他',
}

export const skillReportStates: Record<SkillReport['state'], string> = {
  pending: '待处理',
  confirmed: '已确认',
  dismissed: '未采纳',
}
