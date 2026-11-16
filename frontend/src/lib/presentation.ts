const dateTime = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export function displayTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间暂不可用' : dateTime.format(date)
}

// Keep the advertised runtime version, without exposing build commit/digest text.
export function displayRuntimeVersion(value: string) {
  return value
    .replace(/\+g[0-9a-f]{7,64}\b/gi, '')
    .replace(/\b(?:sha256:)?[0-9a-f]{32,64}\b/gi, '已校验构建')
}
