import type { components } from '../api/generated/schema'

export type HintLevel = NonNullable<components['schemas']['LearningChatRequest']['hint_level']>

export const hintLevels = [
  { value: 'L0', label: '题意澄清' },
  { value: 'L1', label: '方向提示' },
  { value: 'L2', label: '状态与反例' },
  { value: 'L3', label: '局部诊断' },
] as const satisfies ReadonlyArray<{ value: HintLevel; label: string }>

export function hintIndex(level: HintLevel | null | undefined): number {
  return Math.max(
    0,
    hintLevels.findIndex(({ value }) => value === level),
  )
}
