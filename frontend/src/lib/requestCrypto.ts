import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'

export function sha256Fingerprint(bytes: Uint8Array): string {
  return bytesToHex(sha256(bytes))
}

export function newRequestId(): string {
  const source = globalThis.crypto
  if (!source?.getRandomValues) throw new Error('浏览器无法生成请求凭据。请更换浏览器后重试。')
  if (source.randomUUID) return source.randomUUID()

  const bytes = source.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = bytesToHex(bytes)
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}
