export class ApiError extends Error {
  status: number
  constructor(status: number) {
    const messages: Record<number, string> = {
      401: '登录已过期，请重新登录。草稿已保留。',
      403: '请求未通过会话验证，请刷新后重试。',
      404: '记录不存在或无权访问。',
      409: '提交凭据与内容不一致，请检查草稿后重试。',
      422: '请求不符合题目或提交要求。',
      429: '当前容量已满，请稍后重试。',
      503: '服务暂时不可用，请稍后重试。',
    }
    super(messages[status] ?? `请求失败（HTTP ${status}）`)
    this.status = status
  }
}

export function boundedFetch(fetcher: typeof fetch, unauthorized: () => void): typeof fetch {
  return async (input, init) => {
    const request = new Request(input, init)
    try {
      const response = await fetcher(
        new Request(request, {
          credentials: 'same-origin',
          cache: 'no-store',
          signal: AbortSignal.any([request.signal, AbortSignal.timeout(10000)]),
        }),
      )
      if (response.status === 401) unauthorized()
      return response
    } catch (error) {
      if (request.signal.aborted) throw error
      throw new Error('网络中断或请求超时。草稿与提交凭据已保留，可手动重试。', { cause: error })
    }
  }
}

export function unwrap<T>(result: { data?: T; response: Response }): T {
  if (!result.response.ok) throw new ApiError(result.response.status)
  if (result.data === undefined) throw new Error('服务返回了不完整的数据，请重试。')
  return result.data
}

export function csrfHeaders(): Record<string, string> {
  const token = document.cookie
    .split('; ')
    .find((v) => v.startsWith('kf_csrf='))
    ?.slice(8)
  if (!token || !/^[0-9a-f]{64}$/.test(token)) throw new ApiError(401)
  return { 'X-CSRF-Token': token }
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请重试。'
}
