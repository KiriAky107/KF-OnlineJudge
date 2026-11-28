export function isSubmissionReviewQuestion(question: string, hasTarget: boolean): boolean {
  const text = question.trim()
  if (/^(?:什么是|如何(?:实现|设计)|怎么(?:实现|设计))/.test(text)) return false
  if (hasTarget && /(?:对拍|缩减反例|边界测量|测.{0,5}边界|诊断|刚才的结果)/.test(text)) return true
  if (/(?:复盘|回顾)/.test(text)) {
    return hasTarget || /(?:提交|评测|判题|代码)/.test(text) || /^(?:帮我|请|先)?复盘/.test(text)
  }
  if (/^(?:能不能|请|帮我)?\s*(?:分析|检查|看一下|看下).{0,20}(?:提交|评测结果)/.test(text))
    return true
  return (
    (hasTarget || /提交|评测/.test(text)) &&
    /为什么.*(?:没有通过|没过|判错|报错|超时|\bWA\b|\bTLE\b|\bRE\b)/i.test(text)
  )
}
