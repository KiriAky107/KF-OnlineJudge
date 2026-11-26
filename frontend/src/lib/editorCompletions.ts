import * as monaco from 'monaco-editor/editor/editor.api'
import { api } from '../api/client'
import { csrfHeaders } from '../api/transport'

const commonC = `auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while`
const keywords = new Map<string, string[]>(
  Object.entries({
    c: `${commonC} restrict _Alignas _Alignof _Atomic _Bool _Complex _Generic _Imaginary _Noreturn _Static_assert _Thread_local`,
    cpp: `${commonC} alignas alignof and and_eq asm bitand bitor bool catch char8_t char16_t char32_t class compl concept const_cast consteval constexpr constinit co_await co_return co_yield decltype delete dynamic_cast explicit export false friend mutable namespace new noexcept not not_eq nullptr operator or or_eq private protected public reinterpret_cast requires static_assert static_cast template this thread_local throw true try typeid typename using virtual wchar_t xor xor_eq`,
    python: `False None True and as assert async await break case class continue def del elif else except finally for from global if import in is lambda match nonlocal not or pass raise return try type while with yield`,
    java: `abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new null package permits private protected public record return sealed short static strictfp super switch synchronized this throw throws transient true false try var void volatile while yield`,
    rust: `as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while`,
  }).map(([language, words]) => [language, words.split(' ')]),
)

type ModelCompletionContext = {
  language: () => string
  semanticVersion: () => string | undefined
  beforeCompletion: () => Promise<void> | undefined
}

const models = new WeakMap<monaco.editor.ITextModel, ModelCompletionContext>()
const identifier = /^[A-Za-z_][A-Za-z_0-9]*$/
const maxDocumentLength = 128_000
let registered = false

export function enableEditorCompletions(
  model: monaco.editor.ITextModel,
  language: () => string,
  semanticVersion: () => string | undefined,
) {
  const warmup = trackEditorWarmup(model, language, semanticVersion)
  models.set(model, { language, semanticVersion, beforeCompletion: warmup.beforeCompletion })
  if (!registered) registerCompletionProvider()
  return warmup.schedule
}

function registerCompletionProvider() {
  registered = true

  monaco.languages.registerCompletionItemProvider(['cpp', 'python', 'java', 'rust'], {
    triggerCharacters: ['.', '>', ':'],
    provideCompletionItems(model, position, _context, token) {
      const owner = models.get(model)
      const language = owner?.language()
      const words = language ? keywords.get(language) : undefined
      const prefix = model.getWordUntilPosition(position)
      const beforeWord = model.getLineContent(position.lineNumber).slice(0, prefix.startColumn - 1)
      const range = new monaco.Range(
        position.lineNumber,
        prefix.startColumn,
        position.lineNumber,
        model.getWordAtPosition(position)?.endColumn ?? position.column,
      )
      if (/(?:\.|->|::)\s*$/.test(beforeWord)) {
        const id = owner?.semanticVersion()
        if (id && (language === 'c' || language === 'cpp')) {
          return semanticCandidates(
            model,
            position,
            range,
            language,
            id,
            owner?.beforeCompletion(),
            token,
          )
        }
        return { suggestions: [] }
      }
      if (!words || !identifier.test(prefix.word)) return { suggestions: [] }
      const candidates = new Map<string, monaco.languages.CompletionItemKind>()
      for (const word of words) {
        if (word.startsWith(prefix.word))
          candidates.set(word, monaco.languages.CompletionItemKind.Keyword)
      }
      if (model.getValueLength() <= maxDocumentLength) {
        // The prefix is an ASCII identifier, so it contains no regular expression syntax.
        const results = model.findMatches(
          `\\b${prefix.word}[A-Za-z_0-9]*\\b`,
          false,
          true,
          true,
          null,
          true,
          512,
        )
        for (const { matches } of results) {
          const word = matches?.[0]
          if (word && !candidates.has(word))
            candidates.set(word, monaco.languages.CompletionItemKind.Text)
        }
      }
      return {
        suggestions: Array.from(candidates, ([label, kind]) => ({
          label,
          kind,
          insertText: label,
          range,
        })).filter(({ label }) => label !== prefix.word),
      }
    },
  })
}

const maxPreambleLength = 16 * 1024

function editorPreamble(model: monaco.editor.ITextModel) {
  let source = ''
  let hasInclude = false
  for (
    let number = 1;
    number <= model.getLineCount() && source.length < maxPreambleLength;
    number++
  ) {
    const line = model.getLineContent(number)
    const whitespace = line.match(/^\s*/)?.[0] ?? ''
    if (!/^\s*(?:#.*|\/\/.*|\/\*.*\*\/\s*)?$/.test(line)) {
      // The first non-directive token fixes the same preamble boundary as the full document.
      source += whitespace + ';' + model.getEOL()
      return hasInclude && new TextEncoder().encode(source).byteLength <= maxPreambleLength
        ? source
        : ''
    }
    if (/\\\s*$/.test(line)) return ''
    hasInclude ||= /^\s*#\s*include\b/.test(line)
    source += line + model.getEOL()
  }
  return ''
}

function trackEditorWarmup(
  model: monaco.editor.ITextModel,
  getLanguage: () => string,
  getVersion: () => string | undefined,
) {
  let timer: ReturnType<typeof setTimeout> | undefined
  let pending: Promise<void> | undefined
  let lastAttempt = 0
  let draft = ''

  function cancelTimer() {
    clearTimeout(timer)
    timer = undefined
  }

  function schedule() {
    const id = getVersion()
    const language = getLanguage()
    const source = id && (language === 'c' || language === 'cpp') ? editorPreamble(model) : ''
    const key = source ? JSON.stringify([id, language, source]) : ''
    if (key === draft && (timer || pending || performance.now() - lastAttempt < 30_000)) return
    cancelTimer()
    draft = key
    if (!id || !source) return
    timer = setTimeout(() => {
      timer = undefined
      if (pending) return
      lastAttempt = performance.now()
      pending = api
        .POST('/api/v1/problems/versions/{version_id}/editor-completion', {
          params: { path: { version_id: id } },
          headers: csrfHeaders(),
          signal: AbortSignal.timeout(8_000),
          body: {
            language: language === 'c' ? 'c17' : 'cpp20',
            source,
            prepare: true,
            line: 0,
            character: 0,
          },
        })
        .then(
          () => undefined,
          () => undefined,
        )
        .finally(() => {
          pending = undefined
        })
    }, 350)
  }

  model.onDidChangeContent(schedule)
  model.onWillDispose(cancelTimer)
  schedule()
  return {
    schedule,
    beforeCompletion: () => {
      cancelTimer()
      lastAttempt = performance.now()
      return pending
    },
  }
}

const itemKinds = new Map([
  ['method', monaco.languages.CompletionItemKind.Method],
  ['function', monaco.languages.CompletionItemKind.Function],
  ['field', monaco.languages.CompletionItemKind.Field],
  ['variable', monaco.languages.CompletionItemKind.Variable],
  ['type', monaco.languages.CompletionItemKind.Class],
  ['enum', monaco.languages.CompletionItemKind.Enum],
])

function onlyTypedMemberPrefix(source: string, cursorOffset: number, current: string) {
  const afterCursor = source.slice(cursorOffset)
  return (
    current.length > source.length &&
    current.length <= maxDocumentLength &&
    current.startsWith(source.slice(0, cursorOffset)) &&
    current.endsWith(afterCursor) &&
    /^[A-Za-z_0-9]+$/.test(current.slice(cursorOffset, current.length - afterCursor.length))
  )
}

async function semanticCandidates(
  model: monaco.editor.ITextModel,
  position: monaco.Position,
  range: monaco.Range,
  language: 'c' | 'cpp',
  id: string,
  warmup: Promise<void> | undefined,
  token: monaco.CancellationToken,
): Promise<monaco.languages.CompletionList> {
  if (token.isCancellationRequested || model.getValueLength() > maxDocumentLength) {
    return { suggestions: [] }
  }
  const source = model.getValue()
  if (new TextEncoder().encode(source).byteLength > maxDocumentLength) return { suggestions: [] }
  const version = model.getVersionId()
  const cursorOffset = model.getOffsetAt(position)
  const controller = new AbortController()
  const subscription = token.onCancellationRequested(() => controller.abort())
  try {
    if (warmup) await warmup
    if (controller.signal.aborted) return { suggestions: [] }
    const { data } = await api.POST('/api/v1/problems/versions/{version_id}/editor-completion', {
      params: { path: { version_id: id } },
      headers: csrfHeaders(),
      signal: controller.signal,
      body: {
        language: language === 'c' ? 'c17' : 'cpp20',
        source,
        prepare: false,
        line: position.lineNumber - 1,
        character: position.column - 1,
      },
    })
    const owner = models.get(model)
    if (
      !data ||
      controller.signal.aborted ||
      model.isDisposed() ||
      // Monaco can filter suggestions and extend their insertion range as the member is typed.
      (model.getVersionId() !== version &&
        !onlyTypedMemberPrefix(source, cursorOffset, model.getValue())) ||
      owner?.semanticVersion() !== id ||
      owner?.language() !== language
    )
      return { suggestions: [] }
    return {
      incomplete: true,
      suggestions: data.map(({ name, kind }) => ({
        label: name,
        kind: itemKinds.get(kind) ?? monaco.languages.CompletionItemKind.Text,
        insertText: name,
        range,
      })),
    }
  } catch {
    return { suggestions: [] }
  } finally {
    subscription.dispose()
  }
}
