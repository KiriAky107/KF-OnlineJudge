import { createEditorHighlighter } from './editorHighlighting'

const aliases: Record<string, string> = {
  'c++': 'cpp',
  c: 'cpp',
  c17: 'cpp',
  cpp20: 'cpp',
  py: 'python',
  python3: 'python',
  python313: 'python',
  java21: 'java',
  rs: 'rust',
  js: 'javascript',
  ts: 'typescript',
  sh: 'bash',
  shell: 'bash',
}

async function createMarkdownHighlighter() {
  const highlighter = await createEditorHighlighter()
  await highlighter.loadLanguage(
    import('shiki/langs/javascript.mjs'),
    import('shiki/langs/typescript.mjs'),
    import('shiki/langs/bash.mjs'),
    import('shiki/langs/json.mjs'),
    import('shiki/langs/sql.mjs'),
  )
  const loaded = new Set(highlighter.getLoadedLanguages())
  return (source: string, language: string) => {
    const name = language.toLowerCase()
    const lang = Object.hasOwn(aliases, name) ? aliases[name]! : name
    if (!loaded.has(lang)) return ''
    return highlighter.codeToHtml(source, {
      lang,
      themes: { light: 'github-light', dark: 'github-dark' },
      defaultColor: 'light',
    })
  }
}

let shared: ReturnType<typeof createMarkdownHighlighter> | undefined
export function markdownHighlighter() {
  return (shared ??= createMarkdownHighlighter().catch((error: unknown) => {
    shared = undefined
    throw error
  }))
}
