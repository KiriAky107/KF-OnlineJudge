import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileTemplate } from 'vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { importStateLabels, auditActionLabels } from '../src/admin/presentation.ts'
import { displayTime, displayRuntimeVersion } from '../src/lib/presentation.ts'

// Ordinary rendering tests: no API requests, authentication, or execution reports.
async function renderTemplate(relative: string, state: Record<string, unknown>) {
  const file = new URL(relative, import.meta.url)
  const { descriptor } = parse(await readFile(file, 'utf8'))
  assert.ok(descriptor.template)
  const compiled = compileTemplate({
    source: descriptor.template.content,
    filename: file.pathname,
    id: 'presentation-privacy',
    ssr: true,
    ssrCssVars: descriptor.cssVars,
    // Review parent copy and native attributes without mounting child services.
    compilerOptions: { isCustomElement: (tag) => ['el-button', 'JudgementStatus'].includes(tag) },
  })
  assert.deepEqual(compiled.errors, [])
  const code = compiled.code.replace(
    /from "(vue(?:\/server-renderer)?)"/g,
    (_, name: string) => `from ${JSON.stringify(import.meta.resolve(name))}`,
  )
  const { ssrRender } = await import(
    'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
  )
  const app = createSSRApp({ ssrRender, setup: () => state })
  return renderToString(app)
}

const operation = '11111111-1111-4111-8111-111111111111'
const version = '22222222-2222-4222-8222-222222222222'
const actor = '33333333-3333-4333-8333-333333333333'
const created = '2026-11-11T15:41:51Z'
const visibleText = (html: string) => html.replace(/<[^>]*>/g, '')

test('maintenance views keep readable records without rendering raw internal identifiers', async () => {
  const problem = {
    id: version,
    active_version_id: version,
    slug: 'sum',
    title: '求和',
    revision: 2,
    enabled: true,
  }
  const task = {
    operation_id: operation,
    state: 'published',
    created_at: created,
    expires_at: created,
    published_version_id: version,
    archive_bytes: 1024,
  }
  const html = await renderTemplate('../src/admin/AdminProblems.vue', {
    displayTime,
    displayRuntimeVersion,
    importStateLabels,
    auditActionLabels,
    loading: false,
    busy: false,
    error: '',
    info: '',
    problems: [problem],
    enabledFilter: 'all',
    problemAfter: null,
    selected: problem,
    versionBefore: null,
    versions: [
      {
        version_id: version,
        revision: 2,
        title: '求和',
        languages: ['c17'],
        case_count: 1,
        published_at: created,
      },
    ],
    imports: [task],
    importAfter: null,
    task,
    file: null,
    fileName: '',
    uploadAvailable: true,
    candidate: {
      title: '求和',
      slug: 'sum',
      statement: '计算两个数的和。',
      languages: ['c17'],
      case_count: 1,
      samples: [],
      language_configurations: [
        {
          language: 'c17',
          runtime_version: '16.2.1+r23+gd564253eb6c8',
          run_limits: { cpu_time_us: 500000, memory_bytes: 134217728 },
        },
      ],
    },
    publicationTarget: problem,
    audits: [
      {
        id: operation,
        operation_id: operation,
        actor_id: actor,
        actor_kind: 'account',
        operator_name: null,
        created_at: created,
        action: 'published',
        result: 'committed',
      },
    ],
    auditBefore: null,
  })
  const text = visibleText(html)
  for (const identifier of [operation, version, actor, 'd564253eb6c8'])
    assert.ok(!text.includes(identifier))
  for (const label of ['求和', '版本 2', '已发布', '发布版本', '16.2.1+r23'])
    assert.ok(text.includes(label))
})

test('personal submission rows hide the UUID while retaining the exact selection target', async () => {
  const html = await renderTemplate('../src/components/ProblemSubmissionList.vue', {
    displayTime,
    language: '',
    busy: false,
    error: '',
    cursors: [undefined],
    load: () => {},
    previous: () => {},
    next: () => {},
    page: {
      items: [
        {
          submission_id: operation,
          language: 'c17',
          created_at: created,
          state: 'finished',
          verdict: 'AC',
        },
      ],
      next_after: null,
    },
  })
  assert.ok(!visibleText(html).includes(operation))
  assert.ok(visibleText(html).includes('查看详情'))
  assert.ok(html.includes(`data-submission-id="${operation}"`))
})
