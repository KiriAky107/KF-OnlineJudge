import { api } from './client'

export async function fetchLiveness(signal: AbortSignal) {
  const { data, error, response } = await api.GET('/api/v1/health/live', { signal })
  if (!response.ok || error || !data) {
    throw new Error(`平台暂时无法连接（HTTP ${response.status}）`)
  }
  return data
}
