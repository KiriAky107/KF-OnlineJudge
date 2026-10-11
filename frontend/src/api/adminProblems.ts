import { api, uploadApi } from './client'
import { csrfHeaders, unwrap, ApiError } from './transport'
import type { components } from './generated/schema'
export type ImportDetail = components['schemas']['ProblemImportDetail']
export type ManagedProblem = components['schemas']['MaintainedProblem']
export type PublishRequest = components['schemas']['PublishCandidate']
export type AvailabilityRequest = components['schemas']['ChangeProblemAvailability']
export type Candidate = components['schemas']['CandidatePreview']
export type Version = components['schemas']['MaintainedVersion']
export type AuditEvent = components['schemas']['MaintenanceAuditEvent']

export const adminProblems = {
  capabilities: async (signal: AbortSignal) =>
    unwrap(await api.GET('/api/v1/admin/problem-imports/capabilities', { signal })),
  page: async (signal: AbortSignal, after?: string, enabled?: boolean) =>
    unwrap(
      await api.GET('/api/v1/admin/problems', {
        signal,
        params: { query: { after, enabled, limit: 20 } },
      }),
    ),
  bySlug: async (slug: string, signal: AbortSignal) =>
    unwrap(
      await api.GET('/api/v1/admin/problem-slugs/{slug}', { signal, params: { path: { slug } } }),
    ),
  detail: async (id: string, signal: AbortSignal) =>
    unwrap(
      await api.GET('/api/v1/admin/problems/{problem_id}', {
        signal,
        params: { path: { problem_id: id } },
      }),
    ),
  versions: async (id: string, signal: AbortSignal, before?: number) =>
    unwrap(
      await api.GET('/api/v1/admin/problems/{problem_id}/versions', {
        signal,
        params: { path: { problem_id: id }, query: { before, limit: 20 } },
      }),
    ),
  imports: async (signal: AbortSignal, after?: string) =>
    unwrap(
      await api.GET('/api/v1/admin/problem-imports', {
        signal,
        params: { query: { after, limit: 20 } },
      }),
    ),
  import: async (id: string, signal: AbortSignal) =>
    unwrap(
      await api.GET('/api/v1/admin/problem-imports/{operation_id}', {
        signal,
        params: { path: { operation_id: id } },
      }),
    ),
  create: async (body: components['schemas']['CreateProblemImport'], signal: AbortSignal) =>
    unwrap(
      await api.POST('/api/v1/admin/problem-imports', { body, signal, headers: csrfHeaders() }),
    ),
  upload: async (id: string, file: File, signal: AbortSignal) =>
    unwrap(
      await uploadApi.PUT('/api/v1/admin/problem-imports/{operation_id}/archive', {
        signal,
        params: { path: { operation_id: id } },
        headers: { ...csrfHeaders(), 'Content-Type': 'application/zip' },
        body: file as unknown as string,
        bodySerializer: (value) => value as unknown as Blob,
      }),
    ),
  discard: async (id: string, signal: AbortSignal) =>
    unwrap(
      await api.POST('/api/v1/admin/problem-imports/{operation_id}/discard', {
        signal,
        params: { path: { operation_id: id } },
        headers: csrfHeaders(),
      }),
    ),
  publish: async (body: PublishRequest, signal: AbortSignal) =>
    unwrap(
      await api.POST('/api/v1/admin/problem-publications', {
        signal,
        body,
        headers: csrfHeaders(),
      }),
    ),
  availability: async (
    id: string,
    enabled: boolean,
    body: AvailabilityRequest,
    signal: AbortSignal,
  ) => {
    const options = { signal, body, params: { path: { problem_id: id } }, headers: csrfHeaders() }
    return unwrap(
      enabled
        ? await api.POST('/api/v1/admin/problems/{problem_id}/restore', options)
        : await api.POST('/api/v1/admin/problems/{problem_id}/disable', options),
    )
  },
  receipt: async (id: string, signal: AbortSignal) =>
    unwrap(
      await api.GET('/api/v1/admin/problem-maintenance/{operation_id}', {
        signal,
        params: { path: { operation_id: id } },
      }),
    ),
  audit: async (signal: AbortSignal, before?: string) =>
    unwrap(
      await api.GET('/api/v1/admin/maintenance-audit', {
        signal,
        params: { query: { before, limit: 20 } },
      }),
    ),
}

export function maintenanceError(error: unknown): string {
  if (error instanceof ApiError) {
    const text: Record<number, string> = {
      403: '维护权限已失效或请求未通过 CSRF 验证，请重新核对身份。',
      408: '上传超过 30 秒期限，请重新选择文件并核对导入状态。',
      409: '候选或活动版本已变化。请刷新并核对服务器状态后重新操作。',
      413: '题包超出 64 MiB 限制。',
      415: '请上传单层 ZIP 题包。',
      503: '维护服务暂时不可用；操作凭据已保留，可核对后重试。',
    }
    return text[error.status] ?? error.message
  }
  return error instanceof Error ? error.message : '维护请求失败。'
}
