import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { effectScope, nextTick } from 'vue'
import { codeTemplates, useCodeDraft, type Language } from '../src/composables/useCodeDraft.ts'

type SavedDraft = { version: number; language: Language; drafts: Record<Language, string> }

function browser(t: TestContext) {
  const page = new EventTarget()
  const document = Object.assign(new EventTarget(), {
    visibilityState: 'visible' as DocumentVisibilityState,
  })
  const storage = {
    items: new Map<string, string>(),
    writes: 0,
    failWrites: false,
    getItem(key: string) {
      return this.items.get(key) ?? null
    },
    setItem(key: string, value: string) {
      this.writes++
      if (this.failWrites) throw new Error('Storage unavailable')
      this.items.set(key, value)
    },
  }
  const globals = { window: page, document, localStorage: storage }
  const descriptors = Object.keys(globals).map(
    (name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const,
  )
  for (const [name, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
  }
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const scopes: ReturnType<typeof effectScope>[] = []
  t.after(() => {
    for (const scope of scopes) scope.stop()
    for (const [name, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else Reflect.deleteProperty(globalThis, name)
    }
  })
  return {
    page,
    document,
    storage,
    open(problemId = '1001') {
      const scope = effectScope()
      scopes.push(scope)
      return { scope, ...scope.run(() => useCodeDraft(problemId))! }
    },
    read(problemId = '1001'): SavedDraft | null {
      return JSON.parse(storage.getItem('kf-oj:code-draft:' + problemId) ?? 'null')
    },
  }
}

test('pagehide saves the latest edit before the debounce and restores it after reload', (t) => {
  const env = browser(t)
  const draft = env.open()
  draft.code.value = '// latest edit before refresh'
  env.page.dispatchEvent(new Event('pagehide'))
  assert.equal(env.read()?.drafts.cpp, draft.code.value)
  assert.equal(draft.state.value, 'saved')
  t.mock.timers.tick(400)
  assert.equal(env.storage.writes, 1)
  draft.scope.stop()
  assert.equal(env.open().code.value, '// latest edit before refresh')
})

test('hiding the page flushes pending edits; remaining visible does not', async (t) => {
  const env = browser(t)
  const draft = env.open()
  draft.code.value = '// edited while visible'
  await nextTick()
  env.document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(env.storage.writes, 0)
  env.document.visibilityState = 'hidden'
  env.document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(env.read()?.drafts.cpp, draft.code.value)
  t.mock.timers.tick(400)
  assert.equal(env.storage.writes, 1)
})

test('normal typing still debounces writes and saves the most recent edit', async (t) => {
  const env = browser(t)
  const draft = env.open()
  draft.code.value = '// first edit'
  await nextTick()
  t.mock.timers.tick(399)
  assert.equal(env.storage.writes, 0)
  draft.code.value = '// second edit'
  await nextTick()
  t.mock.timers.tick(399)
  assert.equal(env.storage.writes, 0)
  t.mock.timers.tick(1)
  assert.equal(env.read()?.drafts.cpp, '// second edit')
  assert.equal(env.storage.writes, 1)
})

test('leaving a Vue page saves even an edit made in the same tick', (t) => {
  const env = browser(t)
  const draft = env.open()
  draft.code.value = '// latest edit before navigation'
  draft.scope.stop()
  assert.equal(env.read()?.drafts.cpp, '// latest edit before navigation')
  t.mock.timers.tick(400)
  assert.equal(env.storage.writes, 1)
})

test('disposing the scope removes page lifecycle listeners', (t) => {
  const env = browser(t)
  const removePage = t.mock.method(env.page, 'removeEventListener')
  const removeDocument = t.mock.method(env.document, 'removeEventListener')
  env.open().scope.stop()
  assert.deepEqual(
    removePage.mock.calls.map((call) => call.arguments[0]),
    ['pagehide'],
  )
  assert.deepEqual(
    removeDocument.mock.calls.map((call) => call.arguments[0]),
    ['visibilitychange'],
  )
  env.page.dispatchEvent(new Event('pagehide'))
  env.document.visibilityState = 'hidden'
  env.document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(env.storage.writes, 0)
})

test('pagehide preserves the selected language and keeps problem drafts separate', (t) => {
  const env = browser(t)
  const first = env.open('1001')
  first.code.value = '// C++ draft'
  first.language.value = 'python'
  first.code.value = '# Python draft'
  const second = env.open('1002')
  second.code.value = '// another problem'
  env.page.dispatchEvent(new Event('pagehide'))
  assert.deepEqual(env.read('1001'), {
    version: 1,
    language: 'python',
    drafts: {
      c: codeTemplates.c,
      cpp: '// C++ draft',
      python: '# Python draft',
      java: codeTemplates.java,
      rust: codeTemplates.rust,
    },
  })
  assert.equal(env.read('1002')?.drafts.cpp, '// another problem')
  first.scope.stop()
  const restored = env.open('1001')
  assert.equal(restored.language.value, 'python')
  assert.equal(restored.code.value, '# Python draft')
})

test('a failed lifecycle save reports unavailable storage without throwing', (t) => {
  const env = browser(t)
  const draft = env.open()
  env.storage.failWrites = true
  draft.code.value = '// cannot persist yet'
  assert.doesNotThrow(() => env.page.dispatchEvent(new Event('pagehide')))
  assert.equal(draft.state.value, 'unavailable')
  t.mock.timers.tick(400)
  assert.equal(env.storage.writes, 1)
  env.storage.failWrites = false
  draft.code.value = '// storage recovered'
  env.page.dispatchEvent(new Event('pagehide'))
  assert.equal(draft.state.value, 'saved')
  assert.equal(env.read()?.drafts.cpp, '// storage recovered')
})

test('old Python/Rust drafts survive adding C17; C and C++ retain separate contents', (t) => {
  const env = browser(t)
  env.storage.items.set(
    'kf-oj:code-draft:1001',
    JSON.stringify({
      version: 1,
      language: 'rust',
      drafts: { cpp: '// original C++', python: '# original Python', rust: '// original Rust' },
    }),
  )
  const draft = env.open()
  assert.equal(draft.language.value, 'rust')
  assert.equal(draft.code.value, '// original Rust')
  draft.language.value = 'c'
  draft.code.value = '// new C17'
  draft.language.value = 'cpp'
  assert.equal(draft.code.value, '// original C++')
  draft.save()
  assert.equal(env.read()?.drafts.c, '// new C17')
  assert.equal(env.read()?.drafts.python, '# original Python')
  assert.equal(env.read()?.drafts.rust, '// original Rust')
})

test('old drafts acquire Java without replacing their contents; four languages survive reload and version changes', (t) => {
  const env = browser(t)
  env.storage.items.set(
    'kf-oj:code-draft:version-a',
    JSON.stringify({
      version: 1,
      language: 'python',
      drafts: { python: '# old Python', cpp: '// old C++', rust: '// old Rust' },
    }),
  )
  const draft = env.open('version-a')
  assert.equal(draft.code.value, '# old Python')
  draft.language.value = 'java'
  assert.equal(draft.code.value, codeTemplates.java)
  draft.code.value = '// Java draft'
  draft.language.value = 'c'
  draft.code.value = '// C draft'
  draft.language.value = 'java'
  env.page.dispatchEvent(new Event('pagehide'))
  draft.scope.stop()
  const restored = env.open('version-a')
  assert.equal(restored.language.value, 'java')
  assert.equal(restored.code.value, '// Java draft')
  for (const [language, contents] of [
    ['c', '// C draft'],
    ['cpp', '// old C++'],
    ['python', '# old Python'],
    ['rust', '// old Rust'],
  ] as const) {
    restored.language.value = language
    assert.equal(restored.code.value, contents)
  }
  const other = env.open('version-b')
  other.language.value = 'java'
  assert.equal(other.code.value, codeTemplates.java)
  other.code.value = '// other version Java'
  other.save()
  assert.equal(env.read('version-a')?.drafts.java, '// Java draft')
  assert.equal(env.read('version-b')?.drafts.java, '// other version Java')
})
