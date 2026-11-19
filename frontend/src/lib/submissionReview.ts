export type ReviewCandidate = {
  submission_id: string
  created_at: string
  problem_version_id: string
}
export function reviewCandidates<T extends ReviewCandidate>(
  items: T[],
  current: ReviewCandidate,
): T[] {
  return items.filter(
    (item) =>
      item.submission_id !== current.submission_id &&
      item.problem_version_id === current.problem_version_id,
  )
}
export function defaultReviewCandidate(items: ReviewCandidate[], current: ReviewCandidate): string {
  return (
    (items.find((item) => item.created_at < current.created_at) ?? items[0])?.submission_id ?? ''
  )
}
export function reviewNotice(sameVersion: boolean, sameLanguage: boolean): string {
  return [
    !sameVersion ? '题目版本不同，测试数据和限制可能变化。' : '',
    !sameLanguage ? '语言不同，运行环境可能变化。' : '',
    '以下仅展示记录与源码差异；判定和耗时变化不证明某处修改导致通过或性能提升。',
  ]
    .filter(Boolean)
    .join(' ')
}
