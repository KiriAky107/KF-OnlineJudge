import type { components } from '../api/generated/schema'

export const importStateLabels: Record<components['schemas']['ImportState'], string> = {
  uploading: '等待上传',
  uploaded: '等待校验',
  validating: '校验中',
  ready: '可发布',
  published: '已发布',
  failed: '校验失败',
  expired: '已过期',
  discarded: '已丢弃',
}

export const auditActionLabels: Record<
  components['schemas']['MaintenanceAuditEvent']['action'],
  string
> = {
  upload_created: '创建导入',
  upload_received: '接收题包',
  import_ready: '校验通过',
  import_failed: '校验失败',
  import_discarded: '丢弃候选',
  import_expired: '导入过期',
  published: '发布版本',
  disabled: '下架题目',
  restored: '恢复题目',
  authority_bootstrap: '初始化维护权限',
  authority_grant: '授予维护权限',
  authority_revoke: '撤销维护权限',
  authority_enable: '启用账号',
  authority_disable: '停用账号',
}
