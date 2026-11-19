export function sample(value: string) {
  const bytes = Uint8Array.from(atob(value), (c) => c.charCodeAt(0))
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return (
      '字节（十六进制）：' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join(' ')
    )
  }
}
